#!/usr/bin/env python3
"""blackboard-bus: self-hosted SFDC24 Blackboard bus.

A single-file, stdlib-only carrier for the Blackboard bus architecture,
deployable on any Ubuntu VM (target: Google Cloud). Two API surfaces:

  v1-compatible  read / append / time (+ bare-GET health), matching the
                 Apps Script bus contract so existing clients port by
                 changing BUS_URL - plus the fleet's filed fixes:
                   - REPLACE action for doc bodies (D-16 / ORDER 026)
                   - read supports limit (tail) - GEM-GROUND-001 follow-up
                   - schema-aware sheet append: rejects mis-shaped rows
                     instead of silently shifting columns (REQ-B4TQX9),
                     rejects appends that write nothing (REQ-V8QD7R),
                     rejects doc/sheet payload-shape mismatch (REQ-C4NDX7)
                   - success bodies echo bytes/cells actually written
                   - HONEST HTTP STATUS CODES: 401 is a real 401, 400 a
                     real 400 (the _httpStatus body field is kept for
                     transitional client parity)

  v2 ledger      the LEDGER SCHEMA v1 event ledger (PROPOSED, not yet
                 ratified - shipping it does not adopt it): actions
                 event / inbox / work with the documented rejection
                 rules, closed vocabularies, lease arithmetic, and 409
                 on conflicting claims.

Secrets come ONLY from the environment (BUS_SECRET, optional
BUS_PREVIOUS_SECRET during rotation windows) per fleet rule D-18. The
secret is never logged and never appears in any response.
"""

import argparse
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.1"
SERVICE = "sfdc24-blackboard-bus"

# ---- closed vocabularies (LEDGER SCHEMA v1, sections 1 and 5) ----
EVENT_TYPES = {"CREATE", "CLAIM", "PROGRESS", "BLOCK", "RELEASE",
               "COMPLETE", "CANCEL", "NOTE", "FINDING"}
STATUSES = {"OPEN", "CLAIMED", "RUNNING", "BLOCKED", "REVIEW", "DONE", "CANCELLED"}
PROJECTS = {"Blackboard", "Zoom Agent", "X-Ray", "Access Haiti", "Akatia", "Sales"}
MAX_LEASE_HOURS = 4

ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
HUMAN_DATE = re.compile(
    r"^(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}")

WRITE_LOCK = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None if it
    does not actually parse (the regex alone accepts impossible offsets)."""
    if not isinstance(value, str) or not ISO_TS.match(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def human_time():
    """Best-effort America/New_York human timestamp for v1 'time' parity."""
    try:
        from zoneinfo import ZoneInfo
        local = datetime.now(ZoneInfo("America/New_York"))
        return local.strftime("%A, %B %-d, %Y at %-I:%M %p %Z") if os.name != "nt" \
            else local.strftime("%A, %B %d, %Y at %I:%M %p %Z")
    except Exception:
        return datetime.now(timezone.utc).strftime("%A, %B %d, %Y at %H:%M UTC")


# ---------------------------------------------------------------- storage

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
  file_id    TEXT PRIMARY KEY,
  title      TEXT UNIQUE NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('doc','sheet')),
  created_ts TEXT NOT NULL,
  updated_ts TEXT NOT NULL,
  revision   INTEGER NOT NULL DEFAULT 1,
  body       TEXT,
  header     TEXT
);
CREATE TABLE IF NOT EXISTS sheet_rows (
  file_id     TEXT NOT NULL,
  n           INTEGER NOT NULL,
  row         TEXT NOT NULL,
  appended_ts TEXT NOT NULL,
  PRIMARY KEY (file_id, n)
);
CREATE TABLE IF NOT EXISTS events (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT UNIQUE NOT NULL,
  event_ts       TEXT NOT NULL,
  work_id        TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  actor_tag      TEXT NOT NULL,
  assigned_to    TEXT,
  status         TEXT NOT NULL,
  lease_until    TEXT,
  payload        TEXT NOT NULL,
  evidence_ref   TEXT,
  parent_work_id TEXT,
  project        TEXT,
  schema_v       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_work ON events (work_id, seq);
"""


def db_connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


class BusError(Exception):
    """API error with an honest HTTP status code."""

    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


