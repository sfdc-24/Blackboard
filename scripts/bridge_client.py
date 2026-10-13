#!/usr/bin/env python3
"""The direct route to Redis for an agent outside the private network. Stdlib only.

WHY THIS EXISTS
    The owner, 2026-10-13: "opertionalizing redis is number 1 priority right now". Measured the same
    night: the governed board route answers in about ten seconds and nobody uses it, because for an
    agent on a laptop every message is two board rows and a wait. cloud/redis-tool-bridge's
    POST /op (Blackboard PR 400) takes one governed operation from a VERIFIED caller, with no row at
    all. This module is the client for it: docs/REDIS-PRIMARY.md, Phase 3b.

IT IS OFF UNLESS THREE THINGS ARE SET, AND NOTHING DEPLOYED ANSWERS IT YET
    REDIS_BRIDGE=on                     the per-agent switch. Anything else is off: the caller uses
                                        the governed board route exactly as before.
    REDIS_BRIDGE_URL=https://...        the bridge's own URL. It is also the token's audience.
    REDIS_BRIDGE_IMPERSONATE=<sa email> the service account this agent acts as.

WHO THIS AGENT IS ON THE BRIDGE (Aya on a125337: the principal binding)
    The bridge decides who a caller acts as from cloud/redis-tool-bridge/callers.json: a verified
    service-account email maps to one principal. A client given another agent's account would
    write under that agent's name. So before any token is minted this module reads the same file
    and refuses a service account that is not bound to THIS agent's own principal; and every
    request carries "expect": <this agent>, which the bridge checks before it executes anything.
    Neither is an impersonation and neither adds a grant: they can only refuse.

THE TOKEN
    Minted by `gcloud auth print-identity-token --impersonate-service-account=<sa>
    --audiences=<url> --include-email`: short-lived, for this audience only, by impersonation.
    NEVER a key file (decision D2: "no key files"). The token is never printed, logged, written to
    disk or put in a URL, and it is sent to the configured host and nowhere else: a redirect is
    refused rather than followed, because following one would carry the token to its target.

FIVE OUTCOMES, AND WHAT EACH ONE PERMITS (Aya's review of a125337, blockers 1 and 2)
    The first version had two: "worked" and "failed, so use the board". That was wrong twice over.
    A write whose answer was lost may have COMMITTED, so sending it again by the board writes it
    twice; and a caller the bridge DENIED was quietly moved to the board route, where identity is
    only a claim - a downgrade around a refusal.

    NotSent       nothing left this machine: no token could be minted.
                  -> the board route may be used. Counts against the bridge.
    NotExecuted   the bridge, or the platform in front of it, proved nothing ran: 400, 404, 413, or
                  a 503 that says "executed": false.
                  -> the board route may be used. Counts against the bridge.
    Denied        401, or any 403 that is not a governed refusal (not authorised, principal
                  mismatch). TERMINAL. Never retried on the board route, read or write.
    Refused       403 "refused": redis_gov's own access rules said no. TERMINAL. The board route
                  runs the same rules and would refuse it again.
    Unknown       anything else once a request was sent: a timeout, a dropped connection, a 5xx
                  without "executed": false, a 200 that is not the answer. For a READ the board
                  route may be used (asking twice changes nothing). For a WRITE it is TERMINAL:
                  the operation is recorded in a local journal as UNKNOWN and is NOT sent again.
                  Whether it committed is for a reconciliation to find out, not for a retry.

THE JOURNAL, AND HOW AN UNKNOWN IS SETTLED (Aya's Stage 1 exit check 5)
    .redis_bridge_unknown.jsonl is append-only. An UNKNOWN line holds what was sent, where and when,
    and two hashes: of the value, and of the value AS A READ WOULD SHOW IT (redis_gov quotes a value
    at 200 characters and replaces a pipe or a newline). It never holds the value. `redis_inbox.py
    reconcile` READS the place the write was aimed at and looks for an entry by this agent whose
    shown text has that second hash; reading changes nothing, so it can be run any number of times.
    What it finds is appended as a second line that names the first. Nothing here ever sends the
    operation again: a write found absent is reported, and sending it again is a person's decision.

WHAT IS SAID, AND TO WHOM (blocker 5)
    Every failure is said out loud, in CLOSED words: a status number and a label from the table
    below, or an exception's class name. Nothing the server sent in an error body is ever repeated
    - not in the line this prints and not in the one row a tripped breaker posts to the public
    board. The single exception is a governed refusal's reason, which is redis_gov's and is the
    whole point of a refusal; it is printed locally only, stripped to plain characters and with
    anything shaped like a credential removed.

THE BREAKER
    After three bridge failures in a row (NotSent, NotExecuted or Unknown) this agent stays on the
    board route for fifteen minutes and says so ONCE, in one row. A success, a refusal or a denial
    closes it: each proves the bridge is answering.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(REPO, ".redis_bridge_breaker.json")
JOURNAL = os.path.join(REPO, ".redis_bridge_unknown.jsonl")
CALLERS_FILE = os.path.join(REPO, "cloud", "redis-tool-bridge", "callers.json")
SWITCH_ENV = "REDIS_BRIDGE"
URL_ENV = "REDIS_BRIDGE_URL"
IMPERSONATE_ENV = "REDIS_BRIDGE_IMPERSONATE"
TRIP_AFTER = 3
OPEN_SECONDS = 900
HTTP_TIMEOUT = 20
MINT_TIMEOUT = 60
# redis_gov.OPS_READ, restated: asking one of these twice changes nothing a caller owns.
READS = ("get", "hgetall", "xrange")
# redis_gov.PREVIEW_CHARS and redis_gov.preview(), restated and held equal by a test: how a read shows
# a stored value. The journal hashes this form so a reconciliation can match what it reads.
PREVIEW_CHARS = 200
# The ONLY words a status is ever described with. Never the body's own.
STATUS_LABELS = {400: "bad request", 401: "unauthenticated", 403: "not authorised",
                 404: "no such endpoint", 413: "body too large", 429: "rate limited",
                 500: "server error", 502: "failed upstream", 503: "unavailable", 504: "timed out"}
# A Cloud Run URL and nothing looser: https, a host, no path, no query, no credentials in it.
_URL = re.compile(r"^https://[a-z0-9][a-z0-9.-]{2,250}$")
_SA = re.compile(r"^[a-z0-9][a-z0-9-]{4,62}@[a-z0-9-]{4,40}\.iam\.gserviceaccount\.com$")
_JWT = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
# Anything shaped like a credential: three dotted base64url runs (a JWT), or one long unbroken run.
_SECRETISH = re.compile(r"[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|[A-Za-z0-9_+/=-]{24,}")
_PLAIN = re.compile(r"[^A-Za-z0-9 .,:;=()'_/<>-]")


class BridgeProblem(Exception):
    """The base of the four failures. str() is always closed words: safe to print and to post."""


class NotSent(BridgeProblem):
    """Nothing left this machine."""


class NotExecuted(BridgeProblem):
    """The bridge or the platform proved the operation did not run."""


class Denied(BridgeProblem):
    """The bridge refused the caller. Terminal: never downgraded to the board route."""


class Unknown(BridgeProblem):
    """A request was sent and what happened to it is not known."""


class Refused(Exception):
    """redis_gov refused the operation. .reason is redis_gov's, cleaned for a local print only;
    .audit is the bridge's word on whether the refusal itself was recorded."""

    def __init__(self, reason: str, audit: str = ""):
        super().__init__("refused by the access rules")
        self.reason, self.audit = reason, audit


