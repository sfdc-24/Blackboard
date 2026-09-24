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
    cursor as `inflight`, phase `claimed`, with this run's owner id and a
    lease expiry, compare-and-swap. Only that owner may call the model. A
    live claim held by another run is never taken. No reply on the board
    is not proof the owner is dead.
  - Only phase `claimed` can be taken after the lease expires. Immediately
    before the append the owner writes phase `posting` and renews the
    lease to cover the post (400s) plus a margin. A `posting` claim is
    never reclaimed, whatever the lease says: the append may already be
    on the wire. A later run reads the board. A reply is present only when
    some row's answers= is this exact source id: the legacy Row_ID strips
    characters and keeps 40, so it is not an identity. Reply present: mark
    the row answered. Reply absent: quarantine it and never post it again. It is
    not marked answered before that reply is seen. Quarantined ids are
    left out of the per-pass selection window, so they do not crowd out
    a newer row, and they stay in `unknown_ids` for a person to see.
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
# Longer than a model call. A lease shorter than the work would let the next
# run take a claim whose owner is still inside the model. The append has its
# own lease: fleet_agent's post timeout is 400s, and the posting write renews
# for that plus a margin so the lease cannot die while the append is in flight.
CLAIM_LEASE_SECONDS = int(os.environ.get("CLAIM_LEASE_SECONDS") or 600)
POST_TIMEOUT_SECONDS = 400
POST_LEASE_SECONDS = POST_TIMEOUT_SECONDS + 60


def _answers_of(row) -> str:
    """The first answers= field on a row, or empty.

    First value wins, same as the rest of the board: a later quote of
    answers=<something else> is not a rewrite of the reply's identity.
    """
    if not isinstance(row, (list, tuple)) or len(row) <= 5:
        return ""
    for segment in str(row[5] or "").split("|"):
        key, sep, val = segment.partition("=")
        if sep and key.strip().lower() == "answers":
            return val.strip()
    return ""


def _rows_answer(rows, answers_id: str) -> bool:
    """True when some row's answers= field is this exact source id.

    Row_ID is not the identity. The legacy reply id strips `.` and `_`
    and keeps 40 characters, so a receipt for one ask can share a Row_ID
    with a different ask. The payload field is the whole id.
    """
    if not answers_id:
        return False
    for row in rows or []:
        if _answers_of(row) == answers_id:
            return True
    return False