class Store:
    """Thread-safe store: one SQLite connection PER THREAD over WAL.

    A single connection shared across the ThreadingHTTPServer's threads is
    not safe - concurrent statement execution corrupts cursor state
    (InterfaceError) and makes reads flicker. WAL gives every thread's
    connection snapshot reads; WRITE_LOCK keeps writes serialized."""

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self.conn.execute("SELECT 1")  # initialize schema eagerly on the main thread

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = db_connect(self.path)
            self._local.conn = c
        return c

    # -------- files (v1 surface) --------

    def get_file(self, title=None, file_id=None):
        cur = self.conn.execute(
            "SELECT file_id, title, kind, revision, body, header FROM files WHERE title = ? OR file_id = ?",
            (title, file_id))
        row = cur.fetchone()
        if not row:
            ref = title or file_id or "(none)"
            raise BusError(404, f"No file titled or id'd {ref!r}. Use action=create first, or action=list.")
        return {"file_id": row[0], "title": row[1], "kind": row[2],
                "revision": row[3], "body": row[4],
                "header": json.loads(row[5]) if row[5] else None}

    def create_file(self, title, kind, header=None):
        with WRITE_LOCK, self.conn:
            return self._create_file(title, kind, header)

    def _create_file(self, title, kind, header=None):
        """Insert a file within the caller's write lock and transaction."""
        if not title or not str(title).strip():
            raise BusError(400, "create requires a non-empty title.")
        if kind not in ("doc", "sheet"):
            raise BusError(400, f"create kind must be 'doc' or 'sheet', got {kind!r}.")
        if kind == "sheet":
            if not isinstance(header, list) or not header or not all(isinstance(c, str) and c.strip() for c in header):
                raise BusError(400, "create kind=sheet requires header: a non-empty list of non-empty column names.")
            if len(header) < 3:
                raise BusError(400, f"sheet header needs at least 3 columns ({header[0] if header else 'id'}, "
                                    "timestamp, and content) - got " + str(len(header)) + ".")
        fid = str(uuid.uuid4())
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO files (file_id, title, kind, created_ts, updated_ts, revision, body, header) "
                "VALUES (?,?,?,?,?,1,?,?)",
                (fid, title, kind, ts, ts, "" if kind == "doc" else None,
                 json.dumps(header) if header else None))
        except sqlite3.IntegrityError:
            raise BusError(409, f"A file titled {title!r} already exists.")
        return {"fileId": fid, "title": title, "kind": kind}

    def list_files(self):
        out = []
        for fid, title, kind, rev, updated in self.conn.execute(
                "SELECT file_id, title, kind, revision, updated_ts FROM files ORDER BY title"):
            entry = {"fileId": fid, "title": title, "kind": kind,
                     "revision": rev, "updated": updated}
            if kind == "sheet":
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM sheet_rows WHERE file_id = ?", (fid,)).fetchone()[0]
                entry["rows"] = n
            out.append(entry)
        return out

    def read_file(self, title=None, file_id=None, limit=None):
        f = self.get_file(title, file_id)
        if f["kind"] == "doc":
            return {"fileId": f["file_id"], "title": f["title"], "kind": "doc",
                    "revision": f["revision"], "body": f["body"] or ""}
        total = self.conn.execute(
            "SELECT COUNT(*) FROM sheet_rows WHERE file_id = ?", (f["file_id"],)).fetchone()[0]
        if limit is not None:
            if not isinstance(limit, int) or limit < 1:
                raise BusError(400, "limit must be a positive integer.")
            cur = self.conn.execute(
                "SELECT row FROM sheet_rows WHERE file_id = ? ORDER BY n DESC LIMIT ?",
                (f["file_id"], limit))
            data = [json.loads(r[0]) for r in cur][::-1]
        else:
            cur = self.conn.execute(
                "SELECT row FROM sheet_rows WHERE file_id = ? ORDER BY n", (f["file_id"],))
            data = [json.loads(r[0]) for r in cur]
        # v1 parity: rows[0] is the header row.
        return {"fileId": f["file_id"], "title": f["title"], "kind": "sheet",
                "total_rows": total, "rows": [f["header"]] + data}

    def append(self, title=None, file_id=None, text=None, sheet_row=None):
        """Schema-aware append. Exactly one of text / sheetRow, matching the
        target's kind. Rejects writes that would write nothing (REQ-V8QD7R),
        shape mismatches (REQ-C4NDX7), and short/long sheet rows that would
        shift columns (REQ-B4TQX9). Echoes what was actually written."""
        has_text = isinstance(text, str) and text.strip() != ""
        has_row = isinstance(sheet_row, list) and len(sheet_row) > 0
        if not has_text and not has_row:
            raise BusError(400, "append carries neither non-empty text nor sheetRow - nothing to write (REQ-V8QD7R).")
        if has_text and has_row:
            raise BusError(400, "append carries BOTH text and sheetRow - send exactly one.")
        f = self.get_file(title, file_id)

        if f["kind"] == "doc":
            if not has_text:
                raise BusError(400, f"{f['title']!r} is a doc; append needs text, not sheetRow (REQ-C4NDX7).")
            ts = now_iso()
            # Concatenate IN SQL under the lock: a read-modify-write in Python
            # would let two concurrent appends silently drop one (lost update).
            with WRITE_LOCK, self.conn:
                self.conn.execute(
                    "UPDATE files SET body = CASE WHEN body IS NULL OR body = '' THEN ? "
                    "ELSE body || char(10) || char(10) || ? END, "
                    "revision = revision + 1, updated_ts = ? WHERE file_id = ?",
                    (text, text, ts, f["file_id"]))
                revision = self.conn.execute(
                    "SELECT revision FROM files WHERE file_id = ?", (f["file_id"],)).fetchone()[0]
            return {"fileId": f["file_id"], "title": f["title"], "kind": "doc",
                    "bytes_written": len(text.encode("utf-8")),
                    "revision": revision}

        # sheet
        if not has_row:
            raise BusError(400, f"{f['title']!r} is a sheet; append needs sheetRow, not text (REQ-C4NDX7).")
        header = f["header"]
        hl = len(header)
        cells = ["" if c is None else str(c) for c in sheet_row]
        if len(cells) == hl - 2:
            # Server issues Row_ID and Timestamp - the schema-aware fix:
            # clients supply only content cells and columns can never shift.
            row_id = str(uuid.uuid4())
            cells = [row_id, now_iso()] + cells
        elif len(cells) == hl:
            c0, c1 = cells[0].strip(), cells[1].strip()
            if not c0:
                raise BusError(400, f"sheetRow col 0 ({header[0]}) is empty - supply an id or send {hl - 2} content cells.")
            if ISO_TS.match(c0) or HUMAN_DATE.match(c0):
                raise BusError(400, f"sheetRow col 0 ({header[0]}) looks like a timestamp - this is the "
                                    f"column-shift defect REQ-B4TQX9; send {hl - 2} content cells and let the "
                                    "server issue Row_ID and Timestamp.")
            if not ISO_TS.match(c1):
                raise BusError(400, f"sheetRow col 1 ({header[1]}) must be ISO-8601, got {c1[:40]!r}.")
            row_id = c0
        else:
            raise BusError(400, f"sheetRow has {len(cells)} cells; sheet {f['title']!r} needs {hl} "
                                f"(or {hl - 2} content cells with server-issued {header[0]}/{header[1]}).")
        if not any(c.strip() for c in cells[2:]):
            raise BusError(400, "sheetRow content cells are all empty - nothing to write (REQ-V8QD7R).")
        ts = now_iso()
        with WRITE_LOCK, self.conn:
            n = self.conn.execute(
                "SELECT COALESCE(MAX(n), 0) + 1 FROM sheet_rows WHERE file_id = ?",
                (f["file_id"],)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO sheet_rows (file_id, n, row, appended_ts) VALUES (?,?,?,?)",
                (f["file_id"], n, json.dumps(cells), ts))
            self.conn.execute(
                "UPDATE files SET revision = revision + 1, updated_ts = ? WHERE file_id = ?",
                (ts, f["file_id"]))
        return {"fileId": f["file_id"], "title": f["title"], "kind": "sheet",
                "row_id": row_id, "row_n": n, "cells_written": len(cells),
                "bytes_written": sum(len(c.encode("utf-8")) for c in cells)}

    def replace(self, title=None, file_id=None, body=None):
        """REPLACE a doc body in full - the D-16 / ORDER 026 action the
        Apps Script bus never had. Docs only: sheets are append-only ledgers."""
        if not isinstance(body, str) or body.strip() == "":
            raise BusError(400, "replace requires a non-empty body.")
        f = self.get_file(title, file_id)
        if f["kind"] != "doc":
            raise BusError(400, f"{f['title']!r} is a sheet; replace works on docs only - sheets are append-only.")
        ts = now_iso()
        with WRITE_LOCK, self.conn:
            self.conn.execute(
                "UPDATE files SET body = ?, revision = revision + 1, updated_ts = ? WHERE file_id = ?",
                (body, ts, f["file_id"]))
            revision = self.conn.execute(
                "SELECT revision FROM files WHERE file_id = ?", (f["file_id"],)).fetchone()[0]
        return {"fileId": f["file_id"], "title": f["title"], "kind": "doc",
                "bytes_written": len(body.encode("utf-8")),
                "revision": revision}

    # -------- events (v2 ledger, LEDGER SCHEMA v1 - PROPOSED) --------

    def latest_event(self, work_id):
        cur = self.conn.execute(
            "SELECT seq, event_type, actor_tag, status, lease_until, assigned_to "
            "FROM events WHERE work_id = ? ORDER BY seq DESC LIMIT 1", (work_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"seq": row[0], "event_type": row[1], "actor_tag": row[2],
                "status": row[3], "lease_until": row[4], "assigned_to": row[5]}

    def work_hold(self, work_id, latest=None):
        """Effective hold on a work item. The latest event alone is not enough:
        a PROGRESS without lease_until, or a NOTE by a non-holder, must not
        erase the hold. Holder = actor of the most recent CLAIM; lease = most
        recent non-null lease_until. A hold exists only while the LATEST
        status is CLAIMED/RUNNING (reduction rule) and the lease is live."""
        latest = latest or self.latest_event(work_id)
        if not latest or latest["status"] not in ("CLAIMED", "RUNNING"):
            return None
        got = self.conn.execute(
            "SELECT actor_tag FROM events WHERE work_id = ? AND event_type = 'CLAIM' "
            "ORDER BY seq DESC LIMIT 1", (work_id,)).fetchone()
        holder = got[0] if got else latest["actor_tag"]
        got = self.conn.execute(
            "SELECT lease_until FROM events WHERE work_id = ? AND lease_until IS NOT NULL "
            "ORDER BY seq DESC LIMIT 1", (work_id,)).fetchone()
        lease = parse_iso(got[0]) if got else None
        return {"holder": holder, "lease_until": got[0] if got else None, "lease_dt": lease}

    def add_event(self, body):
        # Rejection rules, LEDGER SCHEMA v1 section 4: fail loudly, never default.
        payload = body.get("payload")
        if not isinstance(payload, str) or payload.strip() == "":
            raise BusError(400, "payload absent, empty, or whitespace only.")
        actor = body.get("actor_tag")
        if not isinstance(actor, str) or not actor.strip():
            raise BusError(400, "actor_tag absent - an unattributable event is refused, not stored.")
        etype = body.get("event_type")
        if etype not in EVENT_TYPES:
            raise BusError(400, f"event_type {etype!r} outside the closed set {sorted(EVENT_TYPES)}.")
        status = body.get("status")
        if status not in STATUSES:
            raise BusError(400, f"status {status!r} outside the closed set {sorted(STATUSES)}.")
        work_id = body.get("work_id")
        if not isinstance(work_id, str) or not work_id.strip():
            raise BusError(400, "work_id absent.")
        project = body.get("project")
        if project is not None and project not in PROJECTS:
            raise BusError(400, f"project {project!r} outside the controlled list {sorted(PROJECTS)}.")
        assigned_to = body.get("assigned_to")
        if etype == "CREATE" and (not isinstance(assigned_to, str) or not assigned_to.strip()):
            raise BusError(400, "CREATE with no assigned_to.")
        for field in ("assigned_to", "evidence_ref", "parent_work_id"):
            val = body.get(field)
            if val is not None and not isinstance(val, str):
                raise BusError(400, f"{field} must be a string when present.")
        lease_until = body.get("lease_until")
        now = datetime.now(timezone.utc)
        if etype == "CLAIM":
            lease_dt = parse_iso(lease_until) if isinstance(lease_until, str) else None
            if lease_dt is None:
                raise BusError(400, "CLAIM with no parseable lease_until (ISO-8601 UTC required).")
            if lease_dt > now + timedelta(hours=MAX_LEASE_HOURS):
                raise BusError(400, f"lease_until more than {MAX_LEASE_HOURS} hours out.")
        elif lease_until is not None and parse_iso(str(lease_until)) is None:
            raise BusError(400, "lease_until must be parseable ISO-8601 UTC when present.")

        with WRITE_LOCK, self.conn:
            latest = self.latest_event(work_id)
            if etype == "CREATE":
                if latest is not None:
                    raise BusError(409, f"work_id {work_id!r} already exists (latest seq {latest['seq']}).")
            else:
                if latest is None:
                    raise BusError(400, f"work_id {work_id!r} has never been CREATEd.")
                if etype == "CLAIM":
                    hold = self.work_hold(work_id, latest)
                    if hold and hold["holder"] != actor and hold["lease_dt"] and hold["lease_dt"] > now:
                        raise BusError(409, f"work_id {work_id!r} is held by {hold['holder']!r} "
                                            f"until {hold['lease_until']} (live lease).",
                                       extra={"holder": hold["holder"],
                                              "lease_until": hold["lease_until"]})
            event_id = str(uuid.uuid4())
            ts = now_iso()
            cur = self.conn.execute(
                "INSERT INTO events (event_id, event_ts, work_id, event_type, actor_tag, assigned_to, "
                "status, lease_until, payload, evidence_ref, parent_work_id, project, schema_v) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (event_id, ts, work_id, etype, actor, assigned_to, status, lease_until,
                 payload, body.get("evidence_ref"), body.get("parent_work_id"), project))
            seq = cur.lastrowid
        # Success response echoes what was actually stored (LEDGER section 4).
        return {"event_id": event_id, "seq": seq, "work_id": work_id,
                "event_ts": ts, "payload_bytes": len(payload.encode("utf-8"))}

    def inbox(self, tag):
        """The wake query, LEDGER SCHEMA v1 section 2: for each work_id whose
        LATEST event is addressed to tag/ALL/ANY and actionable, return only
        that latest event."""
        if not isinstance(tag, str) or not tag.strip():
            raise BusError(400, "inbox requires tag.")
        cur = self.conn.execute(
            "SELECT e.seq, e.event_id, e.event_ts, e.work_id, e.event_type, e.actor_tag, "
            "e.assigned_to, e.status, e.lease_until, e.payload, e.project "
            "FROM events e JOIN (SELECT work_id, MAX(seq) AS mseq FROM events GROUP BY work_id) m "
            "ON e.work_id = m.work_id AND e.seq = m.mseq ORDER BY e.seq")
        out = []
        for r in cur:
            (seq, event_id, event_ts, work_id, etype, actor, assigned_to,
             status, lease_until, payload, project) = r
            if assigned_to is None:
                # assignment carries forward from the last event that set it
                # (CLAIM/PROGRESS etc. need not repeat assigned_to)
                got = self.conn.execute(
                    "SELECT assigned_to FROM events WHERE work_id = ? AND assigned_to IS NOT NULL "
                    "ORDER BY seq DESC LIMIT 1", (work_id,)).fetchone()
                assigned_to = got[0] if got else None
            if assigned_to not in (tag, "ALL", "ANY"):
                continue
            if status in ("OPEN", "BLOCKED"):
                actionable = True
            elif status in ("CLAIMED", "RUNNING"):
                # holder-aware: a NOTE by a non-holder must not hide the item
                # from its actual claimant
                hold = self.work_hold(work_id)
                actionable = bool(hold and hold["holder"] == tag)
            else:
                actionable = False
            if not actionable:
                continue
            out.append({"seq": seq, "event_id": event_id, "event_ts": event_ts,
                        "work_id": work_id, "event_type": etype, "actor_tag": actor,
                        "assigned_to": assigned_to, "status": status,
                        "lease_until": lease_until, "payload": payload, "project": project})
        return out

    def work_history(self, work_id):
        cur = self.conn.execute(
            "SELECT seq, event_id, event_ts, event_type, actor_tag, assigned_to, status, "
            "lease_until, payload, evidence_ref, parent_work_id, project "
            "FROM events WHERE work_id = ? ORDER BY seq", (work_id,))
        rows = [dict(zip(["seq", "event_id", "event_ts", "event_type", "actor_tag",
                          "assigned_to", "status", "lease_until", "payload",
                          "evidence_ref", "parent_work_id", "project"], r)) for r in cur]
        if not rows:
            raise BusError(404, f"work_id {work_id!r} has no events.")
        return rows


