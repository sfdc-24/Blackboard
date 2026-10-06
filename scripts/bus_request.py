"""The request-handling mode: a bounded diagnostic Aya can ask for over the board, answered on the board.

THE CHAIN THIS IMPLEMENTS, END TO END
    Aya posts a request row            (the board; Aya's only tool)
      -> this worker validates it      (allowlisted sender, allowlisted action, age, rate, idempotency)
      -> writes a RECEIPT row          (carrying the request id, so the ask is visibly owned)
      -> TLS Redis synthetic probe     (its own nonce key, short TTL, read back, deleted, verified gone)
      -> writes a RESULT row           (carrying the SAME request id)
      -> Aya reads the result back     (the board again)

WHY THERE IS NO HTTPS BRIDGE IN THAT CHAIN
A bridge was proposed so Aya could call Redis over HTTPS. Aya cannot reach a private Memorystore
address and has no Google identity, and its only tool is the board - so the bridge would add a
public-ingress service, a second identity, an invoker grant and an OIDC path to connect two things
that already share a transport. The worker that reads the board can reach Redis directly. Measured:
the bus-reconciler runtime already holds the board credential, REDIS_AUTH_STRING, REDIS_CA_CERT and
private-ranges-only egress, and it contains no model, so routing costs no paid wake.

THE SECURITY TRUTH ABOUT THE BOARD, STATED RATHER THAN PAPERED OVER
**The board has no authenticated sender.** `source_tag` is a column the caller supplies, so anybody
who can append can claim to be Aya. No allowlist here changes that, and I am not going to describe a
sender allowlist as "verified origin" when it is not.

So the defence is not identity, it is CONSEQUENCE: the only action that can be requested writes a
key the SERVER names, in a namespace the server owns, with a nonce the server generates and a TTL the
server sets, reads it back, deletes it, and returns a receipt with no secret in it. A forged request
buys an attacker one synthetic probe against a key they cannot name or read. Making the action
harmless is what makes the missing authentication survivable - and when a request can ever do
something that matters, it needs a real identity first, not a longer allowlist.

The rest of the bounds exist so a forged flood is not free: a stale request is ignored, a repeat for
a request id already answered does nothing, and the number honoured per run and per hour is capped.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets
import time

# The board's ten cells, as scripts/append.py writes them.
ROW_ID, TS, SOURCE, TARGET, ACTION, PAYLOAD, CATEGORY, PROJECT, GIST, SUBGIST = range(10)

REQUEST_ACTION = "AYA_REQ"
RECEIPT_ACTION = "AYA_RECEIPT"
RESULT_ACTION = "AYA_RESULT"
WORKER_TAG = "bus-reconciler"

# Who MAY ask. Not who DID ask: see the module docstring. This bounds the claim, never the identity.
SENDERS = ("aya", "chatgpt-codex-desktop", "claude-code-cli", "owner", "whatsapp")
# What may be asked. One action, and nothing in the request selects a key, a command or a TTL.
ACTIONS = ("redis-synthetic-probe",)

# A request id is the only thing taken from the row, so its grammar is closed and narrow.
_REQ_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")
_FIELD = re.compile(r"(?:^|\|)\s*%s\s*=\s*([^|]*)")

STALE_MINUTES = 30          # the outbox that delivered 18 stale messages; a late diagnostic is noise
MAX_PER_RUN = 3             # a forged flood is not free, so it is not unbounded either
PROBE_NAMESPACE = "synth:probe:"
PROBE_TTL_SECONDS = 10


def field(payload: str, name: str) -> str:
    """One BCB field, or "". The payload has no escaping, so a value can never contain a pipe."""
    found = re.search(_FIELD.pattern % re.escape(name), payload or "")
    return (found.group(1) or "").strip() if found else ""


def stamp(moment=None) -> str:
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_ts(text):
    """An aware datetime from a board timestamp, or None when the cell is not one."""
    text = str(text or "").strip()
    if len(text) < 19 or text[4:5] != "-":
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def parse_request(row):
    """A request row as a dict, or None when the row is not one this worker may act on."""
    if not isinstance(row, list) or len(row) <= PAYLOAD:
        return None
    if str(row[ACTION] or "").strip().upper() != REQUEST_ACTION:
        return None
    payload = str(row[PAYLOAD] or "")
    req_id = field(payload, "req") or field(payload, "id") or str(row[ROW_ID] or "").strip()
    return {
        "req_id": req_id,
        "claimed_sender": str(row[SOURCE] or "").strip().lower(),
        "action": (field(payload, "do") or "").strip().lower(),
        "row_id": str(row[ROW_ID] or "").strip(),
        "at": read_ts(row[TS]),
    }


def answered(rows) -> set:
    """Request ids that already have a RESULT row. Idempotency, read off the board itself.

    Off the board and not off local state on purpose: the worker is a Cloud Run job with no disk that
    survives it, and a watermark that can be lost is a watermark that re-answers."""
    done = set()
    for row in rows:
        if not isinstance(row, list) or len(row) <= PAYLOAD:
            continue
        if str(row[ACTION] or "").strip().upper() != RESULT_ACTION:
            continue
        got = field(str(row[PAYLOAD] or ""), "answers")
        if got:
            done.add(got)
    return done


def refusals(req, now, already) -> list:
    """Every reason not to act on this request. Empty means act."""
    out = []
    if not req["req_id"] or not _REQ_ID.match(req["req_id"]):
        out.append("the request id is missing or not an id")
    if req["claimed_sender"] not in SENDERS:
        # Named honestly: the sender is a CLAIM. This refuses an unrecognised claim, which is worth
        # doing and is not the same as verifying anybody.
        out.append("the claimed sender %r is not one this worker answers" % req["claimed_sender"])
    if req["action"] not in ACTIONS:
        out.append("the action %r is not allowlisted; the only one is %s"
                   % (req["action"], ", ".join(ACTIONS)))
    if req["at"] is None:
        out.append("the row has no readable timestamp, so staleness cannot be judged")
    elif (now - req["at"]).total_seconds() > STALE_MINUTES * 60:
        out.append("the request is older than %d minutes" % STALE_MINUTES)
    elif req["at"] > now + datetime.timedelta(minutes=5):
        out.append("the request is stamped in the future")
    if req["req_id"] in already:
        out.append("already answered: a RESULT row for this request id is on the board")
    return out


def synthetic_probe(conn, req_id) -> dict:
    """SET a server-named nonce key, read it back, delete it, verify it is gone. Nothing else.

    Every value here is the SERVER's: the key name, the namespace, the nonce and the TTL. The request
    selects none of them, which is what makes an unauthenticated ask survivable."""
    key = PROBE_NAMESPACE + hashlib.sha256(req_id.encode("utf-8")).hexdigest()[:16]
    nonce = secrets.token_hex(16)
    started = time.time()
    steps = {}
    try:
        conn.setex(key, PROBE_TTL_SECONDS, nonce)
        steps["write"] = "ok"
        got = conn.get(key)
        steps["read_back"] = "match" if got == nonce else "MISMATCH"
        ttl = conn.ttl(key)
        steps["ttl"] = "ok" if isinstance(ttl, int) and 0 < ttl <= PROBE_TTL_SECONDS else "MISSING"
        conn.delete(key)
        steps["cleanup"] = "verified" if conn.get(key) is None else "LEFT BEHIND"
    except Exception as error:
        # The TYPE, never the message: a client's error text can quote what it was sent.
        return {"status": "ERROR", "failed_with": type(error).__name__, "steps": steps,
                "latency_ms": int((time.time() - started) * 1000)}
    ok = (steps.get("read_back") == "match" and steps.get("ttl") == "ok"
          and steps.get("cleanup") == "verified")
    return {"status": "OK" if ok else "FAILED", "steps": steps,
            "latency_ms": int((time.time() - started) * 1000)}


def row_for(kind, req, text, gist) -> dict:
    """A board row spec for scripts/append.py. No value here may contain a pipe: the payload has no
    escaping, and a literal pipe would split the row."""
    assert "|" not in text and "|" not in gist, "a BCB value cannot contain a pipe"
    row_id = "%s-%s-%s" % (WORKER_TAG.upper(), kind.split("_")[-1], req["req_id"])
    return {
        "row_id": row_id[:120],
        "source_tag": WORKER_TAG,
        "target_surface": req["claimed_sender"] or "ALL",
        "action_type": kind,
        "category": "OPEN",
        "project_tag": "Blackboard",
        "gist": gist,
        "payload": "BCB*v=1*id=%s*phase=%s*from=%s*to=%s*answers=%s*evidence=MEASURED*text=%s".replace(
            "*", "|") % (row_id[:120], kind, WORKER_TAG, req["claimed_sender"] or "ALL",
                         req["req_id"], text),
    }


def handle(rows, conn, now=None, append=None, max_per_run=MAX_PER_RUN) -> dict:
    """Answer the request rows in `rows`. Returns what was done, in counts and ids.

    `append` takes a row spec dict and is expected to read its own write back - that is append.py's
    contract and the only thing that closes an ambiguous append on this gateway."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    already = answered(rows)
    requests = [r for r in (parse_request(row) for row in rows) if r]
    # Oldest first, so a flood cannot starve the request that has waited longest.
    requests.sort(key=lambda r: r["at"] or now)

    out = {"seen": len(requests), "answered": [], "refused": [], "capped": 0, "errors": []}
    for req in requests:
        why = refusals(req, now, already)
        if why:
            # A refusal is only WRITTEN BACK when the id is usable and it is not a duplicate -
            # otherwise a malformed flood becomes a flood of refusal rows, which is the same problem
            # with our name on it.
            out["refused"].append({"req_id": req["req_id"], "why": why})
            if append and _REQ_ID.match(req["req_id"] or "") and req["req_id"] not in already:
                already.add(req["req_id"])
                append(row_for(RESULT_ACTION, req, "REFUSED. " + "; ".join(why),
                               "Refused: " + why[0][:80]))
            continue
        if len(out["answered"]) >= max_per_run:
            out["capped"] += 1
            continue

        if append:
            append(row_for(RECEIPT_ACTION, req,
                           "Received and owned by %s at %s. Running the bounded synthetic probe; the "
                           "result follows under the same request id." % (WORKER_TAG, stamp(now)),
                           "Receipt for " + req["req_id"][:60]))
        receipt = synthetic_probe(conn, req["req_id"])
        text = ("%s. write=%s read_back=%s ttl=%s cleanup=%s latency_ms=%d. The key, its namespace, "
                "its nonce and its TTL were all chosen by the server; the request selected none of "
                "them, and no secret or key name appears here." % (
                    receipt["status"], receipt["steps"].get("write", "-"),
                    receipt["steps"].get("read_back", "-"), receipt["steps"].get("ttl", "-"),
                    receipt["steps"].get("cleanup", "-"), receipt["latency_ms"]))
        if receipt["status"] == "ERROR":
            text += " failed_with=" + receipt["failed_with"]
            out["errors"].append(req["req_id"])
        if append:
            append(row_for(RESULT_ACTION, req, text,
                           "%s for %s" % (receipt["status"], req["req_id"][:50])))
        already.add(req["req_id"])
        out["answered"].append({"req_id": req["req_id"], "status": receipt["status"],
                                "latency_ms": receipt["latency_ms"]})
    return out


def summary(out) -> str:
    return json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in out.items()},
                      sort_keys=True)
