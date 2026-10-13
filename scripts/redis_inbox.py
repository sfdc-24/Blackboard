#!/usr/bin/env python3
"""A Redis inbox every agent can reach, and a doorbell for mine. Stdlib only.

WHY THIS EXISTS
    Mr. Salam, 2026-10-09 ~21:00Z, to Grok: "communication is still not working ... get the Redis
    communication board tested asap; everyone posts to another agent, then that agent has to see if
    they can respond", with a complete reply inside a few minutes and the Sheet retired once Redis
    proves itself.

    A Redis-native test would have recorded this instance as a silent failure for a reason that has
    nothing to do with whether it was awake: THERE IS NO PATH FROM THIS LAPTOP TO REDIS. The
    instance is private inside the VPC; the one HTTPS surface over it (cloud/redis-tool-bridge)
    serves `/healthz` and a synthetic `/probe` only, and its invoker is a single service account.
    The only route an off-VPC agent has is the governed one: post a board row, which the
    bus-requests worker executes inside the VPC and answers with a RESULT row
    (scripts/bus_request.py, scripts/redis_gov.py). So this module is that route, wrapped.

THE KEY, AND WHY THIS ONE
    fleet:inbox:<agent>     a STREAM, one per instance: {by, text, at}
    Every fleet principal already has write on `fleet:` in scripts/redis_acl.json and read on
    everything, so NO NEW GRANT is needed and no edit to the access list. `v1:agent:` would have
    been the obvious home and is wrong: it is the roster, and it is PROTECTED from agent writes in
    redis_gov code, before the access list is consulted. `conf:side:<date>` is the room's shared
    channel, not an inbox - a message to one agent does not belong in everyone's live state.

WHAT IT DOES NOT DO
    It is not always-on. Each call is one row out and one row back, so a poll costs a row, and a row
    costs money (the owner, 2026-10-04). `--wait` holds for an answer rather than spinning, and the
    caller decides when to look. A wait that ends is evidence about the wait, not about the worker
    and not about the peer: the request row is on the board either way, and the RESULT row that
    answers its id is where the truth is.

    python scripts/redis_inbox.py send --to grok --text "..."    one message
    python scripts/redis_inbox.py read [--wait 180] [--count 4]  new messages to me, after my cursor
    python scripts/redis_inbox.py selftest                       the grammar, no network
    python scripts/redis_inbox.py reconcile                      settle writes whose outcome is unknown

THE DIRECT ROUTE, WHEN THIS AGENT HAS ONE (2026-10-13, docs/REDIS-PRIMARY.md Phase 3b)
    `send` and `read` first try scripts/bridge_client.py: one HTTPS call to the redis-tool-bridge,
    no board row, the caller verified by an impersonated identity token and bound to THIS agent's
    principal. It is OFF unless REDIS_BRIDGE=on is set for this agent, and until the bridge's op
    endpoint is deployed the switch has nothing to reach.

    It is a TRANSPORT, not a second reader (Aya on a125337). A page that came by the bridge is
    judged by the same function as a page that came by the board (whole_page): numeric stream ids,
    in order after the cursor, `last` the last id shown, a text cut at the preview bound stops the
    cursor before it. One cursor, one bookmark, whichever route carried the page; a read may change
    route between two pages and loses and repeats nothing.

    WHAT NEVER FALLS BACK TO THE BOARD ROUTE (the same review, blockers 1 and 2):
    - a WRITE whose outcome is UNKNOWN: it may have committed, so it is journalled and not sent again;
    - a caller the bridge DENIED (401 or 403): the board route identifies by claim, and moving a
      denied caller there would be a downgrade around the refusal;
    - an operation the access rules REFUSED: the board route runs the same rules.

    SETTLING AN UNKNOWN WRITE (`reconcile`; Aya's Stage 1 exit check 5)
    A write the bridge may or may not have committed is journalled and announced in one row (a status
    and a place, never the message). `reconcile` then READS the stream it was aimed at, from five
    minutes before the write to fifteen after, and looks for an entry by this agent whose shown text
    has the journalled hash. Found: COMMITTED, with the entry id. Read to the end of the window and
    not found, ten minutes or more after the write: ABSENT - it did not commit, and whether to send
    it again is a person's decision, not this command's. Anything else stays UNKNOWN. It only ever
    reads, so it may be run as often as wanted.

THE LAPTOP LANE (step 3 of the broken-telephone fix, Converspan Channel Architecture v0.1)
    The cloud routes a Drive request to this laptop only while fleet:alive:claude-code-cli is
    fresh (cloud/drive-intake reads it; 300 s is the owner's window). This laptop cannot reach
    Redis or write a Google Doc, so each of its three words is ONE governed row, executed inside
    the VPC under this principal and audited in gov:audit; cloud/claude-api-worker/lane.py turns
    the acceptance and the terminal word into lines in the originating doc, and says it did.

    python scripts/redis_inbox.py beat [--every 240 --hours 8]  I am here (one row per beat)
    python scripts/redis_inbox.py accept --task task-<id> [--lease 900]
    python scripts/redis_inbox.py done --task task-<id> --state SUCCEEDED --note "..." --evidence https://...

    A beat is a row, and a row is money (the owner, 2026-10-04): post one only while a session is
    at work, and a loop never outlives --hours. The done note travels on the PUBLIC board: a status
    and a pointer, never the work itself (Aya, 2026-10-10).
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = "claude-code-cli"
KEY = "fleet:inbox:%s"
# The roster this module will address, so a typo cannot create a key nobody reads. The canonical ids
# are scripts/redis_acl.json's principals; the aliases there are not repeated here on purpose.
AGENTS = ("claude-code-cli", "grok", "chatgpt-codex-desktop", "cursor", "copilot", "gemini", "owner")
BOOKMARK = os.path.join(REPO, ".redis_inbox_seen.json")
REQUEST_ACTION = "AYA_REQ"
RESULT_PHASE = "AYA_RESULT"
MAX_TEXT = 900                      # one message, not a document: the row carries it as base64
# What to wait for an answer, by default. MEASURED, not guessed: on 2026-10-09 a request posted at
# 21:30:22Z was answered at 21:31:14Z - 52 seconds - and a 40-second budget turned that into a
# "no answer". The owner's mark for this path is a complete reply within a few minutes, so the
# default sits inside that and above the measurement, and the caller can still shorten it.
WAIT_SECONDS = 180
HEARTBEAT_KEY = "fleet:alive:%s" % ME     # what cloud/drive-intake reads: epoch seconds, this clock
ACCEPT_KEY = "fleet:worker:accept:%s"     # cloud/claude-api-worker/lane.py reads these two
DONE_KEY = "fleet:worker:done:%s"
TERMINAL = ("SUCCEEDED", "FAILED")
NOTE_MAX = 300
BEAT_EVERY = 240                           # the most a loop may wait: the reader's window is 300 s and a
                                           # worker pass (10 to 60 s) has to land the next beat inside it
BEAT_HOURS = 8
# THE READ (Aya, 2026-10-12, row AYA-392-MERGED-CURSOR-INTEGRATION-20261012): read after a cursor with the
# worker's `after=` (PR 392), page by its `more`/`last`, persist the cursor, and NEVER move it over data
# that was cut or not parsed. The bounds stay as they are: redis_gov quotes a value at PREVIEW_CHARS and
# a RESULT body at 1500, and a board row is at most 1500 characters in all (scripts/board_row.py).
WORKER = "bus-reconciler"                 # the only author of an answer this reads
CURSOR_KEY = "fleet:cursor:%s"            # the recipient's own read cursor; the hourly archive reads it
PREVIEW_CHARS = 200                       # redis_gov.PREVIEW_CHARS: a longer value arrives as 200 + "..."
READ_COUNT = 4                            # a page of 4 fits one row; a cut page is asked again smaller
READ_PAGES = 5                            # pages per call, so a cold start over a long inbox has a cost cap
U64_MAX = 2 ** 64 - 1


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(agent: str) -> str:
    agent = (agent or "").strip().lower()
    if agent not in AGENTS:
        raise SystemExit("not an agent this writes to: %s (%s)" % (agent, ", ".join(AGENTS)))
    return agent


def message(text: str) -> str:
    """The message, as ONE LINE of text.

    NOT an envelope, and this is measured rather than assumed (scripts/redis_gov.py, the xadd
    branch): the worker writes {"text": <this>, "by": <the principal it decided>, "at": <its own
    clock>}. So `by` is the server's word and not the sender's claim - the right way round - and
    `to` has nowhere to go, because the KEY already names the recipient. A JSON envelope here would
    simply be stored as the text of the message, which is how my first draft was wrong."""
    text = " ".join((text or "").split())
    if not text:
        raise SystemExit("a message needs text")
    if len(text) <= MAX_TEXT:
        return text
    # NEVER SILENTLY. The first real message through this inbox - Grok to me, 21:33:36Z - arrived
    # ending in "..." because its sender trimmed it, which is the same defect Blackboard PR 354
    # closes for board rows and had not been carried to a Redis value. My own code did it too,
    # one line above this one. A cut now says it was cut and how much there was.
    note = " [cut here: %d characters in all]" % len(text)
    return text[:MAX_TEXT - len(note)].rstrip() + note


def request_row(op: str, key: str, *, value: str | None = None, count: int | None = None,
                row_id: str | None = None, after: str | None = None) -> dict:
    """One governed request, in the grammar scripts/bus_request.py parses.

    The value travels as base64 (enc=b64) because the board's payload is pipe-separated and a
    message may hold anything; redis_gov decodes it and checks the field names itself."""
    rid = row_id or ("CCC-INBOX-%s-%s" % (op.upper(), secrets.token_hex(4)))
    fields = ["BCB|v=1", "id=" + rid, "phase=REQUEST", "from=" + ME, "to=bus-reconciler",
              "do=redis-op", "op=" + op, "key=" + key]
    if value is not None:
        # b64 because the payload is pipe-separated and a message may hold a pipe or a newline;
        # decode_value hands the worker back exactly this string, and the stream stores it as `text`.
        fields.append("val=" + base64.b64encode(value.encode("utf-8")).decode("ascii"))
        fields.append("enc=b64")
    if count is not None:
        fields.append("count=%d" % int(count))
    if after is not None:
        if op != "xrange" or stream_id(after) is None:
            raise SystemExit("after= is an xrange stream id, <ms>-<seq>: %r" % after)
        fields.append("after=" + after)
    return {
        "row_id": rid,
        "source_tag": ME,
        "target_surface": "bus-reconciler",
        "action_type": REQUEST_ACTION,
        "category": "OPEN",
        "project_tag": "Blackboard",
        "gist": "redis inbox %s" % op,
        "subgist": key,
        "payload": "|".join(fields),
    }


def post(row: dict) -> str:
    """Append the row through scripts/append.py, which reads it back by Row_ID. Never a hand-built
    cell array, and never an escape through a shell: the row goes to a file and the file is the
    argument."""
    handle, path = tempfile.mkstemp(prefix="redis-inbox-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8", newline="") as fh:
        json.dump(row, fh)
    try:
        run = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "append.py"), path],
                             cwd=REPO, capture_output=True, text=True, timeout=180)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (run.stdout or "") + (run.stderr or "")
    if "READ-BACK OK" not in out:
        raise SystemExit("the request row did not read back; nothing was asked:\n" + out[-600:])
    return row["row_id"]


def answer(rid: str, wait: float, since: str) -> dict | None:
    """The worker's RESULT row for `rid`, or None when this wait ends before one arrives.

    None is not a report that the worker is stopped. The request row may be answered later.
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import bus                                                   # noqa: E402

    env = bus.load_env()
    deadline = time.time() + max(0.0, wait)
    while True:
        res = bus.read_rows(env, since=since)
        rows = res["rows"] if isinstance(res, dict) else res
        for row in rows or ():
            if not isinstance(row, (list, tuple)) or len(row) <= 5:
                continue
            payload = str(row[5] or "")
            if "answers=" + rid in payload:
                return {"from": str(row[2] or ""), "payload": payload}
        if time.time() >= deadline:
            return None
        time.sleep(10)


