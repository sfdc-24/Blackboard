#!/usr/bin/env python3
"""Work records on Redis: a task or an issue any agent can create, read, move, hand off and close.
Stdlib only.

WHY THIS EXISTS
    The owner, 2026-10-13: "the boards to work through tasks, communication of agents, issues, and
    PR's is still not operational. This needs to be operational asap." Aya's Stage 1 exit check 4:
    "minimum task/issue/communication records support usable create/read/update/close and traced
    handoff on the declared canonical store, not empty dashboards/specifications."
    This is the minimum, built to her conditions (AYA-STAGE1-WORK-RECORDS-SOURCE-DESIGN-20261013).

A RECORD IS ITS LOG (namespace proj:work:v1:, beside proj:pr-board:v1:, which this never touches)
    proj:work:v1:rec:<id>           HASH    What was asked: kind, title, project, link, source,
                                            done_when, requested_by, created. Written ONCE.
    proj:work:v1:log:<id>           STREAM  One entry per move. THE LAST ENTRY IS THE RECORD'S STATE:
                                              r<N> <action> <state> <owner> <reviewer> <requester> <lease> <req> <words>
                                            The governor adds who (`by`) and when (`at`). <req> is this
                                            command's request id. Revision N follows N-1 and nothing else.
    proj:work:v1:index:<YYYYMMDD>   HASH    A LIST for finding records, one line per id:
                                              <rev> <state> <owner> <lease> <kind> <title>
                                            Rebuilt from the log (`reindex`). Never the authority.
    An id is W-<YYYYMMDD>-<6 hex>.

A MOVE IS ONE ATOMIC COMPARE-AND-SET
    A move is ONE write: the next log entry. The store takes r<N> only when the log is at r<N-1>
    (redis_gov.SEQUENCED, decided under the write's own WATCH). Two agents who both read r4 cannot
    both write r5: one is refused, nothing of its move is written, and it must read again. A move
    whose answer was lost and that arrives after someone else's is refused the same way. The
    description is write-once and the log is append-only (redis_gov.WRITE_ONCE); neither is given an
    expiry or can be deleted through the governed path: the log is the audit record.
    The list is written after the move and can only lag, never go back (redis_gov.REV_FORWARD). If
    that second write fails the command says so, and `reindex` repairs it.

THE MOVES, AND WHO MAY MAKE THEM
    create                    anyone                 -> requested      (names the owner)
    accept   --next           the owner              requested -> accepted (a lease, and a next step)
    start                     the owner              accepted -> working
    block    --blocker        the owner              accepted|working -> blocked
    unblock                   the owner              blocked -> working
    review   [--reviewer]     the owner              accepted|working -> review
    return   --note           the reviewer           review -> working
    done     --evidence       the reviewer, from review. The owner, only when no reviewer is named;
                              the entry then says "unreviewed"          -> done
    cancel   --note           the owner or the requester               -> cancelled
    handoff  --to             the owner or the requester               -> requested, for the new owner
    renew                     the owner              a new lease; nothing else moves
    reopen   --note           the requester          done|cancelled -> requested
    note     --note           anyone                 an entry; nothing moves
    A handoff returns the record to `requested`: a write naming you is not you accepting it.
    A lease that has run out is SHOWN as expired and gives nobody anything. It is not completion.

WHAT IS ENFORCED WHERE (said here so nobody has to find out)
    - ORDER is the store's: no entry out of sequence, by anyone, by any route.
    - WHO MAY MAKE WHICH MOVE is this tool's, checked twice with one function (`allowed`): before a
      move is written, and by EVERY READER against the `by` the governor wrote. An entry that no
      rule allows makes the record DISPUTED: it is shown as such, nothing is built on it, and
      `history` checks every entry from the first. A writer that goes around this tool is therefore
      caught, not prevented; preventing it would put a state machine in the governor.
    - On the board route an actor is the row's sender tag, a claim. Through the bridge it is the
      verified account's bound principal. The log's `by` is the governor's, either way.
    - A move whose outcome is UNKNOWN (no answer, or a lost one) is never sent again by this tool.
      `show` settles it by reading: if r<N> carries this command's <req>, it landed; if r<N> carries
      another, it never can.
    - NOTHING IS CUT SHORT. A text too long to read back whole, a description too long for one row,
      a day's list that is full: each is refused before anything is written, with the number.
    - One board row carries about a thousand characters of a read, so by the board route a day's
      list holds about nine records and `create` refuses the next one. The bridge's bound is fifty.

IT IS OFF UNTIL THE GOVERNOR IS PROVEN (REDIS_WORK=on)
    Everything above rests on a governor that has the three rules. An older one would take any write.
    So every write here is refused unless REDIS_WORK=on is set for this agent, and that is set only
    after `preflight` has passed on each route the agent can write by: it sends one entry that a
    correct governor must refuse, and says which it met. Reading needs no switch.

    python scripts/redis_work.py create --title "..." --owner grok --project Blackboard \\
           --source "board row AYA-..." --done-when "the suite is green on main"
    python scripts/redis_work.py accept W-20261013-a1b2c3 --next "what I do first"
    python scripts/redis_work.py done   W-20261013-a1b2c3 --evidence https://github.com/...
    python scripts/redis_work.py show   W-20261013-a1b2c3
    python scripts/redis_work.py list [--days 2]
    python scripts/redis_work.py --as cursor handoff W-20261013-a1b2c3 --to grok
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import secrets
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import bridge_client  # noqa: E402
import redis_gov  # noqa: E402
import redis_inbox as inbox  # noqa: E402   the board route's post, answer and page judgement

NS = "proj:work:v1:"
INDEX, REC, LOG = NS + "index:%s", NS + "rec:%s", NS + "log:%s"
ID = re.compile(r"^W-([0-9]{8})-[0-9a-f]{6}$")
KINDS = ("task", "issue")
STATES = ("requested", "accepted", "working", "blocked", "review", "done", "cancelled")
OPEN = STATES[:5]
ISSUE_LINK = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/[0-9]+$")
GITHUB_LINK = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(issues|pull)/[0-9]+$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")
CAPS = {"title": 100, "source": 150, "done-when": 160, "link": 150}
INDEX_TITLE = 30
EVENT_MAX = redis_gov.PREVIEW_CHARS      # a log entry a governed read shows whole
REC_MAX = 900                            # a description one board row carries whole
BOARD_BODY = 1000                        # what one board row carries of a day's list
INDEX_MAX = redis_gov.MAX_HASH_FIELDS    # what one read shows of a hash, by any route
LEASE_MINUTES = 240
SHOWN_LOG = 4                            # log entries one board row carries at the preview bound
REFUSED_EXIT, DENIED_EXIT, UNKNOWN_EXIT, CONFLICT_EXIT, DISPUTED_EXIT = 5, 6, 7, 9, 10
ENV_AGENT, ENV_SWITCH = "FLEET_AGENT", "REDIS_WORK"

# action: (states it may start from, the state it ends in or None to keep it, who, the text it needs)
MOVES = {
    "accept":  (("requested",), "accepted", "owner", "next"),
    "start":   (("accepted",), "working", "owner", ""),
    "block":   (("accepted", "working"), "blocked", "owner", "blocker"),
    "unblock": (("blocked",), "working", "owner", ""),
    "review":  (("accepted", "working"), "review", "owner", ""),
    "return":  (("review",), "working", "reviewer", "note"),
    "done":    (("accepted", "working", "review"), "done", "closer", "evidence"),
    "cancel":  (OPEN, "cancelled", "owner-or-requester", "note"),
    "handoff": (OPEN, "requested", "owner-or-requester", ""),
    "renew":   (("accepted", "working"), None, "owner", ""),
    "reopen":  (("done", "cancelled"), "requested", "requester", "note"),
}


class Stop(Exception):
    """This command ends here. `code` is the exit code; the message is the one thing to print."""

    def __init__(self, code: int, line: str, route: str = ""):
        super().__init__(line)
        self.code, self.route = code, route


# ----------------------------------------------------------------------------------------- small things

def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def new_id(moment: datetime.datetime) -> str:
    return "W-%s-%s" % (moment.strftime("%Y%m%d"), secrets.token_hex(3))


def agent(name: str, acl=None) -> str:
    """The canonical principal for a name, or Stop: an owner nobody can be is a record nobody holds."""
    who = redis_gov.principal(redis_gov.load_acl() if acl is None else acl, name)
    if not who:
        raise Stop(2, "%r is not an agent in scripts/redis_acl.json" % (name,))
    return who


def text_ok(label: str, value: str, cap: int = EVENT_MAX) -> str:
    """One line, exactly as a governed read will show it: no pipe (a read shows a slash), at most
    `cap`. Longer is REFUSED with the number, never cut."""
    value = " ".join(str(value or "").split())
    if "|" in value:
        raise Stop(2, "--%s may not hold a pipe: a governed read shows it as a slash" % label)
    if len(value) > cap:
        raise Stop(2, "--%s is %d characters; at most %d, so it reads back whole. Nothing was written."
                      % (label, len(value), cap))
    return value


def lease_stamp(moment: datetime.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%MZ")


def lease_until(lease: str):
    try:
        return datetime.datetime.strptime(lease, "%Y%m%dT%H%MZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def lease_over(lease: str, moment: datetime.datetime) -> bool:
    until = lease_until(lease)
    return until is not None and moment > until


# ---------------------------------------------------------------------------- an entry, and the rules

def event_text(ev: dict) -> str:
    """One log entry. Stop when it would not read back whole: nothing is ever cut."""
    head = "r%d %s %s %s %s %s %s %s" % (ev["rev"], ev["action"], ev["state"], ev["owner"], ev["reviewer"],
                                         ev["requester"], ev["lease"], ev["req"])
    line = head + ((" " + ev["words"]) if ev.get("words") else "")
    if len(line) > EVENT_MAX:
        raise Stop(2, "this entry would be %d characters; a governed read shows %d, and an entry is never "
                      "cut. Shorten what you wrote by %d. Nothing was written."
                      % (len(line), EVENT_MAX, len(line) - EVENT_MAX))
    return line


def parse_event(shown: dict):
    """A log entry as a read showed it, as a dict - or None when it is not one this tool wrote."""
    parts = str((shown or {}).get("text", "")).split(" ", 8)
    if len(parts) < 8 or not re.fullmatch(r"r[1-9][0-9]{0,3}", parts[0]) or parts[2] not in STATES:
        return None
    if parts[1] not in MOVES and parts[1] not in ("create", "note"):
        return None
    return {"rev": int(parts[0][1:]), "action": parts[1], "state": parts[2], "owner": parts[3],
            "reviewer": parts[4], "requester": parts[5], "lease": parts[6], "req": parts[7],
            "words": parts[8] if len(parts) > 8 else "", "by": str(shown.get("by", "")),
            "at": str(shown.get("at", "")), "id": str(shown.get("id", ""))}


def allowed(prev, ev: dict) -> str:
    """Why `ev` is NOT a move the rules allow after `prev` ("" when it is). `prev` is None for the first
    entry. ONE function: it refuses a move before it is written, and it is what every reader checks
    each entry against, with the `by` the governor wrote."""
    by, action = ev["by"], ev["action"]
    if prev is None:
        if ev["rev"] != 1 or action != "create":
            return "a record's first entry is r1 create"
        if (ev["state"], ev["lease"]) != ("requested", "-") or ev["requester"] != by:
            return "a record is created requested, with no lease, by its requester"
        return "" if ev["reviewer"] != ev["owner"] else "the reviewer cannot be the owner"
    if ev["rev"] != prev["rev"] + 1:
        return "r%d does not follow r%d" % (ev["rev"], prev["rev"])
    if ev["requester"] != prev["requester"]:
        return "the requester of a record never changes"
    same = {k: ev[k] == prev[k] for k in ("state", "owner", "reviewer", "lease")}
    if action == "note":
        return "" if all(same.values()) else "a note moves nothing"
    if action not in MOVES:
        return "%s is not a move" % action
    starts, end, actor, _needs = MOVES[action]
    if prev["state"] not in starts:
        return "%s starts from %s, not from %s" % (action, " or ".join(starts), prev["state"])
    may = {"owner": (prev["owner"],), "reviewer": (prev["reviewer"],), "requester": (prev["requester"],),
           "owner-or-requester": (prev["owner"], prev["requester"]),
           "closer": (prev["reviewer"],) if prev["state"] == "review" else
                     (prev["owner"],) if prev["reviewer"] == "-" else ()}[actor]
    if by not in may:
        if actor == "closer" and not may:
            return "a reviewer (%s) is named: it goes to review, and the reviewer closes it" % prev["reviewer"]
        return "only %s may %s it, and this is %s" % (" or ".join(may), action, by or "(nobody)")
    if ev["state"] != (end or prev["state"]):
        return "%s ends in %s" % (action, end or prev["state"])
    if action == "handoff":
        if ev["owner"] == prev["owner"] or (ev["reviewer"], ev["lease"]) != ("-", "-"):
            return "a handoff names a new owner and clears the reviewer and the lease"
        return ""
    if not same["owner"]:
        return "only a handoff changes the owner"
    if action == "review":
        if ev["reviewer"] in ("-", ev["owner"]):
            return "a review names a reviewer who is not the owner"
        return "" if prev["reviewer"] in ("-", ev["reviewer"]) and same["lease"] else \
            "a review keeps the lease, and the reviewer already named"
    if action == "reopen":
        return "" if (ev["reviewer"], ev["lease"]) == ("-", "-") else "a reopened record has no reviewer and no lease"
    if not same["reviewer"]:
        return "only a review, a handoff or a reopen changes the reviewer"
    if action in ("accept", "renew"):
        return "" if lease_until(ev["lease"]) else "%s sets a lease" % action
    if end in ("done", "cancelled"):
        return "" if ev["lease"] == "-" else "a closed record holds no lease"
    return "" if same["lease"] else "%s does not change the lease" % action


def index_line(ev: dict, kind: str, title: str) -> str:
    return "%d %s %s %s %s %s" % (ev["rev"], ev["state"], ev["owner"], ev["lease"], kind, title[:INDEX_TITLE])


def read_index_line(line: str):
    parts = str(line or "").split(" ", 5)
    if len(parts) < 5 or not parts[0].isdigit() or parts[1] not in STATES or parts[4] not in KINDS:
        return None
    return {"rev": int(parts[0]), "state": parts[1], "owner": parts[2], "lease": parts[3], "kind": parts[4],
            "title": parts[5] if len(parts) > 5 else ""}


# ------------------------------------------------------------------------------------- one governed op

_OK = re.compile(r"OK (?P<op>[a-z]+) (?P<key>\S+)(?: \[[^\]]*\])?: (?P<body>.*)", re.S)
_WROTE = re.compile(r"OK (?P<op>[a-z]+) (?P<key>\S+)(?: field=\S+)? ttl=\S+ old=\S+ new=\S+ \(audited\)")


def request_row(who: str, op: str, key: str, field=None, value=None, count=None, after=None) -> dict:
    """One governed request row from `who`, in the grammar scripts/bus_request.py parses."""
    rid = "WORK-%s-%s" % (op.upper(), secrets.token_hex(5))
    parts = ["BCB|v=1", "id=" + rid, "phase=REQUEST", "from=" + who, "to=" + inbox.WORKER, "do=redis-op",
             "op=" + op, "key=" + key]
    if field:
        parts.append("field=" + field)
    if value is not None:
        parts += ["val=" + base64.b64encode(value.encode("utf-8")).decode("ascii"), "enc=b64"]
    if count is not None:
        parts.append("count=%d" % int(count))
    if after is not None:
        parts.append("after=" + after)
    return {"row_id": rid, "source_tag": who, "target_surface": inbox.WORKER,
            "action_type": inbox.REQUEST_ACTION, "category": "OPEN", "project_tag": "Blackboard",
            "gist": "work record %s" % op, "subgist": key, "payload": "|".join(parts)}


def govern(who: str, op: str, key: str, *, field=None, value=None, count=None, after=None, wait=180.0,
           since="") -> dict:
    """One governed operation as `who`, by the bridge when it is on for this agent, else by the board.

    {"value": what a read showed, "truncated": a hash had more fields than a read shows,
     "route": "bridge" | "board"}; a paged read (after=) returns {"page": redis_inbox's judgement}.
    Raises Stop for everything that is not a success. A WRITE that Stops with UNKNOWN_EXIT, or with 2
    (no answer yet), MAY HAVE HAPPENED and is not sent again by this tool."""
    write = op not in redis_gov.OPS_READ
    try:
        result = bridge_client.attempt(op, key, me=who, field=field, value=value, count=count, after=after)
    except bridge_client.Refused as why:
        raise Stop(REFUSED_EXIT, "REFUSED: %s" % why.reason, "bridge") from None
    except bridge_client.Denied as why:
        raise Stop(DENIED_EXIT, "DENIED by the bridge: %s. Not sent on the board route." % why, "bridge") from None
    except bridge_client.Unknown as why:
        raise Stop(UNKNOWN_EXIT, "UNKNOWN OUTCOME for %s %s: %s. It MAY HAVE HAPPENED and is not sent "
                                 "again." % (op, key, why), "bridge") from None
    if result is not None:
        if after is not None:
            return {"page": inbox.page_of_result(result, key, after), "route": "bridge"}
        if result.get("op") != op or result.get("key") != key:
            raise Stop(3, "the bridge answered another operation; nothing here is trusted", "bridge")
        return {"value": result.get("value"), "truncated": bool(result.get("truncated")), "route": "bridge"}
    rid = inbox.post(request_row(who, op, key, field=field, value=value, count=count, after=after))
    got = inbox.answer(rid, wait, since=since or inbox.stamp())
    if not got:
        raise Stop(2, "NO ANSWER YET to %s (%s %s). The request row is on the board and may still be "
                      "carried out%s." % (rid, op, key, "; it is not sent again" if write else ""), "board")
    if got.get("from") != inbox.WORKER:
        raise Stop(3, "the answer row is from %r, not %s; nothing here is trusted" % (got.get("from"), inbox.WORKER),
                   "board")
    if after is not None:
        return {"page": inbox.page_of(got["payload"], key, after), "route": "board"}
    head, sep, text = got["payload"].partition("|text=")
    text = text.strip()
    if not sep:
        raise Stop(3, "the worker's answer has no text=", "board")
    if text.startswith("REFUSED"):
        raise Stop(REFUSED_EXIT, text[:300], "board")
    if text.startswith("FAILED"):
        raise Stop(UNKNOWN_EXIT if write else 3, "%s%s" % (text[:200], ". It MAY HAVE HAPPENED and is not "
                                                           "sent again." if write else ""), "board")
    if write:
        m = _WROTE.fullmatch(text)
        if not m or m["op"] != op or m["key"] != key:
            raise Stop(UNKNOWN_EXIT, "the worker's answer to %s %s is not one this tool can read. It MAY "
                                     "HAVE HAPPENED and is not sent again." % (op, key), "board")
        return {"value": None, "truncated": False, "route": "board"}
    if re.search(r"(^|\|)(truncated=1|full=)", head + "|" + text):
        raise Stop(4, "the answer to %s %s is longer than one board row" % (op, key), "board")
    m = _OK.fullmatch(text)
    if not m or m["op"] != op or m["key"] != key:
        raise Stop(3, "the worker's answer is not a %s of %s" % (op, key), "board")
    try:
        shown = json.loads(m["body"])
    except ValueError:
        if m["body"].endswith("..."):
            raise Stop(4, "the answer to %s %s is longer than one board row" % (op, key), "board") from None
        raise Stop(3, "the worker's answer to %s %s is not JSON" % (op, key), "board") from None
    return {"value": shown, "truncated": False, "route": "board"}


def switched_on() -> None:
    if (os.environ.get(ENV_SWITCH) or "").strip().lower() != "on":
        raise Stop(2, "work records are OFF for this agent: nothing is written. They rest on a governor "
                      "that takes a log entry only in sequence; set %s=on only after `preflight` has passed "
                      "on every route this agent writes by. Reading needs no switch." % ENV_SWITCH)


# --------------------------------------------------------------------------------------------- records

def index_key(rid: str) -> str:
    m = ID.match(rid or "")
    if not m:
        raise Stop(2, "%r is not a record id (W-<YYYYMMDD>-<6 hex>)" % (rid,))
    return INDEX % m[1]


def current(who: str, rid: str, args) -> tuple:
    """(the record's state, the newest entries read, why it is DISPUTED or ""). The state IS the last
    log entry. The last entry is checked against the one before it with `allowed`."""
    index_key(rid)
    shown = govern(who, "xrange", LOG % rid, count=SHOWN_LOG, wait=args.wait, since=args.since)["value"]
    if not isinstance(shown, list) or not shown:
        raise Stop(2, "no record %s: its log is empty" % rid)
    events = [parse_event(e) for e in shown]
    if events[-1] is None:
        raise Stop(3, "the last entry of %s is not one this tool wrote; nothing is built on it" % rid)
    last = events[-1]
    prev = events[-2] if len(events) > 1 else None
    if last["rev"] > 1 and prev is None:
        return last, shown, "r%d has no readable entry before it" % last["rev"]
    return last, shown, allowed(prev, last)


def must_be_sound(rid: str, last: dict, disputed: str) -> None:
    if disputed:
        raise Stop(DISPUTED_EXIT, "%s is DISPUTED: r%d %s by %s is not a move the rules allow (%s). Nothing "
                                  "is built on it until its requester (%s) has looked."
                   % (rid, last["rev"], last["action"], last["by"], disputed, last["requester"]))


def append(who: str, rid: str, ev: dict, kind: str, title: str, args) -> int:
    """THE MOVE: one log entry, taken only as the next revision. Then the list, which can only lag.
    Returns 0, or 3 when the move landed and the list was not updated."""
    line = event_text(ev)
    try:
        govern(who, "xadd", LOG % rid, value=line, wait=args.wait, since=args.since)
    except Stop as why:
        if why.code == REFUSED_EXIT and "revision conflict" in str(why):
            raise Stop(CONFLICT_EXIT, "CONFLICT: someone moved %s first (%s). NOTHING of this move was "
                                      "written. Read it again: show %s" % (rid, str(why)[9:], rid)) from None
        if why.code in (UNKNOWN_EXIT, 2):
            raise Stop(why.code, "%s\nRead it with `show %s`: if r%d carries req=%s this move landed; if r%d "
                                 "carries another req, this one never can." % (why, rid, ev["rev"], ev["req"],
                                                                               ev["rev"])) from None
        raise
    try:
        govern(who, "hset", index_key(rid), field=rid, value=index_line(ev, kind, title), wait=args.wait,
               since=args.since)
    except Stop as why:
        print("MOVED: %s is at r%d (req=%s). THE LIST WAS NOT UPDATED (%s). Run: reindex %s"
              % (rid, ev["rev"], ev["req"], str(why).splitlines()[0][:160], rid))
        return 3
    return 0


def describe_of(who: str, rid: str, args) -> dict:
    value = govern(who, "hgetall", REC % rid, wait=args.wait, since=args.since)["value"]
    return value if isinstance(value, dict) else {}


def cmd_create(who: str, args) -> int:
    switched_on()
    kind = args.kind
    title = text_ok("title", args.title, CAPS["title"])
    source = text_ok("source", args.source, CAPS["source"])
    done_when = text_ok("done-when", args.done_when, CAPS["done-when"])
    link = text_ok("link", args.link, CAPS["link"])
    for label, value in (("title", title), ("source", source), ("done-when", done_when)):
        if not value:
            raise Stop(2, "--%s is required: a record says what was asked, where the ask came from, and "
                          "what done means" % label)
    if not PROJECT.match(args.project or ""):
        raise Stop(2, "--project is one word of at most 30 letters, digits, dot, dash or underscore")
    if kind == "issue" and not ISSUE_LINK.match(link):
        raise Stop(2, "an issue record is a pointer: --link must be the GitHub issue "
                      "(https://github.com/<owner>/<repo>/issues/<n>). GitHub stays the authority.")
    if link and not GITHUB_LINK.match(link):
        raise Stop(2, "--link is a GitHub pull request or issue: https://github.com/<owner>/<repo>/pull|issues/<n>")
    owner = agent(args.owner)
    reviewer = agent(args.reviewer) if args.reviewer else "-"
    moment = now()
    rid = new_id(moment)
    record = {"kind": kind, "title": title, "project": args.project, "link": link or "-", "source": source,
              "done_when": done_when, "requested_by": who, "created": moment.strftime("%Y-%m-%dT%H:%M:%SZ")}
    described = json.dumps(record, sort_keys=True, ensure_ascii=False)
    if len(json.dumps({k: redis_gov.preview(v) for k, v in record.items()}, sort_keys=True)) > REC_MAX:
        raise Stop(2, "this description would not fit one board row (%d characters; at most %d). Shorten "
                      "it. Nothing was written." % (len(described), REC_MAX))
    ev = {"rev": 1, "action": "create", "state": "requested", "owner": owner, "reviewer": reviewer,
          "requester": who, "lease": "-", "req": secrets.token_hex(4), "words": "", "by": who}
    why_not = allowed(None, ev)
    if why_not:
        raise Stop(2, "not created: %s" % why_not)
    event_text(ev)
    # THE DAY'S LIST MUST STILL BE READABLE AFTERWARDS: a full list refuses the record, it is never cut.
    try:
        listed = govern(who, "hgetall", INDEX % rid[2:10], wait=args.wait, since=args.since)
    except Stop as why:
        if why.code != 4:
            raise
        raise Stop(2, "not created: today's list no longer fits one board row. Create it through the "
                      "bridge, or tomorrow. Nothing was written.") from None
    lines = listed["value"] if isinstance(listed["value"], dict) else {}
    after = json.dumps(dict(lines, **{rid: index_line(ev, kind, title)}), sort_keys=True, ensure_ascii=False)
    if listed["truncated"] or len(lines) >= INDEX_MAX:
        raise Stop(2, "not created: today's list is full (%d records). Create it tomorrow. Nothing was "
                      "written." % len(lines))
    if listed["route"] == "board" and len(after) > BOARD_BODY:
        raise Stop(2, "not created: with one more record today's list (%d) would not fit one board row. "
                      "Create it through the bridge, or tomorrow. Nothing was written." % len(lines))
    govern(who, "hput", REC % rid, value=described, wait=args.wait, since=args.since)
    code = append(who, rid, ev, kind, title, args)
    print("%s created (req=%s): %s for %s, state requested. %s must accept it." % (rid, ev["req"], kind, owner, owner))
    return code


def cmd_move(who: str, args) -> int:
    switched_on()
    action, rid = args.what, args.id
    needs = MOVES[action][3] if action in MOVES else "note"
    text = text_ok(needs, getattr(args, needs, "")) if needs else ""
    if needs and not text:
        raise Stop(2, "%s needs --%s" % (action, needs))
    note = text_ok("note", getattr(args, "note", "")) if needs != "note" else ""
    last, _shown, disputed = current(who, rid, args)
    must_be_sound(rid, last, disputed)
    moment = now()
    ev = dict(last, rev=last["rev"] + 1, action=action, req=secrets.token_hex(4), by=who)
    said = [("%s=%s" % (needs, text)) if text and action != "note" else text, ("note=" + note) if note else ""]
    if action in MOVES:
        ev["state"] = MOVES[action][1] or last["state"]
    if action in ("accept", "renew"):
        ev["lease"] = lease_stamp(moment + datetime.timedelta(minutes=max(5, min(int(args.lease_minutes), 24 * 60))))
    elif action == "review":
        ev["reviewer"] = agent(args.reviewer) if args.reviewer else last["reviewer"]
    elif action == "handoff":
        ev.update(owner=agent(args.to), reviewer="-", lease="-")
        said.insert(0, "from=" + last["owner"])
    elif action == "reopen":
        ev.update(reviewer="-", lease="-")
    elif action == "done" and last["state"] != "review":
        said.insert(0, "unreviewed")
    if ev["state"] in ("done", "cancelled"):
        ev["lease"] = "-"
    ev["words"] = " ".join(s for s in said if s)
    why_not = allowed(last, ev)                      # the same rules every reader will hold this entry to
    if why_not:
        raise Stop(2, "not moved: %s (%s is %s, held by %s)" % (why_not, rid, last["state"], last["owner"]))
    event_text(ev)                                   # too long is refused before anything is read or written
    described = describe_of(who, rid, args)          # the list's line carries the kind and the title
    kind = described.get("kind") if described.get("kind") in KINDS else "task"
    code = append(who, rid, ev, kind, str(described.get("title", "")), args)
    print("%s %s (req=%s): r%d>r%d %s -> %s, owner %s" % (rid, action, ev["req"], last["rev"], ev["rev"],
                                                        last["state"], ev["state"], ev["owner"]))
    return code


def state_line(rid: str, ev: dict, kind: str, title: str, moment: datetime.datetime) -> str:
    lease = ""
    if ev["lease"] != "-":
        lease = "  lease %s%s" % (ev["lease"], " EXPIRED (it gives nobody anything; the owner still holds it)"
                                  if lease_over(ev["lease"], moment) else "")
    return "%s  %-5s %-9s owner %s  rev %d%s%s  %s" % (
        rid, kind, ev["state"], ev["owner"], ev["rev"], lease,
        ("  reviewer " + ev["reviewer"]) if ev.get("reviewer", "-") != "-" else "", title)


def print_entry(shown: dict) -> None:
    ev = parse_event(shown)
    if ev is None:
        print("  %s  %s  by %s: (not an entry this tool wrote) %s" % (shown.get("id", "?"), shown.get("at", "?"),
                                                                     shown.get("by", "?"), shown.get("text", "")))
        return
    print("  %s  %s  by %s  r%d>r%d %s -> %s owner=%s req=%s result=applied%s"
          % (ev["id"], ev["at"], ev["by"], ev["rev"] - 1, ev["rev"], ev["action"], ev["state"], ev["owner"],
             ev["req"], ("  " + ev["words"]) if ev["words"] else ""))


def cmd_show(who: str, args) -> int:
    rid = args.id
    last, shown, disputed = current(who, rid, args)
    record = describe_of(who, rid, args)
    print(state_line(rid, last, record.get("kind", "?"), record.get("title", "(no description was written)"), now()))
    print("project %s; link %s; source: %s" % (record.get("project", "?"), record.get("link", "-"),
                                               record.get("source", "?")))
    print("done when: %s" % record.get("done_when", "?"))
    print("requested by %s at %s" % (last["requester"], record.get("created", "?")))
    for entry in shown:
        print_entry(entry)
    if disputed:
        print("DISPUTED: r%d %s by %s is not a move the rules allow (%s). Nothing is built on it until its "
              "requester (%s) has looked." % (last["rev"], last["action"], last["by"], disputed, last["requester"]))
        return DISPUTED_EXIT
    return 0


def cmd_list(who: str, args) -> int:
    moment, code, seen = now(), 0, 0
    days = args.day or [(moment - datetime.timedelta(days=n)).strftime("%Y%m%d") for n in range(max(1, args.days))]
    for day in days:
        if not re.fullmatch(r"[0-9]{8}", day):
            raise Stop(2, "--day is YYYYMMDD: %r" % day)
        try:
            got = govern(who, "hgetall", INDEX % day, wait=args.wait, since=args.since)
        except Stop as why:
            if why.code != 4:
                raise
            print("%s: NOT LISTED. The day's records do not fit one board row. Read them through the "
                  "bridge, or `show` one by its id." % day)
            code = 4
            continue
        lines = got["value"] if isinstance(got["value"], dict) else {}
        for rid in sorted(lines):
            entry = read_index_line(lines[rid])
            if entry is None:
                print("%s  (a line this tool did not write)" % rid)
            elif args.all or entry["state"] in OPEN:
                print(state_line(rid, entry, entry["kind"], entry["title"], moment))
                seen += 1
        if got["truncated"]:
            print("%s: MORE THAN %d records; a read shows %d. Not all are listed." % (day, INDEX_MAX, INDEX_MAX))
            code = 4
    print("%d %s over %s. A list is for finding a record; `show` reads the record itself."
          % (seen, "records" if args.all else "open records", ", ".join(days)))
    return code


def cmd_history(who: str, args) -> int:
    """Every entry, oldest first, and EVERY ONE checked against the rules from the first."""
    key, cursor, count, prev, bad, seen = LOG % args.id, "0-0", SHOWN_LOG, None, [], 0
    index_key(args.id)
    for _page in range(max(1, int(args.pages))):
        page = govern(who, "xrange", key, count=count, after=cursor, wait=args.wait, since=args.since)["page"]
        if page["status"] == "cut" and count > 1:
            count = max(1, count // 2)
            continue
        if page["status"] != "ok":
            raise Stop(3, "NOT READ after %s: %s" % (cursor, page["why"]))
        for shown in page["entries"]:
            print_entry(shown)
            ev = parse_event(shown)
            why_not = "not an entry this tool wrote" if ev is None else allowed(prev, ev)
            if why_not:
                bad.append("%s: NOT ALLOWED: %s" % (shown.get("id", "?"), why_not))
            prev, seen = ev or prev, seen + 1
        cursor = page["last"]
        if not page["more"]:
            break
    else:
        print("more follows %s: run it again with --pages. NOT VERIFIED: the history was not read to its end."
              % cursor)
        return 4
    for line in bad:
        print(line)
    if not seen:
        raise Stop(2, "no record %s: its log is empty" % args.id)
    print("%d entries of %s. %s" % (seen, args.id, "DISPUTED: %d not allowed" % len(bad) if bad else
                                   "VERIFIED: every one is a move its actor was allowed to make."))
    return DISPUTED_EXIT if bad else 0


def cmd_reindex(who: str, args) -> int:
    """Write the list's line for one record again, from its log. It can only bring the list forward."""
    switched_on()
    last, _shown, disputed = current(who, args.id, args)
    must_be_sound(args.id, last, disputed)
    described = describe_of(who, args.id, args)
    kind = described.get("kind") if described.get("kind") in KINDS else "task"
    govern(who, "hset", index_key(args.id), field=args.id, value=index_line(last, kind, str(described.get("title", ""))),
           wait=args.wait, since=args.since)
    print("%s: the list now shows r%d %s" % (args.id, last["rev"], last["state"]))
    return 0


def cmd_preflight(who: str, args) -> int:
    """Prove the governor on this route takes a log entry only in sequence, by sending one it must
    refuse: r2 to a log that has no r1. A governor without the rule WRITES it, and this says so."""
    if not args.allow_stray_entry:
        raise Stop(2, "preflight sends one entry that a correct governor REFUSES and a governor without the "
                      "rule WRITES. Run it only when that one stray entry has been agreed, and say so: "
                      "--allow-stray-entry")
    key = LOG % ("preflight-" + who)
    try:
        got = govern(who, "xadd", key, value="r2 note requested - - - - %s preflight" % secrets.token_hex(4),
                     wait=args.wait, since=args.since)
    except Stop as why:
        if why.code == REFUSED_EXIT and "revision conflict" in str(why):
            print("PASS by the %s route: the governor refused an entry out of sequence (%s). Nothing was "
                  "written." % (why.route, str(why)[:120]))
            return 0
        raise
    print("FAIL by the %s route: THIS GOVERNOR TOOK AN ENTRY OUT OF SEQUENCE. It does not have the rule, so "
          "a move here would not be a compare-and-set. Do not set %s=on for this route. One stray entry is "
          "now in %s." % (got["route"], ENV_SWITCH, key))
    return 1


def cmd_selftest(_who: str, _args) -> int:
    """The grammar and the rules, with no network and no row."""
    ev = {"rev": 3, "action": "start", "state": "working", "owner": "grok", "reviewer": "-",
          "requester": "claude-code-cli", "lease": "20261013T0830Z", "req": "0a1b2c3d", "words": ""}
    line = event_text(ev)
    back = parse_event({"text": line, "by": "grok", "at": "t", "id": "3-0"})
    assert {k: back[k] for k in ev} == ev, back
    assert redis_gov._revision(line, "r") == 3, "the governor reads the same revision this writes"
    assert redis_gov._revision(index_line(ev, "task", "a title"), "") == 3
    worst = dict(ev, rev=9999, action="handoff", state="requested", owner="chatgpt-codex-desktop",
                 reviewer="chatgpt-codex-desktop", requester="chatgpt-codex-desktop")
    room = EVENT_MAX - len(event_text(worst))
    assert room >= 60, "even the longest head leaves %d characters for words" % room
    try:
        event_text(dict(worst, words="w" * (room + 1)))
    except Stop:
        pass
    else:
        raise AssertionError("an entry longer than a read shows was accepted")
    assert len(index_line(worst, "issue", "t" * 200)) <= redis_gov.PREVIEW_CHARS
    assert parse_event({"text": "not an entry"}) is None and parse_event({"text": "r1 dance requested a b c - x"}) is None
    one = {"rev": 1, "action": "create", "state": "requested", "owner": "grok", "reviewer": "-",
           "requester": "cursor", "lease": "-", "req": "x", "words": "", "by": "cursor"}
    assert allowed(None, one) == "" and allowed(None, dict(one, by="grok")), "only its requester creates it"
    two = dict(one, rev=2, action="accept", state="accepted", lease="20261013T0830Z", by="grok")
    assert allowed(one, two) == "" and allowed(one, dict(two, by="cursor")), "only the owner accepts"
    assert allowed(one, dict(two, rev=3)) and allowed(one, dict(two, requester="grok"))
    row = request_row("grok", "xadd", LOG % "W-20261013-a1b2c3", value=line)
    assert "|op=xadd|key=proj:work:v1:log:W-20261013-a1b2c3|val=" in row["payload"], row
    assert row["source_tag"] == "grok" and row["action_type"] == inbox.REQUEST_ACTION, row
    assert set(MOVES) <= set(__doc__.split()), "every move is in the docstring"
    assert all(end is None or end in STATES for _, end, _, _ in MOVES.values())
    assert (REC % "", LOG % "", INDEX % "") == (redis_gov.WRITE_ONCE[0], redis_gov.SEQUENCED[0], redis_gov.REV_FORWARD[0])
    assert ID.match("W-20261013-a1b2c3") and not ID.match("W-20261013-A1B2C3") and not ID.match("W-2026-a1b2c3")
    assert redis_gov._KEY.match(REC % "W-20261013-a1b2c3") and redis_gov._FIELD.match("W-20261013-a1b2c3")
    print("selftest OK: an entry reads back whole and carries the revision the governor checks; the keys "
          "are proj:work:v1:rec:<id>, log:<id> and index:<day>")
    return 0


# ------------------------------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as", dest="who", default=os.environ.get(ENV_AGENT, "claude-code-cli"),
                        help="the agent acting (default: $%s, else claude-code-cli)" % ENV_AGENT)
    sub = parser.add_subparsers(dest="what", required=True)
    for name in ["create", "note", "show", "list", "history", "reindex", "preflight", "selftest"] + sorted(MOVES):
        p = sub.add_parser(name)
        p.add_argument("--wait", type=float, default=180.0, help="seconds to wait for the worker, per operation")
        p.add_argument("--since", default="", help="board timestamp to read answers from")
        if name == "create":
            p.add_argument("--kind", choices=KINDS, default="task")
            p.add_argument("--title", required=True)
            p.add_argument("--owner", required=True)
            p.add_argument("--project", required=True, help="one word: Blackboard, converspan, conference, site")
            p.add_argument("--source", required=True, help="where the ask came from: a board row id, a doc, a call")
            p.add_argument("--done-when", dest="done_when", required=True, help="what done means")
            p.add_argument("--link", default="", help="the GitHub pull request or issue; required for an issue")
            p.add_argument("--reviewer", default="", help="who closes it; it then cannot be closed unreviewed")
        elif name == "list":
            p.add_argument("--day", action="append", help="YYYYMMDD; may repeat (default: the last --days)")
            p.add_argument("--days", type=int, default=2)
            p.add_argument("--all", action="store_true", help="also the done and the cancelled")
        elif name not in ("selftest", "preflight"):
            p.add_argument("id")
        if name == "history":
            p.add_argument("--pages", type=int, default=10)
        if name == "preflight":
            p.add_argument("--allow-stray-entry", action="store_true",
                           help="agreed: a governor without the rule will write one stray entry")
        if name in MOVES or name == "note":
            for flag in sorted({"note", MOVES.get(name, ("", "", "", ""))[3] or "note"}):
                p.add_argument("--" + flag, default="")
        if name in ("accept", "renew"):
            p.add_argument("--lease-minutes", type=int, default=LEASE_MINUTES)
        if name == "review":
            p.add_argument("--reviewer", default="", help="not needed when one was named at create")
        if name == "handoff":
            p.add_argument("--to", required=True)
    return parser


def main(argv=()) -> int:
    args = build_parser().parse_args(list(argv))
    try:
        if args.what == "selftest":
            return cmd_selftest("", args)
        who = agent(args.who)
        if args.what in MOVES or args.what == "note":
            return cmd_move(who, args)
        if args.what == "preflight":
            return cmd_preflight(who, args)
        return {"create": cmd_create, "show": cmd_show, "list": cmd_list, "history": cmd_history,
                "reindex": cmd_reindex}[args.what](who, args)
    except Stop as why:
        print(str(why))
        return why.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
