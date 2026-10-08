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

THE OWNER DECIDED, 2026-10-08 ~01:40Z - and this is where his decision lands against the paragraph above.
That paragraph says a request that can do something that matters "needs a real identity first, not a
longer allowlist". On 2026-10-07 that was the advice - mine and, independently, aya's - and he took
it: "don't widen the stopgap". On 2026-10-08 he decided the trade differently, directly to
claude-code-cli: "Everyone gets read write access, you control the Redis access rights and make
actions auditable so there is governance."

So `redis-op` exists, bounded by GOVERNANCE rather than by identity (scripts/redis_gov.py): every fleet
agent may read everything and write the governed namespaces; one controller owns the access list
(scripts/redis_acl.json); every operation, allowed or refused, is audited in gov:audit with the raw
tag, the board row, and the old and new values; and the namespaces other things depend on - the board
mirror the comparison gate checks, the roster, the chair's projection, the audit itself - are refused
for writes in code, whatever the access list says. The identity is still a claim, and the audit says
so by recording the raw tag beside the principal. Per-agent identity is the redis-tool-bridge (OIDC)
for the agents that have a Google identity at all; until then, this is the path every agent can reach.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets
import time

import redis_gov

# The board's ten cells, as scripts/append.py writes them.
ROW_ID, TS, SOURCE, TARGET, ACTION, PAYLOAD, CATEGORY, PROJECT, GIST, SUBGIST = range(10)

REQUEST_ACTION = "AYA_REQ"
RECEIPT_ACTION = "AYA_RECEIPT"
RESULT_ACTION = "AYA_RESULT"
WORKER_TAG = "bus-reconciler"

# Who MAY ask. Not who DID ask: see the module docstring. This bounds the claim, never the identity.
SENDERS = ("aya", "chatgpt-codex-desktop", "claude-code-cli", "owner", "whatsapp")
# What may be asked. The probe selects no key, command or TTL. redis-op selects a key and an op -
# inside scripts/redis_gov.py's grammar, its access list and its protected namespaces, every use
# audited. It is listed TOGETHER with its branch in handle(), never without it: listed alone, a
# redis-op request would fall into the synthetic-probe path and be answered "OK" for nothing done.
ACTIONS = ("redis-synthetic-probe", "redis-op")

# A request id is the only thing taken from the row, so its grammar is closed and narrow.
_REQ_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")
_FIELD = re.compile(r"(?:^|\|)\s*%s\s*=\s*([^|]*)")

STALE_MINUTES = 30          # the outbox that delivered 18 stale messages; a late diagnostic is noise
# Raised from 3 when every agent got redis-op: five-plus agents, a handful of ops each per run. It still
# bounds EVERY board response, refusals included - a forged flood is not free.
MAX_PER_RUN = 25
PROBE_NAMESPACE = "v1:synth:probe:"
PROBE_TTL_SECONDS = 10
# redis-op run markers (handle()). Under gov:, which redis_gov refuses to every agent, so a request
# cannot pre-claim or clear another's. Kept well past the 120-minute scan window and the 30-minute
# staleness bound, so a request is unrunnable by staleness long before its marker can expire.
DONE_PREFIX = "gov:req:"
DONE_TTL_SECONDS = 6 * 3600


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
        # redis-op only. Taken verbatim and judged by scripts/redis_gov.py, never used here.
        "op": field(payload, "op"),
        "key": field(payload, "key"),
        "field": field(payload, "field"),
        "val": field(payload, "val"),
        "enc": field(payload, "enc"),
        "count": field(payload, "count"),
        "repeated": repeated_keys(payload),
    }


def repeated_keys(payload) -> list:
    """Keys written more than once in a payload. A redis-op naming `op` or `key` twice is AMBIGUOUS:
    field() reads the first, another reader might read the last, and the two disagree about what was
    asked (aya on 4d13e97). Such a request is refused, never resolved first-value-wins."""
    seen, out = set(), []
    for part in str(payload or "").split("|"):
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip().lower()
        if name in seen and name not in out:
            out.append(name)
        seen.add(name)
    return out


