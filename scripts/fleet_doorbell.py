#!/usr/bin/env python3
"""The doorbell for Grok, Codex (Aya) and Gemini: board rows addressed to one agent, rung as they land.

THE ORDER (the owner, 2026-10-28 8:46 PM ET, O3): "when I ask for work in a call, the real agent wakes and puts an
'In the making' card up within 3 minutes". The call bridge (call_bridge.py) put the ask on fleet:inbox:<agent>, which
nothing reads on a push. claude-code-cli already had a doorbell (claude_cli_inbox.py --wait); the others had none
faster than the 15-minute cloud agent-waker. The bridge now also rings with one board row addressed to the agent
(Row_ID CALLBRIDGE-WAKE-<ask>, call_bridge.wake_row). This is the same doorbell as Claude's, for any one agent:

    python scripts/fleet_doorbell.py --agent grok              print new rows for grok, then move the bookmark
    python scripts/fleet_doorbell.py --agent codex --peek      print, keep the bookmark
    python scripts/fleet_doorbell.py --agent gemini --wait 30  poll every 30 s, exit with the first new rows
                                                               (run it in the background; restart it when it rings)
    --calls-only                                               ring only for the call bridge's wake rows

WHAT RINGS. A row addressed to the agent BY A WHOLE NAME: its Target_Surface or the payload's to=/cc= list names one
of the agent's names, or a WhatsApp row starts with one. Not its own rows, not a waker's reply (wakerreply=1), and
not an information row (phase RESULT, AYA_RESULT, DONE, ACK, RECEIPT): those cost a wake and need no answer.
Nothing new is reached: the bus pair every agent already holds (bus.load_env), read only. No new secret, no IAM.

THE BOOKMARK (FLEET_DOORBELL_BOOKMARK_DIR, default ~) is per agent, moves only after the rows are printed, and a
failed read never moves it - claude_cli_inbox's rules, whose helpers this uses.

THE WAKE, TAKEN (call_bridge.py THE WAKE, CONFIRMED: the bridge marks an agent "did not wake" unless it posts
phase=ACK answers=CALLBRIDGE-WAKE-<ask> from its own tag within 90 s). For the agents in ACK_WIRED (Grok only, for
now), a wake row is ACKed (scripts/wake_ack.py) the moment it is seen, BEFORE it is printed and the work starts:
one ACK per ask (the second ring, a replay or a restart posts none; the asks taken are kept beside the bookmark),
never retried. --peek (nothing is taken) and --no-ack post nothing. The other runners reuse wake_ack.Acker.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_cli_inbox as base  # noqa: E402
import wake_ack  # noqa: E402

# An agent's names in an address list, and the sender tags that are its own rows. Whole names only.
AGENTS = {
    "grok": {"names": {"grok", "grok-bot"}, "self": ("grok",)},
    "codex": {"names": {"codex", "chatgpt-codex-desktop", "aya"}, "self": ("chatgpt-codex-desktop", "codex", "aya")},
    "gemini": {"names": {"gemini"}, "self": ("gemini",)},
}
WAKE_PREFIX = "CALLBRIDGE-WAKE-"            # call_bridge.WAKE_PREFIX
INFO_PHASES = {"RESULT", "AYA_RESULT", "DONE", "ACK", "RECEIPT", "AYA_RECEIPT"}
# The runners whose doorbell takes the call bridge's wake itself, and the board tag each ACK is signed with.
# Codex (via Aya) and Gemini (via claude-code-cli) take theirs in their own runners until they are wired here.
ACK_WIRED = {"grok": "grok"}
PHASE = re.compile(r"(?:^|\|)\s*phase=([^|]*)", re.I)
ROW_ID = re.compile(r"(?:^|\|)\s*id=([^|]*)", re.I)


def bookmark(agent: str) -> Path:
    folder = Path(os.environ.get("FLEET_DOORBELL_BOOKMARK_DIR", str(Path.home())))
    return folder / (".fleet-doorbell-%s-bookmark" % agent)


def is_wake(row) -> bool:
    """A call bridge wake row: from the worker, its id CALLBRIDGE-WAKE-<ask>."""
    sender = str(row[2]).strip().lower() if len(row) > 2 else ""
    payload = str(row[5]) if len(row) > 5 else ""
    found = ROW_ID.search(payload[:300])
    return sender == "bus-reconciler" and bool(found) and found.group(1).strip().startswith(WAKE_PREFIX)


def is_for(row, agent: str, calls_only: bool = False) -> bool:
    cfg = AGENTS[agent]
    sender = str(row[2]).strip().lower() if len(row) > 2 else ""
    payload = str(row[5]) if len(row) > 5 else ""
    if any(sender == tag or sender.startswith(tag + "-") for tag in cfg["self"]):
        return False                                    # its own rows
    if re.search(r"(?:^|\|)\s*wakerreply=1", payload, re.I):
        return False
    phase = PHASE.search(payload[:400])
    if phase and phase.group(1).strip().upper() in INFO_PHASES:
        return False
    if sender == "whatsapp":
        first = re.match(r"^\s*([A-Za-z-]+)", payload)
        hit = bool(first and first.group(1).lower() in cfg["names"])
    else:
        hit = bool(base.addressees(row) & cfg["names"])
    if not hit:
        return False
    return is_wake(row) if calls_only else True


def new_rows(rows, since, agent: str, calls_only: bool = False) -> list:
    found = []
    for row in rows:
        ts = base.parse_ts(row[1] if len(row) > 1 else None)
        if ts is not None and ts > since and is_for(row, agent, calls_only):
            found.append((ts, row))
    return sorted(found, key=lambda pair: pair[0])


def acked_path(agent: str) -> Path:
    """The asks this agent's doorbell has ACKed, kept beside its bookmark so a restart never ACKs one twice."""
    return bookmark(agent).with_name(".fleet-doorbell-%s-acked.json" % agent)


