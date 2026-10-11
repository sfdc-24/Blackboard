#!/usr/bin/env python3
"""The hourly Redis archive and its LEDGER: the record that outlives the instance. Default OFF.

WHY THIS EXISTS
    Mr. Salam approved D5 of docs/REDIS-PRIMARY.md on 2026-10-11: archive the Redis streams to GCS every
    hour. redis-central is standalone Memorystore for Redis: RDB snapshots every ONE_HOUR (read back the
    same day), no AOF, and a snapshot is restorable only by Google's automated recovery. Once Redis is the
    fleet's conversation, a whole-instance loss would take up to an hour of it with no copy we can restore.
    This job is that copy, and gov:audit - the record of every governed op, which lives in the very Redis
    it audits - is archived with it.

WHAT ONE RUN DOES
    1. Takes gov:archive:lock (SET NX, TTL): one archiver at a time, the PR 390 lesson.
    2. For every stream it archives (ARCHIVE_PATTERNS, ARCHIVE_KEYS), reads the entries AFTER the stream's
       archive cursor (gov:archive:cursor, a hash under gov:, which no agent may write), oldest first.
    3. Writes them as JSON lines to gs://<bucket>/<prefix>/<date>/<hour>/<key>/<first>_<last>.jsonl,
       CREATE-ONLY (ifGenerationMatch=0). An archived hour is never rewritten.
    4. Only then moves the cursor. A crash between the two re-writes the same range next run: the object
       name is the range, so it already exists and the run moves on. At-least-once, never lost.
    5. Writes the LEDGER - per stream: entries archived, newest id, length, the object and its SHA-256,
       and for an inbox the unread count after its reader's cursor (fleet:cursor:<agent>) - as one
       create-only JSON object, and ONE board row that names it and carries its hash. Nobody is woken:
       the row is addressed to `ledger`, which is no agent.

WHAT IT NEVER DOES
    No write outside gov:archive:* in Redis; no delete, no trim; no object overwritten; no message text on
    the board (the row carries counts, ids and hashes, never an entry). Off unless REDIS_ARCHIVE_ENABLED=on;
    REDIS_ARCHIVE_DRY_RUN=1 reads and reports and writes nothing anywhere.

    python -B redis_archive.py            # one run (the hourly job)
    python -B redis_archive.py verify <ledger object>   # every object the ledger names, re-hashed
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (HERE, os.path.join(HERE, "..", "..", "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("redis-archive")

# What is archived. Streams only: a stream entry is immutable once written, so "after the cursor" is
# exact. conf:side:* is the room; fleet:inbox:* the agents' inboxes; gov:audit the record of every op.
ARCHIVE_PATTERNS = ("conf:side:*", "fleet:inbox:*")
ARCHIVE_KEYS = ("gov:audit",)

LOCK_KEY = "gov:archive:lock"
LOCK_SECONDS = 900                       # longer than any run; a dead run frees it within 15 minutes
CURSOR_KEY = "gov:archive:cursor"        # hash: stream key -> last archived entry id
READER_CURSOR = "fleet:cursor:%s"        # an inbox reader's own cursor (REDIS-PRIMARY 5.1); may not exist
BATCH = 1000
MAX_PER_STREAM = 50000                   # per run; the rest is the next hour's, and the ledger says so
UNREAD_CAP = 1000

DEFAULT_BUCKET = "sfdc24-fleet-state"
DEFAULT_PREFIX = "redis-archive"
LEDGER_TARGET = "ledger"                 # no agent has this tag, so the row wakes nobody
WORKER_TAG = "bus-reconciler"
ROW_LIMIT = 1400


class Exists(Exception):
    """The create-only object is already there: this exact range was archived by an earlier run."""


def text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ("" if value is None else str(value))


def safe_key(key: str) -> str:
    """A stream key as one path segment. Keys are ours (the patterns above), but a path is not a key."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", key.replace(":", "__"))[:200]


def safe(value) -> str:
    """No pipe, newline or control character in a published value (the board payload has no escaping)."""
    return re.sub(r"[|\r\n\t\x00-\x1f]", " ", text(value))


def streams(conn) -> list:
    """Every key to archive that is a stream right now, sorted, each once."""
    found = set(ARCHIVE_KEYS)
    for pattern in ARCHIVE_PATTERNS:
        for key in conn.scan_iter(match=pattern, count=500):
            found.add(text(key))
    return sorted(k for k in found if text(conn.type(k)) == "stream")