def recognised(req, acl) -> bool:
    """A sender this worker answers at all. For redis-op that is the ACCESS LIST, which the controller
    owns; for the probe it is SENDERS, unchanged."""
    if req.get("action") == "redis-op":
        return bool(redis_gov.principal(acl, req.get("claimed_sender")))
    return req.get("claimed_sender") in SENDERS


def safe(value) -> str:
    """A string fit to publish in a BCB payload: no pipe, no newline, no control character.

    SANITISE AT THE BOUNDARY RATHER THAN ASSERT. Copilot, PR 323: a request claiming to be from
    'bad|sender' put a pipe into the refusal text, row_for() hit its assert, and the AssertionError
    took the whole worker down before any later request ran - so one malformed row could park every
    well-formed one behind it indefinitely. An assertion is the right tool for an invariant the code
    controls; the sender tag is attacker-supplied data, and data gets cleaned, not asserted."""
    text = "" if value is None else str(value)
    for bad in ("|", "\r", "\n", "\t"):
        text = text.replace(bad, " ")
    return "".join(ch for ch in text if ch >= " ").strip()


def receipts(rows) -> dict:
    """req_id -> the Row_IDs of receipt rows already on the board for it.

    WHY THIS IS READ AT ALL. Copilot, PR 323: if a receipt lands and the execution stops before its
    result does, the request stays pending - correctly, it was never answered - but the next pass
    appends the SAME receipt Row_ID again. append.py sees the duplicate and exits 2, and that
    propagates out of serve_requests before the probe can run. So the request could never be
    finished by a retry: it was wedged by its own half-done attempt."""
    found = {}
    for row in rows:
        if not isinstance(row, list) or len(row) <= PAYLOAD:
            continue
        if str(row[ACTION] or "").strip().upper() != RECEIPT_ACTION:
            continue
        got = field(str(row[PAYLOAD] or ""), "answers")
        if got:
            found.setdefault(got, []).append(str(row[ROW_ID] or "").strip())
    return found


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


def refusals(req, now, already, acl=None) -> list:
    """Every reason not to act on this request. Empty means act."""
    out = []
    acl = acl if acl is not None else redis_gov.load_acl()
    if not req["req_id"] or not _REQ_ID.match(req["req_id"]):
        out.append("the request id is missing or not an id")
    if not recognised(req, acl):
        # Named honestly: the sender is a CLAIM. This refuses an unrecognised claim, which is worth
        # doing and is not the same as verifying anybody.
        #
        # THE TAG ITSELF IS NOT QUOTED HERE. It is attacker-supplied, it reaches a payload with no
        # escaping, and a refusal that echoes it hands the sender a way to shape our rows. The
        # unrecognised value stays in the run's diagnostics, where nothing publishes it.
        out.append("the claimed sender is not one this worker answers")
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
    if req.get("action") == "redis-op" and req.get("repeated"):
        out.append("ambiguous: the field(s) %s appear more than once"
                   % ", ".join(safe(k)[:20] for k in req["repeated"][:5]))
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


def reply_to(req) -> str:
    """Where a reply is addressed. An ALLOWLISTED sender or "ALL" - never the raw claim.

    The claim is data. Putting it in Target_Surface made an unrecognised, pipe-bearing tag the
    address of our own row, which is both a malformed row and a sender choosing where our answer
    goes."""
    claimed = (req.get("claimed_sender") or "").strip().lower()
    if claimed in SENDERS:
        return claimed
    # A redis-op sender the access list knows is answered under its own tag - which the grammar of
    # an ACL key already guarantees is pipe-free and short. Anything else is "ALL", as before.
    if req.get("action") == "redis-op" and redis_gov.principal(redis_gov.load_acl(), claimed):
        return claimed if re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", claimed) else "ALL"
    return "ALL"


