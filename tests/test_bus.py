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

import json
import os
import subprocess
import sys
import tempfile
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


def call(body=None, method="POST", with_secret=True, raw=None):
    """Returns (http_status, parsed_json). Never raises on 4xx/5xx."""
    if method == "GET":
        req = urllib.request.Request(BASE)
    else:
        if raw is None:
            body = dict(body or {})
            if with_secret:
                body.setdefault("secret", SECRET)
            raw = json.dumps(body).encode()
        req = urllib.request.Request(BASE, data=raw,
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
