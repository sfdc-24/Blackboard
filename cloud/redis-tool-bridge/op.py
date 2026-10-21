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
        the chair         must state both on every op: a 400 without them.
        a probe account   is admitted to the service for /probe and is refused here: a 403.

    THE PILOT (conference docs/PERSONA-WRITE-CONFIRMATION-A1.md, section 12; Aya,
    AYA-PILOT-SOURCE-DIRECTION-20261014T0031Z). For the pilot the chair is the ONLY caller of this endpoint,
    and all it may do is `get` and `set` ONE synthetic key a day for ONE listed persona:
        the op            get or set. Nothing else, whatever the access list would allow.
        the key           conf:persona:<yyyymmdd>:<author>, where the day is today or yesterday in UTC and
                          the author is on the list in the chair's own line of callers.json.
        the ceiling       CHAIR_WRITES_PER_HOUR sets an hour: a 429 past it (THE HOUR, below).
    Each is refused before any connection, with "executed": false. The value's cap and its 24 hours are
    the access list's, for the prefix.

    THE CANVAS (the owner, directly, 2026-10-20: "I approve the conference chair writing to the canvas (its own
    slot only, text and diagrams only, no images or links, max 20 posts an hour, stops on 'Hold on, team')";
    board row CLAUDE-MOBILE-OWNER-CHAIR-CANVAS-20261020T2252Z). One more thing the chair may do, and only this:
        the op            hset, as sent.
        the key           conf:canvas:<room>:chair, its own slot of a room's canvas (Blackboard
                          scripts/canvas_publish.py publishes the public items of every slot). The author it
                          states is "chair"; no persona's name serves here.
        the value         one canvas item, at most CANVAS_VALUE_BYTES: a JSON object of the canvas's own
                          members, type note or mermaid, public true, and nothing in it that is a link (canvas_refusal).
        the ceiling       CHAIR_CANVAS_PER_HOUR posts an hour, apart from the persona writes': a 429 (THE HOUR).
    Each is refused before any connection, with "executed": false. 'Hold on, team' is the chair's to keep: the
    bridge does not hear the call.

    THE HOUR, KEPT IN REDIS (Gemini on e6433ea, GROK-407FIX-428REV-0225Z: the service scales to zero, and a count
    kept only in memory starts again at nought with every new instance). Each ceiling is held twice:
        in Redis          INCR and EXPIRE on gov:quota:chair:<what>:<UTC hour> (redis_gov.hourly), so a restart, a
                          scale-to-zero or a second instance shares one count. The key is under gov:, which
                          redis_gov PROTECTS, so no caller of /op can reset it. At most the ceiling in a clock hour.
        in memory         take(), the last 3,600 seconds, per instance: it holds the span across an hour's turn
                          while the instance lives, and a flood it refuses costs Redis nothing.
    A post is counted before it is written, so one that then fails is still spent: the count errs toward fewer
    posts, never more. If Redis cannot count, nothing is written: a 503 with "executed": false.

    WHAT THIS DOES NOT DO, AND MUST BE SAID WHEREVER THE PILOT IS APPROVED: THE BRIDGE TRUSTS THE CHAIR'S
    IDENTITY. It does not see the owner's confirmation and does not enforce it; the chair does. Whoever
    holds the chair's identity can make these writes with no confirmation at all. What bounds that is the
    list above and nothing else, which is why the list is this short, and why the pilot may not be widened
    to real content while this is so.
