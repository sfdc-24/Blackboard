"""The claude-code-cli doorbell for call asks (v5): wakes this Claude Code session when the call bridge claims an ask
for Claude. It only RINGS: a cue makes PENDING (call_state.ring) and nothing more. Authority, provenance and the ACK
are call_ask.py accept's, from private reads. The cue is the PUBLIC canvas file (free, posts no row): a claimed
card claude-code-cli/call-<12hex> whose ask is not yet a task. At start it rings again for any PENDING task, and
once for a LEGACY_OPEN one. Exit 0 RING (prints the ask id only, never words). Poll 10 s.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import call_state as cs             # noqa: E402

CANVAS = "https://storage.googleapis.com/sfdc24-canvas/rooms/workshop.json"
CARD = re.compile(r"^claude-code-cli/(call-[0-9a-f]{12})$")
POLL = 10


def say(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ"), *a, flush=True)


def canvas(fetch=None):
    """{ask: status} of this agent's bridge cards in the public file. Raises when unreadable."""
    if fetch is None:
        url = "%s?t=%d" % (CANVAS, int(time.time()))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}),
                                    timeout=20) as r:
            data = json.load(r)
    else:
        data = fetch()
    out = {}
    for item in data.get("items") or []:
        m = CARD.match(str(item.get("id", ""))) if isinstance(item, dict) else None
        if m:
            out[m.group(1)] = str(item.get("status", ""))
    return out


def held():
    """An ask to ring for at start: PENDING, or LEGACY_OPEN not yet rung (marked rung under the lock)."""
    with cs.locked(wait=200) as txn:
        for ask, t in sorted(txn.state["tasks"].items()):
            if t["state"] == "PENDING":
                return ask
            if t["state"] == "LEGACY_OPEN" and not t.get("rang"):
                cs.touch(t, rang=cs.utc_stamp())
                return ask
    return None


def main(fetch=None, sleep=time.sleep, polls=None):
    ask = held()
    if ask:
        say("RING", "held", ask)
        return 0
    say("ringing for call asks from the public canvas")
    n = 0
    while polls is None or n < polls:
        n += 1
        try:
            tasks = cs.load()["tasks"]
            fresh = sorted(a for a, s in canvas(fetch).items() if s == "claimed" and a not in tasks)
            if fresh and cs.ring(fresh[0], "canvas"):
                say("RING", "canvas", fresh[0])
                return 0
        except Exception as error:                            # the type only
            say("canvas unreadable", type(error).__name__)
        sleep(POLL)
    return 1


if __name__ == "__main__":
    sys.exit(main())
