"""Bounded source-only containment regressions: real governance -> Redis -> canvas snapshot.

No live Redis, network, tokens, uploads or model calls. Legacy rows are explicitly
seeded as preexisting unsafe data; acceptance is always checked by the real sink.
"""
import base64
import copy
import datetime as dt
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import bus_request as br
import call_bridge as bridge
import call_lifecycle as life
import canvas_publish as canvas
from test_redis_gov import FakeRedis as GovernanceRedis, FakePipeline

NOW = dt.datetime(2026, 10, 30, 21, 0, tzinfo=dt.timezone.utc)
ASK = "call-0123456789ab"
OTHER = "call-abcdef012345"
AGENT = "claude-code-cli"
ROOM = "workshop"
CONF = "conf-0123456789abcdef-01234567"


class LifecyclePipeline(FakePipeline):
    def watch(self, *keys):
        for key in keys:
            super().watch(key)


class Redis(GovernanceRedis):
    """Existing typed/versioned governance fake, extended for actual lifecycle I/O."""
    def pipeline(self, transaction=True):
        return LifecyclePipeline(self)

    def hdel(self, key, *fields):
        self._need(key, "hash")
        value = self.data.get(key, {})
        removed = 0
        for field in fields:
            if field in value:
                del value[field]
                removed += 1
        self._bump(key)
        return removed

    def xrange(self, key, min="-", max="+", count=None):
        self._need(key, "stream")
        def order(entry):
            return tuple(int(part) for part in entry.split("-"))
        exclusive = min.startswith("(")
        lower = min[1:] if exclusive else min
        rows = []
        for eid, fields in self.data.get(key, []):
            if lower != "-" and (order(eid) < order(lower) or exclusive and eid == lower):
                continue
            if max != "+" and order(eid) > order(max):
                continue
            rows.append((eid, dict(fields)))
        return rows[:count] if count else rows