"""
from __future__ import annotations

import base64
import collections
import datetime
import json
import os
import re
import time

# A value is at most 8 KB (proj:milestones:) and travels base64, so about 11 KB; 16 KB bounds the
# read without refusing a legitimate write. main.py reads no more than this for /op.
MAX_BODY_BYTES = 16384
CALLERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callers.json")
MEMBERS = ("op", "key", "field", "val", "enc", "count", "after", "expect", "author", "request")
# The ops that change nothing a caller owns. A failure inside one of these changed nothing; a failure
# inside any other op is an unknown outcome. Restated from redis_gov.OPS_READ and held equal by a test.
READS = ("get", "hgetall", "xrange")
# What a caller is FOR: its line in callers.json says, and a line that says anything else maps nobody.
FLEET, CHAIR, PROBE = "fleet-agent", "chair", "probe"
PURPOSES = (FLEET, CHAIR, PROBE)
CHAIR_OPS = ("get", "set")
CHAIR_WRITES_PER_HOUR = 20              # in Redis by the clock hour, and in memory by the last hour (THE HOUR)
MAX_PERSONAS = 4                        # as the chair's own cast
# The one kind of key the chair touches: a persona's own, for one day. Its day, and the author it states.
_PERSONA = re.compile(r"[a-z][a-z0-9-]{1,31}")
_PERSONA_KEY = re.compile(r"conf:persona:([0-9]{8}):([a-z][a-z0-9-]{1,31})")
# The chair's canvas slot (Blackboard scripts/canvas_publish.py: ROOM, ITEM_ID and the item's members).
CANVAS_OPS = ("hset",)
CANVAS_AUTHOR = "chair"
CHAIR_CANVAS_PER_HOUR = 20              # the owner's own number; kept as CHAIR_WRITES_PER_HOUR is
CANVAS_VALUE_BYTES = 512
CANVAS_TYPES = ("note", "mermaid")      # text and diagrams; never image_url, never wireframe's HTML
CANVAS_STATUSES = ("claimed", "working", "review", "done")
CANVAS_MEMBERS = ("v", "type", "title", "body", "status", "version", "public", "ts")
CANVAS_REQUIRED = ("v", "type", "title", "body", "status", "version", "public")
_CANVAS_KEY = re.compile(r"conf:canvas:([a-z0-9][a-z0-9-]{1,39}):chair")
_CANVAS_ITEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")
# Anything that is, or makes, a link: a scheme, a bare www. host, Mermaid's click and href directives.
_LINKISH = re.compile(r"(?i)://|\bwww\.|\b(?:javascript|data|mailto|vbscript):|\bhref\b|\bclick\s")
_CANVAS_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
clock = time.time                       # the door's clock; a test puts its own here
_SPENT = collections.deque()            # when this instance's chair writes were made, oldest first
_CANVAS_SPENT = collections.deque()     # when this instance's chair canvas posts were made, oldest first


def _lines(path=None) -> dict:
    """{verified email: (principal, what the caller is for, the personas it may name)}, lower-cased.

    A line is one of three, and a line that is none of them maps NOBODY:
        "principal"                                             a fleet agent, as every line was at first
        {"principal": ..., "for": "fleet-agent" | "probe"}      a fleet agent; or an account admitted for the
                                                                probe only, which /op refuses
        {"principal": ..., "for": "chair", "personas": [...]}   the conference chair, and the personas it may
                                                                name: one to MAX_PERSONAS names, each once
    Nothing rides in on an extra member, and a list of personas on a line that is not the chair's is one."""
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
        personas = ()
        if isinstance(line, str):
            who, purpose = line, FLEET
        elif isinstance(line, dict) and set(line) == {"principal", "for"} and line["for"] in (FLEET, PROBE):
            who, purpose = line["principal"], line["for"]
        elif isinstance(line, dict) and set(line) == {"principal", "for", "personas"} and line["for"] == CHAIR:
            who, purpose, named = line["principal"], CHAIR, line["personas"]
            if not isinstance(named, list) or not 1 <= len(named) <= MAX_PERSONAS or len(set(map(str, named))) != len(named) \
                    or not all(isinstance(name, str) and _PERSONA.fullmatch(name) for name in named):
                continue
            personas = tuple(named)
        else:
            continue
        if not isinstance(who, str):
            continue
        email, who = str(email).strip().lower(), who.strip().lower()
        if email and who:
            out[email] = (who, purpose, personas)
    return out


def load_callers(path=None) -> dict:
    """{verified email: principal} for the callers that may use /op, lower-cased. An account admitted for the
    probe only is NOT here: it acts as nobody. A missing or malformed file maps NOBODY: the same direction as
    redis_gov.load_acl, where an absent rule makes access stricter, never looser."""
    return {email: who for email, (who, purpose, _) in _lines(path).items() if purpose != PROBE}


def load_purposes(path=None) -> dict:
    """{verified email: what that caller is for}, every line. The same file, the same strictness."""
    return {email: purpose for email, (_, purpose, _) in _lines(path).items()}


def load_personas(path=None) -> dict:
    """{the chair's verified email: the personas it may name}."""
    return {email: personas for email, (_, purpose, personas) in _lines(path).items() if purpose == CHAIR}


def days(now: float) -> tuple:
    """Today and yesterday in UTC, as a persona key carries a day. Yesterday: a write proposed a minute before
    midnight is confirmed a minute after it, under the key it was shown with."""
    return tuple((datetime.datetime.fromtimestamp(now, datetime.timezone.utc) - datetime.timedelta(days=back))
                 .strftime("%Y%m%d") for back in (0, 1))


def take(now: float, spent, ceiling: int = CHAIR_WRITES_PER_HOUR) -> bool:
    """May the chair make one more write in this hour? `spent` holds the times of the ones made. THIS HALF IS
    PER INSTANCE AND IN MEMORY, and forgotten when the instance stops; redis_gov.hourly is the half that is not."""
    while spent and now - spent[0] >= 3600.0:
        spent.popleft()
    if len(spent) >= ceiling:
        return False
    spent.append(now)
    return True



