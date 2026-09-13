#!/usr/bin/env python3
"""End-to-end tests for blackboard-bus. Stdlib only.

Starts a real server on a scratch port + DB, drives it over HTTP the way
fleet clients do, and checks the documented contract: v1 parity, the
schema-aware rejections (REQ-B4TQX9 / REQ-V8QD7R / REQ-C4NDX7), honest
status codes, write echoes, and the LEDGER SCHEMA v1 rules (400 naming
the field, 409 on a live conflicting claim, the inbox reduction).

Usage: python3 test_bus.py [path-to-bus_server.py]
Exit 0 = all pass.
"""

import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

SECRET = "test-secret-not-a-real-one"
PORT = int(os.environ.get("TEST_PORT", "18787"))
BASE = f"http://127.0.0.1:{PORT}/"

PASS, FAIL = 0, 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}  {detail}")


def call(body=None, method="POST", with_secret=True, raw=None, base=BASE):
    """Returns (http_status, parsed_json). Never raises on 4xx/5xx."""
    if method == "GET":
        req = urllib.request.Request(base)
    else:
        if raw is None:
            body = dict(body or {})
            if with_secret:
                body.setdefault("secret", SECRET)
            raw = json.dumps(body).encode()
        req = urllib.request.Request(base, data=raw,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.load(err)
        except Exception:
            return err.code, {}


BOARD_HEADER = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
                "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]


def test_import_atomicity(server_py, tmp):
    """Exercise the real import CLI and read committed state from fresh processes."""
    db = os.path.join(tmp, "migration.db")
    dump = os.path.join(tmp, "migration.json")
    header = ["Row_ID", "Timestamp", "Payload"]
    row = ["synthetic-1", "2026-09-12T12:00:00Z", "historical payload"]
    rows = [header, row]

    def import_rows(title, data):
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump({"title": title, "rows": data}, fh)
        return subprocess.run([sys.executable, server_py, "--db", db, "import-file", dump],
                              capture_output=True, text=True, timeout=15)

    def snapshot():
        with sqlite3.connect(db) as conn:
            return (conn.execute("SELECT * FROM files ORDER BY title").fetchall(),
                    conn.execute("SELECT * FROM sheet_rows ORDER BY file_id, n").fetchall())

    def exported_rows(title):
        result = subprocess.run([sys.executable, server_py, "--db", db, "export", "--title", title],
                                capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout).get("rows") if result.returncode == 0 else None

    print("== migration import atomicity ==")
    result = import_rows("Existing synthetic board", rows)
    check("import seeds existing destination", result.returncode == 0 and
          exported_rows("Existing synthetic board") == rows, result.stderr)
    for label, malformed in (("null", None), ("string", "not a row"), ("object", {"cell": "value"})):
        title = "Malformed " + label
        before = snapshot()
        result = import_rows(title, [header, row, malformed])
        check(label + " row rejected with location", result.returncode != 0 and
              "data row 2 must be a list" in result.stderr, result.stderr)
        check(label + " failure leaves no destination or data", snapshot() == before)
        result = import_rows(title, rows)
        check(label + " corrected retry succeeds", result.returncode == 0 and
              exported_rows(title) == rows, result.stderr)

    before = snapshot()
    result = import_rows("Existing synthetic board", [header, ["replacement", "ts", "do not overwrite"]])
    check("duplicate import refuses and preserves existing data", result.returncode != 0 and
          "refusing to double-import" in result.stderr and snapshot() == before, result.stderr)

    # This fails only after the destination and the first row have been inserted.
    # Prevalidation alone cannot pass: the database transaction must roll back.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER fail_second_import_row BEFORE INSERT ON sheet_rows "
                     "WHEN NEW.n = 2 BEGIN SELECT RAISE(ABORT, 'injected second-row failure'); END")
    before = snapshot()
    two_rows = rows + [["synthetic-2", "2026-09-12T12:01:00Z", "second payload"]]
    result = import_rows("Interrupted synthetic import", two_rows)
    check("injected mid-insert failure reached", result.returncode != 0 and
          "injected second-row failure" in result.stderr, result.stderr)
    check("mid-insert failure rolls back title and rows", snapshot() == before)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER fail_second_import_row")
    result = import_rows("Interrupted synthetic import", two_rows)
    check("retry after database failure succeeds", result.returncode == 0 and
          exported_rows("Interrupted synthetic import") == two_rows, result.stderr)


