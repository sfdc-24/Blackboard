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
    python scripts/redis_inbox.py read [--wait 180] [--count 20] new messages to me
    python scripts/redis_inbox.py selftest                       the grammar, no network

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
                row_id: str | None = None) -> dict:
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


def seen() -> dict:
    try:
        with open(BOOKMARK, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def remember(last_id: str) -> None:
    with open(BOOKMARK, "w", encoding="utf-8", newline="") as fh:
        json.dump({"last_id": last_id, "at": stamp()}, fh)


def entries_of(payload: str) -> list:
    """The entries out of a RESULT payload: `text=OK xrange <key>: [ ... ]`."""
    start = payload.find("[")
    if start < 0:
        return []
    try:
        return json.loads(payload[start:payload.rindex("]") + 1])
    except (ValueError, IndexError):
        return []


def cmd_send(args) -> int:
    to = canonical(args.to)
    rid = post(request_row("xadd", KEY % to, value=message(args.text)))
    print("sent to %s, request %s" % (to, rid))
    got = answer(rid, args.wait, since=args.since or stamp())
    print("worker: %s" % (got["payload"][:300] if got else not_yet(args.wait, rid)))
    return 0 if got else 2


def cmd_read(args) -> int:
    rid = post(request_row("xrange", KEY % ME, count=args.count))
    got = answer(rid, args.wait, since=args.since or stamp())
    if not got:
        print(read_timeout(args.wait, rid))
        return 2
    rows = entries_of(got["payload"])
    last = seen().get("last_id") or ""
    fresh = [e for e in rows if str(e.get("id") or "") > last]
    for entry in fresh:
        print("%s | from %s | %s" % (entry.get("at", "?"), entry.get("by", "?"),
                                     str(entry.get("text", ""))[:400]))
    if fresh:
        remember(str(fresh[-1].get("id") or last))
    print("%d new of %d in %s" % (len(fresh), len(rows), KEY % ME))
    return 0


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
    for name in ("send", "read", "selftest", "beat", "accept", "done"):
        p = sub.add_parser(name)
        if name not in ("selftest", "beat"):
            p.add_argument("--wait", type=float, default=WAIT_SECONDS,
                           help="seconds to wait for the worker (default %d)" % WAIT_SECONDS)
            p.add_argument("--since", default="", help="board timestamp to read answers from")
        if name == "send":
            p.add_argument("--to", required=True)
            p.add_argument("--text", required=True)
        if name == "read":
            p.add_argument("--count", type=int, default=20)
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
            "accept": cmd_accept, "done": cmd_done}[args.what](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