def read_after(conn, key: str, cursor: str, limit: int) -> list:
    """Entries strictly after `cursor`, oldest first, at most `limit`, as (id, {field: value})."""
    out, after = [], cursor
    while len(out) < limit:
        page = conn.xrange(key, min="(" + after, max="+", count=min(BATCH, limit - len(out))) or []
        if not page:
            break
        for entry_id, fields in page:
            out.append((text(entry_id), {text(k): text(v) for k, v in (fields or {}).items()}))
        after = out[-1][0]
        if len(page) < BATCH:
            break
    return out


def unread(conn, key: str):
    """For fleet:inbox:<agent>, how many entries are after the agent's own read cursor. None: no cursor."""
    if not key.startswith("fleet:inbox:"):
        return None
    cursor = text(conn.get(READER_CURSOR % key[len("fleet:inbox:"):]))
    if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", cursor):
        return None
    return len(conn.xrange(key, min="(" + cursor, max="+", count=UNREAD_CAP + 1) or [])


def archive_stream(conn, key: str, put, folder: str, dry_run=False) -> dict:
    cursor = text(conn.hget(CURSOR_KEY, key)) or "0-0"
    entries = read_after(conn, key, cursor, MAX_PER_STREAM)
    row = {"key": key, "new": len(entries), "after": cursor, "length": int(conn.xlen(key) or 0),
           "unread": unread(conn, key)}
    if not entries:
        row["last"] = cursor
        return row
    first, last = entries[0][0], entries[-1][0]
    body = "".join(json.dumps({"id": i, "fields": f}, sort_keys=True, ensure_ascii=False) + "\n"
                   for i, f in entries).encode("utf-8")
    name = "%s/%s/%s_%s.jsonl" % (folder, safe_key(key), first, last)
    row.update(last=last, object=name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
               more=len(entries) >= MAX_PER_STREAM)
    if dry_run:
        row["written"] = "dry-run"
        return row
    try:
        put(name, body, "application/x-ndjson")
        row["written"] = "created"
    except Exists:
        row["written"] = "exists"            # an earlier run wrote this exact range and died before the cursor
    conn.hset(CURSOR_KEY, key, last)         # ONLY after the object is there
    return row


def ledger_row(ledger: dict, ledger_name: str, ledger_sha: str, bucket: str) -> dict:
    """The one board row: counts, ids and the ledger's hash. Never an entry's text."""
    inboxes = [s for s in ledger["streams"] if s["key"].startswith("fleet:inbox:")]
    unread_text = ",".join("%s:%s" % (s["key"][len("fleet:inbox:"):], "-" if s["unread"] is None else s["unread"])
                           for s in inboxes)
    row_id = safe("BUS-RECONCILER-LEDGER-%s" % ledger["run"])[:120]
    text_part = safe("hour=%s streams=%d new=%d objects=%d more=%s ledger=gs://%s/%s sha256=%s unread=%s" % (
        ledger["hour"], len(ledger["streams"]), ledger["totals"]["new"], ledger["totals"]["objects"],
        ",".join(s["key"] for s in ledger["streams"] if s.get("more")) or "none",
        bucket, ledger_name, ledger_sha, unread_text or "none"))
    payload = "BCB|v=1|id=%s|phase=LEDGER|from=%s|to=%s|evidence=MEASURED|text=%s" % (
        row_id, WORKER_TAG, LEDGER_TARGET, text_part)
    if len(payload) > ROW_LIMIT:              # the ledger object holds everything; the row only points at it
        payload = payload[:ROW_LIMIT - 15] + " (see ledger)"
    return {"row_id": row_id, "source_tag": WORKER_TAG, "target_surface": LEDGER_TARGET,
            "action_type": "LEDGER", "category": "DONE", "project_tag": "Blackboard",
            "gist": safe("Redis archive %s: %d new entries in %d streams" % (
                ledger["hour"], ledger["totals"]["new"], len(ledger["streams"]))),
            "payload": payload}


