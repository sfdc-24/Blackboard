#!/usr/bin/env python3
r"""One watcher for the board, instead of one timer per agent.

WHY. Mr Salam, 2026-09-24: "why is scheduler needed? shouldn't things work based
on pull system, only one or two things should run on scheduler". He was right.
The board cannot push, so every agent had its own timer and read the whole
window every fifteen minutes whether or not anything was addressed to it - and
a question waited up to fifteen minutes for its doorbell.

WHAT IT DOES. This is the ONE thing on a short timer. Each tick it reads only
the rows new since its cursor, asks each route whether a row is for it, and
starts that route's Cloud Run job - once, however many rows it has. A tick with
nothing new starts nothing. The jobs keep their own cursors and dedupe, so the
watcher decides only WHEN a job runs, never WHAT it sends.

WHAT IT WILL NOT DO
  - It never answers, posts or sends anything itself. It holds the bus pair to
    read, and a metadata-server token to start jobs. No model key, no Meta token.
  - It never starts a job that is already running, and it never starts one
    whose running state it could not read. Two copies of a job answer or
    send the same row twice - their cursors are compare-and-swap, which
    catches the second writer only AFTER it has already posted. So a running
    job is skipped, and so is a job whose status lookup failed. That failure
    stays on the route: its pending work is held, the other routes still
    start, and the cursor is still saved. The watermark is HELD before the
    rows a skipped or failed route was needed for: the next tick tries again
    rather than forgetting them.
  - A start is not a finish. Each route keeps its own pending list until THAT
    JOB's cursor records the id as answered (delivered, for the outbox) or
    quarantined. While anything is still pending, the next tick starts the
    route again, once, and not while the job is already running. How many
    rows one run actually answers stays the job's own per-pass cap.
  - It reads the lease back immediately before a start. A slow read can
    outlive the 120s lease; if another tick owns it by then, this one does
    not start the job and does not write the cursor.
  - A start that fails also holds the watermark. A doorbell that silently
    drops a ring is the failure this replaces.

READING. since = watermark minus an overlap, deduped by Row_ID. A row whose
timestamp lands slightly behind a later one is still seen, and a row seen
twice is routed once.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS)

import state_store  # noqa: E402

CURSOR = os.environ.get("CURSOR_NAME") or "board_watcher"
PROJECT = os.environ.get("RUN_PROJECT") or "sfdc24"
REGION = os.environ.get("RUN_REGION") or "us-central1"
OVERLAP = timedelta(minutes=int(os.environ.get("OVERLAP_MINUTES") or 10))
SEEN_CAP = 1000
# Longer than the task timeout, so a tick killed mid-run cannot leave a lease
# that expires while it is still starting jobs.
LEASE = timedelta(seconds=int(os.environ.get("LEASE_SECONDS") or 120))
RUN_API = "https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s"
# Cursor each route's job writes. The watcher does not decide that a row is
# done; that cursor does, when the id is answered (delivered, for the outbox)
# or quarantined. Defaults match the cloud jobs' CURSOR_NAME.
ROUTE_CURSORS = {
    "gemini-waker": "gemini_waker",
    "claude-api-waker": "claude_api_waker",
    "wa-outbox": "wa_outbox",
}


def finished_ids(state, job: str) -> set:
    """Ids this route's own cursor has answered or quarantined.

    Anything else is still pending, including a row whose job was started
    and then killed, and a row the per-pass cap did not reach.
    """
    if not isinstance(state, dict):
        return set()
    if job == "wa-outbox":
        found = set(state.get("delivered_row_ids") or [])
        found.update(state.get("delivered_bcb_ids") or [])
        found.update(state.get("unknown_row_ids") or [])
    else:
        found = set(state.get("answered_ids") or [])
        found.update(state.get("unknown_ids") or [])
    return {str(item) for item in found if item}


def reconcile(pending, store) -> tuple:
    """Drop pending work the route has finished. Returns (still, done row ids)."""
    still, done = {}, []
    for job, items in (pending or {}).items():
        cursor, _token = store.load(ROUTE_CURSORS.get(job) or job)
        finished = finished_ids(cursor, job)
        keep = []
        for item in items or []:
            if isinstance(item, str):
                item = {"row_id": item, "work_id": item}
            if not isinstance(item, dict):
                continue
            rid = str(item.get("row_id") or "").strip()
            wid = str(item.get("work_id") or rid).strip()
            if not rid and not wid:
                continue
            if rid in finished or wid in finished:
                if rid:
                    done.append(rid)
                continue
            keep.append({"row_id": rid, "work_id": wid or rid})
        if keep:
            still[job] = keep[-SEEN_CAP:]
    return still, done


# ------------------------------------------------------------------ routing

def _routes():
    """job name -> predicate(row). Imported lazily so tests can stub them."""
    import agent_waker as aw
    import wa_board_outbox as ob

    def for_agent(tag):
        def pred(row):
            return (len(row) > aw.C_PAYLOAD and not aw.is_from(row, tag)
                    and not aw.is_waker_reply(row) and aw.addressed_to(row, tag))
        return pred

    # foundry was DROPPED the same day: "it never added real value and right
    # now we don't have time to spoonfeed it" - and its Azure resource is gone.
    # grok is deliberately NOT routed. Mr Salam, 2026-09-24: out of usage
    # allowance, reserved for his exclusive use, "not for typical work".
    # claude-api is the standby that also answers his WhatsApp rows addressed
    # to claude-code-cli - agent_waker.addressed_to carries that rule.
    return {
        "gemini-waker": for_agent("gemini"),
        "claude-api-waker": for_agent("claude-api"),
        "wa-outbox": lambda row: ob.parse_wa_request(row) is not None,
    }


def plan(rows, seen, routes):
    """Which jobs to start, for which rows. Pure: no IO.

    Returns ({job: [(ts, row_id), ...]}, new_seen_ids, newest_ts, errors).
    A route that RAISES on a row is not a "no" - that swallowed ring is what
    this watcher replaces. The row is kept out of seen and the error reported.
    """
    import agent_waker as aw
    wanted, fresh, newest, errors = {}, [], None, {}
    for row in rows:
        rid = str(row[aw.C_ROW_ID] if row else "").strip()
        ts = aw.parse_ts(row[aw.C_TS] if len(row) > aw.C_TS else "")
        if not rid or ts is None or rid in seen:
            continue
        fresh.append(rid)
        if newest is None or ts > newest:
            newest = ts
        for job, pred in routes.items():
            try:
                hit = pred(row)
            except Exception as exc:  # noqa: BLE001 - one odd row must not stop the tick
                errors.setdefault(rid, (ts, []))[1].append(
                    "%s: %s: %s" % (job, type(exc).__name__, str(exc)[:120]))
                continue
            if hit:
                # work_id is what the job records: the BCB id, or the Row_ID
                # when the row has none. Pending is complete only when that
                # id shows up as answered or quarantined.
                wid = aw.bcb_id(row) if len(row) > aw.C_PAYLOAD else rid
                wanted.setdefault(job, []).append((ts, rid, wid))
    return wanted, fresh, newest, errors


# --------------------------------------------------------------- run api

def _token():
    return state_store.metadata_token()[0]


def _call(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def job_running(job, token) -> bool:
    got = _call("GET", RUN_API % (PROJECT, REGION, job), token)
    latest = got.get("latestCreatedExecution") or {}
    return bool(latest) and not latest.get("completionTime")


def start_job(job, token) -> str:
    got = _call("POST", (RUN_API % (PROJECT, REGION, job)) + ":run", token, {})
    return str((got.get("metadata") or {}).get("name") or got.get("name") or "")


# ------------------------------------------------------------------ the tick

def tick(store, read_since, routes, running, start, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    state, token = store.load(CURSOR)
    if token is None or not state.get("watermark"):
        return {"ran": False, "refused": "no seeded %s cursor" % CURSOR}

    # ONE TICK AT A TIME. At a one-minute cadence a slow tick (a cold start
    # measured 110 s) overlaps the next, and two ticks that both see a job idle
    # would both start it - the double answer this file exists to prevent. The
    # lease is taken with compare-and-swap, so exactly one of two racing ticks
    # gets it; the loser exits without touching anything.
    lease = state.get("lease_until") or ""
    if lease and lease > now.strftime("%Y-%m-%dT%H:%M:%SZ"):
        return {"ran": False, "reason": "another tick holds the lease until %s" % lease}
    state["lease_until"] = (now + LEASE).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["lease_owner"] = uuid.uuid4().hex
    try:
        token = store.save(CURSOR, state, token)
    except state_store.Conflict:
        # Lost the race for the lease to a tick that loaded the same state.
        # Nothing has been started, so this is a clean no-op, not a failure -
        # measured: at a one-minute cadence two ticks start within seconds.
        return {"ran": False, "reason": "lost the lease race"}

    import agent_waker as aw
    owner = state["lease_owner"]
    wm = aw.parse_ts(state["watermark"])
    since = (wm - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = read_since(since)
    # Completion is read from each route's cursor, not from the fact that
    # an earlier tick managed to start the job. A capped pass that answered
    # three of four leaves the fourth pending, and the next tick starts
    # that route again.
    pending, done_rows = reconcile(state.get("pending") or {}, store)
    seen_ids = [str(r) for r in (state.get("seen") or []) if r]
    for rid in done_rows:
        if rid not in seen_ids:
            seen_ids.append(rid)
    wanted, fresh, newest, errors = plan(rows, set(seen_ids), routes)

    def add_pending(job, hits):
        items = list(pending.get(job) or [])
        have = {item["row_id"] for item in items}
        for _ts, rid, wid in hits:
            if rid in have:
                continue
            items.append({"row_id": rid, "work_id": wid or rid})
            have.add(rid)
        if items:
            pending[job] = items[-SEEN_CAP:]

    def still_owns() -> bool:
        # The lease was taken above. A slow read can outlive it, and the
        # tick that then acquires it is the one allowed to start jobs.
        current, _gen = store.load(CURSOR)
        return (current.get("lease_owner") or "") == owner

    started, skipped, failed = {}, {}, {}
    hold = None  # earliest row whose job could not be started this tick
    for ts, _msgs in errors.values():
        hold = ts if hold is None else min(hold, ts)
    order = list(wanted)
    for job in pending:
        if job not in wanted:
            order.append(job)
    lost = False
    for job in order:
        hits = wanted.get(job) or []
        earliest = min((t for t, _rid, _wid in hits), default=None)
        try:
            already = running(job)
        except Exception as exc:  # noqa: BLE001
            # Unknown is not idle. Starting here could be a second copy, and
            # the error belongs to this route: pending stays, later routes
            # still run, and the cursor save below still happens.
            failed[job] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            if earliest is not None:
                hold = earliest if hold is None else min(hold, earliest)
            add_pending(job, hits)
            continue
        if already:
            # The backoff: one copy of the job. Pending stays, so the tick
            # after it finishes still has the work.
            skipped[job] = len(hits) or len(pending.get(job) or [])
            if earliest is not None:
                hold = earliest if hold is None else min(hold, earliest)
            add_pending(job, hits)
            continue
        if not hits and not pending.get(job):
            continue
        if not still_owns():
            lost = True
            break
        try:
            started[job] = start(job)
        except Exception as exc:  # noqa: BLE001
            failed[job] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            if earliest is not None:
                hold = earliest if hold is None else min(hold, earliest)
        # Started, skipped, failed, or unread: the row is pending until the
        # job's own cursor says so. Recording it here is what lets the next
        # tick retry a capped pass after the watermark has moved on.
        add_pending(job, hits)

    if lost:
        # The other tick owns the cursor. Writing ours back would either
        # lose the compare-and-swap or, worse, start from a stale read.
        return {"ran": True, "reason": "lease moved before start",
                "since": since, "rows": len(rows), "new": len(fresh),
                "started": started, "skipped_running": skipped, "failed": failed,
                "routed": {job: [rid for _ts, rid, _wid in hits]
                           for job, hits in wanted.items()}}

    # Rows whose job did not start stay OUT of seen, and so does every row
    # we did start: a start is not a completion. Unrouted rows are remembered
    # so the overlap does not offer them again.
    routed_ids = {rid for hits in wanted.values() for _ts, rid, _wid in hits}
    held_ids = {rid for job in list(skipped) + list(failed) for _ts, rid, _wid in wanted.get(job) or []}
    held_ids |= set(errors)
    new_seen = list(seen_ids)
    have = set(new_seen)
    for rid in fresh:
        if rid in held_ids or rid in routed_ids or rid in have:
            continue
        new_seen.append(rid)
        have.add(rid)
    new_seen = new_seen[-SEEN_CAP:]

    target = newest if newest and newest > wm else wm
    if hold is not None:
        target = min(target, hold - timedelta(seconds=1))
    target = max(target, wm)  # never backwards

    new_state = {"watermark": target.strftime("%Y-%m-%dT%H:%M:%SZ"), "seen": new_seen,
                 "pending": pending,
                 "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    store.save(CURSOR, new_state, token)
    return {"ran": True, "since": since, "rows": len(rows), "new": len(fresh),
            "started": started, "skipped_running": skipped, "failed": failed,
            "routed": {job: [rid for _ts, rid, _wid in hits] for job, hits in wanted.items()},
            "route_errors": {rid: msgs for rid, (_, msgs) in errors.items()},
            "watermark": new_state["watermark"]}


def main() -> int:
    uri = (os.environ.get("BLACKBOARD_STATE_URI") or "").strip()
    if not uri.startswith("gs://"):
        print(json.dumps({"ran": False, "refused": "BLACKBOARD_STATE_URI must be gs://"}))
        return 2
    import agent_waker as aw
    store = state_store.open_store(uri)
    env = aw.load_env()
    tok = _token()
    out = tick(store,
               read_since=lambda since: aw.read_since(env, since)["rows"],
               routes=_routes(),
               running=lambda job: job_running(job, tok),
               start=lambda job: start_job(job, tok))
    print(json.dumps(out, sort_keys=True))
    if out.get("refused"):
        return 2
    return 1 if out.get("failed") else 0  # a tick that yielded the lease is fine


if __name__ == "__main__":
    try:
        sys.exit(main())
    except state_store.Conflict as e:
        print(json.dumps({"ran": True, "conflict": str(e)}))
        sys.exit(3)
