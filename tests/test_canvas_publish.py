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

    def test_an_activity_phrase_is_published_beside_the_card(self):
        # GROK-CANVAS-RULE-ACTIVITY-20261024T0405Z: the page's activity strip shows it beside the agent's chip.
        got = cp.clean_item("grok", "3", item(activity="crafting four checks"))
        self.assertEqual("crafting four checks", got["activity"])
        self.assertEqual("x" * cp.ACTIVITY_MAX, cp.clean_item("grok", "3", item(activity="x" * cp.ACTIVITY_MAX))["activity"])
        self.assertNotIn("activity", cp.clean_item("grok", "3", item()))
        self.assertNotIn("activity", cp.clean_item("grok", "3", item(activity="")))
        self.assertEqual(80, cp.ACTIVITY_MAX)

    def test_an_activity_outside_the_rule_drops_the_card(self):
        for bad in ("x" * (cp.ACTIVITY_MAX + 1), "two|parts", "line\nbreak", "tab\there", "bell\x07", 7, True,
                    ["crafting"], {"a": 1}, None):
            raw = json.loads(item())
            raw["activity"] = bad
            if bad is None:
                self.assertIsNotNone(cp.clean_item("grok", "3", json.dumps(raw)), "null is absent")
                continue
            self.assertIsNone(cp.clean_item("grok", "3", json.dumps(raw)), repr(bad))

    def test_the_chair_s_activity_is_held_to_its_no_link_rule(self):
        note = dict(type="note", body="three checks")
        self.assertIsNotNone(cp.clean_item("chair", "1", item(activity="tracking three changes", **note)))
        for link in ("see www.example.com", "open https://x.test", "[the plan](notes.md)"):
            self.assertIsNone(cp.clean_item("chair", "1", item(activity=link, **note)), link)

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
                     {"body": "JavaScript:alert(1)"}, {"body": "b" * 500},
                     {"body": "see [the plan](notes.md)"}, {"body": "see [the plan](/rooms/demo)"},
                     {"body": "![diagram](pic.png)"}, {"title": "[x](y)"},
                     {"body": "[x](../secret)"}):
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

    def __call__(self, bucket, name, data, allow=None):
        if allow is not None and not allow():
            return
        if self.fail:
            self.fail -= 1
            raise OSError("boom: Bearer abc")
        self.calls.append((bucket, name, json.loads(data)))


