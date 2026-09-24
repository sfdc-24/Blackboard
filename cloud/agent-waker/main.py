#!/usr/bin/env python3
r"""An API agent's doorbell, in the cloud. One image; AGENT picks the agent.

Began as Gemini's alone. Mr Salam, 2026-09-24: "add foundry, grok and claude to
the watcher" - so the image carries every adapter agent_waker knows, and each
Cloud Run job (gemini-waker, foundry-waker, claude-api-waker) sets
AGENT and holds only that agent's credential. The rest of this note was written
for gemini and holds for all three: each has its own cursor, <agent>_waker.

WHY THIS EXISTS. Gemini has no process of its own. Its only doorbell was the
laptop task SFDC24-GeminiWaker, running scripts/agent_waker.py --agent gemini
every fifteen minutes. All eleven laptop tasks were disabled on 2026-09-23, and
an architecture review addressed to gemini at 01:19Z on the 24th sat unseen
until it was rung by hand. Mr Salam, 2026-09-24: "move gemini to the cloud".

WHAT IT RUNS. The laptop waker itself, unchanged: agent_waker.main() in-process,
fleet_agent.py for the post, Row_ID read-back before a reply counts. A second
copy of the answering logic would be a second thing to get wrong on an
append-only board.

THE ONE THING THE CLOUD CHANGES IS WHERE THE CURSOR LIVES, and it is the thing
that decides whether a reply is sent twice.

  - The cursor (watermark + answered_ids) lives in durable state, not in the
    container's disk, which is gone after every run.
  - IT REFUSES TO RUN WITHOUT ONE. A missing cursor would fall back to a
    six-hour look-back with an empty answered list, and re-answer everything
    the laptop answered in those six hours. Seeding it is a deliberate act.
  - Each posted reply is written to the store AS IT LANDS, not only at the end.
    A run killed between two replies must not leave the first one unrecorded,
    or the next run posts it again.
  - Saves are compare-and-swap. A second writer with a stale token is refused
    and this run exits non-zero rather than merging.

ONE WRITER. From 2026-09-24 this job owns the gemini cursor. The laptop task
SFDC24-GeminiWaker keeps its own file cursor and must STAY DISABLED: two
doorbells with two cursors answer every row twice. A manual laptop run is the
same mistake.

DEPLOYED AS: Cloud Run job gemini-waker (us-central1), triggered by Cloud
Scheduler gemini-waker-15min at :07/:22/:37/:52 America/Toronto, cursor
gs://sfdc24-fleet-state/wakers gemini_waker, seeded from the laptop file.

WHAT IT IS NOT GIVEN: no META_TOKEN (cannot message Mr Salam), no GH_TOKEN
(cannot touch a repository). It holds the bus pair and GEMINI_API_KEY.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import state_store  # noqa: E402

# grok is deliberately absent. Mr Salam, 2026-09-24: it is out of usage
# allowance and reserved for his exclusive use, "not for typical work;
# everyone else should be servicing that".
AGENTS = ("gemini", "foundry", "claude-api")
AGENT = (os.environ.get("AGENT") or "gemini").strip()
CURSOR = os.environ.get("CURSOR_NAME") or "%s_waker" % AGENT.replace("-", "_")


def run(argv=None, store=None, waker=None) -> int:
    """Parameters are for tests: a fake store and a fake waker module."""
    uri = (os.environ.get("BLACKBOARD_STATE_URI") or "").strip()
    if store is None:
        if not uri.startswith("gs://"):
            print(json.dumps({"ran": False, "refused":
                              "BLACKBOARD_STATE_URI must be a gs:// URI; a cursor on "
                              "a container's own disk is lost after every run"}))
            return 2
        store = state_store.open_store(uri)

    state, token = store.load(CURSOR)
    if AGENT not in AGENTS:
        print(json.dumps({"ran": False, "refused": "AGENT %r is not one of %s" % (AGENT, AGENTS)}))
        return 2
    if token is None or "answered_ids" not in state:
        print(json.dumps({"ran": False, "refused":
                          "no %s cursor in durable state; seed it from the laptop's "
                          ".<agent>_waker_state.json before the first run" % CURSOR}))
        return 2

    if waker is None:
        import agent_waker as waker  # noqa: F811

    box = {"state": {"watermark": state.get("watermark", ""),
                     "answered_ids": list(state.get("answered_ids") or [])},
           "token": token}

    def persist(new_state):
        box["token"] = store.save(CURSOR, new_state, box["token"])
        box["state"] = new_state

    # Record each reply as it lands. The waker only saves at the end of a pass.
    original_post = waker.post_reply

    def post_and_record(me, cfg, text, to, answers, verbose):
        ok = original_post(me, cfg, text, to, answers, verbose)
        if ok:
            s = dict(box["state"])
            s["answered_ids"] = (list(s["answered_ids"]) + [answers])[-400:]
            persist(s)
        return ok

    waker.post_reply = post_and_record

    workdir = tempfile.mkdtemp(prefix="gemini-waker-")
    os.environ["BLACKBOARD_STATE_DIR"] = workdir
    path = os.path.join(workdir, ".%s_waker_state.json" % AGENT)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(box["state"], fh)

    try:
        rc = waker.main(argv or ["--agent", AGENT, "--max", "3"])
    finally:
        waker.post_reply = original_post

    # The pass's own view: its watermark, and answered_ids that already include
    # everything recorded above. Union, so nothing recorded mid-pass is dropped.
    try:
        with open(path, encoding="utf-8") as fh:
            final = json.load(fh)
    except Exception:  # noqa: BLE001
        final = dict(box["state"])
    ids = list(box["state"]["answered_ids"])
    for i in final.get("answered_ids") or []:
        if i not in ids:
            ids.append(i)
    final["answered_ids"] = ids[-400:]
    if final != box["state"]:
        persist(final)
    print(json.dumps({"ran": True, "rc": rc, "watermark": final.get("watermark"),
                      "answered": len(final["answered_ids"])}))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(run())
    except state_store.Conflict as e:
        print(json.dumps({"ran": True, "conflict": str(e)}))
        sys.exit(3)
