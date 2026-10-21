"""scripts/canvas_publish.py: what reaches the PUBLIC canvas file, and only that.

The file is world-readable, so the tests that matter are about what must NOT reach it: an item without
public:true, a field outside the allowlist, a type, status or host outside the rules, a partition of an
agent nobody listed, and an owner named from inside the value instead of from the key. Then the window:
one per room, time-boxed, refused rather than doubled. No network: the bucket and the token are fakes.
"""
import datetime
import fnmatch
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import bus_request as br                                                 # noqa: E402
import canvas_publish as cp                                              # noqa: E402

NOW = datetime.datetime(2026, 10, 20, 22, 0, 0, tzinfo=datetime.timezone.utc)


class FakeRedis:
    def __init__(self):
        self.store, self.hashes, self.ttls = {}, {}, {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def scan_iter(self, match="*", count=None):
        return [k for k in list(self.store) if fnmatch.fnmatchcase(k, match)]


def item(**over):
    base = {"v": 1, "id": "x", "type": "mermaid", "title": "Access map", "body": "flowchart LR\n  a-->b\n",
            "status": "working", "version": 1, "public": True, "ts": "2026-10-20T21:30:00Z"}
    base.update(over)
    return json.dumps(base)


def put(conn, agent, field, raw, room="demo"):
    conn.hashes.setdefault(cp.PARTITION % (room, agent), {})[field] = raw


class CleanItem(unittest.TestCase):
    def test_a_public_item_is_published_with_its_owner_taken_from_the_key(self):
        got = cp.clean_item("grok", "3", item(agent="claude-code-cli", by="owner"))
        self.assertEqual(got["agent"], "grok")
        self.assertEqual(got["id"], "grok/3")
        self.assertEqual(sorted(got), sorted(["v", "id", "type", "title", "body", "status", "version",
                                              "public", "ts", "agent"]))

    def test_anything_not_exactly_public_true_stays_in_redis(self):
        for public in (False, "true", 1, None):
            self.assertIsNone(cp.clean_item("grok", "3", item(public=public)), public)
        raw = json.loads(item())
        del raw["public"]
        self.assertIsNone(cp.clean_item("grok", "3", json.dumps(raw)))

    def test_fields_outside_the_allowlist_are_not_published(self):
        got = cp.clean_item("grok", "3", item(email="someone@example.com", phone="+1 555 0100", html="<b>"))
        self.assertNotIn("someone@example.com", json.dumps(got))
        self.assertNotIn("email", got)

    def test_type_status_version_and_v_must_be_on_the_rules(self):
        for over in ({"type": "script"}, {"status": "shipped"}, {"version": True}, {"version": -1},
                     {"version": 1.5}, {"version": "2"}, {"v": 2}, {"v": True}, {"title": 7}, {"body": None},
                     {"ts": 5}):
            self.assertIsNone(cp.clean_item("grok", "3", item(**over)), over)

    def test_lengths_and_control_characters(self):
        self.assertIsNone(cp.clean_item("grok", "3", item(title="t" * (cp.TITLE_MAX + 1))))
        self.assertIsNone(cp.clean_item("grok", "3", item(type="note", body="b" * (cp.BODY_MAX + 1))))
        self.assertIsNotNone(cp.clean_item("grok", "3", item(type="note", body="b" * cp.BODY_MAX)))
        self.assertIsNone(cp.clean_item("grok", "3", item(title="two\nlines")))
        self.assertIsNone(cp.clean_item("grok", "3", item(type="note", body="bell\x07")))
        self.assertIsNotNone(cp.clean_item("grok", "3", item(title="t" * cp.TITLE_MAX, body="flowchart LR\r\n\ta-->b")))

    def test_image_urls_are_https_on_an_allowlisted_host_only(self):
        good = "https://storage.googleapis.com/x/sketch.png"
        self.assertIsNotNone(cp.clean_item("gemini", "s", item(type="image_url", body=good)))
        for bad in ("http://storage.googleapis.com/x.png", "javascript:alert(1)", "https://example.com/x.png",
                    "https://user:pw@storage.googleapis.com/x.png", "https://storage.googleapis.com:8443/x.png",
                    " https://storage.googleapis.com/x.png", "https://storage.googleapis.com/a b.png",
                    "https://storage.googleapis.com.evil.example/x.png"):
            self.assertIsNone(cp.clean_item("gemini", "s", item(type="image_url", body=bad)), bad)

    def test_an_unlisted_agent_a_bad_id_or_a_value_that_is_not_an_object_is_dropped(self):
        self.assertIsNone(cp.clean_item("mallory", "3", item()))
        self.assertIsNone(cp.clean_item("grok", "../3", item()))
        self.assertIsNone(cp.clean_item("grok", "", item()))
        for raw in ("not json", "[1, 2]", "null", None, b"\xff"):
            self.assertIsNone(cp.clean_item("grok", "3", raw), raw)


class ChairSlot(unittest.TestCase):
    def test_the_chair_s_slot_is_text_and_diagrams_short_and_without_links(self):
        self.assertIsNotNone(cp.clean_item("chair", "idea-1", item()))
        self.assertIsNotNone(cp.clean_item("chair", "idea-2", item(type="note", body="Asks:\n- a\n- b")))
        for over in ({"type": "image_url", "body": "https://storage.googleapis.com/x.png"},
                     {"type": "wireframe", "body": "<p>x</p>"}, {"type": "datamodel"},
                     {"body": "see https://x.example"}, {"title": "www.example.com"},
                     {"body": "flowchart LR\n  A-->B\n  click A go"}, {"body": "a HREF b"},
                     {"body": "JavaScript:alert(1)"}, {"body": "b" * 500}):
            self.assertIsNone(cp.clean_item("chair", "idea-1", item(**over)), over)
            if "type" not in over:              # as a note: a link is text there; a Mermaid body is checked for all
                self.assertIsNone(cp.clean_item("chair", "idea-1", item(**dict(over, type="note"))), over)
                self.assertIsNotNone(cp.clean_item("grok", "idea-1", item(**dict(over, type="note"))),
                                     "only the chair's slot: %s" % over)
        for over in ({"body": "A Href b"}, {"body": "Click A now"}, {"title": "WWW.EXAMPLE.COM"}):
            self.assertIsNone(cp.clean_item("chair", "idea-1", item(**dict(over, type="note"))), over)

    def test_the_chair_is_published_with_its_own_name(self):
        conn = FakeRedis()
        put(conn, "chair", "idea-1", item())
        self.assertEqual([("chair/idea-1", "chair")],
                         [(i["id"], i["agent"]) for i in cp.snapshot(conn, "demo", NOW)["items"]])


class MermaidIsCheckedBeforeTheFile(unittest.TestCase):
    """CLAUDE-MOBILE-GEMINI-SPILL-CHAIR-20261021T0135Z: a hostile diagram never reaches the public file, from any slot."""

    GOOD = ("flowchart LR\n  owner-->chair\n  chair-- asks -->grok",
            "graph TD\n  A[Start] --> B{Ready?}\n  B -->|yes| C((Go))\n  B <--> D",
            "%% a comment first\nflowchart LR\n  a-.->b\n  a==>c",
            "erDiagram\n  ACCOUNT ||--o{ CONTACT : has",
            "classDiagram\n  Animal <|-- Duck\n  Duck : +swim()",
            "stateDiagram-v2\n  [*] --> Draft\n  Draft --> Review",
            "mindmap\n  root((Rehearsal))\n    Voice\n    Canvas",
            "timeline\n  title Thursday\n  9am : rehearsal",
            "pie title Time\n  \"Talk\" : 60\n  \"Draw\" : 40")

    HOSTILE = (
        "sequenceDiagram\n  A->>B: hi",                                       # left out: KaTeX labels
        "architecture-beta\n  service s(server)[x]",                           # left out: icon text
        "gantt\n  title x", "xychart-beta\n  x-axis [a]",                      # left out: render loops
        "flowchart LR\n  A-->B\n  click A callback",                           # what acts
        "flowchart LR\n  A-->B\n  click A href \"/x\"",
        "flowchart LR\n  A-->B\n  CLICK A go",
        "%%{init: {\"securityLevel\": \"loose\"}}%%\nflowchart LR\n  A-->B",    # configuration
        "flowchart LR\n  A-->B\n  %%{ init: {\"htmlLabels\": true} }%%",
        "---\nconfig:\n  securityLevel: loose\n---\nflowchart LR\n  A-->B",
        "flowchart LR\n  A[\"<img src=x onerror=alert(1)>\"]-->B",              # HTML
        "flowchart LR\n  A[\"< script >\"]-->B", "flowchart LR\n  A[\"</b>\"]-->B",
        "flowchart LR\n  A[\"<!-- x -->\"]-->B",
        "flowchart LR\n  A[\"#60;img src=x onerror=alert(1)#62;\"]-->B",        # Mermaid's own codes
        "flowchart LR\n  A[\"&lt;img&gt;\"]-->B", "flowchart LR\n  A[\"&#60;img\"]-->B",
        "flowchart LR\n  A[\"`**md** string`\"]-->B",                         # markdown strings
        "flowchart LR\n  A[\"$$\alpha$$\"]-->B",                               # KaTeX
        "flowchart LR\n  classDef bad fill:url(https://x.example/a.png)\n  A-->B",   # CSS
        "flowchart LR\n  A-->B\n  style A fill:#f9f", "flowchart LR\n  A-->B\n  linkStyle 0 stroke:red",
        "flowchart LR\n  A[\"see https://x.example\"]-->B", "flowchart LR\n  A[\"javascript:alert(1)\"]-->B",
        "mindmap\n  root\n    ::icon(fa fa-book)", "flowchart LR\n  A[fa:fa-car Car]-->B",
        "", "\n\n", "%% only a comment", "flowchart\n" + "  a-->b\n" * 60, "Flowchart LR\n  a-->b",
        "flowchartLR\n  a-->b", "flowchart;evil\n  a-->b", "not a diagram")

    def test_plain_diagrams_of_the_listed_kinds_are_published(self):
        for body in self.GOOD:
            self.assertTrue(cp.mermaid_ok(body), body)
            self.assertIsNotNone(cp.clean_item("grok", "m", item(body=body)), body)
            self.assertIsNotNone(cp.clean_item("chair", "m", item(body=body)), body)

    def test_a_hostile_or_unlisted_diagram_is_dropped_from_every_slot(self):
        for body in self.HOSTILE:
            self.assertFalse(cp.mermaid_ok(body), body)
            for agent in ("grok", "gemini", "chair", "claude-code-cli"):
                self.assertIsNone(cp.clean_item(agent, "m", item(body=body)), (agent, body))

    def test_the_same_words_in_a_note_are_text_and_are_published(self):
        """The check is for what Mermaid would render; a note is drawn as text."""
        self.assertIsNotNone(cp.clean_item("grok", "n", item(type="note", body="click A href <b>x</b> $$")))

    def test_a_hostile_diagram_never_reaches_the_snapshot(self):
        conn = FakeRedis()
        put(conn, "grok", "ok", item(body=self.GOOD[0]))
        put(conn, "chair", "bad", item(body=self.HOSTILE[7]))
        put(conn, "gemini", "bad", item(body=self.HOSTILE[10]))
        doc = cp.snapshot(conn, "demo", NOW)
        self.assertEqual(["grok/ok"], [i["id"] for i in doc["items"]])
        self.assertNotIn("onerror", json.dumps(doc))
        self.assertNotIn("securityLevel", json.dumps(doc))

    def test_a_final_newline_does_not_make_an_id_or_a_bucket(self):
        self.assertIsNone(cp.clean_item("grok", "x\n", item()))
        with self.assertRaises(cp.Refused):
            cp.open_window(FakeRedis(), "demo", 5, "grok", NOW, "sfdc24-canvas\n")


class Snapshot(unittest.TestCase):
    def test_only_allowlisted_partitions_and_public_items_reach_the_file(self):
        conn = FakeRedis()
        put(conn, "grok", "map", item())
        put(conn, "gemini", "map", item(version=4))
        put(conn, "grok", "secret", item(public=False, body="PRIVATE-CANVAS-ITEM"))
        put(conn, "mallory", "x", item(body="UNLISTED-AGENT"))
        put(conn, "grok", "other-room", item(body="OTHER-ROOM"), room="elsewhere")
        doc = cp.snapshot(conn, "demo", NOW, NOW + datetime.timedelta(minutes=30))
        blob = json.dumps(doc)
        self.assertEqual(doc["format"], "proto-canvas/1")
        self.assertEqual([i["id"] for i in doc["items"]], ["grok/map", "gemini/map"])
        for absent in ("PRIVATE-CANVAS-ITEM", "UNLISTED-AGENT", "OTHER-ROOM"):
            self.assertNotIn(absent, blob)
        self.assertEqual(doc["until"], "2026-10-20T22:30:00Z")

    def test_two_agents_may_use_the_same_item_id_without_colliding(self):
        conn = FakeRedis()
        put(conn, "grok", "1", item(version=9))
        put(conn, "claude-code-cli", "1", item(version=1))
        ids = [i["id"] for i in cp.snapshot(conn, "demo", NOW)["items"]]
        self.assertEqual(sorted(ids), ["claude-code-cli/1", "grok/1"])

    def test_the_item_count_is_capped(self):
        conn = FakeRedis()
        for n in range(cp.MAX_ITEMS + 5):
            put(conn, "grok", "i%03d" % n, item())
        self.assertEqual(len(cp.snapshot(conn, "demo", NOW)["items"]), cp.MAX_ITEMS)

    def test_the_digest_ignores_the_clock_and_sees_a_change(self):
        conn = FakeRedis()
        put(conn, "grok", "1", item())
        a = cp.content_digest(cp.snapshot(conn, "demo", NOW))
        b = cp.content_digest(cp.snapshot(conn, "demo", NOW + datetime.timedelta(seconds=5)))
        self.assertEqual(a, b)
        put(conn, "grok", "1", item(version=2))
        self.assertNotEqual(a, cp.content_digest(cp.snapshot(conn, "demo", NOW)))


class Window(unittest.TestCase):
    def test_a_window_is_time_boxed_and_one_per_room(self):
        conn = FakeRedis()
        got = cp.open_window(conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self.assertEqual(got["until"], "2026-10-20T22:30:00Z")
        self.assertEqual(conn.ttls[cp.window_key("demo")], 1800)
        with self.assertRaises(cp.Refused) as again:
            cp.open_window(conn, "demo", "120", "gemini", NOW, "sfdc24-canvas")
        self.assertIn("already publishing until 2026-10-20T22:30:00Z", str(again.exception))
        self.assertEqual(json.loads(conn.store[cp.window_key("demo")])["by"], "grok")

    def test_minutes_zero_stops_it(self):
        conn = FakeRedis()
        cp.open_window(conn, "demo", "10", "grok", NOW, "sfdc24-canvas")
        self.assertTrue(cp.open_window(conn, "demo", "0", "grok", NOW, "")["stopped"])
        self.assertFalse(cp.open_window(conn, "demo", "0", "grok", NOW, "")["stopped"])
        self.assertNotIn(cp.window_key("demo"), conn.store)

    def test_bad_rooms_minutes_and_a_missing_bucket_are_refused(self):
        conn = FakeRedis()
        for room, minutes, bucket in (("Demo", "5", "b-1"), ("a", "5", "b-1"), ("x" * 41, "5", "b-1"),
                                      ("de mo", "5", "b-1"), ("demo", "121", "b-1"), ("demo", "-1", "b-1"),
                                      ("demo", "five", "b-1"), ("demo", "", "b-1"), ("demo", "1.5", "b-1"),
                                      ("demo", "5", ""), ("demo", "5", "Bad_Bucket!")):
            with self.assertRaises(cp.Refused, msg=(room, minutes, bucket)):
                cp.open_window(conn, room, minutes, "grok", NOW, bucket)
        self.assertEqual(conn.store, {})

    def test_only_open_windows_are_active(self):
        conn = FakeRedis()
        cp.open_window(conn, "demo", "10", "grok", NOW, "b-1")
        cp.open_window(conn, "late", "1", "grok", NOW - datetime.timedelta(minutes=5), "b-1")
        conn.store[cp.window_key("junk")] = "not json"
        self.assertEqual([r for r, _ in cp.active_windows(conn, NOW)], ["demo"])


class Uploads:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, bucket, name, data):
        if self.fail:
            self.fail -= 1
            raise OSError("boom: Bearer abc")
        self.calls.append((bucket, name, json.loads(data)))


class Publish(unittest.TestCase):
    def setUp(self):
        self.conn, self.up, self.t = FakeRedis(), Uploads(), {"now": NOW}
        self.log = mock.Mock()
        self.pub = cp.Publisher(self.conn, "sfdc24-canvas", self.up, self.log, clock=lambda: self.t["now"])
        put(self.conn, "grok", "1", item())
        put(self.conn, "grok", "2", item(public=False, body="PRIVATE"))

    def later(self, seconds):
        self.t["now"] += datetime.timedelta(seconds=seconds)

    def test_nothing_is_written_without_an_open_window(self):
        self.assertEqual(self.pub.tick(), [])
        self.assertEqual(self.up.calls, [])

    def test_written_once_then_only_on_change_or_keepalive(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self.assertEqual(self.pub.tick(), ["demo"])
        bucket, name, doc = self.up.calls[0]
        self.assertEqual((bucket, name), ("sfdc24-canvas", "rooms/demo.json"))
        self.assertNotIn("PRIVATE", json.dumps(doc))
        self.later(5)
        self.assertEqual(self.pub.tick(), [])
        put(self.conn, "grok", "1", item(version=2))
        self.later(5)
        self.assertEqual(self.pub.tick(), ["demo"])
        self.later(cp.KEEPALIVE_SECONDS - 1)
        self.assertEqual(self.pub.tick(), [])
        self.later(1)
        self.assertEqual(self.pub.tick(), ["demo"])

    def test_a_failed_upload_is_retried_and_logs_only_the_type(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self.up.fail = 1
        self.assertEqual(self.pub.tick(), [])
        self.assertNotIn("abc", repr(self.log.warning.call_args))
        self.later(5)
        self.assertEqual(self.pub.tick(), ["demo"])

    def test_the_window_end_stops_publishing(self):
        cp.open_window(self.conn, "demo", "1", "grok", NOW, "sfdc24-canvas")
        self.pub.tick()
        self.later(61)
        put(self.conn, "grok", "1", item(version=3))
        self.assertEqual(self.pub.tick(), [])


class Upload(unittest.TestCase):
    def test_one_multipart_call_sets_cache_control_and_carries_the_token(self):
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen.append(req)
            return Resp()

        self.assertEqual(cp.gcs_upload("sfdc24-canvas", "rooms/demo.json", b'{"a":1}', lambda: "tok", opener), 200)
        req = seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.startswith(
            "https://storage.googleapis.com/upload/storage/v1/b/sfdc24-canvas/o?uploadType=multipart"))
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        body = req.data.decode("utf-8")
        self.assertIn('"cacheControl": "public, max-age=5"', body)
        self.assertIn('"name": "rooms/demo.json"', body)
        self.assertIn('{"a":1}', body)

    def test_the_token_is_kept_until_a_minute_before_it_expires(self):
        calls, clock = [], {"t": 1000.0}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"access_token": "t%d" % len(calls), "expires_in": 3600}).encode()

        def opener(req, timeout):
            calls.append(req)
            self.assertEqual(req.get_header("Metadata-flavor"), "Google")
            return Resp()

        token = cp.Token(opener, clock=lambda: clock["t"])
        self.assertEqual(token(), "t1")
        clock["t"] += 3600 - 61
        self.assertEqual(token(), "t1")
        clock["t"] += 2
        self.assertEqual(token(), "t2")

    def test_no_bucket_starts_nothing(self):
        for bucket in ("", None, "Bad_Bucket!"):
            self.assertIsNone(cp.start(FakeRedis(), bucket, mock.Mock(), upload=Uploads()))

    def test_start_runs_a_thread_that_stops(self):
        pub = cp.start(FakeRedis(), "sfdc24-canvas", mock.Mock(), upload=Uploads())
        pub.stop.set()
        pub.thread.join(timeout=10)
        self.assertFalse(pub.thread.is_alive())


