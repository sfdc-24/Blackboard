#!/usr/bin/env python3
r"""An API agent's doorbell, in the cloud. One image; AGENT picks the agent.

Began as Gemini's alone. Mr Salam, 2026-09-24: "add foundry, grok and claude to
the watcher" - so the image carries every adapter agent_waker knows, and each
Cloud Run job (gemini-waker, claude-api-waker) sets
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
  - BEFORE the model is called, the row's answers id is written into the
    cursor as `inflight`, with this run's owner id and a lease expiry,
    compare-and-swap. Only that owner may call the model. A live claim held
    by another run is never taken. No reply on the board is not proof the
    owner is dead.
  - A later run that finds `inflight` not yet answered looks the reply up
    by its deterministic Row_ID (`<AGENT>-WAKE-<answers id>`). Present:
    mark it answered and do not post again. Absent, and the lease is still
    live: leave it. Absent, and the lease has expired: this run may take
    the claim. Immediately before the append it compare-and-swaps again,
    and the post is refused if this run no longer owns the claim — so the
    run that lost the lease cannot append after a takeover.
  - The answered id is still recorded when the post is confirmed. Saves are
    compare-and-swap. A second writer with a stale token is refused and this
    run exits non-zero rather than merging.

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
import uuid
from datetime import datetime, timedelta, timezone

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import state_store  # noqa: E402

# grok is deliberately absent. Mr Salam, 2026-09-24: it is out of usage
# allowance and reserved for his exclusive use, "not for typical work;
# everyone else should be servicing that".
# foundry dropped by Mr Salam the same day; see cloud/board-watcher/main.py.
AGENTS = ("gemini", "claude-api")
AGENT = (os.environ.get("AGENT") or "gemini").strip()
CURSOR = os.environ.get("CURSOR_NAME") or "%s_waker" % AGENT.replace("-", "_")
# Longer than a model call plus the append. A lease shorter than the work
# would let the next run take a claim whose owner is still inside the model.
CLAIM_LEASE_SECONDS = int(os.environ.get("CLAIM_LEASE_SECONDS") or 600)


def _board_has_row(row_id: str) -> bool:
    """True when a row with this exact Row_ID is on the board.

    `match` is a substring over the whole row, so a later note that merely
    quotes the id is not a hit. The Row_ID column has to be that id.
    """
    import bus
    env = bus.load_env()
    obj = bus.read_rows(env, match=row_id, limit=20)
    for row in obj.get("rows") or []:
        if row and str(row[0]).strip() == row_id:
            return True
    return False


def _stamp(value) -> str:
    """UTC timestamp the lease is compared as text. Same shape as the board."""
    if callable(value):
        return _stamp(value())
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value, timezone.utc)
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(argv=None, store=None, waker=None, board_contains=None,
        now=None, lease_seconds=None) -> int:
    """Parameters are for tests: a fake store and a fake waker module.

    board_contains(row_id) -> bool overrides the live board lookup. A test
    waker may also set reply_on_board to the same effect. now is a timestamp
    or a callable returning one, so a test can expire a lease without sleeping.
    """
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

    owner = uuid.uuid4().hex
    seconds = int(lease_seconds if lease_seconds is not None else CLAIM_LEASE_SECONDS)

    def current() -> str:
        # Re-read every time. A test clock moves while this run is inside
        # the model call; a lease checked once at entry would stay live.
        return _stamp(None if now is None else now)

    def expiry() -> str:
        moment = datetime.fromisoformat(current().replace("Z", "+00:00"))
        return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    box = {"state": {"watermark": state.get("watermark", ""),
                     "answered_ids": list(state.get("answered_ids") or []),
                     "inflight": state.get("inflight") or "",
                     "claim_owner": state.get("claim_owner") or "",
                     "claim_until": state.get("claim_until") or ""},
           "token": token}

    def persist(new_state):
        box["token"] = store.save(CURSOR, new_state, box["token"])
        box["state"] = new_state

    def claim_of(s) -> dict:
        return {"id": s.get("inflight") or "",
                "owner": s.get("claim_owner") or "",
                "until": s.get("claim_until") or ""}

    def live(held: dict) -> bool:
        return bool(held["id"] and held["until"] and held["until"] > current())

    def clear_claim(s) -> None:
        s["inflight"] = ""
        s["claim_owner"] = ""
        s["claim_until"] = ""

    def write_claim(s, answers_id: str) -> None:
        s["inflight"] = answers_id
        s["claim_owner"] = owner
        s["claim_until"] = expiry()

    def reply_visible(answers_id: str) -> bool:
        import agent_waker
        rid = agent_waker.reply_row_id(AGENT, answers_id)
        finder = board_contains
        if finder is None:
            finder = getattr(waker, "reply_on_board", None)
        if finder is None:
            finder = _board_has_row
        return bool(finder(rid))

    def resolve_inflight():
        """A previous run owned this answers id and may already have posted.

        The board is the record of a confirmed append. If the reply row is
        there, mark the id answered and do not call the model — even while
        the lease is still live. If it is not, leave the claim. Absence is
        not proof the owner is dead, and this run must not take a live claim.
        """
        inflight = box["state"].get("inflight") or ""
        if not inflight:
            return
        answered = list(box["state"].get("answered_ids") or [])
        visible = inflight in answered or reply_visible(inflight)
        if not visible:
            return
        s = dict(box["state"])
        if inflight not in answered:
            ids = list(s.get("answered_ids") or [])
            ids.append(inflight)
            s["answered_ids"] = ids[-400:]
        clear_claim(s)
        persist(s)

    resolve_inflight()

    # A claim saved by a run that is still inside the model. Do not enter
    # the loop, and do not save: a watermark write here would move the
    # generation out from under the owner before they can append.
    held = claim_of(box["state"])
    if live(held) and held["owner"] != owner:
        print(json.dumps({"ran": False, "reason":
                          "live claim %s held by %s until %s"
                          % (held["id"], held["owner"], held["until"])}))
        return 0

    def claim_answer(answers_id: str) -> bool:
        answered = list(box["state"].get("answered_ids") or [])
        if answers_id in answered:
            return False
        held_now = claim_of(box["state"])
        # A different id, live or expired, stays put. The loop has to meet
        # that id; starting a second row on top of it is how two answers race.
        if held_now["id"] and held_now["id"] != answers_id:
            return False
        if held_now["id"] == answers_id and live(held_now):
            # Ours already: keep it. Someone else's: never take it.
            return held_now["owner"] == owner
        s = dict(box["state"])
        write_claim(s, answers_id)
        # Conflict propagates. The loser must not call the model.
        persist(s)
        return True

    original_post = waker.post_reply
    original_call = getattr(waker, "call_agent", None)
    had_claim = hasattr(waker, "claim_answer")
    original_claim = getattr(waker, "claim_answer", None)

    def still_owns(answers_id: str) -> bool:
        """Compare-and-swap the claim immediately before any append.

        A local lease that has expired, or a cursor someone else has
        written, means this run no longer owns the row. Refuse the post.
        Do not write the stale owner back: that would steal the takeover.
        """
        def ours(s) -> bool:
            held_now = claim_of(s)
            return (held_now["id"] == answers_id
                    and held_now["owner"] == owner
                    and live(held_now))

        if not ours(box["state"]):
            return False
        try:
            persist(dict(box["state"]))
        except state_store.Conflict:
            return False
        return ours(box["state"])

    def post_and_record(me, cfg, text, to, answers, verbose):
        if not still_owns(answers):
            return False
        ok = original_post(me, cfg, text, to, answers, verbose)
        if ok:
            s = dict(box["state"])
            ids = list(s.get("answered_ids") or [])
            if answers not in ids:
                ids.append(answers)
            s["answered_ids"] = ids[-400:]
            if s.get("inflight") == answers and s.get("claim_owner") == owner:
                clear_claim(s)
            persist(s)
        return ok

    def call_agent(*args, **kwargs):
        text, route = original_call(*args, **kwargs)
        # A definite miss is not an unknown post: nothing was appended.
        # Release only our own claim, so a lost lease is not written back.
        s = dict(box["state"])
        if not text and s.get("inflight") and s.get("claim_owner") == owner:
            clear_claim(s)
            try:
                persist(s)
            except state_store.Conflict:
                pass
        return text, route

    waker.post_reply = post_and_record
    waker.claim_answer = claim_answer
    if original_call is not None:
        waker.call_agent = call_agent

    workdir = tempfile.mkdtemp(prefix="gemini-waker-")
    os.environ["BLACKBOARD_STATE_DIR"] = workdir
    path = os.path.join(workdir, ".%s_waker_state.json" % AGENT)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(box["state"], fh)

    try:
        rc = waker.main(argv or ["--agent", AGENT, "--max", "3"])
    finally:
        waker.post_reply = original_post
        if original_call is not None:
            waker.call_agent = original_call
        if had_claim:
            waker.claim_answer = original_claim
        elif hasattr(waker, "claim_answer"):
            del waker.claim_answer

    # The pass's own view: its watermark, and answered_ids that already include
    # everything recorded above. Union, so nothing recorded mid-pass is dropped.
    # inflight stays whatever the claims left: the file the waker rewrites does
    # not know about a claim that outlived this pass.
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
    final["inflight"] = box["state"].get("inflight") or ""
    final["claim_owner"] = box["state"].get("claim_owner") or ""
    final["claim_until"] = box["state"].get("claim_until") or ""
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
