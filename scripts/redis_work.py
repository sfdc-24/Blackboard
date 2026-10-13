#!/usr/bin/env python3
"""Work records on Redis: a task or an issue an agent can create, read, move, hand off and close.
Stdlib only.

WHY THIS EXISTS
    The owner, 2026-10-13: "the boards to work through tasks, communication of agents, issues, and
    PR's is still not operational. This needs to be operational asap." Aya's Stage 1 exit check 4:
    "minimum task/issue/communication records support usable create/read/update/close and traced
    handoff on the declared canonical store, not empty dashboards/specifications."
    Built to her conditions (AYA-STAGE1-WORK-RECORDS-SOURCE-DESIGN-20261013) and to her review of the
    first version (AYA-PR403-ACTOR-ADMISSION-ADDENDUM-20261013): that version DETECTED a forbidden
    move after the fact. This one does not let it be written.

A RECORD IS ITS LOG (namespace proj:work:v1:, beside proj:pr-board:v1:, which this never touches)
    proj:work:v1:rec:<id>           HASH    What was asked: kind, title, project, link, source,
                                            done_when, requested_by, created. Written ONCE.
    proj:work:v1:log:<id>           STREAM  One entry per move. THE LAST ENTRY IS THE RECORD'S STATE:
                                              r<N> <action> <state> <owner> <reviewer> <requester> <lease> <req> <words>
                                            The governor adds who (`by`) and when (`at`). <req> is this
                                            command's request id.
    proj:work:v1:index:<YYYYMMDD>   HASH    A LIST for finding records, one line per id:
                                              <rev> <state> <owner> <lease> <kind> <title>
    An id is W-<YYYYMMDD>-<6 hex>.

WHO DECIDES WHAT: THE STORE, NOT THIS TOOL
    The rules live in scripts/redis_gov.py (WORK_MOVES, work_allowed) and are applied INSIDE the
    governed write, under its one WATCH, before the entry and its audit are queued:
    - THE ACTOR IS THE VERIFIED CALLER. A work record is written only by a caller whose identity its
      transport verified: the bridge, after OIDC verification and the caller binding. A board row's
      sender tag is a claim, so the board route can READ a record and can never write one. `--as`
      chooses which agent this command is; it cannot make it someone the bridge did not verify.
    - A MOVE IS ONE COMPARE-AND-SET. The store replays the whole log, then takes the entry only if it
      is the next revision, a move that actor may make from that state and owner, and carries the
      evidence the move requires. Two agents who both read r4 cannot both write r5. An unauthorized
      done, a theft by handoff, a forged requester: refused before the stream changes.
    - A TAINTED RECORD TAKES NOTHING. If any entry in a log is not a legal move, the store adds
      nothing to it, and neither does this tool: `show`, a move and `reindex` all replay the whole
      chain first, never just the last pair. The history stays readable for a person to reconcile.
    - THE LIST CANNOT LIE. A list line is taken only when it says what the record's log says.
    - The description is written once, by its verified requester. Neither it nor the log is given an
      expiry or can be deleted through the governed path.
    - A LOG BELONGS TO ITS DESCRIPTION. The store admits a record's first entry only if its
      description exists, has its whole schema, and names that entry's verified actor as requester;
      it reads the description under the same WATCH as the entry. So nobody begins a record another
      agent described, and no log exists without a description. If a create is cut off between the
      two writes, `resume` lets the SAME requester finish it: the description is not written again,
      and nobody else can.
    This tool applies the same function before it sends, to say why a move would be refused without
    spending a call. That is a courtesy. The store's answer is the authority.

THE MOVES, AND WHO MAY MAKE THEM
    create                    anyone verified        -> requested      (names the owner)
    resume   --owner          the requester          finishes a create cut off after its description
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
    note     --note           anyone verified        an entry; nothing moves
    A handoff returns the record to `requested`: a write naming you is not you accepting it.
    A lease that has run out is SHOWN as expired and gives nobody anything. It is not completion.

WHAT ELSE IS TRUE (said here so nobody has to find out)
    - A move whose outcome is UNKNOWN (no answer, or a lost one) is never sent again by this tool.
      `show` settles it by reading: if r<N> carries this command's <req>, it landed; if r<N> carries
      another, it never can.
    - NOTHING IS CUT SHORT. A text too long to read back whole, a description too long for one row,
      a day's list that is full: each is refused before anything is written, with the number.
    - The list is written after the move and can only lag. If that write fails the command says
      so, and `reindex` repairs it.
    - Reading by the board route costs rows: a record of N entries is about N/4 request rows.

IT IS OFF UNTIL THE GOVERNOR IS PROVEN (REDIS_WORK=on)
    All of it rests on a governor that has these rules. An older one would take any write. So every
    write here is refused unless REDIS_WORK=on is set for this agent, and that is set only after
    `preflight` has passed on each route: it sends one entry a correct governor must refuse, and says
    which governor it met. Reading needs no switch.

    python scripts/redis_work.py create --title "..." --owner grok --project Blackboard \\
           --source "board row AYA-..." --done-when "the suite is green on main"
    python scripts/redis_work.py accept W-20261013-a1b2c3 --next "what I do first"
    python scripts/redis_work.py done   W-20261013-a1b2c3 --evidence https://github.com/...
    python scripts/redis_work.py show   W-20261013-a1b2c3
    python scripts/redis_work.py list [--days 2]
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
KINDS = redis_gov.WORK_KINDS
STATES, OPEN, MOVES = redis_gov.WORK_STATES, redis_gov.WORK_OPEN, redis_gov.WORK_MOVES
allowed = redis_gov.work_allowed          # THE rules: the store's, used here only to explain a refusal early
ISSUE_LINK = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/[0-9]+$")
GITHUB_LINK = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(issues|pull)/[0-9]+$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")
CAPS = {"title": 100, "source": 150, "done-when": 160, "link": 150}
INDEX_TITLE = redis_gov.WORK_LIST_TITLE   # a list line must equal what the store derives; the store refuses any other
EVENT_MAX = redis_gov.PREVIEW_CHARS      # a log entry a governed read shows whole
REC_MAX = 900                            # a description one board row carries whole
BOARD_BODY = 1000                        # what one board row carries of a day's list
INDEX_MAX = redis_gov.MAX_HASH_FIELDS    # what one read shows of a hash, by any route
LEASE_MINUTES = 240
PAGE = 4                                 # log entries one board row carries at the preview bound
PAGES_MAX = 2500                         # 9999 revisions at four a page
REFUSED_EXIT, DENIED_EXIT, UNKNOWN_EXIT, CONFLICT_EXIT, DISPUTED_EXIT = 5, 6, 7, 9, 10
ENV_AGENT, ENV_SWITCH = "FLEET_AGENT", "REDIS_WORK"


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


def lease_over(lease: str, moment: datetime.datetime) -> bool:
    until = redis_gov.work_lease(lease)
    return until is not None and moment > until


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
    """A log entry as a read showed it, as redis_gov reads it - or None when it is not one."""
    shown = shown or {}
    return redis_gov.work_event(shown.get("text", ""), shown.get("by", ""), shown.get("at", ""), shown.get("id", ""))


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
NO_BOARD_WRITE = ("NOT WRITTEN. A work record is written only through the bridge, by a caller it has "
                  "verified. The bridge was not used for this agent (the line above says why), and nothing "
                  "is sent by the board route: there a sender is a claim, and the store refuses it.")


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
           since="", unverified_write=False) -> dict:
    """One governed operation as `who`.

    A WRITE goes by the bridge or not at all: the board route carries a claimed identity, which the
    store refuses for this namespace, so no row is spent on it. (`unverified_write` is for `preflight`
    alone, whose whole purpose is to see that refusal.) A READ goes by the bridge when it is on for
    this agent, else by the board.

    {"value": what a read showed, "truncated": a hash had more fields than a read shows,
     "route": "bridge" | "board"}; a paged read (after=) returns {"page": redis_inbox's judgement}.
    Raises Stop for everything that is not a success. A WRITE that Stops with UNKNOWN_EXIT MAY HAVE
    HAPPENED and is not sent again by this tool."""
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
    if write and not unverified_write:
        raise Stop(2, NO_BOARD_WRITE, "board")
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
        # Only `preflight` sends a write here, and it expects the refusal above. An OK is the finding.
        raise Stop(UNKNOWN_EXIT, "%s  <- an unverified write was NOT refused" % text[:200], "board")
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
                      "that admits a move itself; set %s=on only after `preflight` has passed on every "
                      "route this agent uses. Reading needs no switch." % ENV_SWITCH)


# --------------------------------------------------------------------------------------------- records

def index_key(rid: str) -> str:
    m = ID.match(rid or "")
    if not m:
        raise Stop(2, "%r is not a record id (W-<YYYYMMDD>-<6 hex>)" % (rid,))
    return INDEX % m[1]


def chain(who: str, rid: str, args) -> tuple:
    """THE WHOLE LOG, read from its first entry and replayed with the store's own rules.
    (every entry as shown, the last entry parsed, [(entry id, why)] for every entry that is not a legal
    move). A record with anything in that list is DISPUTED, however legal its last entries look."""
    index_key(rid)
    key, cursor, count, shown = LOG % rid, "0-0", PAGE, []
    for _page in range(PAGES_MAX):
        page = govern(who, "xrange", key, count=count, after=cursor, wait=args.wait, since=args.since)["page"]
        if page["status"] == "cut" and count > 1:
            count = max(1, count // 2)
            continue
        if page["status"] != "ok":
            raise Stop(3, "%s NOT READ after %s: %s. Nothing is built on a record that was not read whole."
                          % (rid, cursor, page["why"]))
        shown += page["entries"]
        cursor = page["last"]
        if not page["more"]:
            break
    else:
        raise Stop(3, "%s has more entries than this reads; nothing is built on it" % rid)
    if not shown:
        raise Stop(2, "no record %s: its log is empty" % rid)
    prev, bad = None, []
    for entry in shown:
        ev = parse_event(entry)
        why = "it is not a work-record entry" if ev is None else allowed(prev, ev)
        if why:
            bad.append((str(entry.get("id", "?")), why))
        prev = ev if ev is not None else prev
    return shown, prev, bad


def disputed_line(rid: str, bad: list) -> str:
    return ("%s is DISPUTED: %d of its entries %s not a legal move (first: %s, %s). Nothing is built on it. "
            "Its history is kept for a person to reconcile: history %s"
            % (rid, len(bad), "is" if len(bad) == 1 else "are", bad[0][0], bad[0][1], rid))


def sound(who: str, rid: str, args) -> dict:
    """The record's state, from a WHOLE chain with nothing wrong in it - or Stop."""
    _shown, last, bad = chain(who, rid, args)
    if bad or last is None:
        raise Stop(DISPUTED_EXIT, disputed_line(rid, bad or [("?", "no readable entry")]))
    return last


def append(who: str, rid: str, ev: dict, kind: str, title: str, args) -> int:
    """THE MOVE: one log entry, admitted by the store or not. Then the list, which can only lag.
    Returns 0, or 3 when the move landed and the list was not updated."""
    line = event_text(ev)
    try:
        govern(who, "xadd", LOG % rid, value=line, wait=args.wait, since=args.since)
    except Stop as why:
        if why.code == REFUSED_EXIT and "revision conflict" in str(why):
            raise Stop(CONFLICT_EXIT, "CONFLICT: someone moved %s first (%s). NOTHING of this move was "
                                      "written. Read it again: show %s" % (rid, str(why)[9:], rid)) from None
        if why.code == UNKNOWN_EXIT:
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
        raise Stop(2, "not created: today's list no longer fits one board row. Nothing was written.") from None
    lines = listed["value"] if isinstance(listed["value"], dict) else {}
    after = json.dumps(dict(lines, **{rid: index_line(ev, kind, title)}), sort_keys=True, ensure_ascii=False)
    if listed["truncated"] or len(lines) >= INDEX_MAX:
        raise Stop(2, "not created: today's list is full (%d records). Create it tomorrow. Nothing was "
                      "written." % len(lines))
    if len(after) > BOARD_BODY and not args.past_board_list:
        raise Stop(2, "not created: with one more record today's list (%d) would no longer fit one board "
                      "row, and an agent that reads by the board could not list the day. Create it tomorrow, "
                      "or say --past-board-list to accept that. Nothing was written." % len(lines))
    govern(who, "hput", REC % rid, value=described, wait=args.wait, since=args.since)
    code = append(who, rid, ev, kind, title, args)
    print("%s created (req=%s): %s for %s, state requested. %s must accept it." % (rid, ev["req"], kind, owner, owner))
    return code


def cmd_resume(who: str, args) -> int:
    """Finish a create whose description landed and whose first entry did not. NOTHING IS REWRITTEN: the
    description stays exactly as it was written, and only the agent it names may begin the record. The
    tool checks that to say so early; the store is what refuses anyone else."""
    switched_on()
    rid = args.id
    index_key(rid)
    record = describe_of(who, rid, args)
    why = redis_gov.work_described(record, redis_gov.load_acl())
    if why:
        raise Stop(2, "nothing to resume: %s has no description this tool wrote (%s)" % (rid, why))
    if record["requested_by"] != who:
        raise Stop(2, "only %s, who described %s, may begin it; this is %s" % (record["requested_by"], rid, who))
    page = govern(who, "xrange", LOG % rid, count=1, after="0-0", wait=args.wait, since=args.since)["page"]
    if page["status"] != "ok":
        raise Stop(3, "%s NOT READ: %s. Nothing is resumed on a log that was not read." % (rid, page["why"]))
    if page["entries"]:
        raise Stop(2, "%s is already begun: its first entry is there. Nothing to resume. If the list lacks it: "
                      "reindex %s" % (rid, rid))
    ev = {"rev": 1, "action": "create", "state": "requested", "owner": agent(args.owner),
          "reviewer": agent(args.reviewer) if args.reviewer else "-", "requester": who, "lease": "-",
          "req": secrets.token_hex(4), "words": "", "by": who}
    why_not = allowed(None, ev)
    if why_not:
        raise Stop(2, "not resumed: %s" % why_not)
    code = append(who, rid, ev, record["kind"], record["title"], args)
    print("%s resumed (req=%s): its description is as it was written; %s for %s, state requested."
          % (rid, ev["req"], record["kind"], ev["owner"]))
    return code


def cmd_move(who: str, args) -> int:
    switched_on()
    action, rid = args.what, args.id
    needs = MOVES[action][3] if action in MOVES else "note"
    text = text_ok(needs, getattr(args, needs, "")) if needs else ""
    if needs and not text:
        raise Stop(2, "%s needs --%s" % (action, needs))
    note = text_ok("note", getattr(args, "note", "")) if needs != "note" else ""
    last = sound(who, rid, args)                     # the WHOLE chain, or nothing is built on it
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
    why_not = allowed(last, ev)                      # the store's rule, asked early to save the call
    if why_not:
        raise Stop(2, "not moved: %s (%s is %s, held by %s)" % (why_not, rid, last["state"], last["owner"]))
    event_text(ev)                                   # too long is refused before anything is written
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


def print_entry(shown: dict, bad: dict) -> None:
    ev = parse_event(shown)
    mark = ("  <- NOT ALLOWED: " + bad[str(shown.get("id"))]) if str(shown.get("id")) in bad else ""
    if ev is None:
        print("  %s  %s  by %s: %s%s" % (shown.get("id", "?"), shown.get("at", "?"), shown.get("by", "?"),
                                         shown.get("text", ""), mark))
        return
    print("  %s  %s  by %s  r%d>r%d %s -> %s owner=%s req=%s result=applied%s%s"
          % (ev["id"], ev["at"], ev["by"], ev["rev"] - 1, ev["rev"], ev["action"], ev["state"], ev["owner"],
             ev["req"], ("  " + ev["words"]) if ev["words"] else "", mark))


def cmd_show(who: str, args) -> int:
    """The record: its state from the WHOLE chain, what was asked, and its newest entries."""
    rid = args.id
    shown, last, bad = chain(who, rid, args)
    record = describe_of(who, rid, args)
    if last is not None:
        print(state_line(rid, last, record.get("kind", "?"), record.get("title", "(no description was written)"), now()))
    print("project %s; link %s; source: %s" % (record.get("project", "?"), record.get("link", "-"),
                                               record.get("source", "?")))
    print("done when: %s" % record.get("done_when", "?"))
    print("requested by %s at %s" % (last["requester"] if last else "?", record.get("created", "?")))
    for entry in shown[-PAGE:]:
        print_entry(entry, dict(bad))
    if bad:
        print(disputed_line(rid, bad))
        return DISPUTED_EXIT
    print("%d entries, each a move its actor was allowed to make" % len(shown))
    return 0


def cmd_history(who: str, args) -> int:
    """Every entry, oldest first, each marked when it is not a legal move. Kept whole for reconciliation."""
    shown, _last, bad = chain(who, args.id, args)
    for entry in shown:
        print_entry(entry, dict(bad))
    if bad:
        print(disputed_line(args.id, bad))
        return DISPUTED_EXIT
    print("%d entries of %s. VERIFIED: every one is a move its actor was allowed to make." % (len(shown), args.id))
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


def cmd_reindex(who: str, args) -> int:
    """Write the list's line for one record again, from its log. The store takes it only if it is true."""
    switched_on()
    last = sound(who, args.id, args)
    described = describe_of(who, args.id, args)
    kind = described.get("kind") if described.get("kind") in KINDS else "task"
    govern(who, "hset", index_key(args.id), field=args.id, value=index_line(last, kind, str(described.get("title", ""))),
           wait=args.wait, since=args.since)
    print("%s: the list now shows r%d %s" % (args.id, last["rev"], last["state"]))
    return 0


def cmd_preflight(who: str, args) -> int:
    """Prove the governor on this route admits a work-record move itself, by sending one it must
    refuse: r2 to a log that has no r1. Through the bridge a correct governor answers "revision
    conflict". By the board a correct governor refuses the claimed identity before it looks at anything.
    A governor without the rules WRITES the entry, and this says so."""
    if not args.allow_stray_entry:
        raise Stop(2, "preflight sends one entry that a correct governor REFUSES and a governor without the "
                      "rules WRITES. Run it only when that one stray entry has been agreed, and say so: "
                      "--allow-stray-entry")
    key = LOG % ("preflight-" + who)
    try:
        got = govern(who, "xadd", key, value="r2 note requested %s - %s - %s preflight" % (who, who, secrets.token_hex(4)),
                     wait=args.wait, since=args.since, unverified_write=True)
    except Stop as why:
        reason = str(why)
        if why.code == REFUSED_EXIT and why.route == "bridge" and "revision conflict" in reason:
            print("PASS by the bridge route: the governor admitted this caller as verified and refused an "
                  "entry out of sequence (%s). Nothing was written." % reason[:120])
            return 0
        if why.code == REFUSED_EXIT and why.route == "board" and "authenticated caller" in reason:
            print("PASS by the board route: the governor refused a work-record write from a claimed "
                  "identity (%s). Nothing was written. This route can read records; it cannot move them."
                  % reason[:120])
            return 0
        if why.code == UNKNOWN_EXIT and "NOT refused" in reason:
            print("FAIL by the board route: THIS GOVERNOR TOOK A WORK-RECORD WRITE IT MUST REFUSE. It does "
                  "not have the rules. Do not set %s=on. One stray entry is now in %s." % (ENV_SWITCH, key))
            return 1
        raise
    print("FAIL by the %s route: THIS GOVERNOR TOOK AN ENTRY OUT OF SEQUENCE. It does not have the rules, so "
          "a move here would not be admitted by the store. Do not set %s=on. One stray entry is now in %s."
          % (got["route"], ENV_SWITCH, key))
    return 1


def cmd_selftest(_who: str, _args) -> int:
    """The grammar and the rules, with no network and no row."""
    ev = {"rev": 3, "action": "start", "state": "working", "owner": "grok", "reviewer": "-",
          "requester": "claude-code-cli", "lease": "20261013T0830Z", "req": "0a1b2c3d", "words": ""}
    line = event_text(ev)
    back = parse_event({"text": line, "by": "grok", "at": "t", "id": "3-0"})
    assert {k: back[k] for k in ev} == ev and back["by"] == "grok", back
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
    assert allowed is redis_gov.work_allowed and MOVES is redis_gov.WORK_MOVES, "one set of rules, the store's"
    row = request_row("grok", "xrange", LOG % "W-20261013-a1b2c3", count=4, after="0-0")
    assert "|op=xrange|key=proj:work:v1:log:W-20261013-a1b2c3|count=4|after=0-0" in row["payload"], row
    assert row["source_tag"] == "grok" and row["action_type"] == inbox.REQUEST_ACTION, row
    assert set(MOVES) | {"resume"} <= set(__doc__.split()), "every move is in the docstring"
    assert (REC % "", LOG % "", INDEX % "") == (redis_gov.WRITE_ONCE[0], redis_gov.SEQUENCED[0], redis_gov.REV_FORWARD[0])
    assert ID.match("W-20261013-a1b2c3") and not ID.match("W-20261013-A1B2C3") and not ID.match("W-2026-a1b2c3")
    assert redis_gov._KEY.match(REC % "W-20261013-a1b2c3") and redis_gov._FIELD.match("W-20261013-a1b2c3")
    print("selftest OK: an entry reads back whole in the grammar the governor admits; the keys are "
          "proj:work:v1:rec:<id>, log:<id> and index:<day>")
    return 0


# ------------------------------------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as", dest="who", default=os.environ.get(ENV_AGENT, "claude-code-cli"),
                        help="the agent acting (default: $%s, else claude-code-cli); a write still needs the "
                             "bridge to have verified it" % ENV_AGENT)
    sub = parser.add_subparsers(dest="what", required=True)
    for name in ["create", "resume", "note", "show", "list", "history", "reindex", "preflight", "selftest"] + sorted(MOVES):
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
            p.add_argument("--past-board-list", action="store_true",
                           help="create it although the day's list will no longer fit one board row")
        elif name == "list":
            p.add_argument("--day", action="append", help="YYYYMMDD; may repeat (default: the last --days)")
            p.add_argument("--days", type=int, default=2)
            p.add_argument("--all", action="store_true", help="also the done and the cancelled")
        elif name not in ("selftest", "preflight"):
            p.add_argument("id")
        if name == "resume":
            p.add_argument("--owner", required=True)
            p.add_argument("--reviewer", default="")
        if name == "preflight":
            p.add_argument("--allow-stray-entry", action="store_true",
                           help="agreed: a governor without the rules will write one stray entry")
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
        return {"create": cmd_create, "resume": cmd_resume, "show": cmd_show, "list": cmd_list,
                "history": cmd_history, "reindex": cmd_reindex}[args.what](who, args)
    except Stop as why:
        print(str(why))
        return why.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