def req_row(req="CANVAS-0001", source="grok", extra="|room=demo|minutes=30", at="2026-10-20T21:58:00Z"):
    payload = "BCB|v=1|req=%s|do=canvas-publish%s" % (req, extra)
    return ["GROK-" + req, at, source, "bus-reconciler", "AYA_REQ", payload, "OPEN", "Blackboard", "g", ""]


class Requests(unittest.TestCase):
    def run_handle(self, rows, conn=None, bucket="sfdc24-canvas"):
        conn = conn or FakeRedis()
        rows_out = []
        with mock.patch.dict(br.os.environ, {"CANVAS_BUCKET": bucket}):
            out = br.handle(rows, conn, now=NOW, append=rows_out.append, clock=lambda: NOW.timestamp())
        return conn, out, rows_out

    def test_a_fleet_agent_opens_a_window_and_gets_one_result_row(self):
        conn, out, rows = self.run_handle([req_row()])
        self.assertEqual([r["action_type"] for r in rows], ["AYA_RESULT"])
        self.assertIn("OK. Room demo publishes", rows[0]["payload"])
        self.assertIn("gs://sfdc24-canvas/rooms/demo.json", rows[0]["payload"])
        self.assertEqual(rows[0]["target_surface"], "grok")
        self.assertEqual(json.loads(conn.store[cp.window_key("demo")])["by"], "grok")
        self.assertEqual(out["answered"][0]["status"], "OK")

    def test_the_window_records_the_principal_not_the_raw_tag(self):
        conn, _, rows = self.run_handle([req_row(source="grok-bot")])
        self.assertEqual(json.loads(conn.store[cp.window_key("demo")])["by"], "grok")

    def test_the_same_request_is_never_answered_twice(self):
        conn, _, first = self.run_handle([req_row()])
        _, out, again = self.run_handle([req_row()], conn=conn)
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])

    def test_a_request_another_worker_claims_first_is_not_run(self):
        class Racing(FakeRedis):
            """The marker reads empty, then another worker's SET NX lands before ours."""

            def set(self, key, value, nx=False, ex=None):
                if nx and key.startswith(br.DONE_PREFIX) and key not in self.store:
                    self.store[key] = "OTHER-ROW|%d" % int(NOW.timestamp())
                    return None
                return FakeRedis.set(self, key, value, nx=nx, ex=ex)

        conn, out, rows = self.run_handle([req_row()], conn=Racing())
        self.assertEqual(rows, [])
        self.assertNotIn(cp.window_key("demo"), conn.store)

    def test_a_second_window_for_the_room_is_refused_on_the_board(self):
        conn, _, _ = self.run_handle([req_row()])
        _, out, rows = self.run_handle([req_row(req="CANVAS-0002", source="gemini")], conn=conn)
        self.assertIn("REFUSED. room demo is already publishing", rows[0]["payload"])
        self.assertEqual(out["answered"][0]["status"], "REFUSED")

    def test_an_unknown_sender_gets_no_row_and_no_window(self):
        conn, out, rows = self.run_handle([req_row(source="mallory")])
        self.assertEqual(rows, [])
        self.assertNotIn(cp.window_key("demo"), conn.store)

    def test_an_ambiguous_request_is_refused(self):
        conn, _, rows = self.run_handle([req_row(extra="|room=demo|minutes=5|room=other")])
        self.assertIn("ambiguous", rows[0]["payload"])
        self.assertEqual([k for k in conn.store if k.startswith(cp.WINDOW_PREFIX)], [])

    def test_without_a_bucket_nothing_opens(self):
        conn, _, rows = self.run_handle([req_row()], bucket="")
        self.assertIn("no public canvas bucket is configured", rows[0]["payload"])
        self.assertNotIn(cp.window_key("demo"), conn.store)