def canvas_refusal(request: dict, author: str) -> str:
    """Why this is not a canvas post the chair may make, for our log; "" when it is. Never the value itself."""
    if request["op"] not in CANVAS_OPS:
        return "not hset"
    if author != CANVAS_AUTHOR:
        return "the author is not the chair"
    if not _CANVAS_ITEM.fullmatch(request["field"]):
        return "not an item id"
    if request["enc"] != "b64":
        return "not base64"             # as every chair write travels (the chair's bridge transport)
    try:
        raw = base64.b64decode(request["val"], validate=True)
        item = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return "not a JSON item"
    if len(raw) > CANVAS_VALUE_BYTES:
        return "over %d bytes" % CANVAS_VALUE_BYTES
    if not isinstance(item, dict) or set(item) - set(CANVAS_MEMBERS) or set(CANVAS_REQUIRED) - set(item):
        return "not the canvas item's members"
    whole = lambda value: isinstance(value, int) and not isinstance(value, bool)  # noqa: E731
    if not (whole(item["v"]) and item["v"] == 1) or item["type"] not in CANVAS_TYPES \
            or item["status"] not in CANVAS_STATUSES or item["public"] is not True \
            or not (whole(item["version"]) and 0 <= item["version"] < 10 ** 9):
        return "a member the canvas does not take"
    title, body, ts = item["title"], item["body"], item.get("ts", "")
    if not all(isinstance(text, str) for text in (title, body, ts)) or not title.strip() or not body.strip():
        return "a member the canvas does not take"
    if _CANVAS_CONTROL.search(title + body + ts) or any(ch in title + ts for ch in "\t\r\n"):
        return "a control character"
    if _LINKISH.search(title + "\n" + body):
        return "a link"
    return ""


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


def handle(caller: str, raw: bytes, connect, log, callers=None, gov=None, acl=None, purposes=None,
           personas=None, now=None, spent=None, canvas_spent=None) -> tuple:
    """(status, body) for one /op request from an ALREADY VERIFIED caller.

    caller   the email main.verify_caller returned: verified, and on BRIDGE_CALLERS.
    connect  main.connect; called only after the request has parsed, so a malformed body costs no
             connection.
    log      a callable taking one line. No value, no key content and no body ever goes to it.
    """
    email = str(caller or "").strip().lower()
    purpose = (load_purposes() if purposes is None else purposes).get(email, FLEET)
    if purpose == PROBE:
        # Admitted to the service, for the probe. Not our misconfiguration and not a caller of this endpoint.
        log("op by %s: this account is admitted for the probe only" % caller)
        return 403, {"error": "not authorised", "executed": False}
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
    canvas = False                      # the chair's canvas slot (canvas_refusal), and only the chair's
    if purpose == CHAIR:
        if not author or not asked:
            log("op by %s: the chair did not say which persona and which request" % caller)
            return 400, {"error": "bad request", "executed": False}
        # The op and the key as they were SENT, not as redis_gov would tidy them: the chair sends them exact.
        canvas = _CANVAS_KEY.fullmatch(request["key"]) is not None
        if canvas:
            why = canvas_refusal(request, author)
            if why:
                log("op by %s: not a canvas post the chair may make (%s)" % (caller, why))
                return 400, {"error": "bad request", "executed": False}
        else:
            named = _PERSONA_KEY.fullmatch(request["key"])
            listed = (load_personas() if personas is None else personas).get(email, ())
            if request["op"] not in CHAIR_OPS or named is None or named.group(2) != author or author not in listed \
                    or named.group(1) not in days(clock() if now is None else now):
                log("op by %s: not an op the chair makes, or not today's key of a persona it may name" % caller)
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
    if purpose == CHAIR and (op == "set" or canvas):
        # After every other refusal, so only a write that would have run counts; before the write, so
        # "executed": false is true. Memory first, so a flood it refuses costs Redis nothing (THE HOUR).
        what, ceiling, mine, named = (
            ("canvas", CHAIR_CANVAS_PER_HOUR, _CANVAS_SPENT if canvas_spent is None else canvas_spent, "canvas posts")
            if canvas else ("persona", CHAIR_WRITES_PER_HOUR, _SPENT if spent is None else spent, "writes"))
        at = clock() if now is None else now
        try:
            allowed = take(at, mine, ceiling) and gov.hourly(conn, "chair:" + what, at, ceiling)
        except Exception as error:  # noqa: BLE001 - the TYPE only, as at the connection
            log("op by %s: redis could not count the chair's hour (%s)" % (caller, type(error).__name__))
            return 503, {"error": "redis unreachable", "executed": False}
        if not allowed:
            log("op by %s: the chair's hourly ceiling of %d %s is reached" % (caller, ceiling, named))
            return 429, {"error": "quota", "executed": False}
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
