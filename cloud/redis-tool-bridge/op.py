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
        403 refused              redis_gov refused it; `reason` is redis_gov's own
        502 failed               the operation raised inside Redis; the error TYPE only
        503 redis unreachable    no connection, or the connection failed mid-operation
        200                      redis_gov's result, as it returned it

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
"""
from __future__ import annotations

import json
import os

# A value is at most 8 KB (proj:milestones:) and travels base64, so about 11 KB; 16 KB bounds the
# read without refusing a legitimate write. main.py reads no more than this for /op.
MAX_BODY_BYTES = 16384
CALLERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callers.json")
MEMBERS = ("op", "key", "field", "val", "enc", "count", "after")
# What a failed connection is called, by TYPE NAME, across redis-py and the socket layer. A result
# whose error is one of these is "Redis unreachable" (503); any other error is "failed" (502).
UNREACHABLE = ("ConnectionError", "TimeoutError", "BusyLoadingError", "ConnectionRefusedError",
               "ConnectionResetError", "OSError", "gaierror", "timeout", "SSLError",
               "AuthenticationError")


def load_callers(path=None) -> dict:
    """{verified email: principal}, lower-cased. A missing or malformed file maps NOBODY: the same
    direction as redis_gov.load_acl, where an absent rule makes access stricter, never looser."""
    try:
        with open(path or CALLERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        callers = data["callers"]
        if not isinstance(callers, dict):
            return {}
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in callers.items()
                if str(k).strip() and str(v).strip()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


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


def handle(caller: str, raw: bytes, connect, log, callers=None, gov=None, acl=None) -> tuple:
    """(status, body) for one /op request from an ALREADY VERIFIED caller.

    caller   the email main.verify_caller returned: verified, and on BRIDGE_CALLERS.
    connect  main.connect; called only after the request has parsed, so a malformed body costs no
             connection.
    log      a callable taking one line. No value, no key content and no body ever goes to it.
    """
    who = (load_callers() if callers is None else callers).get(str(caller or "").strip().lower())
    if not who:
        log("REFUSING /op: %s is on the caller allowlist and has no principal in callers.json"
            % caller)
        return 503, {"error": "service unavailable"}
    request, why = parse(raw)
    if why:
        log("op by %s: bad request (%s)" % (caller, why))
        return 400, {"error": "bad request"}
    if gov is None:
        import redis_gov as gov
    try:
        conn = connect()
    except Exception as error:  # noqa: BLE001 - the TYPE only: a client's message can quote a secret
        log("op by %s: redis unreachable (%s)" % (caller, type(error).__name__))
        return 503, {"error": "redis unreachable"}
    result = gov.execute(conn, gov.load_acl() if acl is None else acl, who,
                         ("bridge:%s" % caller)[:120], request["op"], request["key"],
                         field=request["field"], raw_value=request["val"], enc=request["enc"],
                         count=request["count"], after=request["after"])
    op = request["op"].strip().lower()[:16]
    if result.get("ok"):
        log("op by %s as %s: %s ok" % (caller, who, op))
        return 200, dict(result, caller=caller, principal=who)
    if "refused" in result:
        log("op by %s as %s: %s refused" % (caller, who, op))
        return 403, {"error": "refused", "reason": str(result["refused"]),
                     "audit": result.get("audit", ""), "caller": caller, "principal": who}
    kind = str(result.get("error") or "unknown")
    log("op by %s as %s: %s failed (%s)" % (caller, who, op, kind))
    if kind in UNREACHABLE:
        return 503, {"error": "redis unreachable"}
    return 502, {"error": "failed", "type": kind, "caller": caller, "principal": who}
