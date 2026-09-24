#!/usr/bin/env python3
r"""The WhatsApp outbox, in the cloud: board rows for Mr Salam reach his phone.

WHY THIS EXISTS. The outbox was the laptop task SFDC24-WA-Board-Outbox, disabled
with the other ten on 2026-09-23. Mr Salam, 2026-09-24: "move the outbox to the
cloud". It runs scripts/wa_board_outbox.py unchanged - which since this change
also delivers agent-waker answers to his own WhatsApp rows, something no path
did before - and sends through scripts/wa_notify.py, no PowerShell.

WHAT MAKES THIS SAFE TO SCHEDULE, because every mistake here lands on his phone:

  - The delivered-id cursor lives in durable state, never the container's disk.
  - IT REFUSES TO RUN WITHOUT ONE. The outbox primes on an empty state, which is
    safe, but a cursor lost between runs would re-prime and then silently skip -
    or worse, a partial one would resend. Seeding is a deliberate act.
  - BEFORE each send, the outbox writes that row id into the cursor as
    `inflight` and this wrapper compare-and-swaps the save. Only the run
    whose write lands may send. The loser is refused and exits non-zero.
  - A WhatsApp send cannot be read back. A later run that finds `inflight`
    set and no delivered receipt marks the id `unknown` and does not send
    it again; a human decides. A kill between the send returning and the
    receipt save is this case. A send that returns a definite failure is
    not unknown: the claim is cleared and a later run may retry it.

ONE WRITER. From 2026-09-24 this job owns the outbox cursor. The laptop task
SFDC24-WA-Board-Outbox keeps its own file and must STAY DISABLED: two outboxes
with two cursors send him every message twice.

GIVEN: bus pair, META_TOKEN, WA_PHONE_NUMBER_ID, WA_TO. The recipient is a secret,
never an argument, and never read from the board.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import state_store  # noqa: E402

CURSOR = os.environ.get("CURSOR_NAME") or "wa_outbox"


def run(argv=None, store=None, outbox=None) -> int:
    uri = (os.environ.get("BLACKBOARD_STATE_URI") or "").strip()
    if store is None:
        if not uri.startswith("gs://"):
            print(json.dumps({"ran": False, "refused":
                              "BLACKBOARD_STATE_URI must be a gs:// URI"}))
            return 2
        store = state_store.open_store(uri)

    state, token = store.load(CURSOR)
    if token is None or not (state.get("delivered_row_ids") or state.get("delivered_bcb_ids")):
        print(json.dumps({"ran": False, "refused":
                          "no seeded %s cursor in durable state" % CURSOR}))
        return 2

    if outbox is None:
        import wa_board_outbox as outbox  # noqa: F811

    box = {"token": token}
    original_save = outbox.save_state

    def save_through(new_state, path=None):
        # The outbox calls this BEFORE send_via_notify, with inflight set.
        # The compare-and-swap therefore lands before the message does: the
        # loser raises here and never sends. A later save records the receipt
        # or, on the next run, the unknown id.
        original_save(new_state, path) if path is not None else original_save(new_state)
        box["token"] = store.save(CURSOR, json.loads(json.dumps(new_state)), box["token"])

    outbox.save_state = save_through
    workdir = tempfile.mkdtemp(prefix="wa-outbox-")
    path = Path(workdir) / "wa_board_outbox_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    try:
        rc = outbox.main(argv or ["once", "--state", str(path)])
    finally:
        outbox.save_state = original_save
    print(json.dumps({"ran": True, "rc": rc}))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(run())
    except state_store.Conflict as e:
        print(json.dumps({"ran": True, "conflict": str(e)}))
        sys.exit(3)