def acker_for(agent: str, post=None, path: Path = None, clock=None, log=None):
    """The wake ACK for a wired agent (ACK_WIRED), or None. `post` defaults to the board append path."""
    if agent not in ACK_WIRED:
        return None
    return wake_ack.Acker(ACK_WIRED[agent], AGENTS[agent]["names"], post or wake_ack.append_spec,
                          path=path or acked_path(agent), clock=clock, log=log)


def check(read, agent: str, path: Path = None, peek: bool = False, calls_only: bool = False, out=None, ack=None):
    """Print the agent's new rows and (unless `peek`) move its bookmark past them. How many, or None on a failed
    read (the bookmark stays). `ack` (an Acker): each wake row is ACKed before it is printed; never under peek."""
    path = path or bookmark(agent)
    out = out or print
    since = base.baseline(path, out)
    try:
        rows = read(since)
    except (SystemExit, OSError, ValueError) as error:
        out("READ FAILED (%s): bookmark kept at %s" % (error, since.isoformat()))
        return None
    found = new_rows(rows or [], since, agent, calls_only)
    for ts, row in found:
        if ack is not None and not peek and is_wake(row):
            ack(row)                                     # taken first, then the work: the bridge sees it in seconds
        out(("RING " if is_wake(row) else "") + base.line(ts, row))
    if found and not peek:
        path.write_text(found[-1][0].isoformat(), encoding="utf-8")
    return len(found)


def main(argv) -> int:
    if "--agent" not in argv or argv[argv.index("--agent") + 1:][:1] == [] or \
            argv[argv.index("--agent") + 1] not in AGENTS:
        print("usage: fleet_doorbell.py --agent grok|codex|gemini [--peek] [--wait SECONDS] [--calls-only] [--no-ack]")
        return 2
    agent = argv[argv.index("--agent") + 1]
    calls_only = "--calls-only" in argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import bus                                                           # noqa: PLC0415
    env = bus.load_env()

    def read(since):
        return bus.read_rows(env, since=base.since_arg(since))["rows"]

    ack = None if "--no-ack" in argv or "--peek" in argv else acker_for(agent)
    if "--wait" in argv:
        every = max(5.0, float(argv[argv.index("--wait") + 1]))
        while True:
            if check(read, agent, calls_only=calls_only, ack=ack):
                return 0
            time.sleep(every)
    count = check(read, agent, peek="--peek" in argv, calls_only=calls_only, ack=ack)
    if count is None:
        return 2
    if count == 0:
        print("nothing new for %s since %s" % (agent, base.read_bookmark(bookmark(agent))[0].isoformat()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
