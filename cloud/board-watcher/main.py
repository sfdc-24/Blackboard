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
  - It never starts a job that is already running. Two copies of a job answer
    or send the same row twice - their cursors are compare-and-swap, which
    catches the second writer only AFTER it has already posted. So a running
    job is skipped, and the watermark is HELD before the rows it was needed
    for: the next tick tries again rather than forgetting them.
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

    return {
        "gemini-waker": for_agent("gemini"),
        "wa-outbox": lambda row: ob.parse_wa_request(row) is not None,
    }


def plan(rows, seen, routes):
    """Which jobs to start, for which rows. Pure: no IO.

    Returns ({job: [(ts, row_id), ...]}, new_seen_ids, newest_ts).
    """
    import agent_waker as aw
    wanted, fresh, newest = {}, [], None
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
            except Exception:  # noqa: BLE001 - one odd row must not stop the tick
                hit = False
            if hit:
                wanted.setdefault(job, []).append((ts, rid))
    return wanted, fresh, newest


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
    try:
        token = store.save(CURSOR, state, token)
    except state_store.Conflict:
        # Lost the race for the lease to a tick that loaded the same state.
        # Nothing has been started, so this is a clean no-op, not a failure -
        # measured: at a one-minute cadence two ticks start within seconds.
        return {"ran": False, "reason": "lost the lease race"}

    import agent_waker as aw
    wm = aw.parse_ts(state["watermark"])
    since = (wm - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = read_since(since)
    seen = set(state.get("seen") or [])
    wanted, fresh, newest = plan(rows, seen, routes)

    started, skipped, failed = {}, {}, {}
    hold = None  # earliest row whose job could not be started this tick
    for job, hits in wanted.items():
        earliest = min(t for t, _ in hits)
        try:
            if running(job):
                skipped[job] = len(hits)
                hold = earliest if hold is None else min(hold, earliest)
                continue
            started[job] = start(job)
        except Exception as exc:  # noqa: BLE001
            failed[job] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            hold = earliest if hold is None else min(hold, earliest)

    # Rows whose job did not start stay OUT of seen, so the next tick routes
    # them again; everything else is remembered.
    held_ids = {rid for job in list(skipped) + list(failed) for _, rid in wanted[job]}
    new_seen = (list(state.get("seen") or []) + [r for r in fresh if r not in held_ids])[-SEEN_CAP:]

    target = newest if newest and newest > wm else wm
    if hold is not None:
        target = min(target, hold - timedelta(seconds=1))
    target = max(target, wm)  # never backwards

    new_state = {"watermark": target.strftime("%Y-%m-%dT%H:%M:%SZ"), "seen": new_seen,
                 "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    store.save(CURSOR, new_state, token)
    return {"ran": True, "since": since, "rows": len(rows), "new": len(fresh),
            "started": started, "skipped_running": skipped, "failed": failed,
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