def not_yet(waited: float, request_id: str) -> str:
    """What a timeout actually knows, and nothing more.

    The first version of this line said "the governed worker is not running". It said that on a
    40-second budget about a request that was answered at 52 seconds, by a worker that had been
    running for five minutes. A timeout is evidence about the wait, never about the peer: the
    row is on the board either way. The line names that row. The gcloud command is on its own
    line, with the project and region this repo puts on the same invocation, so it can be pasted.
    """
    return ("NO ANSWER YET after %gs. The request row is on the board and is not lost; it may be "
            "answered after this wait. Look for the AYA_RESULT row answering %s. Check the worker "
            "with:\n"
            "gcloud run jobs executions list --job=bus-requests --project=sfdc24 --region=us-central1"
            % (waited, request_id))


def read_timeout(waited: float, request_id: str) -> str:
    """The read path's timeout. The 'nothing was read' sentence is its own line, after the command,
    so it is not glued onto `gcloud`."""
    return (not_yet(waited, request_id)
            + "\nNothing was read, which is not the same as an empty inbox.")


def stream_id(value):
    """`<ms>-<seq>` as a tuple of two unsigned 64-bit integers, or None. Ids are compared as numbers:
    as strings "9-0" sorts after "10-0", which is how the old reader could skip or repeat a message."""
    # ASCII digits only: str.isdigit() also passes Arabic-Indic, fullwidth and superscript digits, which
    # redis_gov refuses (and int() raises on "¹") - Cursor on PR 397.
    if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", str(value or "")):
        return None
    pair = tuple(int(p) for p in str(value).split("-"))
    return pair if max(pair) <= U64_MAX else None