# ---------------------------------------------------------------- HTTP

def load_secrets():
    secrets = []
    for var in ("BUS_SECRET", "BUS_PREVIOUS_SECRET"):
        val = os.environ.get(var, "").strip()
        if val:
            secrets.append(val)
    return secrets


def secret_ok(supplied, secrets):
    if not isinstance(supplied, str) or not supplied:
        return False
    # compare as bytes: compare_digest raises TypeError on non-ASCII str,
    # which would turn a bad secret into a 500 instead of a 401
    supplied_b = supplied.encode("utf-8")
    return any(hmac.compare_digest(supplied_b, s.encode("utf-8")) for s in secrets)


class Handler(BaseHTTPRequestHandler):
    server_version = f"blackboard-bus/{VERSION}"
    timeout = 30      # socket timeout: a stalled client must not pin a thread forever
    store = None      # set at serve time
    secrets = None

    # never log request bodies (they carry the secret)
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, obj):
        obj.setdefault("_httpStatus", code)   # transitional parity with the v1 body field
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Bare-GET health, v1 parity: no secret, no data.
        self._send(200, {"ok": True, "service": SERVICE, "host": "self-hosted",
                         "version": VERSION, "time": human_time(), "iso": now_iso()})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise BusError(400, "Request body must be JSON.")
            if length > int(os.environ.get("BUS_MAX_BODY", 2 * 1024 * 1024)):
                raise BusError(413, "Request body too large.")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except Exception:
                raise BusError(400, "Request body must be JSON.")
            if not isinstance(body, dict):
                raise BusError(400, "Request body must be a JSON object.")

            if not secret_ok(body.get("secret"), self.secrets):
                raise BusError(401, "Bad or missing secret.")

            action = body.get("action")
            result = self.dispatch(action, body)
            result["ok"] = True
            self._send(200, result)
        except BusError as err:
            out = {"ok": False, "error": err.message}
            out.update(err.extra)
            self._send(err.code, out)
        except Exception as exc:  # honest 500, no traceback leak
            sys.stderr.write(f"ERROR: {type(exc).__name__}: {exc}\n")
            self._send(500, {"ok": False, "error": "Internal error."})

    def dispatch(self, action, body):
        s = self.store
        if action == "ping":
            return {"service": SERVICE, "version": VERSION}
        if action == "time":
            return {"time": human_time(), "iso": now_iso()}
        if action == "read":
            if not body.get("title") and not body.get("fileId"):
                raise BusError(400, "title or fileId is required.")
            return s.read_file(body.get("title"), body.get("fileId"), body.get("limit"))
        if action == "append":
            if not body.get("title") and not body.get("fileId"):
                raise BusError(400, "title or fileId is required.")
            return s.append(body.get("title"), body.get("fileId"),
                            body.get("text"), body.get("sheetRow"))
        if action == "replace":
            if not body.get("title") and not body.get("fileId"):
                raise BusError(400, "title or fileId is required.")
            return s.replace(body.get("title"), body.get("fileId"), body.get("body"))
        if action == "create":
            return s.create_file(body.get("title"), body.get("kind"), body.get("header"))
        if action == "list":
            return {"files": s.list_files()}
        # ---- v2 ledger (LEDGER SCHEMA v1 - PROPOSED, not ratified) ----
        if action == "event":
            return s.add_event(body)
        if action == "inbox":
            return {"tag": body.get("tag"), "items": s.inbox(body.get("tag"))}
        if action == "work":
            return {"work_id": body.get("work_id"),
                    "events": s.work_history(body.get("work_id"))}
        raise BusError(400, f"Unknown action: {action}")