def _board_has_answer(answers_id: str) -> bool:
    """True when the board has a reply whose answers= is this exact id.

    Search by the answers token, and also by the new and legacy Row_IDs,
    because `match` is a substring and either form can be how the row is
    found. A hit still has to carry this full answers= value. A row whose
    Row_ID merely collides does not count.
    """
    import bus
    import agent_waker
    if not answers_id:
        return False
    env = bus.load_env()
    needles = ["answers=%s" % answers_id]
    for extra in (agent_waker.reply_row_id(AGENT, answers_id),
                  agent_waker.legacy_reply_row_id(AGENT, answers_id)):
        if extra and extra not in needles:
            needles.append(extra)
    for needle in needles:
        obj = bus.read_rows(env, match=needle, limit=20)
        if _rows_answer(obj.get("rows") or [], answers_id):
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

    def expiry_after(span: int) -> str:
        moment = datetime.fromisoformat(current().replace("Z", "+00:00"))
        return (moment + timedelta(seconds=span)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def expiry() -> str:
        return expiry_after(seconds)

    box = {"state": {"watermark": state.get("watermark", ""),
                     "answered_ids": list(state.get("answered_ids") or []),
                     "unknown_ids": list(state.get("unknown_ids") or []),
                     "inflight": state.get("inflight") or "",
                     "claim_owner": state.get("claim_owner") or "",
                     "claim_until": state.get("claim_until") or "",
                     "claim_phase": state.get("claim_phase") or ""},
           "token": token}

    def persist(new_state):
        box["token"] = store.save(CURSOR, new_state, box["token"])
        box["state"] = new_state

    def claim_of(s) -> dict:
        return {"id": s.get("inflight") or "",
                "owner": s.get("claim_owner") or "",
                "until": s.get("claim_until") or "",
                "phase": s.get("claim_phase") or ""}

    def live(held: dict) -> bool:
        return bool(held["id"] and held["until"] and held["until"] > current())

    def clear_claim(s) -> None:
        s["inflight"] = ""
        s["claim_owner"] = ""
        s["claim_until"] = ""
        s["claim_phase"] = ""

    def write_claim(s, answers_id: str) -> None:
        s["inflight"] = answers_id
        s["claim_owner"] = owner
        s["claim_until"] = expiry()
        s["claim_phase"] = "claimed"

    def mark_answered(s, answers_id: str) -> None:
        ids = list(s.get("answered_ids") or [])
        if answers_id not in ids:
            ids.append(answers_id)
        s["answered_ids"] = ids[-400:]
        clear_claim(s)

    def quarantine(s, answers_id: str) -> None:
        """The append may have landed. Do not post again, and do not call it answered."""
        ids = list(s.get("unknown_ids") or [])
        if answers_id not in ids:
            ids.append(answers_id)
        s["unknown_ids"] = ids[-400:]
        clear_claim(s)

    def reply_visible(answers_id: str) -> bool:
        """A reply counts only when it answers this exact source id.

        `reply_rows`, when a test sets it, is the board. Otherwise a test
        may still plant the Row_ID this run would write (`reply_on_board`
        / `board_contains`). The live lookup reads answers= off the row.
        """
        import agent_waker
        rows_of = getattr(waker, "reply_rows", None)
        if rows_of is not None:
            rows = rows_of() if callable(rows_of) else rows_of
            return _rows_answer(rows, answers_id)
        finder = board_contains
        if finder is None:
            finder = getattr(waker, "reply_on_board", None)
        if finder is not None:
            return bool(finder(agent_waker.reply_row_id(AGENT, answers_id)))
        return _board_has_answer(answers_id)

    def resolve_inflight():
        """A previous run owned this answers id and may already have posted.

        phase=posting means an append was dispatched. It is never reclaimed,
        lease or not: the post may still be on the wire. Reconcile it. A
        reply on the board marks the row answered. No reply quarantines it.
        Absence of a reply is not a reason to call the model again.

        phase=claimed is the model call. A visible reply is recorded. No
        reply leaves the claim for the lease: live claims stay with their
        owner, and only an expired claimed row may be taken.
        """
        inflight = box["state"].get("inflight") or ""
        if not inflight:
            return
        phase = box["state"].get("claim_phase") or ""
        answered = list(box["state"].get("answered_ids") or [])
        if inflight in answered:
            s = dict(box["state"])
            clear_claim(s)
            persist(s)
            return
        visible = reply_visible(inflight)
        if phase == "posting":
            s = dict(box["state"])
            if visible:
                mark_answered(s, inflight)
            else:
                quarantine(s, inflight)
                print(json.dumps({
                    "unknown": inflight,
                    "reason": "posting claim has no reply on the board; not reposting",
                }))
            persist(s)
            return
        if visible:
            s = dict(box["state"])
            mark_answered(s, inflight)
            persist(s)

    resolve_inflight()

    # A claim saved by a run that is still inside the model. Do not enter
    # the loop, and do not save: a watermark write here would move the
    # generation out from under the owner before they can append.
    # phase=posting was reconciled above; it is not a live claim to defer.
    held = claim_of(box["state"])
    if held["phase"] != "posting" and live(held) and held["owner"] != owner:
        print(json.dumps({"ran": False, "reason":
                          "live claim %s held by %s until %s"
                          % (held["id"], held["owner"], held["until"])}))
        return 0

    def claim_answer(answers_id: str) -> bool:
        answered = list(box["state"].get("answered_ids") or [])
        if answers_id in answered or answers_id in (box["state"].get("unknown_ids") or []):
            return False
        held_now = claim_of(box["state"])
        # A posting claim is never taken. A different id, live or expired,
        # stays put. Starting a second row on top of either is how two answers race.
        if held_now["phase"] == "posting":
            return False
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

    def begin_post(answers_id: str) -> bool:
        """Write phase=posting and renew the lease, then the append may start.

        An ownership check after the subprocess has been launched cannot
        pull the append back. The renewed lease is at least the post
        timeout plus a margin, counted from this write. A lost compare-and-swap
        or an expired claimed lease refuses the post. The stale owner is
        not written back.
        """
        held_now = claim_of(box["state"])
        phase = held_now["phase"] or "claimed"
        if not (held_now["id"] == answers_id and held_now["owner"] == owner
                and phase == "claimed" and live(held_now)):
            return False
        s = dict(box["state"])
        s["claim_phase"] = "posting"
        renewed = expiry_after(POST_LEASE_SECONDS)
        if (s.get("claim_until") or "") < renewed:
            s["claim_until"] = renewed
        try:
            persist(s)
        except state_store.Conflict:
            return False
        held_now = claim_of(box["state"])
        return (held_now["id"] == answers_id and held_now["owner"] == owner
                and held_now["phase"] == "posting" and live(held_now))

    def post_and_record(me, cfg, text, to, answers, verbose):
        if not begin_post(answers):
            return False
        ok = original_post(me, cfg, text, to, answers, verbose)
        if ok:
            s = dict(box["state"])
            # Confirmed on the board. Record that, and drop the posting claim
            # only when this run still holds it. A takeover's claim is not ours
            # to clear from a stale copy — the compare-and-swap refuses the write.
            if s.get("claim_owner") in ("", owner) and s.get("inflight") in ("", answers):
                mark_answered(s, answers)
            else:
                ids = list(s.get("answered_ids") or [])
                if answers not in ids:
                    ids.append(answers)
                s["answered_ids"] = ids[-400:]
            persist(s)
        # A post that did not confirm stays phase=posting. The next run
        # reconciles it from the board. It is not claimed again.
        return ok

    def call_agent(*args, **kwargs):
        text, route = original_call(*args, **kwargs)
        # A definite miss is not an unknown post: nothing was appended.
        # Release only our own claim, so a lost lease is not written back.
        s = dict(box["state"])
        # Only a claimed row is released. phase=posting may already have
        # an append on the wire; clearing it would let the next run post again.
        if (not text and s.get("inflight") and s.get("claim_owner") == owner
                and (s.get("claim_phase") or "claimed") == "claimed"):
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
    final["claim_phase"] = box["state"].get("claim_phase") or ""
    final["unknown_ids"] = list(box["state"].get("unknown_ids") or [])
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