def run(conn, put, append=None, now=None, run_id="", dry_run=False, prefix=DEFAULT_PREFIX,
        bucket=DEFAULT_BUCKET) -> dict:
    """One archive pass. `put(name, body, content_type)` writes create-only and raises Exists;
    `append(spec)` posts one board row and reads it back (scripts/append.py's contract)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    run_id = safe(run_id or now.strftime("%Y%m%dT%H%M%SZ")).replace(" ", "")[:64]
    if not conn.set(LOCK_KEY, run_id, nx=True, ex=LOCK_SECONDS):
        return {"status": "locked", "holder": text(conn.get(LOCK_KEY))}
    try:
        folder = "%s/%s/%s" % (prefix, now.strftime("%Y-%m-%d"), now.strftime("%H"))
        rows = [archive_stream(conn, key, put, folder, dry_run=dry_run) for key in streams(conn)]
        # A cursor for a stream that no longer exists (a conf:side: day past its TTL) is dropped, so the
        # hash does not grow forever. Its last archive object stays where it is.
        live = {r["key"] for r in rows}
        stale = [k for k in (text(k) for k in (conn.hkeys(CURSOR_KEY) or [])) if k not in live]
        if stale and not dry_run:
            conn.hdel(CURSOR_KEY, *stale)
        ledger = {"run": run_id, "hour": now.strftime("%Y-%m-%dT%HZ"), "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "dry_run": bool(dry_run), "streams": rows, "dropped_cursors": stale,
                  "totals": {"new": sum(r["new"] for r in rows),
                             "objects": sum(1 for r in rows if r.get("object"))}}
        body = (json.dumps(ledger, indent=1, sort_keys=True) + "\n").encode("utf-8")
        ledger_name = "%s/LEDGER-%s.json" % (folder, run_id)
        ledger_sha = hashlib.sha256(body).hexdigest()
        out = {"status": "dry-run" if dry_run else "ok", "ledger": ledger_name, "sha256": ledger_sha,
               "streams": len(rows), "new": ledger["totals"]["new"]}
        if dry_run:
            return out
        try:
            put(ledger_name, body, "application/json")
        except Exists:
            out["ledger_written"] = "exists"
        if append is not None:
            spec = ledger_row(ledger, ledger_name, ledger_sha, bucket)
            try:
                append(spec)
                out["row"] = spec["row_id"]
            except SystemExit as stop:      # append.py's unconfirmed read-back: the archive stands regardless
                out["row"] = "unconfirmed (exit %s)" % stop.code
        return out
    finally:
        if text(conn.get(LOCK_KEY)) == run_id:
            conn.delete(LOCK_KEY)


# ----------------------------------------------------------------------------------------------- GCS

def gcs_put(bucket: str, token_provider):
    """put(name, body, ctype): one create-only upload (ifGenerationMatch=0). 412 means it exists."""
    import urllib.request

    def put(name, body, ctype):
        url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=media&name=%s"
               "&ifGenerationMatch=0" % (bucket, urllib.parse.quote(name, safe="")))
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + token_provider()[0])
        req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except urllib.error.HTTPError as e:
            if e.code == 412:
                raise Exists(name)
            raise
    return put


def gcs_get(bucket: str, token_provider):
    import urllib.request

    def get(name):
        url = "https://storage.googleapis.com/storage/v1/b/%s/o/%s?alt=media" % (
            bucket, urllib.parse.quote(name, safe=""))
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token_provider()[0])
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    return get


def verify(get, ledger_name: str) -> dict:
    """Re-read the ledger and every object it names; each must hash to what the ledger says."""
    ledger = json.loads(get(ledger_name).decode("utf-8"))
    checked, bad = 0, []
    for s in ledger.get("streams", []):
        if not s.get("object"):
            continue
        checked += 1
        if hashlib.sha256(get(s["object"])).hexdigest() != s.get("sha256"):
            bad.append(s["object"])
    return {"ledger": ledger_name, "checked": checked, "mismatched": bad, "ok": not bad}


def main(argv=None) -> int:  # pragma: no cover - the job entry point
    argv = list(sys.argv[1:] if argv is None else argv)
    import state_store                                                  # noqa: PLC0415
    bucket = os.environ.get("REDIS_ARCHIVE_BUCKET", DEFAULT_BUCKET)
    prefix = os.environ.get("REDIS_ARCHIVE_PREFIX", DEFAULT_PREFIX).strip("/")
    if argv[:1] == ["verify"] and len(argv) == 2:
        out = verify(gcs_get(bucket, state_store.metadata_token), argv[1])
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    if os.environ.get("REDIS_ARCHIVE_ENABLED") != "on":
        print(json.dumps({"status": "disabled"}))
        return 0
    import redis_dual                                                   # noqa: PLC0415
    conn = redis_dual.client(redis_dual.Settings(), precheck=False)
    if conn is None:
        print(json.dumps({"status": "no_redis"}))
        return 1
    append = None
    if os.environ.get("REDIS_ARCHIVE_LEDGER_ROW", "on") == "on":
        import serve_requests                                           # noqa: PLC0415
        from bus import load_env                                        # noqa: PLC0415
        append = serve_requests.appender(load_env())
    out = run(conn, gcs_put(bucket, state_store.metadata_token), append=append,
              run_id=os.environ.get("CLOUD_RUN_EXECUTION", ""),
              dry_run=os.environ.get("REDIS_ARCHIVE_DRY_RUN") == "1", prefix=prefix, bucket=bucket)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] in ("ok", "dry-run", "locked") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