def test_complete_replay(server_py, tmp):
    """Real process restart and independent processes sharing one SQLite DB."""
    import concurrent.futures

    db = os.path.join(tmp, "replay.db")
    processes = []

    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        proc.stderr.close()

    def start():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}/"
        env = dict(os.environ, BUS_SECRET=SECRET, BUS_PORT=str(port),
                   BUS_BIND="127.0.0.1", BUS_DB=db)
        proc = subprocess.Popen([sys.executable, server_py, "--db", db, "serve"],
                                env=env, stderr=subprocess.PIPE)
        processes.append(proc)
        for _ in range(50):
            if proc.poll() is not None:
                raise RuntimeError("replay test server exited during startup")
            try:
                code, health = call(method="GET", base=base)
                if code == 200 and health.get("service") == "sfdc24-blackboard-bus":
                    return proc, base
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("replay test server never came up")

    def create(work_id, base):
        code, result = call({"action": "event", "work_id": work_id, "event_type": "CREATE",
                             "actor_tag": "dispatcher", "assigned_to": "ANY",
                             "status": "OPEN", "payload": "synthetic replay fixture"}, base=base)
        if code != 200:
            raise RuntimeError(f"replay fixture CREATE failed: {result}")

    def count(work_id):
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM events WHERE work_id = ? "
                                "AND event_type = 'COMPLETE'", (work_id,)).fetchone()[0]

    def snapshot(work_id):
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT * FROM events WHERE work_id = ? ORDER BY seq",
                                (work_id,)).fetchall()

    def receipt(result):
        return tuple(result.get(key) for key in ("event_id", "seq", "event_ts", "work_id", "payload_bytes"))

    def seed_legacy(original_id, event_id, work_id, schema_v):
        # Synthetic legacy rows only; no schema changes and no deletion/repair.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (event_id, event_ts, work_id, event_type, actor_tag, assigned_to, "
                "status, lease_until, payload, evidence_ref, parent_work_id, project, schema_v) "
                "SELECT ?, ?, ?, event_type, actor_tag, assigned_to, status, lease_until, payload, "
                "evidence_ref, parent_work_id, project, ? FROM events WHERE event_id = ?",
                (event_id, "2000-01-01T00:00:00Z", work_id, schema_v, original_id))

    print("== COMPLETE exact replay ==")
    try:
        proc, base = start()
        work_id = "WRK-REPLAY"
        create(work_id, base)
        complete = {"action": "event", "work_id": work_id, "event_type": "COMPLETE",
                    "actor_tag": "worker", "status": "DONE", "payload": "résultat: 42"}
        code, first = call(complete, base=base)
        check("first unkeyed COMPLETE inserts with UTF-8 byte receipt", code == 200 and
              first.get("payload_bytes") == len(complete["payload"].encode("utf-8")) and
              "replayed" not in first and count(work_id) == 1)
        stop(proc)
        restarted, base = start()
        code, retry = call(complete, base=base)
        check("same-DB process restart retry returns original receipt", proc.poll() is not None and
              restarted.pid != proc.pid and code == 200 and receipt(retry) == receipt(first) and
              retry.get("replayed") is True)
        check("unkeyed restart retry leaves exactly one COMPLETE", count(work_id) == 1)

        explicit_nulls = dict(complete, assigned_to=None, lease_until=None, evidence_ref=None,
                              parent_work_id=None, project=None)
        code, retry = call(explicit_nulls, base=base)
        check("omitted and explicit NULL optional fields replay identically", code == 200 and
              receipt(retry) == receipt(first) and count(work_id) == 1)
        code, retry = call(dict(complete, event_id="caller-not-an-event-id", event_ts="ignored", seq=-1,
                               schema_v=99), base=base)
        check("server receipt and effective schema ignore caller transport fields", code == 200 and
              receipt(retry) == receipt(first) and count(work_id) == 1)

        create("WRK-REPLAY-OTHER", base)
        variants = [("work_id", "WRK-REPLAY-OTHER"), ("actor_tag", "worker-other"),
                    ("actor_tag", "worker "),
                    ("assigned_to", ""), ("status", "REVIEW"),
                    ("lease_until", "2026-09-13T00:00:00Z"),
                    ("lease_until", "2026-09-13T00:00:00+00:00"),
                    ("payload", complete["payload"] + " "), ("evidence_ref", ""),
                    ("parent_work_id", ""), ("project", "Blackboard")]
        for field, value in variants:
            changed = dict(complete, **{field: value})
            before = count(changed["work_id"])
            code, result = call(changed, base=base)
            check(f"changed accepted {field}={value!r} is a new COMPLETE", code == 200 and
                  result.get("event_id") != first.get("event_id") and count(changed["work_id"]) == before + 1)
        before = snapshot(work_id)
        code, result = call(dict(complete, evidence_ref={"invalid": "object"}), base=base)
        check("invalid COMPLETE still validates before replay", code == 400 and snapshot(work_id) == before)

        seed_legacy(first["event_id"], "legacy-duplicate", work_id, 1)
        before = snapshot(work_id)
        code, retry = call(complete, base=base)
        check("legacy duplicates replay the earliest sequence receipt without deletion", code == 200 and
              receipt(retry) == receipt(first) and snapshot(work_id) == before)
        version_work = "WRK-REPLAY-VERSION"
        create(version_work, base)
        seed_legacy(first["event_id"], "legacy-other-schema", version_work, 2)
        code, version_first = call(dict(complete, work_id=version_work), base=base)
        code2, version_retry = call(dict(complete, work_id=version_work), base=base)
        check("effective schema 1 does not replay a different stored schema", code == code2 == 200 and
              version_first.get("event_id") != "legacy-other-schema" and
              receipt(version_retry) == receipt(version_first) and count(version_work) == 2)

        caller_version_work = "WRK-REPLAY-CALLER-VERSION"
        create(caller_version_work, base)
        caller_version = dict(complete, work_id=caller_version_work, schema_v=99)
        code, version_first = call(caller_version, base=base)
        with sqlite3.connect(db) as conn:
            stored_version = conn.execute("SELECT schema_v FROM events WHERE event_id = ?",
                                          (version_first.get("event_id"),)).fetchone()
        check("first COMPLETE still stores literal schema 1 for caller schema 99", code == 200 and
              stored_version == (1,) and "replayed" not in version_first)
        code, version_retry = call(caller_version, base=base)
        check("caller schema 99 retry returns its effective-schema original receipt", code == 200 and
              receipt(version_retry) == receipt(version_first) and version_retry.get("replayed") is True and
              count(caller_version_work) == 1)

        note_work = "WRK-REPLAY-NOTE"
        create(note_work, base)
        note = dict(complete, work_id=note_work, event_type="NOTE")
        code1, note1 = call(note, base=base)
        code2, note2 = call(note, base=base)
        check("identical non-COMPLETE events remain separate appends", code1 == code2 == 200 and
              note1.get("event_id") != note2.get("event_id"))
        code, result = call(dict(note, event_type="COMPLETE"), base=base)
        check("COMPLETE does not replay an otherwise identical NOTE", code == 200 and
              result.get("event_id") not in (note1.get("event_id"), note2.get("event_id")) and count(note_work) == 1)

        # Invalid input must not acquire a writer reservation. Initialize the
        # connection before contention and bound the negative control's wait.
        spec = importlib.util.spec_from_file_location("replay_validation_bus", server_py)
        bus = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bus)
        store = bus.Store(db)
        store.conn.execute("PRAGMA busy_timeout=50")
        gate = sqlite3.connect(db)
        try:
            gate.execute("BEGIN IMMEDIATE")
            invalid_inputs = [("payload", {"payload": ""}),
                              ("late evidence_ref", dict(complete, evidence_ref={})),
                              ("non-CLAIM lease_until", dict(complete, lease_until="invalid"))]
            for field, invalid in invalid_inputs:
                try:
                    store.add_event(invalid)
                    rejected = False
                except bus.BusError as error:
                    rejected = error.code == 400
                except sqlite3.OperationalError:
                    rejected = False
                check(f"invalid {field} is rejected before taking the SQLite writer", rejected)
        finally:
            gate.rollback()
            gate.close()
            store.conn.close()

        peer, peer_base = start()
        race_work = "WRK-REPLAY-RACE"
        create(race_work, base)
        race_complete = dict(complete, work_id=race_work)
        barrier = threading.Barrier(3)

        def concurrent_complete(endpoint):
            barrier.wait(timeout=5)
            return call(race_complete, base=endpoint)

        # Force overlapping requests while a third connection holds the writer
        # reservation. Both servers must acquire it BEFORE doing their lookup.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            with sqlite3.connect(db) as gate:
                gate.execute("BEGIN IMMEDIATE")
                futures = [pool.submit(concurrent_complete, endpoint) for endpoint in (base, peer_base)]
                barrier.wait(timeout=5)
                time.sleep(0.2)
                check("independent replay processes wait for the SQLite writer", peer.pid != restarted.pid and
                      all(not future.done() for future in futures))
            results = [future.result(timeout=15) for future in futures]
        check("concurrent separate-process retries return one original receipt", all(code == 200 for code, _ in results) and
              receipt(results[0][1]) == receipt(results[1][1]) and count(race_work) == 1)
    finally:
        for proc in processes:
            stop(proc)