def status_label(status) -> str:
    return "HTTP %d %s" % (status, STATUS_LABELS.get(status, "unexpected status"))


def clean_reason(text) -> str:
    """A refusal's reason, fit for a LOCAL print: plain characters, bounded, and nothing shaped like
    a credential. It is never put in a row."""
    text = _SECRETISH.sub("[removed]", " ".join(str(text or "").split()))
    return _PLAIN.sub("?", text)[:200] or "no reason given"


def config(environ=None):
    """(url, service account) when this agent's switch is on and both are well formed, else None.
    Off is silent: it is the default. On-but-malformed raises NotSent, which attempt() says."""
    env = os.environ if environ is None else environ
    if str(env.get(SWITCH_ENV) or "").strip().lower() != "on":
        return None
    url = str(env.get(URL_ENV) or "").strip().rstrip("/").lower()
    account = str(env.get(IMPERSONATE_ENV) or "").strip().lower()
    if not _URL.match(url) or not _SA.match(account):
        raise NotSent("%s is on but %s or %s is missing or malformed"
                      % (SWITCH_ENV, URL_ENV, IMPERSONATE_ENV))
    return url, account


def load_callers(path=None) -> dict:
    """{service-account email: principal} from the bridge's own callers.json. A missing or malformed
    file maps NOBODY, so the bridge is not used: stricter, never looser."""
    try:
        with open(path or CALLERS_FILE, encoding="utf-8") as fh:
            callers = json.load(fh)["callers"]
        if not isinstance(callers, dict):
            return {}
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in callers.items()
                if str(k).strip() and str(v).strip()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def mint(url: str, account: str, run=subprocess.run, which=shutil.which) -> str:
    """One identity token for `url`, as `account`, by impersonation. Raises NotSent.

    Nothing gcloud printed is ever repeated: its error text can name accounts and its output IS the
    credential. A failure is reported as its exit code and nothing else."""
    tool = which("gcloud")
    if not tool:
        raise NotSent("gcloud is not on PATH, so no token can be minted")
    try:
        done = run([tool, "auth", "print-identity-token",
                    "--impersonate-service-account=" + account,
                    "--audiences=" + url, "--include-email"],
                   capture_output=True, text=True, timeout=MINT_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise NotSent("minting a token timed out after %d s" % MINT_TIMEOUT) from None
    except OSError as error:
        raise NotSent("gcloud could not be run (%s)" % type(error).__name__) from None
    if done.returncode != 0:
        raise NotSent("gcloud could not mint a token (exit %d)" % done.returncode)
    token = (done.stdout or "").strip()
    if not _JWT.match(token):
        raise NotSent("gcloud returned something that is not an identity token")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure. Following one would send the bearer token to the redirect's target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def call(url: str, token: str, request: dict, opener=None) -> dict:
    """POST one operation to <url>/op. The result dict on a 200 that IS the answer; otherwise one of
    Refused, Denied, NotExecuted, Unknown. Which one is decided by the status and by two closed
    members of the body ("error" == "refused", "executed" is False) and by nothing else in it."""
    body = json.dumps(request, sort_keys=True).encode("utf-8")
    req = urllib.request.Request(url + "/op", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as response:
            status, raw = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            answer = json.loads(error.read(65536).decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable error body is still just this status
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        if status == 403 and answer.get("error") == "refused":
            raise Refused(clean_reason(answer.get("reason")),
                          "ok" if answer.get("audit") == "ok" else "not confirmed") from None
        if status == 403 and answer.get("error") == "principal mismatch":
            raise Denied("HTTP 403 principal mismatch") from None
        if status in (401, 403):
            raise Denied(status_label(status)) from None
        if status in (400, 404, 413) or (status == 503 and answer.get("executed") is False):
            raise NotExecuted(status_label(status)) from None
        raise Unknown(status_label(status)) from None
    except Exception as error:  # noqa: BLE001 - the CLASS NAME only: a message can quote the request
        raise Unknown("no answer (%s)" % re.sub(r"[^A-Za-z]", "", type(error).__name__)[:40]) from None
    if status != 200:
        raise Unknown(status_label(status))
    try:
        answer = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise Unknown("HTTP 200 with a body that is not JSON") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise Unknown("HTTP 200 without ok: true")
    return answer


# ------------------------------------------------------------------------------------- the breaker

def load_state(path=None) -> dict:
    try:
        with open(path or STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"failures": int(data.get("failures") or 0),
                "open_until": float(data.get("open_until") or 0.0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"failures": 0, "open_until": 0.0}


def save_state(state: dict, path=None) -> None:
    with open(path or STATE, "w", encoding="utf-8", newline="") as fh:
        json.dump(state, fh, sort_keys=True)


def clock(moment: float) -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime(moment))


def shown(value) -> str:
    """A value as a governed read shows it: redis_gov.preview(), restated."""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "..."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def journal_unknown(op: str, key: str, value, label: str, now: float, path=None) -> dict:
    """One line per write whose outcome is unknown: what, where, when, and hashes of the value - never
    the value. This is what a reconciliation reads; nothing here retries. Returns the record."""
    text = "" if value is None else str(value)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    record = {"id": _sha("%s|%s|%s|%s" % (at, op, key, _sha(text)))[:16], "at": at, "epoch": int(now),
              "op": op, "key": key, "value_sha256": _sha(text), "shown_sha256": _sha(shown(text)),
              "value_chars": len(text), "outcome": "UNKNOWN", "why": label}
    with open(path or JOURNAL, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def journal(path=None) -> list:
    """Every line of the journal that is a JSON object, in order. A line that is not is skipped: one
    damaged line must not hide the rest."""
    out = []
    try:
        with open(path or JOURNAL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        pass
    return out


def pending(path=None) -> list:
    """The UNKNOWN writes that no later line has settled."""
    lines = journal(path)
    settled = {str(r.get("resolves")) for r in lines if r.get("resolves")}
    return [r for r in lines if r.get("outcome") == "UNKNOWN" and r.get("id") and r["id"] not in settled]


def settle(entry: dict, outcome: str, detail: str, now: float, path=None) -> None:
    """Append what a reconciliation found. Append-only: the UNKNOWN line it names is never rewritten."""
    record = {"resolves": entry["id"], "outcome": outcome, "detail": detail,
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    with open(path or JOURNAL, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def attempt(op: str, key: str, *, me: str, value=None, count=None, after=None, environ=None,
            now=None, state_path=None, journal_path=None, callers=None, say=None, announce=None,
            minter=None, caller=None):
    """Try one operation through the bridge, as `me`.

    Returns the bridge's result dict on success.
    Returns None when the caller may use the governed board route: the switch is off (silently),
    this account is not bound to `me`, the breaker is open, nothing was sent, the bridge proved
    nothing ran, or a READ's outcome is unknown. Everything but "off" is said first.
    Raises Refused, Denied or (for a write only) Unknown: all terminal. The caller must not send
    the operation again by any route.

    announce(text) is called at most once per trip of the breaker, at the moment it opens, with
    closed words only.
    """
    now = time.time() if now is None else now
    say = say or (lambda line: print(line))
    me = str(me or "").strip().lower()
    write = op not in READS
    try:
        found = config(environ)
    except NotSent as why:
        say("BRIDGE NOT USED: %s. Using the board route." % why)
        return None
    if found is None:
        return None
    url, account = found
    bound = (load_callers() if callers is None else callers).get(account)
    if not me or bound != me:
        say("BRIDGE NOT USED: %s is not bound to %s in the bridge's callers.json, so this agent "
            "would not be acting as itself. Using the board route." % (account, me or "(nobody)"))
        return None
    state = load_state(state_path)
    if now < state["open_until"]:
        say("bridge breaker is open until %s; using the board route" % clock(state["open_until"]))
        return None
    request = {"op": op, "key": key, "expect": me}
    if value is not None:
        request["val"] = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
        request["enc"] = "b64"
    if count is not None:
        request["count"] = int(count)
    if after is not None:
        request["after"] = str(after)
    try:
        token = (minter or mint)(url, account)
        result = (caller or call)(url, token, request)
        if str(result.get("principal") or "").strip().lower() != me:
            # An endpoint that did not check `expect` and answered as someone else. For a write this
            # may already have happened under that name: unknown, and never sent again.
            raise Unknown("answered as another principal") if write else Denied(
                "answered as another principal")
    except (Refused, Denied):
        _record(state_path, {"failures": 0, "open_until": 0.0}, say, only_if=state)  # it answered
        raise
    except (NotSent, NotExecuted, Unknown) as why:
        _count_failure(state, state_path, now, str(why), say, announce)
        if isinstance(why, Unknown) and write:
            try:
                journal_unknown(op, key, value, str(why), now, journal_path)
            except OSError as error:
                say("the UNKNOWN journal could not be written (%s)" % type(error).__name__)
            raise
        say("Using the board route for this %s." % ("read" if not write else "message"))
        return None
    _record(state_path, {"failures": 0, "open_until": 0.0}, say, only_if=state)
    return result


def _count_failure(state, state_path, now, label, say, announce) -> None:
    failures = state["failures"] + 1
    if failures < TRIP_AFTER:
        _record(state_path, {"failures": failures, "open_until": 0.0}, say)
        say("BRIDGE FAILED (%d of %d): %s." % (failures, TRIP_AFTER, label))
        return
    until = now + OPEN_SECONDS
    _record(state_path, {"failures": 0, "open_until": until}, say)
    line = ("the Redis bridge failed %d times in a row (last: %s); this agent uses the board "
            "route until %s" % (TRIP_AFTER, label, clock(until)))
    say("BRIDGE FAILED (%d of %d): %s. BREAKER OPEN: %s." % (failures, TRIP_AFTER, label, line))
    if announce is not None:
        try:
            announce(line)
        except Exception as error:  # noqa: BLE001 - the announcement must not cost the message
            say("the breaker's one row could not be posted (%s)" % type(error).__name__)


def _record(path, state, say, only_if=None) -> None:
    """Persist the breaker. With only_if, write only when that earlier state was not already clean.
    A state file that cannot be written is said, and is not a reason to lose the message."""
    if only_if is not None and not (only_if["failures"] or only_if["open_until"]):
        return
    try:
        save_state(state, path)
    except OSError as error:
        say("the bridge breaker's state could not be saved (%s)" % type(error).__name__)