def cmd_serve(args):
    secrets = load_secrets()
    if not secrets:
        sys.exit("REFUSING TO START: BUS_SECRET is not set. Set it in the environment "
                 "(systemd: /etc/blackboard-bus/env). Never hardcode it (D-18).")
    Handler.store = Store(args.db)
    Handler.secrets = secrets
    bind = os.environ.get("BUS_BIND", "127.0.0.1")
    port = int(os.environ.get("BUS_PORT", "8787"))
    httpd = ThreadingHTTPServer((bind, port), Handler)
    sys.stderr.write(f"{SERVICE} {VERSION} serving on {bind}:{port}, db={args.db}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def _rows_from_dump(data):
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) < 1 or not isinstance(rows[0], list):
        sys.exit("Dump does not look like a bus read response: need rows[0] as a header list.")
    return rows


def _import_rows(store, title, rows):
    header = [str(c) for c in rows[0]]
    try:
        store.get_file(title=title)
        sys.exit(f"A file titled {title!r} already exists - refusing to double-import.")
    except BusError:
        pass
    ts = now_iso()
    # The destination and its history are one commit. A failed import must not
    # leave an empty title behind that prevents a corrected retry.
    with WRITE_LOCK, store.conn:
        created = store._create_file(title, "sheet", header)
        fid = created["fileId"]
        for i, row in enumerate(rows[1:], start=1):
            if not isinstance(row, list):
                raise BusError(400, f"Import data row {i} must be a list of cells.")
            cells = ["" if c is None else str(c) for c in row]
            # preserve the board verbatim - imports are history, not new writes
            store.conn.execute(
                "INSERT INTO sheet_rows (file_id, n, row, appended_ts) VALUES (?,?,?,?)",
                (fid, i, json.dumps(cells), ts))
        store.conn.execute("UPDATE files SET updated_ts = ? WHERE file_id = ?", (ts, fid))
    print(f"Imported {len(rows) - 1} rows into sheet {title!r} (header: {len(header)} columns).")


