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
    caller decides when to look. It also cannot work while the governed worker is stopped: with no
    worker, a request row simply sits there - which is exactly what this prints rather than hanging.

    python scripts/redis_inbox.py send --to grok --text "..."    one message
    python scripts/redis_inbox.py read [--wait 120] [--count 20] new messages to me
    python scripts/redis_inbox.py selftest                       the grammar, no network
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
    """The worker's RESULT row for `rid`, or None if it has not come. A missing answer is NOT a
    failure to report as one: with the governed worker stopped, no row is ever executed."""
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


def not_yet(waited: float) -> str:
    """What a timeout actually knows, and nothing more.

    The first version of this line said "the governed worker is not running". It said that on a
    40-second budget about a request that was answered at 52 seconds, by a worker that had been
    running for five minutes. A timeout is evidence about the wait, never about the peer: the
    row is on the board either way, and the RESULT row is where the truth is."""
    return ("NO ANSWER YET after %gs. The request row is on the board and is not lost; it may be "
            "answered after this wait. Look for the AYA_RESULT row answering its id, and check "
            "the worker with: gcloud run jobs executions list --job=bus-requests" % waited)


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
    print("worker: %s" % (got["payload"][:300] if got else not_yet(args.wait)))
    return 0 if got else 2


def cmd_read(args) -> int:
    rid = post(request_row("xrange", KEY % ME, count=args.count))
    got = answer(rid, args.wait, since=args.since or stamp())
    if not got:
        print(not_yet(args.wait) + " Nothing was read, which is not the same as an empty inbox.")
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
    print("selftest OK: the grammar is what redis_gov parses, and only fleet:inbox: is addressed")
    return 0


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="what", required=True)
    for name in ("send", "read", "selftest"):
        p = sub.add_parser(name)
        if name != "selftest":
            p.add_argument("--wait", type=float, default=WAIT_SECONDS,
                           help="seconds to wait for the worker (default %d)" % WAIT_SECONDS)
            p.add_argument("--since", default="", help="board timestamp to read answers from")
        if name == "send":
            p.add_argument("--to", required=True)
            p.add_argument("--text", required=True)
        if name == "read":
            p.add_argument("--count", type=int, default=20)
    args = parser.parse_args(list(argv))
    return {"send": cmd_send, "read": cmd_read, "selftest": cmd_selftest}[args.what](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