def row_for(kind, req, text, gist) -> dict:
    """A board row spec for scripts/append.py.

    Every published value goes through safe(): the payload has no escaping, so a literal pipe would
    split the row, and the values here include an attacker-supplied request id."""
    req_id = safe(req["req_id"])
    row_id = safe("%s-%s-%s" % (WORKER_TAG.upper(), kind.split("_")[-1], req_id))[:120]
    target = reply_to(req)
    return {
        "row_id": row_id,
        "source_tag": WORKER_TAG,
        "target_surface": target,
        "action_type": kind,
        "category": "OPEN",
        "project_tag": "Blackboard",
        "gist": safe(gist),
        "payload": "BCB*v=1*id=%s*phase=%s*from=%s*to=%s*answers=%s*evidence=MEASURED*text=%s".replace(
            "*", "|") % (row_id, kind, WORKER_TAG, target, req_id, safe(text)),
    }


def handle(rows, conn, now=None, append=None, max_per_run=MAX_PER_RUN) -> dict:
    """Answer the request rows in `rows`. Returns what was done, in counts and ids.

    `append` takes a row spec dict and is expected to read its own write back - that is append.py's
    contract and the only thing that closes an ambiguous append on this gateway."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    already = answered(rows)
    seen_receipts = receipts(rows)
    requests = [r for r in (parse_request(row) for row in rows) if r]
    # Oldest first, so a flood cannot starve the request that has waited longest.
    requests.sort(key=lambda r: r["at"] or now)

    # ONE BUDGET FOR EVERY BOARD RESPONSE, refusals included.
    #
    # Copilot, PR 323: refusals were appended before the cap was consulted and never counted toward
    # it, so a flood of DISTINCT valid request ids carrying a rejected action produced one board
    # write and one read-back each - max_per_run=3 and all of them answered. The cap exists because
    # a forged flood costs him money, and a cap that only counts the expensive path is not a cap.
    budget = max(0, int(max_per_run))
    # ONE access list per run, so every decision in a run is made under the same rules - and its
    # digest goes into every audit entry those decisions write.
    acl = redis_gov.load_acl()
    out = {"seen": len(requests), "answered": [], "refused": [], "capped": 0, "errors": [],
           "receipts_reused": [], "acl": acl.get("digest", "")}
    for req in requests:
        why = refusals(req, now, already, acl)
        if why:
            out["refused"].append({"req_id": req["req_id"], "why": why,
                                   # The unrecognised tag is recorded HERE, where nothing publishes it.
                                   "claimed_sender": req["claimed_sender"]})
            # A refusal is only WRITTEN BACK when the id is usable, it is not a duplicate, the
            # claimed sender is one we answer at all, and there is budget left. An unrecognised
            # sender gets diagnostics and no row: answering it would let anyone who can append make
            # us write, which is the flood with our name on it.
            publishable = (append and _REQ_ID.match(req["req_id"] or "")
                           and req["req_id"] not in already
                           and recognised(req, acl))
            if publishable and budget <= 0:
                out["capped"] += 1
                continue
            if publishable:
                budget -= 1
                already.add(req["req_id"])
                append(row_for(RESULT_ACTION, req, "REFUSED. " + "; ".join(why),
                               "Refused: " + why[0][:80]))
            continue
        if budget <= 0:
            out["capped"] += 1
            continue
        budget -= 1

        if req["action"] == "redis-op":
            # ONE ROW, NOT TWO. The probe writes a RECEIPT then a RESULT because it takes a moment;
            # a governed op is one round trip, and every row addressed to an agent can wake it (a row
            # is money he pays). The op is in gov:audit whether or not this row lands.
            try:
                count = int(req.get("count") or 10)
            except ValueError:
                count = 10
            started = time.time()
            # AT MOST ONCE, DECIDED IN REDIS. The board's RESULT rows are not enough: if the op ran
            # and its result row was lost, or a second run read a snapshot taken before the first
            # run's row landed, "no RESULT on the board" re-applied the write (aya on 4d13e97). So
            # the run is CLAIMED first with SET NX on a marker under gov:, which no agent can write.
            # Claimed before the op on purpose: a crash between the claim and the op loses that op
            # and says so - it never applies one twice.
            marker = DONE_PREFIX + hashlib.sha256(req["req_id"].encode("utf-8")).hexdigest()[:32]
            try:
                claimed = conn.set(marker, safe(req["row_id"])[:120] or "-", nx=True, ex=DONE_TTL_SECONDS)
            except Exception as error:
                out["errors"].append(req["req_id"])
                out["refused"].append({"req_id": req["req_id"],
                                       "why": ["could not claim the run marker (%s); not run"
                                               % type(error).__name__]})
                continue
            if not claimed:
                already.add(req["req_id"])
                out["refused"].append({"req_id": req["req_id"],
                                       "why": ["already claimed: Redis holds this request's run marker"]})
                if append:
                    # CLAIMED, NOT "RAN" (aya on ce941b6). The marker is set BEFORE the op, so a worker
                    # that died between the two left a marker and no operation. What is known is the
                    # claim; whether the op happened is only in gov:audit, and this row says so.
                    append(row_for(RESULT_ACTION, req,
                                   "NOT RUN AGAIN. An earlier run CLAIMED this request (Redis holds its "
                                   "run marker). Whether that run completed is UNKNOWN here: a gov:audit "
                                   "entry naming this request id means it ran; none means it did not.",
                                   "Already claimed: " + req["req_id"][:60]))
                continue
            result = redis_gov.execute(conn, acl, req["claimed_sender"],
                                       "%s/%s" % (req["req_id"], req["row_id"]),
                                       req.get("op"), req.get("key"), field=req.get("field") or "",
                                       raw_value=req.get("val") or "", enc=req.get("enc") or "",
                                       count=count, now=now)
            if append:
                append(row_for(RESULT_ACTION, req, redis_gov.summarise(result),
                               ("%s %s %s" % ("OK" if result.get("ok") else "NOT DONE",
                                              req.get("op") or "?", req.get("key") or "?"))[:100]))
            already.add(req["req_id"])
            status = ("OK" if result.get("ok")
                      else "REFUSED" if "refused" in result else "ERROR")
            if status == "ERROR":
                out["errors"].append(req["req_id"])
            # latency_ms is part of the contract, not decoration: cloud/bus-reconciler/serve_requests.py
            # logs item["latency_ms"] for every answer. The first live run, bus-requests-hl95p at
            # 02:25Z, did all four test ops correctly, posted all four results - and then exited 1
            # on a KeyError here, so Cloud Run recorded a FAILED execution over a run that had worked.
            out["answered"].append({"req_id": req["req_id"], "status": status,
                                    "op": req.get("op"), "key": req.get("key"),
                                    "latency_ms": int((time.time() - started) * 1000)})
            continue

        if append:
            # REUSE AN EXISTING RECEIPT RATHER THAN REPLAY IT. A receipt that landed while the
            # result did not leaves the request pending and its receipt Row_ID taken; appending it
            # again makes append.py exit 2 on the duplicate, which killed the run before the probe.
            mine = seen_receipts.get(req["req_id"], [])
            expected = row_for(RECEIPT_ACTION, req, "", "")["row_id"]
            if len(mine) > 1 or (mine and mine[0] != expected):
                # Ambiguous or conflicting: FAIL CLOSED. Two receipts for one request, or one under
                # an id this worker would not have written, is not something to reason past.
                out["refused"].append({"req_id": req["req_id"],
                                       "why": ["%d receipt row(s) already exist for this request "
                                               "and at least one is not the one this worker would "
                                               "write; refusing to guess" % len(mine)]})
                continue
            if mine:
                out["receipts_reused"].append(req["req_id"])
            else:
                append(row_for(RECEIPT_ACTION, req,
                               "Received and owned by %s at %s. Running the bounded synthetic probe; "
                               "the result follows under the same request id." % (WORKER_TAG, stamp(now)),
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