class CallCanvasContainment(unittest.TestCase):
    def setUp(self):
        self.conn = Redis()
        self.sequence = 0
        self.requests = 0
        self.conn.hset("v1:conf:%s:state" % CONF, mapping={"session_epoch": "epoch-containment", "closed": "0"})

    def event(self, **fields):
        self.sequence += 1
        eid = "%d-%d" % (int(NOW.timestamp() * 1000), self.sequence)
        value = {"room_id": CONF, "session_epoch": "epoch-containment", "actor_id": "own_containment",
                 "control_sequence": str(self.sequence), "proj_n": str(self.sequence)}
        value.update(fields)
        key = "v1:conf:%s:events" % CONF
        self.conn.data.setdefault(key, []).append((eid, value))
        self.conn._bump(key)
        return eid

    def claim(self, ask=ASK, agent=AGENT, room=ROOM):
        eid = self.event(kind="turn.started", utterance="Claude, draw a private input's exact flow.")
        info = life.claim(self.conn, ask, CONF, eid, agent, room, NOW, bridge.claimed_card(agent, NOW),
                          lambda utterance: {"text": utterance, "from": "call-bridge", "ask": ask})
        self.assertIsNotNone(info)
        self.assertEqual("OPEN", info["state"])
        return info

    def card(self, info, **overrides):
        value = {"v": 1, "type": "note", "title": "Agent-authored public result", "body": "Exact published content.",
                 "status": "working", "public": True, "version": 1, "ts": life.stamp(NOW),
                 "ask": info["ask"], "source_revision": info["source_revision"]}
        value.update(overrides)
        return value

    def commit_card(self, info, card=None):
        card = card or self.card(info)
        result = life.operate(self.conn, info["agent"], info["ask"], "card",
                              {"source_revision": info["source_revision"], "task_revision": info["rev"], "card": card},
                              "CLI6-CARD-" + "1" * 32, NOW)
        self.assertEqual(info["ask"] + "-work", result["field"])
        return life.get(self.conn, info["ask"]), card

    def cancel(self, info):
        eid = self.event(kind="task.cancel.requested", task_id=info["ask"], task_revision=str(info["rev"]),
                         source_revision=info["source_revision"], command_id="containment-cancel")
        result = life.cancel_event(self.conn, CONF, eid, NOW)
        self.assertEqual("CANCELLED", result["state"])
        return result

    def legacy(self, field, card, room=ROOM, agent=AGENT):
        """Bypass current write guard ONLY to represent data already stored before hardening."""
        self.conn.hset(canvas.PARTITION % (room, agent), field, life.dump(card))

    def snapshot(self, room=ROOM, now=NOW, scope=None):
        return canvas.snapshot(self.conn, room, now, scope=scope)["items"]

    def ids(self, **kwargs):
        return {item["id"] for item in self.snapshot(**kwargs)}

    def governed(self, op, *, field="", value=None, room=ROOM, agent=AGENT):
        self.requests += 1
        rid = "REQ-CONTAIN%04d" % self.requests
        parts = ["BCB", "v=1", "id=" + rid, "req=" + rid, "do=redis-op", "op=" + op,
                 "key=" + canvas.PARTITION % (room, agent)]
        if field:
            parts.append("field=" + field)
        if value is not None:
            raw = value if isinstance(value, str) else life.dump(value)
            parts.extend(["enc=b64", "val=" + base64.b64encode(raw.encode()).decode()])
        row = [rid, life.stamp(NOW), AGENT, "bus-reconciler", "AYA_REQ", "|".join(parts), "OPEN", "Blackboard", "", ""]
        written = []
        out = br.handle([row], self.conn, now=NOW, append=written.append)
        self.assertEqual(1, len(out["answered"]), out)
        self.assertEqual(["AYA_RESULT"], [entry["action_type"] for entry in written])
        return out["answered"][0]["status"], written[0]

    def assert_hidden(self, field, **kwargs):
        self.assertNotIn(AGENT + "/" + field, self.ids(**kwargs))

    def test_authoritative_claim_and_committed_work_publish_but_generic_hset_cannot_replace_them(self):
        info = self.claim()
        self.assertIn(AGENT + "/" + ASK, self.ids())
        info, card = self.commit_card(info)
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids())
        partition = canvas.PARTITION % (ROOM, AGENT)
        before = self.conn.hgetall(partition)
        status, _ = self.governed("hset", field=ASK + "-work", value=dict(card, title="FORGED replacement"))
        self.assertEqual("REFUSED", status)
        self.assertEqual(before, self.conn.hgetall(partition))
        self.assertNotIn("FORGED", json.dumps(self.snapshot()))

    def test_initial_claim_digest_and_authoritative_refresh_cannot_be_forged_or_revived(self):
        info = self.claim()
        partition = canvas.PARTITION % (ROOM, AGENT)
        initial = json.loads(self.conn.hget(partition, ASK))
        self.assertEqual(life.digest(canvas.clean_item(AGENT, ASK, life.dump(initial))), info["claim_hash"])
        self.legacy(ASK, dict(initial, body="not the committed claim"))
        self.assert_hidden(ASK)
        refreshed = dict(initial, title="Still working", body="Authoritative bridge status", version=2)
        self.assertTrue(life.refresh_claim_card(self.conn, ASK, refreshed, NOW))
        self.assertIn(AGENT + "/" + ASK, self.ids())
        current = life.get(self.conn, ASK)
        self.assertEqual(info["rev"], current["rev"], "a display refresh does not advance work revision")
        self.assertEqual(life.digest(canvas.clean_item(AGENT, ASK, life.dump(refreshed))), current["claim_hash"])
        self.cancel(current)
        self.assertFalse(life.refresh_claim_card(self.conn, ASK, refreshed, NOW))
        self.legacy(ASK, refreshed)
        self.assert_hidden(ASK)
        self.assertEqual("CANCELLED", life.get(self.conn, ASK)["state"])

    def test_generic_managed_fields_or_markers_are_refused_even_without_task_authority(self):
        public = {"v": 1, "type": "note", "title": "legacy", "body": "content", "status": "working",
                  "version": 1, "public": True, "ts": life.stamp(NOW)}
        cases = [(ASK, public), (ASK + "-work", public), (ASK + "-alias", public), ("call-invalid", public)]
        cases += [("ordinary", dict(public, ask=value)) for value in (ASK, "", "bad", None, [], 7)]
        cases += [("ordinary", dict(public, source_revision=value)) for value in ("a" * 64, "", None)]
        for field, card in cases:
            with self.subTest(field=field, card=card):
                status, _ = self.governed("hset", field=field, value=card)
                self.assertEqual("REFUSED", status)
                self.assertIsNone(self.conn.hget(canvas.PARTITION % (ROOM, AGENT), field))
                self.assertEqual([], self.snapshot())

    def test_missing_bad_or_other_task_metadata_cannot_publish_preexisting_work(self):
        info = self.claim()
        info, card = self.commit_card(info)
        other = self.claim(OTHER)
        bad = []
        for key in ("ask", "source_revision"):
            altered = dict(card)
            altered.pop(key)
            bad.append(altered)
        bad += [dict(card, ask=value) for value in ("", "wrong", None, [], 7)]
        bad += [dict(card, ask=OTHER, source_revision=other["source_revision"]), dict(card, source_revision="f" * 64)]
        for altered in bad:
            with self.subTest(altered=altered):
                self.legacy(ASK + "-work", altered)
                self.assert_hidden(ASK + "-work")
                self.assert_hidden(ASK + "-work", scope={AGENT + "/" + ASK})
        self.legacy(ASK + "-work", card)
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids())

    def test_missing_malformed_or_key_misbound_authority_never_publishes_managed_work(self):
        info, card = self.commit_card(self.claim())
        key = life.TASK_KEY % ASK
        saved = self.conn.get(key)
        for authority in (None, "not JSON", life.dump(dict(info, ask=OTHER)),
                          life.dump(dict(info, state="PREPARED")), life.dump(dict(info, state="UNKNOWN")),
                          life.dump(dict(info, source_revision="invalid"))):
            with self.subTest(authority=authority):
                if authority is None:
                    self.conn.delete(key)
                else:
                    self.conn.set(key, authority)
                self.assert_hidden(ASK)
                self.assert_hidden(ASK + "-work")
        self.conn.set(key, saved)
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids())

    def test_alias_fields_and_copied_work_require_exact_partition_and_field(self):
        info, card = self.commit_card(self.claim())
        for field in (ASK + "-alias", "call-invalid", "ordinary", OTHER + "-work"):
            with self.subTest(field=field):
                self.legacy(field, card)
                self.assert_hidden(field)
        self.legacy(ASK + "-work", card, room="other-room")
        self.assert_hidden(ASK + "-work", room="other-room")
        self.legacy(ASK + "-work", card, agent="grok")
        self.assertNotIn("grok/" + ASK + "-work", self.ids())
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids())

    def test_uncommitted_or_changed_public_work_fails_exact_authoritative_digest(self):
        info = self.claim()
        card = self.card(info)
        self.legacy(ASK + "-work", card)
        self.assert_hidden(ASK + "-work")
        info, card = self.commit_card(info, card)
        for key, value in (("title", "different"), ("body", "different"), ("version", 2), ("status", "done"),
                           ("ts", "2026-10-30T22:00:00Z"), ("activity", "different activity")):
            with self.subTest(key=key):
                self.legacy(ASK + "-work", dict(card, **{key: value}))
                self.assert_hidden(ASK + "-work")
        self.legacy(ASK + "-work", card)
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids())

    def test_cancelled_legacy_work_never_resurrects_and_ordinary_canvas_survives(self):
        info, card = self.commit_card(self.claim())
        ordinary = {k: v for k, v in card.items() if k not in ("ask", "source_revision")}
        self.assertEqual("OK", self.governed("hset", field="ordinary", value=ordinary)[0])
        self.cancel(info)
        self.legacy(ASK + "-work", card)
        self.legacy(ASK + "-alias", {k: v for k, v in card.items() if k != "ask"})
        self.assert_hidden(ASK + "-work")
        self.assert_hidden(ASK + "-alias")
        self.assertEqual({AGENT + "/ordinary"}, self.ids())
        self.assertEqual("REFUSED", self.governed("hset", field=ASK + "-work", value=card)[0])
        self.assertEqual("CANCELLED", life.get(self.conn, ASK)["state"])

    def test_open_task_requires_live_expiry_fence_and_not_just_an_open_record(self):
        info, card = self.commit_card(self.claim())
        late = NOW + dt.timedelta(seconds=life.MAX_AGE)
        self.assert_hidden(ASK, now=late)
        self.assert_hidden(ASK + "-work", now=late)
        self.conn.delete(life.EXPIRY_KEY % ASK)
        self.assert_hidden(ASK)
        self.assert_hidden(ASK + "-work")
        self.conn.set(life.EXPIRY_KEY % ASK, "f" * 64)
        self.assert_hidden(ASK + "-work")
        self.assertEqual("OPEN", life.get(self.conn, ASK)["state"], "publisher filters; it is not the state writer")
        expired = life.operate(self.conn, AGENT, ASK, "status", {}, "CLI6-STATUS-" + "2" * 32, late)
        self.assertEqual("EXPIRED", expired["state"])
        self.legacy(ASK + "-work", card)
        self.assert_hidden(ASK + "-work", now=late)

    def test_authoritative_done_keeps_only_exact_committed_work_not_a_bridge_claim(self):
        info, card = self.commit_card(self.claim())
        initial = json.loads(self.conn.hget(canvas.PARTITION % (ROOM, AGENT), ASK))
        done = life.operate(self.conn, AGENT, ASK, "complete",
                            {"source_revision": info["source_revision"], "task_revision": info["rev"],
                             "card_hash": info["card_hash"]}, "CLI6-COMPLETE-" + "3" * 32, NOW)
        self.assertEqual("DONE", done["state"])
        self.conn.delete(life.EXPIRY_KEY % ASK)
        self.assertIn(AGENT + "/" + ASK + "-work", self.ids(now=NOW + dt.timedelta(seconds=life.MAX_AGE)))
        self.legacy(ASK, initial)
        self.assert_hidden(ASK)
        self.legacy(ASK + "-work", dict(card, body="late overwrite"))
        self.assert_hidden(ASK + "-work")

    def test_hash_replacement_and_deletion_cannot_erase_managed_entries(self):
        info, card = self.commit_card(self.claim())
        ordinary = {k: v for k, v in card.items() if k not in ("ask", "source_revision")}
        before = copy.deepcopy(self.conn.hgetall(canvas.PARTITION % (ROOM, AGENT)))
        for op, value in (("hput", {"ordinary": life.dump(ordinary)}), ("del", None), ("set", "replace hash")):
            with self.subTest(op=op):
                self.assertEqual("REFUSED", self.governed(op, value=value)[0])
                self.assertEqual(before, self.conn.hgetall(canvas.PARTITION % (ROOM, AGENT)))
        self.assertEqual("REFUSED", self.governed("hput", value={ASK + "-work": life.dump(card)}, room="empty-room")[0])
        self.assertEqual([], self.snapshot(room="empty-room"))

    def test_partition_watch_prevents_replacement_if_managed_data_arrives_after_inspection(self):
        ordinary = {"v": 1, "type": "note", "title": "ordinary", "body": "unrelated", "status": "working",
                    "version": 1, "public": True, "ts": life.stamp(NOW)}
        self.assertEqual("OK", self.governed("hset", field="ordinary", value=ordinary)[0])
        partition = canvas.PARTITION % (ROOM, AGENT)
        conn = self.conn
        original_pipeline = conn.pipeline
        injected = []
        class ArrivingManagedData(LifecyclePipeline):
            def execute(self, raise_on_error=True):
                if partition in self.watched and not injected:
                    injected.append(True)
                    conn.hset(partition, ASK + "-work", life.dump(ordinary))
                return super().execute(raise_on_error=raise_on_error)
        conn.pipeline = lambda transaction=True: ArrivingManagedData(conn)
        try:
            status, _ = self.governed("hput", value={"replacement": life.dump(ordinary)})
        finally:
            conn.pipeline = original_pipeline
        self.assertEqual([True], injected)
        self.assertEqual("REFUSED", status)
        self.assertIsNotNone(conn.hget(partition, ASK + "-work"), "a conflicting replacement cannot erase it")
        self.assertIsNone(conn.hget(partition, "replacement"))
        self.assert_hidden(ASK + "-work", scope=None)
        self.assertEqual({AGENT + "/ordinary"}, self.ids())

    def test_unrelated_canvas_writes_replacements_and_deletions_remain_governed_and_functional(self):
        ordinary = {"v": 1, "type": "note", "title": "ordinary", "body": "unrelated", "status": "working",
                    "version": 1, "public": True, "ts": life.stamp(NOW)}
        self.assertEqual("OK", self.governed("hset", field="one", value=ordinary)[0])
        self.assertEqual({AGENT + "/one"}, self.ids())
        self.assertEqual("OK", self.governed("hput", value={"two": life.dump(ordinary)})[0])
        self.assertEqual({AGENT + "/two"}, self.ids())
        self.assertEqual("OK", self.governed("del")[0])
        self.assertEqual([], self.snapshot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
