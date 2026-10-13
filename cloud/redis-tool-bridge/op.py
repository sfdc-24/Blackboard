"""POST /op: one governed Redis operation for a VERIFIED caller. docs/REDIS-PRIMARY.md, decision D1.

WHY THIS IS A SEPARATE FILE
    main.py says of itself that the only endpoint touching Redis is a synthetic probe, and that
    get/set/xadd are "not refused - ABSENT". That was the locked decision of 2026-10-06 and it is
    still true of main.py: it makes the same four Redis calls it always did. Everything a caller
    can choose lives HERE, and this file makes no Redis call of its own at all. It hands the
    request to scripts/redis_gov.execute - the same function the board route calls - so the access
    list, the protected namespaces, the server-set TTL, the value cap and the audit are the tested
    code of tests/test_redis_gov.py, not a second implementation of them.

    EXTENDING THE BRIDGE IS THE OWNER'S DECISION D1 AND IT HAS NOT BEEN GIVEN. This file is source
    for review. Nothing deploys it; deploy.sh still says DO NOT RUN THIS YET.

WHAT THE BRIDGE ADDS THAT THE BOARD CANNOT
    On the board the actor is a row's Source_Tag: a claim. Here the actor is the email of an OIDC
    token whose audience and signature main.verify_caller has checked, mapped to a principal by a
    file in git (callers.json). The audit entry's `via` is "bridge:<that email>", so gov:audit
    names an authenticated caller. And a message costs no board row.

THE TWO LAYERS THAT CAN FAIL ON THEIR OWN, AND THEIR STATUSES
    main.py answers before this file is reached:
        401 unauthenticated      no token, a bad signature, the wrong audience, an unverified email
        403 not authorised       a verified identity that is not on BRIDGE_CALLERS
        503 service unavailable  the bridge itself is misconfigured (said in OUR log, not to them)
    This file answers:
        400 bad request          the body is not the small JSON object described below
        403 principal mismatch   the caller is bound to a different principal than the one the
                                 client said it expects to be; nothing was executed
        403 refused              redis_gov refused it; `reason` is redis_gov's own, and `audit` says
                                 whether the refusal itself was recorded
        503 redis unreachable    NO CONNECTION WAS MADE. Always with "executed": false
        502 failed               a READ raised inside Redis; the error TYPE only; nothing changed
        502 outcome unknown      a WRITE raised after the connection was made. It may have
                                 committed. The error TYPE only
        200                      redis_gov's result, as it returned it

    "EXECUTED": FALSE IS A PROMISE, AND ITS ABSENCE IS NOT ONE (Aya on 919fd52)
    A client that gets a failure has one question: may I send this again by another route? For a
    write the answer is only yes when the first attempt provably did nothing. So every body this
    file writes for a request it did NOT hand to redis_gov carries "executed": false - a bad body, a
    principal mismatch, an unmapped caller, no connection. A write that reached redis_gov and then
    raised is "outcome unknown": the transaction may have committed before the connection dropped,
    and this file does not guess. The first version called that 503 "redis unreachable", the same
    words as a connection that was never made, and a client that fell back on it would have written
    the message twice.

    The plan's draft said 403 "not-a-caller" for a token off the allowlist. That refusal already
    exists in main.py as "not authorised", deliberately coarse and already tested; one refusal with
    two names would be an oracle. A caller that IS on BRIDGE_CALLERS but has no principal in
    callers.json is not the caller's failure, it is ours, so it is a 503 and a loud log line - and
    tests/test_redis_tool_bridge.py fails the build before that deploy can happen.

THE BODY
    {"op": "xadd", "key": "fleet:inbox:grok", "val": "<base64>", "enc": "b64",
     "field": "", "count": 10, "after": ""}
    Every member is optional except op and key. Every member is a string, except count, which is an
    integer. Nothing else is accepted: an unknown member is a 400, so a caller cannot believe it
    passed something this file ignored. No caller-chosen TTL exists; redis_gov sets it from the key.

    "expect" IS A CHECK, NOT A CHOICE (Aya on 919fd52, the principal binding)
    {"expect": "claude-code-cli"} says who the client believes it is acting as. It can never select
    a principal: who the caller acts as is callers.json's word alone. It can only REFUSE: if the
    bound principal is not the expected one, the answer is 403 "principal mismatch" and nothing is
    executed. Without it a client configured with another agent's service account would write under
    that agent's name and learn of it only from the answer, after the fact.

    "author" AND "request" ARE FOR THE CONFERENCE CHAIR AND FOR NOBODY ELSE
    (conference docs/PERSONA-WRITE-CONFIRMATION-A1.md, rows 6 and 7 and section 10.6)
    The chair writes under its OWN verified identity for a persona that holds no identity at all. So it
    must say, every time, which persona ("author") and under which request id ("request"); both go into
    the audit entry as CLAIMS beside the verified actor (redis_gov.said), and both come back in the answer,
    so the chair can tell the acknowledgment of ITS request from any other 200. What a caller is FOR is
    its line in callers.json, never the request:
        a fleet agent     may not state either member: a 400. Its own name is already the verified one.
        the chair         must state both on every op: a 400 without them. Its ops are the three reads
                          and `set`, and a `set` goes only to the key of the persona it names,
                          conf:persona:<yyyymmdd>:<author>: a 400 for any other op or key, before any
                          connection. What it may touch at all is its narrow line in redis_acl.json.
"""
from __future__ import annotations