class LiveWithCanvas(unittest.TestCase):
    def test_a_live_run_goes_through_the_canvas_wrapper_and_a_single_pass_does_not(self):
        import serve_requests as sr
        for loop_until, wrapped in (("2026-10-20T23:00:00Z", 1), ("", 0)):
            with mock.patch.object(sr.redis_dual, "client", return_value=object()), \
                    mock.patch.object(sr, "take_lock", return_value=None), \
                    mock.patch.object(sr, "drop_lock"), \
                    mock.patch.object(sr, "one_pass") as one_pass, \
                    mock.patch.object(sr, "live", return_value=0) as bare_live, \
                    mock.patch.object(sr, "live_with_canvas", return_value=0) as wrapper, \
                    mock.patch.dict(sr.os.environ, {"LOOP_UNTIL": loop_until}), \
                    mock.patch("bus.load_env", return_value={}):
                self.assertEqual(sr.main(), 0)
            self.assertEqual(wrapper.call_count, wrapped, loop_until)
            self.assertEqual(bare_live.call_count, 0, loop_until)
            self.assertEqual(one_pass.call_count, 1 - wrapped, loop_until)

    def test_the_publisher_runs_only_beside_a_live_run_and_stops_with_it(self):
        import serve_requests as sr
        events = []

        class Pub:
            def __init__(self):
                self.stop = mock.Mock(set=lambda: events.append("stop"))
                self.thread = mock.Mock(join=lambda timeout: events.append("join"))

        def start(conn, bucket, log):
            events.append(("start", bucket))
            return Pub() if bucket else None

        with mock.patch.object(sr, "live", side_effect=lambda *a: events.append("live") or 0), \
                mock.patch.dict(sr.os.environ, {"CANVAS_BUCKET": "sfdc24-canvas"}):
            self.assertEqual(sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start), 0)
        self.assertEqual(events, [("start", "sfdc24-canvas"), "live", "stop", "join"])
        events.clear()
        with mock.patch.object(sr, "live", side_effect=RuntimeError("boom")), \
                mock.patch.dict(sr.os.environ, {"CANVAS_BUCKET": "sfdc24-canvas"}):
            with self.assertRaises(RuntimeError):
                sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start)
        self.assertEqual(events, [("start", "sfdc24-canvas"), "stop", "join"])
        events.clear()
        with mock.patch.object(sr, "live", side_effect=lambda *a: 0), \
                mock.patch.dict(sr.os.environ, {"CANVAS_BUCKET": ""}):
            sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start)
        self.assertEqual(events, [("start", "")])


if __name__ == "__main__":
    unittest.main()