class Publish(unittest.TestCase):
    def setUp(self):
        self.conn, self.up, self.t = FakeRedis(), Uploads(), {"now": NOW}
        self.log = mock.Mock()
        self.lease = {"held": True}
        self.pub = cp.Publisher(self.conn, "sfdc24-canvas", self.up, self.log, clock=lambda: self.t["now"],
                                holds=lambda: self.lease["held"])
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

    # Aya on c633373: publication after expiry, after a stop, and after the lease was lost.
    def test_nothing_is_written_once_the_lease_is_another_worker_s(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self.lease["held"] = False
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))
        self.assertIn("not provably this worker's", repr(self.log.warning.call_args))
        self.lease["held"] = True
        self.assertEqual(self.pub.tick(), ["demo"], "the lease back, the window still open: it publishes")

    def test_a_lease_that_cannot_be_read_is_not_held(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")

        def broken():
            raise ConnectionError("dropped")
        self.pub.holds = broken
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))
        self.pub.holds = None
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []), "no way to ask is no")

    def test_a_stop_that_lands_inside_the_round_is_kept(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        real = self.conn.hgetall

        def stop_while_reading(key):
            cp.open_window(self.conn, "demo", "0", "grok", NOW, "sfdc24-canvas")      # minutes=0: stopped
            return real(key)
        self.conn.hgetall = stop_while_reading
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_an_expiry_that_lands_inside_the_round_is_kept(self):
        cp.open_window(self.conn, "demo", "1", "grok", NOW, "sfdc24-canvas")
        self.later(59)
        real = self.conn.hgetall

        def slow(key):
            self.t["now"] = NOW + datetime.timedelta(seconds=61)                         # the round took 2 s
            return real(key)
        self.conn.hgetall = slow
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_a_stopped_publisher_writes_nothing_more(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self.pub.stop.set()
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def _after_precheck(self, mutate):
        """`mutate` runs once may_write has said yes, which is the last check before the snapshot is sent."""
        real = self.pub.may_write

        def wrapped(room):
            ok = real(room)
            if ok and not getattr(self, "_mutated", False):
                self._mutated = True
                mutate()
            return ok
        self.pub.may_write = wrapped

    def test_a_stop_after_the_last_precheck_does_not_commit(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self._after_precheck(lambda: self.pub.stop.set())
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_a_deleted_window_after_the_last_precheck_does_not_commit(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self._after_precheck(lambda: self.conn.delete(cp.window_key("demo")))
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_a_lost_lease_after_the_last_precheck_does_not_commit(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self._after_precheck(lambda: self.lease.update(held=False))
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_an_expiry_after_the_last_precheck_does_not_commit(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self._after_precheck(lambda: self.t.update(now=NOW + datetime.timedelta(hours=3)))
        self.assertEqual((self.pub.tick(), self.up.calls), ([], []))

    def test_an_entered_upload_does_not_commit_after_a_stop(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        sent = []

        def upload(bucket, name, data, allow=None):
            self.pub.stop.set()
            if allow is not None and not allow():
                return
            sent.append(data)
        self.pub.upload = upload
        self.assertEqual((self.pub.tick(), sent), ([], []))

    def test_a_stop_and_reopen_does_not_send_the_old_snapshot(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")

        def reopen():
            cp.open_window(self.conn, "demo", "0", "grok", NOW, "sfdc24-canvas")
            put(self.conn, "grok", "1", item(type="note", version=2, body="FRESH"))
            cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        self._after_precheck(reopen)
        self.assertEqual(self.pub.tick(), ["demo"])
        self.assertEqual(["FRESH"], [i["body"] for i in self.up.calls[-1][2]["items"]])
        self.assertNotIn("flowchart", json.dumps(self.up.calls[0][2]))

    def test_a_privacy_revocation_after_the_snapshot_is_not_what_gets_sent(self):
        cp.open_window(self.conn, "demo", "30", "grok", NOW, "sfdc24-canvas")
        real = self.conn.hgetall

        def revoke_after_read(key):
            found = real(key)
            put(self.conn, "grok", "1", item(public=False, body="PRIVATE-REVOKED"))
            return found
        self.conn.hgetall = revoke_after_read
        self.pub.tick()
        blob = json.dumps([doc for _, _, doc in self.up.calls])
        self.assertNotIn("PRIVATE-REVOKED", blob)
        self.assertNotIn("flowchart", blob)


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

    def test_an_entered_upload_is_not_sent_once_the_fence_fails(self):
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

        with self.assertRaises(cp.Aborted):
            cp.gcs_upload("sfdc24-canvas", "rooms/demo.json", b"{}", lambda: "tok", opener, allow=lambda: False)
        self.assertEqual(seen, [])

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
            self.assertIsNone(cp.start(FakeRedis(), bucket, mock.Mock(), upload=Uploads(), holds=lambda: True))
        self.assertIsNone(cp.start(FakeRedis(), "sfdc24-canvas", mock.Mock(), upload=Uploads()),
                          "without a way to ask about the lease, nothing starts")

    def test_start_hands_the_lease_to_the_publisher(self):
        import threading
        conn, up, asked = FakeRedis(), Uploads(), threading.Event()
        put(conn, "grok", "1", item())
        cp.open_window(conn, "demo", "30", "grok", datetime.datetime.now(datetime.timezone.utc), "sfdc24-canvas")

        def holds():
            asked.set()
            return False
        pub = cp.start(conn, "sfdc24-canvas", mock.Mock(), upload=up, holds=holds)
        self.assertTrue(asked.wait(10), "the publisher asks about the lease before it writes")
        pub.stop.set()
        pub.thread.join(timeout=10)
        self.assertEqual([], up.calls)

    def test_start_runs_a_thread_that_stops(self):
        pub = cp.start(FakeRedis(), "sfdc24-canvas", mock.Mock(), upload=Uploads(), holds=lambda: True)
        pub.stop.set()
        pub.thread.join(timeout=10)
        self.assertFalse(pub.thread.is_alive())


FAMILY = "conf-0123456789abcdef"
HIS_ROOM = FAMILY + "-a1b2c3d4"
ME = "bus-requests-me"


class CallRedis(FakeRedis):
    """FakeRedis with redis-py's WATCH/MULTI pipeline. `race` runs just before EXEC, the way another writer
    would; any WATCHed key it changes fails the EXEC with WatchError and nothing queued is written."""

    def __init__(self):
        super().__init__()
        self.race = None

    def pipeline(self):
        conn = self

        class Pipe:
            def __enter__(self):
                self.watched, self.queued = {}, None
                return self

            def __exit__(self, *exc):
                return False

            def watch(self, *keys):
                self.watched = {k: conn.store.get(k) for k in keys}

            def unwatch(self):
                self.watched = {}

            def get(self, key):
                return conn.store.get(key)

            def multi(self):
                self.queued = []

            def set(self, key, value, ex=None, nx=False):
                self.queued.append(("set", key, value, ex, nx))

            def delete(self, key):
                self.queued.append(("delete", key))

            def execute(self):
                if conn.race:
                    race, conn.race = conn.race, None
                    race()
                if any(conn.store.get(k) != v for k, v in self.watched.items()):
                    raise cp.WatchError()
                out = []
                for op in self.queued:
                    if op[0] == "set":
                        out.append(conn.set(op[1], op[2], nx=op[4], ex=op[3]))
                    else:
                        out.append(conn.delete(op[1]))
                return out
        return Pipe()


def call_state(conn, room=HIS_ROOM, at=NOW, closed=False):
    key = cp.STATE_PREFIX + room + cp.STATE_SUFFIX
    conn.store[key] = "x"                  # SCAN finds it; the hash holds the record
    conn.hashes[key] = {"room_id": room, "updated_at": str(int(at.timestamp()))}
    if closed:
        conn.hashes[key]["closed"] = "1"


class HisCalls(unittest.TestCase):
    """The owner, 2026-10-29: the page's room publishes while one of his calls is under way."""

    def setUp(self):
        self.conn, self.up, self.t = CallRedis(), Uploads(), {"now": NOW}
        self.conn.store[cp.LEASE_KEY] = ME
        self.log = mock.Mock()
        self.pub = cp.Publisher(self.conn, "sfdc24-canvas", self.up, self.log, clock=lambda: self.t["now"],
                                holds=lambda: self.conn.store.get(cp.LEASE_KEY) == ME, family=FAMILY)
        put(self.conn, "grok", "1", item(), room=cp.CALL_ROOM)

    def later(self, seconds):
        self.t["now"] = self.t["now"] + datetime.timedelta(seconds=seconds)

    def window(self):
        return cp.read_window(self.conn, cp.CALL_ROOM)

    def round(self, seconds=None):
        self.later(cp.CALL_SCAN_SECONDS if seconds is None else seconds)
        return self.pub.calls()

    def test_a_live_call_opens_the_page_room_and_it_publishes_in_the_same_tick(self):
        call_state(self.conn)
        self.assertEqual(self.pub.tick(), [cp.CALL_ROOM])
        held = self.window()
        self.assertEqual(held["by"], cp.CALL_BY)
        self.assertEqual(self.conn.ttls[cp.window_key(cp.CALL_ROOM)], cp.CALL_MINUTES * 60)
        self.assertEqual(self.up.calls[-1][1], "rooms/%s.json" % cp.CALL_ROOM)

    def test_no_call_opens_nothing(self):
        self.assertEqual(self.pub.tick(), [])
        self.assertIsNone(self.window())

    def test_an_open_window_is_kept_as_it_is_while_the_call_goes_on(self):
        call_state(self.conn)
        self.assertEqual(self.pub.calls(), "opened")
        before = self.conn.store[cp.window_key(cp.CALL_ROOM)]
        call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=cp.CALL_SCAN_SECONDS))
        self.assertEqual(self.round(), "open")
        self.assertEqual(self.conn.store[cp.window_key(cp.CALL_ROOM)], before, "not reopened under a new gen")

    def test_a_closed_call_closes_the_window_this_rule_opened(self):
        call_state(self.conn)
        self.pub.tick()
        call_state(self.conn, closed=True)
        self.assertEqual(self.round(), "closed")
        self.assertIsNone(self.window())

    def test_a_record_left_unwritten_counts_as_over(self):
        call_state(self.conn)
        self.pub.tick()
        self.assertEqual(self.round(cp.CALL_IDLE_SECONDS + 1), "closed")
        self.assertIsNone(self.window())

    def test_a_long_call_gets_the_window_again_when_it_lapses(self):
        call_state(self.conn)
        self.pub.tick()
        self.later(cp.CALL_MINUTES * 60 + 1)
        call_state(self.conn, at=self.t["now"])          # the call is still being written
        self.assertEqual(self.pub.calls(), "opened")
        self.assertGreater(cp._until_of(json.dumps(self.window()), self.t["now"]), self.t["now"])

    def test_a_window_someone_asked_for_is_never_touched(self):
        cp.open_window(self.conn, cp.CALL_ROOM, "30", "aya", NOW, "sfdc24-canvas")
        before = self.conn.store[cp.window_key(cp.CALL_ROOM)]
        call_state(self.conn)
        self.assertEqual(self.pub.calls(), "open")
        call_state(self.conn, closed=True)
        self.assertEqual(self.round(), "idle")
        self.assertEqual(self.conn.store[cp.window_key(cp.CALL_ROOM)], before)

    def test_a_window_replaced_before_the_delete_is_kept(self):
        call_state(self.conn)
        self.pub.tick()
        call_state(self.conn, closed=True)

        def an_ask_lands():
            self.conn.store[cp.window_key(cp.CALL_ROOM)] = json.dumps(
                {"by": "aya", "gen": "g2", "room": cp.CALL_ROOM, "until": "2026-10-20T23:00:00Z"})
        self.conn.race = an_ask_lands
        self.assertEqual(self.round(), "idle")
        self.assertEqual(self.window()["by"], "aya")

    def test_an_ask_that_wins_the_race_to_open_is_left_open(self):
        call_state(self.conn)
        self.conn.race = lambda: cp.open_window(self.conn, cp.CALL_ROOM, "30", "aya", NOW, "sfdc24-canvas")
        self.assertEqual(self.pub.calls(), "idle")
        self.assertEqual(self.window()["by"], "aya")

    # --- Aya's cycle-1 HOLD on 0fde974: a stop holds for the call; writes are fenced on the lease and the stop.

    def test_a_stop_during_his_call_holds_until_that_call_is_over(self):
        call_state(self.conn)
        self.assertEqual(self.pub.calls(), "opened")
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", self.t["now"], "sfdc24-canvas")
        for _ in range(4):
            call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=cp.CALL_SCAN_SECONDS))
            self.assertEqual(self.round(), "stopped")
            self.assertIsNone(self.window(), "the same call does not reopen what was stopped")
        self.assertEqual(cp._json_of(self.conn.store[cp.CALL_STOP_KEY])["call"], HIS_ROOM)
        call_state(self.conn, closed=True)
        self.assertEqual(self.round(), "idle")
        self.assertNotIn(cp.CALL_STOP_KEY, self.conn.store, "the stop ends with its call")
        call_state(self.conn, room=FAMILY + "-0000ffff", at=self.t["now"] + datetime.timedelta(seconds=15))
        self.assertEqual(self.round(), "opened", "his next call opens again")

    def test_a_stop_bound_to_one_call_does_not_hold_the_next(self):
        call_state(self.conn)
        self.pub.calls()
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", self.t["now"], "sfdc24-canvas")
        call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=15))
        self.assertEqual(self.round(), "stopped")
        call_state(self.conn, closed=True)
        call_state(self.conn, room=FAMILY + "-0000ffff", at=self.t["now"] + datetime.timedelta(seconds=15))
        self.assertEqual(self.round(), "opened")
        self.assertNotIn(cp.CALL_STOP_KEY, self.conn.store)

    def test_a_stop_made_between_his_calls_does_not_hold_the_next_call(self):
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "claude-code-cli", NOW, "sfdc24-canvas")
        self.assertEqual(self.pub.calls(), "idle")
        self.assertNotIn(cp.CALL_STOP_KEY, self.conn.store, "a stop with no call live is spent at once")
        call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=cp.CALL_SCAN_SECONDS))
        self.assertEqual(self.round(), "opened")

    def test_a_stop_that_lands_before_the_watch_is_read_inside_it(self):
        call_state(self.conn)
        real = cp.read_window

        def read_then_stop(conn, room):
            held = real(conn, room)
            self.conn.store[cp.CALL_STOP_KEY] = json.dumps({"by": "owner", "at": "x"})
            return held
        with mock.patch.object(cp, "read_window", side_effect=read_then_stop):
            self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())

    def test_another_opener_s_lapsed_window_is_not_overwritten_before_its_ttl_ends(self):
        cp.open_window(self.conn, cp.CALL_ROOM, "1", "aya", NOW, "sfdc24-canvas")
        before = self.conn.store[cp.window_key(cp.CALL_ROOM)]
        self.later(120)                                  # its until has passed; the fake's TTL never fires
        call_state(self.conn, at=self.t["now"])
        self.assertEqual(self.pub.calls(), "open")
        self.assertEqual(self.conn.store[cp.window_key(cp.CALL_ROOM)], before)

    def newer_stop_after_the_read(self):
        """The first read of the stop marker returns what is there; a newer stop lands right after it."""
        real, fired = self.conn.get, []

        def get(key):
            value = real(key)
            if key == cp.CALL_STOP_KEY and not fired:
                fired.append(1)
                self.conn.store[cp.CALL_STOP_KEY] = json.dumps({"by": "owner", "at": "newer"})
            return value
        return mock.patch.object(self.conn, "get", side_effect=get)

    def test_a_newer_stop_landing_before_the_old_one_is_spent_is_kept_and_nothing_opens(self):
        call_state(self.conn)
        self.pub.calls()
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", self.t["now"], "sfdc24-canvas")
        call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=15))
        self.assertEqual(self.round(), "stopped")                      # bound to HIS_ROOM
        call_state(self.conn, closed=True)
        call_state(self.conn, room=FAMILY + "-0000ffff", at=self.t["now"] + datetime.timedelta(seconds=15))
        with self.newer_stop_after_the_read():
            self.assertEqual(self.round(), "stopped")
        self.assertIsNone(self.window(), "the round that met a newer stop does not open")
        self.assertEqual(cp._json_of(self.conn.store[cp.CALL_STOP_KEY])["at"], "newer", "the newer stop is kept")
        self.assertEqual(self.round(), "stopped", "and it then holds the call it is bound to")
        self.assertEqual(cp._json_of(self.conn.store[cp.CALL_STOP_KEY])["call"], FAMILY + "-0000ffff")

    def test_a_newer_stop_landing_before_a_spent_stop_is_cleared_with_no_call_is_kept(self):
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", NOW, "sfdc24-canvas")
        with self.newer_stop_after_the_read():
            self.assertEqual(self.pub.calls(), "idle")
        self.assertEqual(cp._json_of(self.conn.store[cp.CALL_STOP_KEY])["at"], "newer")

    def test_a_newer_stop_landing_before_the_bind_is_not_overwritten(self):
        call_state(self.conn)
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", NOW, "sfdc24-canvas")
        with self.newer_stop_after_the_read():
            self.assertEqual(self.pub.calls(), "stopped")
        held = cp._json_of(self.conn.store[cp.CALL_STOP_KEY])
        self.assertEqual(held["at"], "newer")
        self.assertNotIn("call", held, "bound on a later round, from what is there then")
        self.assertIsNone(self.window())

    def test_a_stop_not_confirmed_removed_keeps_the_round_from_opening(self):
        call_state(self.conn)
        self.pub.calls()
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", self.t["now"], "sfdc24-canvas")
        call_state(self.conn, at=self.t["now"] + datetime.timedelta(seconds=15))
        self.round()
        call_state(self.conn, closed=True)
        call_state(self.conn, room=FAMILY + "-0000ffff", at=self.t["now"] + datetime.timedelta(seconds=15))
        self.conn.race = lambda: self.conn.store.__setitem__(cp.LEASE_KEY, "bus-requests-other")
        self.assertEqual(self.round(), "stopped")
        self.assertIsNone(self.window())

    def test_a_stop_only_of_the_page_room_is_remembered(self):
        cp.open_window(self.conn, "demo", "0", "owner", NOW, "sfdc24-canvas")
        self.assertNotIn(cp.CALL_STOP_KEY, self.conn.store)
        cp.open_window(self.conn, cp.CALL_ROOM, "0", "owner", NOW, "sfdc24-canvas")
        self.assertEqual(self.conn.ttls[cp.CALL_STOP_KEY], cp.CALL_STOP_SECONDS)

    def test_a_stop_that_lands_just_before_the_open_wins(self):
        call_state(self.conn)
        self.conn.race = lambda: self.conn.set(cp.CALL_STOP_KEY, json.dumps({"by": "owner", "at": "x"}))
        self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())

    def test_a_lease_lost_during_the_scan_opens_nothing(self):
        call_state(self.conn)
        real = cp.live_call

        def scan_then_lose(*a):
            room = real(*a)
            self.conn.store[cp.LEASE_KEY] = "bus-requests-other"
            return room
        with mock.patch.object(cp, "live_call", side_effect=scan_then_lose):
            self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())

    def test_a_lease_lost_between_the_watch_and_the_write_opens_nothing(self):
        call_state(self.conn)
        self.conn.race = lambda: self.conn.store.__setitem__(cp.LEASE_KEY, "bus-requests-other")
        self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())

    def test_a_lease_lost_closes_nothing_either(self):
        call_state(self.conn)
        self.pub.calls()
        call_state(self.conn, closed=True)
        self.conn.race = lambda: self.conn.store.__setitem__(cp.LEASE_KEY, "bus-requests-other")
        self.assertEqual(self.round(), "idle")
        self.assertEqual(self.window()["by"], cp.CALL_BY, "the next lease holder closes it, or its TTL does")

    def test_a_stopped_publisher_opens_nothing(self):
        call_state(self.conn)
        self.pub.stop.set()
        self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())
        self.assertFalse(self.pub.may_govern())

    def test_a_publisher_stopped_mid_round_opens_nothing(self):
        call_state(self.conn)
        real = cp.live_call

        def scan_then_stop(*a):
            room = real(*a)
            self.pub.stop.set()
            return room
        with mock.patch.object(cp, "live_call", side_effect=scan_then_stop):
            self.assertEqual(self.pub.calls(), "idle")
        self.assertIsNone(self.window())

    def test_keep_call_window_writes_nothing_when_fenced_off(self):
        call_state(self.conn)
        self.assertEqual(cp.keep_call_window(self.conn, FAMILY, NOW, "sfdc24-canvas", lambda: False), "idle")
        self.assertIsNone(self.window())
        self.assertEqual(cp.keep_call_window(self.conn, FAMILY, NOW, "sfdc24-canvas", lambda: True), "opened")
        call_state(self.conn, closed=True)
        self.assertEqual(cp.keep_call_window(self.conn, FAMILY, NOW, "sfdc24-canvas", lambda: False), "idle")
        self.assertEqual(self.window()["by"], cp.CALL_BY)

    # ---

    def test_only_his_family_counts(self):
        for room in ("conf-fedcba9876543210-a1b2c3d4", FAMILY + "-a1b2c3d", FAMILY + "-A1B2C3D4",
                     FAMILY + "-a1b2c3d4-x", "workshop"):
            conn = CallRedis()
            call_state(conn, room=room)
            self.assertEqual(cp.live_call(conn, FAMILY, NOW), "", room)
        conn = CallRedis()
        call_state(conn)
        self.assertEqual(cp.live_call(conn, FAMILY, NOW), HIS_ROOM)

    def test_unreadable_or_future_records_do_not_count(self):
        call_state(self.conn)
        self.conn.hashes[cp.STATE_PREFIX + HIS_ROOM + cp.STATE_SUFFIX]["updated_at"] = "soon"
        self.assertEqual(cp.live_call(self.conn, FAMILY, NOW), "")
        call_state(self.conn, at=NOW + datetime.timedelta(seconds=60))
        self.assertEqual(cp.live_call(self.conn, FAMILY, NOW), "")

    def test_off_without_a_family(self):
        for family in ("", None, "conf-short", "conf-0123456789ABCDEF", "workshop"):
            pub = cp.Publisher(self.conn, "sfdc24-canvas", self.up, self.log, clock=lambda: NOW,
                               holds=lambda: True, family=family)
            call_state(self.conn)
            self.assertEqual(pub.calls(), "", family)
            self.assertIsNone(self.window())
            self.assertEqual(cp.live_call(self.conn, family or "", NOW), "")
        self.assertEqual(cp.call_family({"CANVAS_CALL_FAMILY": " %s " % FAMILY}), FAMILY)
        self.assertEqual(cp.call_family({}), "")
        self.assertEqual(cp.call_family({"CANVAS_CALL_FAMILY": "conf-*"}), "")

    def test_nothing_opens_without_the_lease(self):
        call_state(self.conn)
        self.conn.store[cp.LEASE_KEY] = "bus-requests-other"
        self.assertEqual(self.pub.calls(), "")
        self.assertIsNone(self.window())

    def test_his_calls_are_looked_at_every_scan_interval_not_every_tick(self):
        call_state(self.conn)
        self.assertEqual(self.pub.calls(), "opened")
        self.conn.delete(cp.window_key(cp.CALL_ROOM))
        self.assertEqual(self.round(cp.CALL_SCAN_SECONDS - 1), "")
        self.assertIsNone(self.window())
        self.assertEqual(self.round(1), "opened")

    def test_a_redis_failure_costs_one_round_and_names_only_its_type(self):
        call_state(self.conn)
        with mock.patch.object(self.conn, "scan_iter", side_effect=OSError("secret-host:6378")):
            self.assertEqual(self.pub.calls(), "")
        said = " ".join(str(c) for c in self.log.warning.call_args_list)
        self.assertIn("OSError", said)
        self.assertNotIn("secret-host", said)

    def test_start_takes_the_family_from_the_environment(self):
        with mock.patch.dict(cp_os().environ, {"CANVAS_CALL_FAMILY": FAMILY}):
            pub = cp.start(CallRedis(), "sfdc24-canvas", mock.Mock(), upload=Uploads(), holds=lambda: False)
        pub.stop.set()
        pub.thread.join(timeout=10)
        self.assertEqual(pub.family, FAMILY)
        with mock.patch.dict(cp_os().environ, {}, clear=False):
            cp_os().environ.pop("CANVAS_CALL_FAMILY", None)
            pub = cp.start(CallRedis(), "sfdc24-canvas", mock.Mock(), upload=Uploads(), holds=lambda: False)
        pub.stop.set()
        pub.thread.join(timeout=10)
        self.assertEqual(pub.family, "")

    def test_the_lease_key_is_the_live_worker_s(self):
        import serve_requests as sr                                       # noqa: PLC0415
        self.assertEqual(cp.LEASE_KEY, sr.WORKER_LOCK_KEY)

    def test_no_agent_can_write_the_records_that_open_or_hold_the_page(self):
        import redis_gov                                                 # noqa: PLC0415
        self.assertIn(cp.STATE_PREFIX, redis_gov.PROTECTED)
        self.assertTrue(cp.CALL_STOP_KEY.startswith("gov:") and "gov:" in redis_gov.PROTECTED)


def cp_os():
    import os                                                            # noqa: PLC0415
    return os


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

        def start(conn, bucket, log, holds):
            events.append(("start", bucket))
            leases.append(holds)
            return Pub() if bucket else None
        leases = []

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
        conn = FakeRedis()
        with mock.patch.object(sr, "live", side_effect=lambda *a: 0),                 mock.patch.dict(sr.os.environ, {"CANVAS_BUCKET": "sfdc24-canvas"}):
            sr.live_with_canvas(conn, {}, "", "me", NOW, 10, 300, start=start)
        holds = leases[-1]
        conn.set(sr.WORKER_LOCK_KEY, "me")
        self.assertTrue(holds(), "the lease is this run's")
        conn.set(sr.WORKER_LOCK_KEY, "bus-requests-other")
        self.assertFalse(holds(), "another execution took it")
        conn.delete(sr.WORKER_LOCK_KEY)
        self.assertFalse(holds(), "nobody holds it")


if __name__ == "__main__":
    unittest.main()