def main():
    server_py = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "src", "bus_server.py")
    tmp = tempfile.mkdtemp(prefix="busround-")
    db = os.path.join(tmp, "test.db")
    env = dict(os.environ, BUS_SECRET=SECRET, BUS_PORT=str(PORT),
               BUS_BIND="127.0.0.1", BUS_DB=db)
    proc = subprocess.Popen([sys.executable, server_py, "--db", db, "serve"],
                            env=env, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                code, health = call(method="GET")
                if code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            sys.exit("server never came up")

        print("== health / auth ==")
        check("bare GET health 200 ok:true", health.get("ok") is True and health.get("service") == "sfdc24-blackboard-bus")
        code, r = call({"action": "time", "secret": "wrong"}, with_secret=False)
        check("wrong secret -> REAL 401, fast, v1 message", code == 401 and r.get("error") == "Bad or missing secret.")
        code, r = call({"action": "time"}, with_secret=False)
        check("missing secret -> 401", code == 401)
        code, r = call(raw=b"this is not json")
        check("non-JSON body -> 400 v1 message", code == 400 and r.get("error") == "Request body must be JSON.")
        code, r = call({"action": "nope"})
        check("unknown action -> 400", code == 400 and "Unknown action" in r.get("error", ""))
        code, r = call({"action": "time"})
        check("time -> ok with iso", code == 200 and r.get("ok") and "iso" in r)

        print("== docs: create / append / replace / read ==")
        code, r = call({"action": "create", "title": "SESSION STATE test", "kind": "doc"})
        check("create doc 200", code == 200 and r.get("ok"))
        code, r = call({"action": "create", "title": "SESSION STATE test", "kind": "doc"})
        check("duplicate create -> 409", code == 409)
        code, r = call({"action": "append", "title": "SESSION STATE test", "text": "first entry"})
        check("append doc echoes bytes", code == 200 and r.get("bytes_written") == len(b"first entry"))
        code, r = call({"action": "append", "title": "SESSION STATE test"})
        check("empty append -> 400 REQ-V8QD7R", code == 400 and "REQ-V8QD7R" in r.get("error", ""))
        code, r = call({"action": "replace", "title": "SESSION STATE test", "body": "REPLACED SNAPSHOT v2"})
        check("REPLACE doc works (D-16)", code == 200 and r.get("bytes_written") == len(b"REPLACED SNAPSHOT v2"))
        code, r = call({"action": "read", "title": "SESSION STATE test"})
        check("read-back proves replace (D-4)", code == 200 and r.get("body") == "REPLACED SNAPSHOT v2")
        code, r = call({"action": "read", "title": "no such file"})
        check("read missing -> 404", code == 404)
        code, r = call({"action": "read"})
        check("read w/o title -> 400 v1 message", code == 400 and r.get("error") == "title or fileId is required.")

        print("== sheets: schema-aware append ==")
        code, r = call({"action": "create", "title": "Blackboard - Test DB", "kind": "sheet",
                        "header": BOARD_HEADER})
        check("create sheet 200", code == 200)
        content8 = ["vm-cli", "ALL", "APPEND", "BCB|v=1|phase=RESULT|test", "DONE", "Blackboard", "gist", "subgist"]
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "sheetRow": content8})
        check("8 content cells -> server issues Row_ID+Timestamp", code == 200 and r.get("cells_written") == 10 and r.get("row_id"))
        shifted = ["2026-09-02T17:01:00.000Z", "BCB|v=1|payload-in-wrong-col"] + [""] * 8
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "sheetRow": shifted})
        check("shifted row -> 400 naming REQ-B4TQX9", code == 400 and "REQ-B4TQX9" in r.get("error", ""))
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "sheetRow": ["only", "three", "cells"]})
        check("wrong cell count -> 400 naming counts", code == 400 and "3 cells" in r.get("error", ""))
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "text": "wrong shape"})
        check("text to sheet -> 400 REQ-C4NDX7", code == 400 and "REQ-C4NDX7" in r.get("error", ""))
        full10 = ["custom-id-001", "2026-09-03T20:00:00Z", "vm-cli", "ALL", "APPEND",
                  "payload", "DONE", "Blackboard", "g", "sg"]
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "sheetRow": full10})
        check("valid full 10-cell row accepted", code == 200 and r.get("row_id") == "custom-id-001")
        blank8 = [""] * 8
        code, r = call({"action": "append", "title": "Blackboard - Test DB", "sheetRow": blank8})
        check("all-empty content cells -> 400", code == 400)
        code, r = call({"action": "read", "title": "Blackboard - Test DB"})
        check("read sheet: header row 0 + 2 data rows", code == 200 and r.get("rows", [[]])[0] == BOARD_HEADER and len(r["rows"]) == 3)
        code, r = call({"action": "read", "title": "Blackboard - Test DB", "limit": 1})
        check("read limit=1 -> header + newest row, total_rows honest",
              code == 200 and len(r["rows"]) == 2 and r["rows"][1][0] == "custom-id-001" and r.get("total_rows") == 2)
        code, r = call({"action": "replace", "title": "Blackboard - Test DB", "body": "nope"})
        check("replace sheet -> 400 (append-only)", code == 400)

        print("== v2 ledger (LEDGER SCHEMA v1, proposed) ==")
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CREATE",
                        "actor_tag": "vm-cli", "assigned_to": "gemini", "status": "OPEN",
                        "payload": "test work item", "project": "Blackboard"})
        check("CREATE ok, echoes seq+bytes", code == 200 and r.get("seq") and r.get("payload_bytes") == 14)
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CREATE",
                        "actor_tag": "vm-cli", "assigned_to": "gemini", "status": "OPEN", "payload": "dupe"})
        check("second CREATE same work_id -> 409", code == 409)
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "PROGRESS",
                        "actor_tag": "gemini", "status": "RUNNING", "payload": ""})
        check("empty payload -> 400 naming payload", code == 400 and "payload" in r.get("error", ""))
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "DANCE",
                        "actor_tag": "gemini", "status": "RUNNING", "payload": "x"})
        check("bad event_type -> 400 naming closed set", code == 400 and "closed set" in r.get("error", ""))
        code, r = call({"action": "event", "work_id": "WRK-NEVER", "event_type": "NOTE",
                        "actor_tag": "gemini", "status": "OPEN", "payload": "x"})
        check("event on unCREATEd work_id -> 400", code == 400 and "never been CREATEd" in r.get("error", ""))
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CLAIM",
                        "actor_tag": "gemini", "status": "CLAIMED", "payload": "claiming"})
        check("CLAIM without lease -> 400", code == 400 and "lease_until" in r.get("error", ""))
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CLAIM",
                        "actor_tag": "gemini", "status": "CLAIMED", "payload": "claiming",
                        "lease_until": "2030-01-01T00:00:00Z"})
        check("CLAIM with 4h+ lease -> 400", code == 400 and "4 hours" in r.get("error", ""))
        import datetime as _dt
        lease = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CLAIM",
                        "actor_tag": "gemini", "status": "CLAIMED", "payload": "claiming",
                        "lease_until": lease})
        check("valid CLAIM accepted", code == 200)
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "CLAIM",
                        "actor_tag": "chatgpt-codex-desktop", "status": "CLAIMED",
                        "payload": "stealing", "lease_until": lease})
        check("conflicting CLAIM -> 409 naming holder", code == 409 and r.get("holder") == "gemini")
        code, r = call({"action": "inbox", "tag": "gemini"})
        check("inbox(gemini): sees own claimed item", code == 200 and len(r.get("items", [])) == 1
              and r["items"][0]["work_id"] == "WRK-TEST01")
        code, r = call({"action": "inbox", "tag": "meta-ai-web"})
        check("inbox(other): empty", code == 200 and r.get("items") == [])
        code, r = call({"action": "event", "work_id": "WRK-TEST01", "event_type": "COMPLETE",
                        "actor_tag": "gemini", "status": "DONE", "payload": "done"})
        check("COMPLETE accepted", code == 200)
        code, r = call({"action": "inbox", "tag": "gemini"})
        check("inbox after DONE: empty (reduction rule)", code == 200 and r.get("items") == [])
        code, r = call({"action": "work", "work_id": "WRK-TEST01"})
        check("work history: 3 events in seq order", code == 200 and len(r.get("events", [])) == 3
              and [e["event_type"] for e in r["events"]] == ["CREATE", "CLAIM", "COMPLETE"])

        print("== regression: review findings ==")
        import concurrent.futures
        code, r = call({"action": "create", "title": "race doc", "kind": "doc"})
        check("create race doc", code == 200)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(call, {"action": "append", "title": "race doc", "text": f"entry-{i:02d}"})
                    for i in range(10)]
            codes = [f.result()[0] for f in futs]
        code, r = call({"action": "read", "title": "race doc"})
        present = sum(1 for i in range(10) if f"entry-{i:02d}" in r.get("body", ""))
        check("10 concurrent doc appends: none lost", all(c == 200 for c in codes) and present == 10,
              f"present={present}")
        code, r = call({"action": "time", "secret": "café-nicht-ascii"}, with_secret=False)
        check("non-ASCII secret -> 401 not 500", code == 401)
        code, r = call({"action": "create", "title": "tiny sheet", "kind": "sheet", "header": ["Name"]})
        check("1-column sheet -> 400", code == 400)
        code, r = call({"action": "create", "title": "tiny sheet2", "kind": "sheet", "header": ["Id", "Ts"]})
        check("2-column sheet -> 400", code == 400)

        print("== blank-padded header: the shape the LIVE board actually returns ==")
        # Measured 2026-09-09 against "Blackboard - Alpha DB": every one of its 1883
        # rows is 12 cells wide, and the header is the ten names plus two empty
        # padding cells. Requiring non-empty names rejected the real board outright.
        padded = BOARD_HEADER + ["", ""]
        code, r = call({"action": "create", "title": "Padded board", "kind": "sheet", "header": padded})
        check("A:L header (10 named + 2 blank padding) -> 200", code == 200, f"got {code} {r.get('error','')}")
        live_row = ["r-1", "2026-09-09T16:00:00Z", "claude-code-cli", "ALL", "APPEND",
                    "BCB|v=1|id=X", "OPEN", "proj", "gist", "sub", "", ""]
        code, r = call({"action": "append", "title": "Padded board", "sheetRow": live_row})
        check("12-wide row appends to the padded sheet", code == 200, f"got {code} {r.get('error','')}")
        code, r = call({"action": "read", "title": "Padded board"})
        rows = r.get("rows", [])
        check("padded header round-trips verbatim, padding still blank",
              code == 200 and len(rows) == 2 and rows[0] == padded and rows[0][10] == "" and rows[0][11] == "",
              f"header={rows[0] if rows else None}")
        check("the 12-cell data row survives verbatim",
              len(rows) == 2 and rows[1][:10] == live_row[:10] and len(rows[1]) == 12,
              f"row={rows[1] if len(rows) > 1 else None}")
        # The negations. Padding is a TRAILING run, and it is not a free column count.
        code, r = call({"action": "create", "title": "Holed board", "kind": "sheet",
                        "header": ["Row_ID", "Timestamp", "", "Payload"]})
        check("blank BETWEEN named columns is a hole -> 400", code == 400, f"got {code}")
        code, r = call({"action": "create", "title": "Padding is not width", "kind": "sheet",
                        "header": ["Id", "Ts", "", ""]})
        check("4 columns but only 2 NAMED -> still 400", code == 400, f"got {code}")

        print("== appending to a padded sheet: the shift codex-oversight found ==")
        # THE DEFECT: append reasoned about len(header) - the WIRE width - so on a
        # sheet imported from the live board a TEN-CELL FULL ROW (what every fleet
        # client sends) looked like eight content cells. The server prepended its
        # own Row_ID and Timestamp and shifted every field two columns left, with
        # HTTP 200 and no error. That is REQ-B4TQX9 reintroduced by the padding fix.
        code, r = call({"action": "create", "title": "Live shape", "kind": "sheet", "header": padded})
        check("create a sheet with the live board's exact header", code == 200)
        full10 = ["11111111-2222-3333-4444-555555555555", "2026-09-09T23:00:00Z", "claude-code-cli",
                  "ALL", "APPEND", "BCB|v=1|id=X", "OPEN", "Blackboard", "a gist", "a sub"]
        code, r = call({"action": "append", "title": "Live shape", "sheetRow": full10})
        check("a 10-cell FULL ROW is accepted on a padded sheet", code == 200, f"got {code} {r.get('error','')}")
        code, r = call({"action": "read", "title": "Live shape"})
        stored = r.get("rows", [[], []])[1] if len(r.get("rows", [])) > 1 else []
        check("and every field stays in its own column - NO SHIFT",
              stored[:10] == full10, f"stored={stored}")
        check("stored at the sheet's wire width, padding empty",
              len(stored) == 12 and stored[10] == "" and stored[11] == "", f"stored={stored}")
        # content-cell form still works, and still cannot shift
        code, r = call({"action": "append", "title": "Live shape",
                        "sheetRow": ["chat-mobile", "ALL", "APPEND", "BCB|v=1|id=Y", "OPEN", "P", "g", "s"]})
        check("8 content cells still get a server Row_ID and Timestamp", code == 200, f"got {code} {r.get('error','')}")
        code, r = call({"action": "read", "title": "Live shape"})
        rows_ls = r.get("rows", [])
        row2 = rows_ls[2] if len(rows_ls) > 2 else []
        check("and land in the NAMED columns, not shifted",
              len(row2) == 12 and row2[2] == "chat-mobile" and row2[3] == "ALL", f"row={row2}")
        # a client echoing a padded row back
        code, r = call({"action": "append", "title": "Live shape",
                        "sheetRow": ["22222222-2222-3333-4444-555555555555", "2026-09-09T23:30:00Z", "vm-cli",
                                     "ALL", "APPEND", "BCB|v=1|id=Z", "OPEN", "P", "g", "s", "", ""]})
        check("a 12-cell row whose padding is EMPTY is accepted", code == 200, f"got {code} {r.get('error','')}")
        # THE NEGATIONS
        code, r = call({"action": "append", "title": "Live shape",
                        "sheetRow": ["33333333-2222-3333-4444-555555555555", "2026-09-09T23:31:00Z", "vm-cli",
                                     "ALL", "APPEND", "BCB|v=1|id=W", "OPEN", "P", "g", "s", "leaked", ""]})
        check("content in a PADDING column -> 400, same rule bcb_lint applies",
              code == 400 and "padding" in r.get("error", "").lower(), f"got {code} {r.get('error','')}")
        code, r = call({"action": "append", "title": "Live shape",
                        "sheetRow": ["2026-09-09T23:32:00Z", "2026-09-09T23:32:00Z", "vm-cli",
                                     "ALL", "APPEND", "BCB|v=1|id=V", "OPEN", "P", "g", "s"]})
        check("a timestamp in col 0 is still REQ-B4TQX9 -> 400",
              code == 400 and "REQ-B4TQX9" in r.get("error", ""), f"got {code} {r.get('error','')}")
        code, r = call({"action": "append", "title": "Live shape", "sheetRow": ["only", "three", "cells"]})
        check("a wrong cell count still names the NAMED width, not 12",
              code == 400 and " 10 " in (" " + r.get("error", "") + " "), f"got {code} {r.get('error','')}")
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "CREATE",
                        "actor_tag": "vm-cli", "assigned_to": "gemini", "status": "OPEN", "payload": "w2"})
        check("CREATE second item", code == 200)
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "CLAIM",
                        "actor_tag": "gemini", "status": "CLAIMED", "payload": "c",
                        "lease_until": "2026-09-03T20:00:00+99:99"})
        check("impossible lease offset -> 400 not 500", code == 400)
        import datetime as _dt2
        lease2 = (_dt2.datetime.now(_dt2.timezone.utc) + _dt2.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "CLAIM",
                        "actor_tag": "gemini", "status": "CLAIMED", "payload": "c", "lease_until": lease2})
        check("valid CLAIM on second item", code == 200)
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "PROGRESS",
                        "actor_tag": "gemini", "status": "RUNNING", "payload": "working, no lease field"})
        check("PROGRESS without lease accepted", code == 200)
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "CLAIM",
                        "actor_tag": "chatgpt-codex-desktop", "status": "CLAIMED",
                        "payload": "steal after PROGRESS", "lease_until": lease2})
        check("rival CLAIM after lease-less PROGRESS -> still 409", code == 409 and r.get("holder") == "gemini")
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "NOTE",
                        "actor_tag": "vm-cli", "status": "CLAIMED", "payload": "note by non-holder"})
        check("NOTE by non-holder accepted", code == 200)
        code, r = call({"action": "inbox", "tag": "gemini"})
        check("inbox still shows item after foreign NOTE", code == 200 and
              any(i["work_id"] == "WRK-TEST02" for i in r.get("items", [])))
        code, r = call({"action": "event", "work_id": "WRK-TEST02", "event_type": "NOTE",
                        "actor_tag": "vm-cli", "status": "CLAIMED", "payload": "x",
                        "evidence_ref": {"k": 1}})
        check("object evidence_ref -> 400 not 500", code == 400)

        test_import_atomicity(server_py, tmp)
        test_complete_replay(server_py, tmp)

        print(f"\n{PASS} passed, {FAIL} failed")
        if FAILURES:
            print("FAILED:", *FAILURES, sep="\n  - ")
        return 1 if FAIL else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