import json
import os
import re

# A value is at most 8 KB (proj:milestones:) and travels base64, so about 11 KB; 16 KB bounds the
# read without refusing a legitimate write. main.py reads no more than this for /op.
MAX_BODY_BYTES = 16384
CALLERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callers.json")
MEMBERS = ("op", "key", "field", "val", "enc", "count", "after", "expect", "author", "request")
# The ops that change nothing a caller owns. A failure inside one of these changed nothing; a failure
# inside any other op is an unknown outcome. Restated from redis_gov.OPS_READ and held equal by a test.
READS = ("get", "hgetall", "xrange")
# What a caller is FOR: its line in callers.json says, and a line that says anything else maps nobody.
FLEET, CHAIR = "fleet-agent", "chair"
PURPOSES = (FLEET, CHAIR)
CHAIR_OPS = READS + ("set",)
# The one kind of key the chair writes: a persona's own, for one day. The name in it is the author it states.
_PERSONA_KEY = re.compile(r"conf:persona:[0-9]{8}:([a-z][a-z0-9-]{1,31})")


def _lines(path=None) -> dict:
    """{verified email: (principal, what the caller is for)}, lower-cased. A line is
    {"principal": ..., "for": "fleet-agent" | "chair"}; a bare string is a fleet agent, as every line was
    before the chair. A line that is neither, or names no principal, or is for anything else, maps NOBODY."""
    try:
        with open(path or CALLERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        callers = data["callers"]
        if not isinstance(callers, dict):
            return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for email, line in callers.items():
        if isinstance(line, str):
            who, purpose = line, FLEET
        elif isinstance(line, dict) and set(line) == {"principal", "for"}:
            who, purpose = line["principal"], line["for"]
        else:
            continue
        if not isinstance(who, str) or not isinstance(purpose, str) or purpose not in PURPOSES:
            continue
        email, who = str(email).strip().lower(), who.strip().lower()
        if email and who:
            out[email] = (who, purpose)
    return out


def load_callers(path=None) -> dict:
    """{verified email: principal}, lower-cased. A missing or malformed file maps NOBODY: the same
    direction as redis_gov.load_acl, where an absent rule makes access stricter, never looser."""
    return {email: who for email, (who, _) in _lines(path).items()}


def load_purposes(path=None) -> dict:
    """{verified email: what that caller is for}. The same file, the same strictness."""
    return {email: purpose for email, (_, purpose) in _lines(path).items()}


def parse(raw: bytes) -> tuple:
    """(request dict, None) or (None, why). The why is for our log; the caller gets 'bad request'."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "the body is not JSON"
    if not isinstance(body, dict):
        return None, "the body is not an object"
    unknown = sorted(set(body) - set(MEMBERS))
    if unknown:
        return None, "unknown member"
    out = {}
    for name in MEMBERS:
        value = body.get(name, 10 if name == "count" else "")
        if name == "count":
            # bool is an int in Python; True as a count is a caller's mistake, not the number 1.
            if isinstance(value, bool) or not isinstance(value, int):
                return None, "count is not an integer"
        elif not isinstance(value, str):
            return None, "%s is not a string" % name
        out[name] = value
    if not out["op"].strip() or not out["key"].strip():
        return None, "op and key are required"
    return out, None


def handle(caller: str, raw: bytes, connect, log, callers=None, gov=None, acl=None, purposes=None) -> tuple:
    """(status, body) for one /op request from an ALREADY VERIFIED caller.

    caller   the email main.verify_caller returned: verified, and on BRIDGE_CALLERS.
    connect  main.connect; called only after the request has parsed, so a malformed body costs no
             connection.
    log      a callable taking one line. No value, no key content and no body ever goes to it.
    """
    email = str(caller or "").strip().lower()
    who = (load_callers() if callers is None else callers).get(email)
    if not who:
        log("REFUSING /op: %s is on the caller allowlist and has no principal in callers.json"
            % caller)
        return 503, {"error": "service unavailable", "executed": False}
    request, why = parse(raw)
    if why:
        log("op by %s: bad request (%s)" % (caller, why))
        return 400, {"error": "bad request", "executed": False}
    expected = request["expect"].strip().lower()
    if expected and expected != who:
        # BEFORE the connection, so "nothing was executed" is true by construction.
        log("op by %s: principal mismatch (bound to %s; the client expected another)" % (caller, who))
        return 403, {"error": "principal mismatch", "executed": False, "principal": who}
    # WHO THE CALLER SAYS IT ACTS FOR. Decided by what the caller is FOR, which is its line in callers.json
    # and never the request. Every refusal here is before the connection, so "executed": false is true.
    op = request["op"].strip().lower()[:16]
    author, asked = request["author"], request["request"]
    if (load_purposes() if purposes is None else purposes).get(email, FLEET) == CHAIR:
        if not author or not asked:
            log("op by %s: the chair did not say which persona and which request" % caller)
            return 400, {"error": "bad request", "executed": False}
        named = _PERSONA_KEY.fullmatch(request["key"].strip())
        if op not in CHAIR_OPS or (op not in READS and (named is None or named.group(1) != author)):
            log("op by %s: not an op the chair makes, or not the key of the persona it names" % caller)
            return 400, {"error": "bad request", "executed": False}
    elif author or asked:
        log("op by %s: author or request stated by a caller that is not the chair" % caller)
        return 400, {"error": "bad request", "executed": False}
    stated = {name: value for name, value in (("author", author), ("request", asked)) if value}
    if gov is None:
        import redis_gov as gov
    try:
        conn = connect()
    except Exception as error:  # noqa: BLE001 - the TYPE only: a client's message can quote a secret
        log("op by %s: redis unreachable (%s)" % (caller, type(error).__name__))
        return 503, {"error": "redis unreachable", "executed": False}
    result = gov.execute(conn, gov.load_acl() if acl is None else acl, who,
                         ("bridge:%s" % caller)[:120], request["op"], request["key"],
                         field=request["field"], raw_value=request["val"], enc=request["enc"],
                         count=request["count"], after=request["after"],
                         claimed_author=author, request_id=asked)
    if result.get("ok"):
        log("op by %s as %s: %s ok" % (caller, who, op))
        return 200, dict(result, caller=caller, principal=who, **stated)
    if "refused" in result:
        log("op by %s as %s: %s refused" % (caller, who, op))
        # A refusal is redis_gov deciding NOT to act, so nothing ran. `audit` is passed through as
        # it came: "ok", or "FAILED:<type>" when the refusal itself could not be recorded.
        return 403, dict({"error": "refused", "reason": str(result["refused"]), "executed": False,
                          "audit": result.get("audit", ""), "caller": caller, "principal": who}, **stated)
    kind = str(result.get("error") or "unknown")
    if op in READS:
        log("op by %s as %s: %s failed (%s)" % (caller, who, op, kind))
        return 502, {"error": "failed", "type": kind, "caller": caller, "principal": who}
    # A write that reached redis_gov and raised. Whether it committed is NOT known here: a dropped
    # connection can lose the reply to an EXEC that ran. Said as exactly that, never as "unreachable".
    log("op by %s as %s: %s OUTCOME UNKNOWN (%s)" % (caller, who, op, kind))
    return 502, {"error": "outcome unknown", "type": kind, "caller": caller, "principal": who}