def seen() -> dict:
    try:
        with open(BOOKMARK, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def remember(last_id: str, synced: bool = False) -> None:
    """The local cursor, and whether fleet:cursor:<me> in Redis is CONFIRMED to hold it. A bookmark with
    synced=False is retried on the next read even when nothing new arrives (Copilot on PR 397)."""
    with open(BOOKMARK, "w", encoding="utf-8", newline="") as fh:
        json.dump({"last_id": last_id, "at": stamp(), "synced": bool(synced)}, fh)


def entries_of(payload: str) -> list:
    """The entries out of a RESULT payload: `text=OK xrange <key>: [ ... ]`."""
    start = payload.find("[")
    if start < 0:
        return []
    try:
        return json.loads(payload[start:payload.rindex("]") + 1])
    except (ValueError, IndexError):
        return []


_PAGE = re.compile(r"OK xrange (?P<key>\S+) \[after=(?P<after>\S+) more=(?P<more>true|false) "
                   r"last=(?P<last>\S+)\]: (?P<body>.*)", re.S)


def page_of(payload: str, key: str, after: str) -> dict:
    """One RESULT row of an `xrange after=` read, judged. status is "ok" only when the WHOLE page is here:
    the page line is the one asked for, and the body is complete JSON. "cut": the row or the body was cut
    at its bound (asked again smaller). "unparsed": anything else, including a worker that predates
    `after=` and answered the newest-N read instead. Only an "ok" page may move the cursor."""
    head, _, text = payload.partition("|text=")
    if not _:
        return {"status": "unparsed", "why": "no text= in the answer"}
    if re.search(r"(^|\|)(truncated=1|full=)", head + "|" + text):
        return {"status": "cut", "why": "the answer was longer than one row (truncated=1 or full=)"}
    if text.startswith("REFUSED") or text.startswith("FAILED"):
        return {"status": "unparsed", "why": text[:200]}
    m = _PAGE.fullmatch(text.strip())
    if not m:
        return {"status": "unparsed",
                "why": "no [after= more= last=] page line: the worker may predate xrange after= (PR 392)"}
    if m["key"] != key or m["after"] != after or stream_id(m["last"]) is None:
        return {"status": "unparsed", "why": "the page is not the one asked for (%s after %s)" % (m["key"], m["after"])}
    body = m["body"]
    try:
        entries = json.loads(body)
    except ValueError:
        if body.endswith("..."):
            return {"status": "cut", "why": "the body was cut at its 1500-character bound"}
        return {"status": "unparsed", "why": "the body is not JSON"}
    return whole_page(entries, m["more"] == "true", m["last"], after)


def whole_page(entries, more, last, after: str) -> dict:
    """The ONE judgement of a page, whichever route carried it (the board's RESULT row or the bridge's
    answer). "ok" only when the page can move the cursor: a list of entries, every shown id a stream id,
    in numeric order after the cursor, and `last` the last of them."""
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return {"status": "unparsed", "why": "the body is not a list of entries"}
    if not isinstance(more, bool) or stream_id(last) is None:
        return {"status": "unparsed", "why": "the page has no usable more/last"}
    last = str(last)
    # Every shown id is a stream id, in order, after the cursor, and `last` IS the last of them. A worker
    # from before PR 397 showed a field named `id` in place of the stream id, so such a page is not read
    # rather than trusted; and a `last` beyond the entries shown would skip the ones between (Cursor).
    ids = [stream_id(e.get("id")) for e in entries]
    if None in ids or any(b <= a for a, b in zip([stream_id(after)] + ids, ids)):
        return {"status": "unparsed", "why": "the shown ids are not stream ids in order after %s (a worker "
                                             "from before PR 397 shows a field named id over them)" % after}
    if (str(entries[-1]["id"]) if entries else after) != last:
        return {"status": "unparsed", "why": "last=%s is not the last id shown after %s" % (last, after)}
    return {"status": "ok", "entries": entries, "more": more, "last": last}


def page_of_result(result, key: str, after: str) -> dict:
    """One answer from the bridge to an `xrange after=`, judged exactly as a board page is. The answer is
    redis_gov's own result dict. It is the page asked for only when it names this key and this cursor:
    an endpoint that ignored after= answers the newest-N read, which carries no `after`, and is not read."""
    if not isinstance(result, dict) or result.get("op") != "xrange":
        return {"status": "unparsed", "why": "the bridge's answer is not an xrange result"}
    if result.get("key") != key or result.get("after") != after:
        return {"status": "unparsed", "why": "the page is not the one asked for (%s after %s)" % (key, after)}
    return whole_page(result.get("value"), result.get("more"), result.get("last"), after)


def text_cut(entry: dict) -> bool:
    """redis_gov quoted this entry's text at PREVIEW_CHARS: what arrived is not the whole message."""
    text = str(entry.get("text", ""))
    return len(text) == PREVIEW_CHARS + 3 and text.endswith("...")


def advance(page: dict, cursor: str, past_cut: bool):
    """(the entries to show, the new cursor, why it stopped early or ""). The cursor moves to the page's
    `last` - the raw stream id the worker names, never a rendered entry's "id" - unless an entry's text
    was cut at the preview bound. Then it stops just BEFORE that entry, which stays unread, unless the
    reader says --past-cut. page_of has already proved every shown id is the stream id, in order."""
    entries = page["entries"]
    if past_cut:
        return entries, page["last"], ""
    for n, entry in enumerate(entries):
        if text_cut(entry):
            stop = str(entries[n - 1]["id"]) if n else cursor        # page_of proved these are stream ids
            return entries[:n + 1], stop, ("entry %s arrived cut at %d characters; the cursor stops before it "
                                           "(read --past-cut to move on)" % (entry.get("id"), PREVIEW_CHARS))
    return entries, page["last"], ""


# Exit codes of the three outcomes that end a command without the board route being used.
REFUSED_EXIT, DENIED_EXIT, UNKNOWN_EXIT = 5, 6, 7


def breaker_row(text: str) -> None:
    """The ONE row a tripped bridge breaker posts (bridge_client.attempt's `announce`). bridge_client
    hands it closed words only - a count, a status label, a time - and never a message or an error body."""
    rid = "CCC-BRIDGE-BREAKER-%s" % secrets.token_hex(4)
    line = " ".join(str(text).replace("|", "/").split())[:400]
    post({
        "row_id": rid, "source_tag": ME, "target_surface": "claude-code-cli;aya",
        "action_type": "NOTE", "category": "OPEN", "project_tag": "Blackboard",
        "gist": "redis bridge breaker open", "subgist": "",
        "payload": "|".join(["BCB|v=1", "id=" + rid, "phase=NOTE", "from=" + ME,
                             "to=claude-code-cli,aya", "via=direct", "text=" + line]),
    })


def unknown_row(op: str, key: str) -> None:
    """The alert for ONE write whose outcome is unknown: closed words, the op and the key, never a value.
    Best effort: an alert that cannot be posted must not turn an UNKNOWN into a crash."""
    rid = "CCC-BRIDGE-UNKNOWN-%s" % secrets.token_hex(4)
    line = ("UNKNOWN outcome for one %s on %s through the Redis bridge; it was NOT sent again; reconcile "
            "is pending on the sender's machine" % (re.sub(r"[^a-z]", "", op)[:12],
                                                    re.sub(r"[^A-Za-z0-9:._-]", "?", key)[:120]))
    try:
        post({
            "row_id": rid, "source_tag": ME, "target_surface": "claude-code-cli;aya",
            "action_type": "NOTE", "category": "OPEN", "project_tag": "Blackboard",
            "gist": "redis bridge unknown outcome", "subgist": "",
            "payload": "|".join(["BCB|v=1", "id=" + rid, "phase=NOTE", "from=" + ME,
                                 "to=claude-code-cli,aya", "via=direct", "text=" + line]),
        })
    except (Exception, SystemExit) as error:  # noqa: BLE001
        print("the UNKNOWN alert row could not be posted (%s)" % type(error).__name__)


def direct(op: str, key: str, *, value: str | None = None, count: int | None = None,
           after: str | None = None) -> tuple:
    """One operation by the direct route. (kind, what):
        ("ok", result)        done through the bridge; no board row was spent
        ("board", None)       use the governed board route (the switch is off, or bridge_client has
                              already said why the bridge was not used for this one)
        ("refused", line)     the access rules refused it            - TERMINAL, never the board route
        ("denied", line)      the bridge refused this caller         - TERMINAL, never the board route
        ("unknown", line)     a WRITE was sent and may have committed - TERMINAL, never sent again
    """
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import bridge_client                                          # noqa: E402

    try:
        result = bridge_client.attempt(op, key, me=ME, value=value, count=count, after=after,
                                       announce=breaker_row)
    except bridge_client.Refused as why:
        return "refused", ("REFUSED by the access rules: %s (the refusal's audit entry: %s)\n"
                           "Not sent on the board route: it runs the same rules and would refuse it again."
                           % (why.reason, why.audit or "not confirmed"))
    except bridge_client.Denied as why:
        return "denied", ("DENIED by the bridge: %s.\nNot sent on the board route: that route identifies a "
                          "sender by claim, and a denied caller is not moved to a weaker check." % why)
    except bridge_client.Unknown as why:
        unknown_row(op, key)
        return "unknown", ("UNKNOWN OUTCOME: %s.\nThe write was sent and MAY HAVE COMMITTED. It is NOT sent "
                           "again by any route; it is recorded in %s. Settle it with: "
                           "python scripts/redis_inbox.py reconcile"
                           % (why, os.path.basename(bridge_client.JOURNAL)))
    return ("ok", result) if result is not None else ("board", None)


_EXITS = {"refused": REFUSED_EXIT, "denied": DENIED_EXIT, "unknown": UNKNOWN_EXIT}


def cmd_send(args) -> int:
    to = canonical(args.to)
    text = message(args.text)
    kind, what = direct("xadd", KEY % to, value=text)
    if kind in _EXITS:
        print(what)
        return _EXITS[kind]
    if kind == "ok":
        print("sent to %s through the bridge as %s: no board row" % (to, what.get("principal", "?")))
        return 0
    rid = post(request_row("xadd", KEY % to, value=text))
    print("sent to %s, request %s" % (to, rid))
    got = answer(rid, args.wait, since=args.since or stamp())
    print("worker: %s" % (got["payload"][:300] if got else not_yet(args.wait, rid)))
    return 0 if got else 2


def cmd_read(args) -> int:
    """Everything after my cursor, oldest first, page by page. The cursor is persisted after every whole
    page (the local bookmark) and once at the end in Redis (fleet:cursor:<me>, which the archive's
    unread count reads). A page that was cut or not understood moves nothing."""
    key = KEY % ME
    book = seen()
    start = (args.after or book.get("last_id") or "0-0").strip()
    # The Redis copy is owed when this read moves the cursor, or when an earlier read moved it and the
    # Redis write was never confirmed (no "synced": a bookmark from before this, or a failed set).
    owed = not args.after and bool(book.get("last_id")) and book.get("synced") is not True
    if stream_id(start) is None:
        raise SystemExit("the read cursor is not a stream id: %r (fix %s or pass --after)" % (start, BOOKMARK))
    cursor, count, shown, code = start, max(1, min(int(args.count), 20)), 0, 0
    for _page in range(max(1, int(args.pages))):
        kind, what = direct("xrange", key, count=count, after=cursor)
        if kind in _EXITS:                       # refused or denied: a read is never "unknown"
            print(what)
            code = _EXITS[kind]
            break
        if kind == "ok":
            page = page_of_result(what, key, cursor)
        else:
            rid = post(request_row("xrange", key, count=count, after=cursor))
            got = answer(rid, args.wait, since=args.since or stamp())
            if not got:
                print(read_timeout(args.wait, rid))
                code = 2
                break
            page = (page_of(got["payload"], key, cursor) if got.get("from") == WORKER else
                    {"status": "unparsed", "why": "the answer row is from %r, not %s" % (got.get("from"), WORKER)})
        if page["status"] == "cut" and count > 1:
            count = max(1, count // 2)
            print("page cut (%s); asking again for %d" % (page["why"], count))
            continue
        if page["status"] != "ok":
            print("NOT READ, cursor stays at %s: %s" % (cursor, page["why"]))
            code = 3
            break
        entries, moved, stopped = advance(page, cursor, args.past_cut)
        for entry in entries:
            print("%s | %s | from %s | %s%s" % (entry.get("id", "?"), entry.get("at", "?"), entry.get("by", "?"),
                                                str(entry.get("text", "")), "  [cut by the reader's bound]"
                                                if text_cut(entry) else ""))
        shown += len(entries)
        if moved != cursor:
            cursor = moved
            remember(cursor)
        if stopped:
            print(stopped)
            break
        if not page["more"]:
            break
    else:
        print("more is waiting after %s: read again (each page is one row)" % cursor)
    if cursor != start or owed:
        # The cursor's copy in Redis is a SET of one value: writing it twice is writing it once. So an
        # unconfirmed or UNKNOWN save is not sent again NOW, by either route; it is left "not synced" and
        # the NEXT read saves the cursor it has then, which is this one or a later one. Unlike an xadd, a
        # repeat here cannot add anything.
        kind, what = direct("set", CURSOR_KEY % ME, value=cursor)
        if kind == "ok":
            ok = True
        elif kind in _EXITS:
            print(what)
            ok = False
        else:
            rid = post(request_row("set", CURSOR_KEY % ME, value=cursor))
            saved = answer(rid, args.wait, since=args.since or stamp())
            ok = bool(saved) and saved.get("from") == WORKER and "|text=OK" in saved["payload"]
        if ok:
            remember(cursor, synced=True)
        else:
            code = code or 4
        print("cursor %s -> %s (local); in Redis: %s" % (start, cursor, "saved" if ok else
                                                          "NOT confirmed, the local one stands; retried next read"))
    print("%d shown from %s, cursor %s" % (shown, key, cursor))
    return code


# ---------------------------------------------------------------------------------------- reconcile
SETTLE_BEFORE = 300        # seconds before the journalled time the search starts: this clock is not Redis's
SETTLE_AFTER = 900         # and seconds after it the search ends: a slow commit, and the same skew
SETTLE_MIN_AGE = 600       # ABSENT is not said of a write younger than this: it could still land
SETTLE_PAGES = 3           # pages read per unknown write; on the board route each page is one row
UNSETTLED_EXIT = 8


def one_page(key: str, cursor: str, count: int, wait: float, since: str) -> tuple:
    """(page, "") for one judged page after `cursor`, by whichever route answers; (None, why) when no
    page could be had at all. A read, on either route: it changes nothing."""
    kind, what = direct("xrange", key, count=count, after=cursor)
    if kind in _EXITS:
        return None, what.splitlines()[0]
    if kind == "ok":
        return page_of_result(what, key, cursor), ""
    rid = post(request_row("xrange", key, count=count, after=cursor))
    got = answer(rid, wait, since=since or stamp())
    if not got:
        return None, "no answer yet from the worker (request %s)" % rid
    if got.get("from") != WORKER:
        return None, "the answer row is from %r, not %s" % (got.get("from"), WORKER)
    return page_of(got["payload"], key, cursor), ""


def find_write(entry: dict, wait: float, since: str, pages: int) -> tuple:
    """Look for one journalled xadd in its stream. ("found", id) | ("absent", "") when the whole window
    was read and it is not there | ("unread", why) when the window could not be read to its end."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import bridge_client                                          # noqa: E402

    key, at = str(entry["key"]), int(entry["epoch"])
    cursor = "%d-0" % (max(0, at - SETTLE_BEFORE) * 1000)
    last_ms = (at + SETTLE_AFTER) * 1000
    count = READ_COUNT
    for _page in range(max(1, int(pages))):
        page, why = one_page(key, cursor, count, wait, since)
        if page is None:
            return "unread", why
        if page["status"] == "cut" and count > 1:
            count = max(1, count // 2)
            continue
        if page["status"] != "ok":
            return "unread", page["why"]
        for shown_entry in page["entries"]:
            if stream_id(shown_entry["id"])[0] > last_ms:
                return "absent", ""
            if (str(shown_entry.get("by", "")) == ME and
                    bridge_client._sha(str(shown_entry.get("text", ""))) == entry.get("shown_sha256")):
                return "found", str(shown_entry["id"])
        if not page["more"]:
            return "absent", ""
        cursor = page["last"]
    return "unread", "the window is longer than %d pages; run it again with --pages" % pages


def cmd_reconcile(args) -> int:
    """Settle every UNKNOWN write in the journal that can be settled by READING. It sends no write."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import bridge_client                                          # noqa: E402

    waiting = bridge_client.pending()
    if not waiting:
        print("nothing to reconcile: no write has an unknown outcome")
        return 0
    left = 0
    for entry in waiting:
        what = "%s %s on %s at %s" % (entry["id"], entry.get("op"), entry.get("key"), entry.get("at"))
        now = time.time()
        if entry.get("op") == "set" and entry.get("key") == CURSOR_KEY % ME:
            # A SET of the read cursor: the next read saves the cursor it has then, this one or a later
            # one, and writing one value twice is writing it once. Nothing to look for.
            bridge_client.settle(entry, "SUPERSEDED", "the next read saves the cursor", now)
            print("SUPERSEDED  %s: the next read saves the cursor again; nothing can be duplicated" % what)
            continue
        if entry.get("op") != "xadd" or not entry.get("shown_sha256") or "epoch" not in entry:
            left += 1
            print("UNKNOWN     %s: this command can only look for an xadd; settle it by hand" % what)
            continue
        state, detail = find_write(entry, args.wait, args.since, args.pages)
        if state == "found":
            bridge_client.settle(entry, "COMMITTED", "entry %s" % detail, now)
            print("COMMITTED   %s: it is entry %s. Do NOT send it again." % (what, detail))
        elif state == "absent" and now - int(entry["epoch"]) >= SETTLE_MIN_AGE:
            bridge_client.settle(entry, "ABSENT", "not in the stream from %d s before to %d s after"
                                 % (SETTLE_BEFORE, SETTLE_AFTER), now)
            print("ABSENT      %s: it did not commit. Sending it again is your decision; this command "
                  "sends nothing." % what)
        elif state == "absent":
            left += 1
            print("UNKNOWN     %s: not there yet, and it is under %d s old, so it could still land. "
                  "Run this again later." % (what, SETTLE_MIN_AGE))
        else:
            left += 1
            print("UNKNOWN     %s: the stream could not be read to the end of the window (%s)" % (what, detail))
    print("%d settled, %d still unknown" % (len(waiting) - left, left))
    return UNSETTLED_EXIT if left else 0


def task_id_ok(task: str) -> str:
    task = (task or "").strip()
    if len(task) != 21 or not task.startswith("task-") or any(c not in "0123456789abcdef" for c in task[5:]):
        raise SystemExit("not a task id (task-<16 hex>): %r" % task)
    return task


def accept_value(now: float, lease: int) -> str:
    """'claude-code-cli:<epoch>[:<lease seconds>]' - the shape lane.py parses, nothing else."""
    lease = max(0, int(lease or 0))
    return "%s:%d%s" % (ME, int(now), (":%d" % lease) if lease else "")


def done_value(state: str, note: str, evidence: str) -> str:
    """The terminal word as JSON: by, state, a bounded note, an https link. The note is cut and SAYS so."""
    state = (state or "").strip().upper()
    if state not in TERMINAL:
        raise SystemExit("--state must be one of %s" % ", ".join(TERMINAL))
    note = " ".join((note or "").split())
    if len(note) > NOTE_MAX:
        # The label is INSIDE the bound (Cursor on 517a918: 300 kept plus a label stored 336). Same
        # shape as message() above: what is kept, then how much there was.
        label = " [note cut here: %d characters in all]" % len(note)
        note = note[:NOTE_MAX - len(label)].rstrip() + label
    evidence = (evidence or "").strip()
    if evidence and (not evidence.startswith("https://") or any(c.isspace() for c in evidence)):
        raise SystemExit("--evidence must be one https:// link, or absent")
    return json.dumps({"by": ME, "state": state, "note": note, "evidence": evidence}, sort_keys=True)


def beat_interval(every) -> int:
    """0 is one beat. A loop waits 60..BEAT_EVERY seconds: under 60 is a row a minute, and over
    BEAT_EVERY the reader's 300 s window lapses before the worker's next pass lands the beat."""
    every = int(every or 0)
    if every == 0:
        return 0
    if not 60 <= every <= BEAT_EVERY:
        raise SystemExit("--every must be 0 (one beat) or 60..%d seconds; the reader's window is 300 s "
                         "and the worker's pass has to land the next beat inside it" % BEAT_EVERY)
    return every


def cmd_beat(args) -> int:
    """One beat, or a loop of them under a cap. Not waited for: the next beat supersedes this one.
    The loop stops BEFORE an interval that would cross --hours, so it never runs past the cap."""
    every = beat_interval(args.every)
    stop = time.time() + float(args.hours) * 3600 if every else None
    while True:
        value = "%d" % int(time.time())
        rid = post(request_row("set", HEARTBEAT_KEY, value=value))
        print("beat %s posted as %s (the worker writes it on its next pass)" % (value, rid))
        if not every:
            return 0
        if time.time() + every > stop:
            print("beat loop: the next interval would cross the %g h cap; stopping before it. "
                  "Start it again if the session goes on." % float(args.hours))
            return 0
        time.sleep(every)


def cmd_accept(args) -> int:
    task = task_id_ok(args.task)
    rid = post(request_row("set", ACCEPT_KEY % task, value=accept_value(time.time(), args.lease)))
    print("accept of %s posted as %s" % (task, rid))
    got = answer(rid, args.wait, since=args.since or stamp())
    print("worker: %s" % (got["payload"][:300] if got else not_yet(args.wait, rid)))
    return 0 if got else 2


def cmd_done(args) -> int:
    task = task_id_ok(args.task)
    value = done_value(args.state, args.note, args.evidence)
    print("NOTE: the note and the link travel on the public board. Sending: %s" % value)
    rid = post(request_row("set", DONE_KEY % task, value=value))
    print("done for %s posted as %s" % (task, rid))
    got = answer(rid, args.wait, since=args.since or stamp())
    print("worker: %s" % (got["payload"][:300] if got else not_yet(args.wait, rid)))
    return 0 if got else 2


def cmd_selftest(_args) -> int:
    """The grammar, with no network and no row: what the worker will be asked."""
    row = request_row("xadd", KEY % "grok", value=message("hello  there\n\nagain"),
                      row_id="CCC-INBOX-TEST")
    assert row["action_type"] == REQUEST_ACTION, row
    assert "do=redis-op|op=xadd|key=fleet:inbox:grok|val=" in row["payload"], row["payload"]
    assert row["payload"].endswith("|enc=b64"), row["payload"]
    blob = row["payload"].split("val=")[1].split("|")[0]
    body = base64.b64decode(blob).decode("utf-8")
    # THE VALUE IS THE TEXT, not an envelope. redis_gov's xadd writes {"text": value, "by": the
    # principal it decided, "at": its own clock}, so a JSON envelope would be stored AS the message.
    # My first draft sent one, and a selftest that asserted my own shape said it was fine.
    assert body == "hello there again", body                      # one line, whatever was typed
    assert "{" not in body, body
    long_one = message("word " * 400)
    assert len(long_one) <= MAX_TEXT, len(long_one)
    assert long_one.endswith("characters in all]"), long_one[-60:]      # never silently
    assert "1999 characters in all" in long_one, long_one[-60:]   # the true length, not the kept one
    read = request_row("xrange", KEY % ME, count=20, row_id="CCC-INBOX-READ")
    assert read["payload"].endswith("|count=20"), read["payload"]
    assert "val=" not in read["payload"], read["payload"]         # a read carries no value
    for bad in ("nobody", "", "v1:agent:grok"):
        try:
            canonical(bad)
        except SystemExit:
            continue
        raise AssertionError("accepted %r" % bad)
    sample = ('text=OK xrange fleet:inbox:claude-code-cli: [{"at": "2026-10-09T21:00:00Z", '
              '"by": "grok", "id": "1-0", "text": "ping"}]')
    assert entries_of(sample)[0]["by"] == "grok", entries_of(sample)
    assert entries_of("text=refused: nothing here") == []
    # THE TIMEOUT WORDING. A timeout that does not name the request, or that glues the next
    # sentence onto the gcloud command, or that still claims the worker is stopped, fails here.
    timed = read_timeout(WAIT_SECONDS, "CCC-INBOX-TEST")
    assert "CCC-INBOX-TEST" in timed, timed
    assert "NO ANSWER YET after 180s." in timed, timed
    assert "the governed worker is not running" not in timed, timed
    lines = timed.splitlines()
    command = [ln for ln in lines if ln.startswith("gcloud ")]
    assert command == ["gcloud run jobs executions list --job=bus-requests "
                       "--project=sfdc24 --region=us-central1"], command
    assert "Nothing was read" not in command[0], command
    assert lines[-1] == "Nothing was read, which is not the same as an empty inbox.", lines
    assert "--wait 180" in __doc__ and "--wait 120" not in __doc__, __doc__
    assert "exactly what this prints" not in __doc__, __doc__
    assert "governed worker stopped" not in (answer.__doc__ or ""), answer.__doc__
    # THE LAPTOP LANE. A beat is a plain epoch under fleet:alive:, which drive-intake's reader
    # parses as a float; accept and done are the exact shapes cloud/claude-api-worker/lane.py
    # parses, so a value this writes is never a value that lane refuses.
    beat = request_row("set", HEARTBEAT_KEY, value="1760000000", row_id="CCC-INBOX-BEAT")
    assert "do=redis-op|op=set|key=fleet:alive:claude-code-cli|val=" in beat["payload"], beat["payload"]
    assert base64.b64decode(beat["payload"].split("val=")[1].split("|")[0]) == b"1760000000"
    assert accept_value(1760000000.9, 900) == "claude-code-cli:1760000000:900", accept_value(1760000000.9, 900)
    # THE KEYS, as the rows the verbs post carry them - not only the sentence that names them.
    acc = request_row("set", ACCEPT_KEY % "task-0123456789abcdef", value=accept_value(1760000000, 900),
                      row_id="CCC-INBOX-ACCEPT")
    assert "|op=set|key=fleet:worker:accept:task-0123456789abcdef|val=" in acc["payload"], acc["payload"]
    assert base64.b64decode(acc["payload"].split("val=")[1].split("|")[0]) == b"claude-code-cli:1760000000:900"
    fin = request_row("set", DONE_KEY % "task-0123456789abcdef", value=done_value("FAILED", "n", ""),
                      row_id="CCC-INBOX-DONE")
    assert "|op=set|key=fleet:worker:done:task-0123456789abcdef|val=" in fin["payload"], fin["payload"]
    assert json.loads(base64.b64decode(fin["payload"].split("val=")[1].split("|")[0]))["state"] == "FAILED"
    assert accept_value(1760000000, 0) == "claude-code-cli:1760000000"
    done = json.loads(done_value("succeeded", "  Answered  in PR 371.  ", "https://github.com/sfdc-24/Blackboard/pull/371"))
    assert done == {"by": "claude-code-cli", "evidence": "https://github.com/sfdc-24/Blackboard/pull/371",
                    "note": "Answered in PR 371.", "state": "SUCCEEDED"}, done
    cut = json.loads(done_value("FAILED", "x" * 400, ""))["note"]
    assert len(cut) <= NOTE_MAX, len(cut)                              # the label is inside the bound
    assert cut.endswith("[note cut here: 400 characters in all]"), cut[-60:]
    for every in (0, 60, 240):
        assert beat_interval(every) == every
    for bad in (59, 241, 600):
        try:
            beat_interval(bad)
        except SystemExit:
            continue
        raise AssertionError("accepted --every %r" % bad)
    # THE PARSER, not only the literals: each verb parses with its options and its defaults.
    parser = build_parser()
    assert parser.parse_args(["beat", "--every", "240", "--hours", "2"]).every == 240
    assert parser.parse_args(["beat"]).every == 0 and parser.parse_args(["beat"]).hours == BEAT_HOURS
    assert parser.parse_args(["accept", "--task", "task-0123456789abcdef"]).lease == 900
    done_args = parser.parse_args(["done", "--task", "task-0123456789abcdef", "--state", "FAILED",
                                   "--note", "n", "--evidence", "https://x"])
    assert (done_args.state, done_args.evidence, done_args.wait) == ("FAILED", "https://x", WAIT_SECONDS)
    for bad in (("DONE", "", ""), ("SUCCEEDED", "", "http://plain"), ("SUCCEEDED", "", "https://a b")):
        try:
            done_value(*bad)
        except SystemExit:
            continue
        raise AssertionError("accepted %r" % (bad,))
    for bad in ("task-123", "fleet:worker:done:x", "task-0123456789abcdeg"):
        try:
            task_id_ok(bad)
        except SystemExit:
            continue
        raise AssertionError("accepted %r" % bad)
    assert task_id_ok(" task-0123456789abcdef ") == "task-0123456789abcdef"
    assert "beat [--every 240 --hours 8]" in __doc__ and "public board" in __doc__.lower(), __doc__
    print("selftest OK: the grammar is what redis_gov parses; the keys addressed are fleet:inbox:<agent>, "
          "fleet:alive:claude-code-cli and fleet:worker:accept|done:<task>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="what", required=True)
    for name in ("send", "read", "selftest", "beat", "accept", "done", "reconcile"):
        p = sub.add_parser(name)
        if name not in ("selftest", "beat"):
            p.add_argument("--wait", type=float, default=WAIT_SECONDS,
                           help="seconds to wait for the worker (default %d)" % WAIT_SECONDS)
            p.add_argument("--since", default="", help="board timestamp to read answers from")
        if name == "send":
            p.add_argument("--to", required=True)
            p.add_argument("--text", required=True)
        if name == "read":
            p.add_argument("--count", type=int, default=READ_COUNT,
                           help="entries per page, 1..20 (default %d); a cut page is asked again smaller" % READ_COUNT)
            p.add_argument("--pages", type=int, default=READ_PAGES,
                           help="pages per call (default %d); each page is one board row" % READ_PAGES)
            p.add_argument("--after", default="", help="read after this stream id instead of the saved cursor")
            p.add_argument("--past-cut", action="store_true",
                           help="move the cursor past entries whose text arrived cut at %d characters" % PREVIEW_CHARS)
        if name == "reconcile":
            p.add_argument("--pages", type=int, default=SETTLE_PAGES,
                           help="pages read per unknown write (default %d); on the board route each is one row"
                                % SETTLE_PAGES)
        if name == "beat":
            p.add_argument("--every", type=int, default=0,
                           help="loop: seconds between beats, 60..%d (0 = one beat); the reader's window is 300 s" % BEAT_EVERY)
            p.add_argument("--hours", type=float, default=BEAT_HOURS,
                           help="the loop's cap in hours (default %d); it never runs past it" % BEAT_HOURS)
        if name in ("accept", "done"):
            p.add_argument("--task", required=True, help="task-<16 hex>, from the doorbell row or the doc")
        if name == "accept":
            p.add_argument("--lease", type=int, default=900, help="seconds this session gives itself")
        if name == "done":
            p.add_argument("--state", required=True, help="SUCCEEDED or FAILED")
            p.add_argument("--note", default="", help="at most %d characters; it is PUBLIC" % NOTE_MAX)
            p.add_argument("--evidence", default="", help="one https:// link to the PR or the evidence")
    return parser


def main(argv=()) -> int:
    args = build_parser().parse_args(list(argv))
    return {"send": cmd_send, "read": cmd_read, "selftest": cmd_selftest, "beat": cmd_beat,
            "accept": cmd_accept, "done": cmd_done, "reconcile": cmd_reconcile}[args.what](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