def cmd_import_file(args):
    with open(args.path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    title = args.title or (data.get("title") if isinstance(data, dict) else None)
    if not title:
        sys.exit("No title in the dump; pass --title.")
    _import_rows(Store(args.db), title, _rows_from_dump(data))


def cmd_import_bus(args):
    url = os.environ.get("BUS_URL", "").strip()
    secret = os.environ.get("BUS_SECRET_SOURCE", os.environ.get("BUS_SECRET", "")).strip()
    if not url or not secret:
        sys.exit("import-bus needs BUS_URL and BUS_SECRET_SOURCE (or BUS_SECRET) in the environment.")
    body = json.dumps({"action": "read", "title": args.title, "secret": secret}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303, 307):
            raise
        location = err.headers.get("Location")
        # Apps Script echo layer flakes intermittently - one retry is the rule.
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(location, timeout=120) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError:
                if attempt == 2:
                    raise
    if not data.get("ok"):
        sys.exit(f"source bus said: {data.get('error')}")
    _import_rows(Store(args.db), args.title, _rows_from_dump(data))


def cmd_export(args):
    store = Store(args.db)
    print(json.dumps(store.read_file(title=args.title), indent=None))


def main():
    ap = argparse.ArgumentParser(prog="blackboard-bus", description=__doc__)
    ap.add_argument("--db", default=os.environ.get("BUS_DB", "./bus.db"),
                    help="SQLite database path (env BUS_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the bus HTTP service")
    p = sub.add_parser("import-file", help="import a saved bus read dump (JSON) as a sheet")
    p.add_argument("path")
    p.add_argument("--title", default=None)
    p = sub.add_parser("import-bus", help="pull a sheet from the live v1 bus (BUS_URL + BUS_SECRET_SOURCE env)")
    p.add_argument("--title", default="Blackboard - Alpha DB")
    p = sub.add_parser("export", help="print a file as JSON")
    p.add_argument("--title", required=True)
    args = ap.parse_args()
    {"serve": cmd_serve, "import-file": cmd_import_file,
     "import-bus": cmd_import_bus, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
