"""scripts/bus_request.py: the bounded diagnostic Aya can ask for over the board.

The tests that matter most are the ones about what a FORGED request can do, because the board has no
authenticated sender and no allowlist here changes that. The defence is consequence, not identity, so
these prove the consequence is small: the server names the key, generates the nonce, sets the TTL,
and nothing a request says selects any of them.
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bus_request as br                                                 # noqa: E402

NOW = datetime.datetime(2026, 10, 6, 14, 0, 0, tzinfo=datetime.timezone.utc)


def row(action="AYA_REQ", source="aya", req="REQ-0001", do="redis-synthetic-probe",
        at="2026-10-06T13:55:00Z", row_id=None, extra=""):
    payload = "BCB|v=1|req=%s|do=%s%s" % (req, do, extra)
    return [row_id or ("AYA-" + req), at, source, "bus-reconciler", action,
            payload, "OPEN", "Blackboard", "a gist", ""]


def result_row(req="REQ-0001"):
    return ["X", "2026-10-06T13:58:00Z", "bus-reconciler", "aya", "AYA_RESULT",
            "BCB|v=1|answers=%s|text=OK" % req, "OPEN", "Blackboard", "g", ""]


class FakeRedis:
    def __init__(self, fail=None, drop_ttl=False, keep_after_del=False, corrupt=False):
        self.store, self.ttls = {}, {}
        self.fail, self.drop_ttl = fail, drop_ttl
        self.keep_after_del, self.corrupt = keep_after_del, corrupt
        self.keys_seen = []

    def setex(self, key, ttl, value):
        if self.fail == "setex":
            raise RuntimeError("boom: tried hunter2")
        self.keys_seen.append(key)
        self.store[key] = ("WRONG" if self.corrupt else value)
        if not self.drop_ttl:
            self.ttls[key] = ttl

    def get(self, key):
        if self.fail == "get":
            raise RuntimeError("boom")
        return self.store.get(key)

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def delete(self, key):
        if not self.keep_after_del:
            self.store.pop(key, None)


class Appender:
    def __init__(self):
        self.rows = []

    def __call__(self, spec):
        self.rows.append(spec)

    def kinds(self):
        return [r["action_type"] for r in self.rows]

    def blob(self):
        return repr(self.rows)


class TheHappyPath(unittest.TestCase):
    def test_a_good_request_gets_a_receipt_then_a_result(self):
        out = br.handle([row()], FakeRedis(), now=NOW, append=(app := Appender()))
        self.assertEqual(["AYA_RECEIPT", "AYA_RESULT"], app.kinds())
        self.assertEqual("OK", out["answered"][0]["status"])

    def test_both_rows_carry_the_same_request_id(self):
        br.handle([row(req="REQ-ABC")], FakeRedis(), now=NOW, append=(app := Appender()))
        for spec in app.rows:
            self.assertIn("answers=REQ-ABC", spec["payload"])

    def test_the_probe_cleans_up_after_itself(self):
        fake = FakeRedis()
        br.handle([row()], fake, now=NOW, append=Appender())
        self.assertEqual({}, fake.store)


class WhatAForgedRequestCanDo(unittest.TestCase):
    """The board has no authenticated sender, so this is the real threat model."""

    def test_the_request_cannot_choose_the_key(self):
        fake = FakeRedis()
        br.handle([row(extra="|key=bus:row:CCC-SOMETHING|namespace=bus:")], fake, now=NOW,
                  append=Appender())
        self.assertEqual(1, len(fake.keys_seen))
        self.assertTrue(fake.keys_seen[0].startswith(br.PROBE_NAMESPACE), fake.keys_seen)
        self.assertNotIn("bus:row", fake.keys_seen[0])

    def test_the_request_cannot_choose_the_ttl(self):
        fake = FakeRedis()
        br.handle([row(extra="|ttl=999999")], fake, now=NOW, append=Appender())
        self.assertEqual([br.PROBE_TTL_SECONDS], list(fake.ttls.values()))

    def test_the_request_cannot_choose_the_command(self):
        """One allowlisted action. FLUSHALL is not a word this worker knows."""
        out = br.handle([row(do="FLUSHALL")], FakeRedis(), now=NOW, append=Appender())
        self.assertEqual([], out["answered"])
        self.assertIn("not allowlisted", " ".join(out["refused"][0]["why"]))

    def test_an_unrecognised_claimed_sender_is_refused(self):
        out = br.handle([row(source="stranger")], FakeRedis(), now=NOW, append=Appender())
        self.assertEqual([], out["answered"])
        self.assertIn("not one this worker answers", " ".join(out["refused"][0]["why"]))

    def test_a_flood_is_capped_per_run(self):
        rows = [row(req="REQ-%04d" % n) for n in range(10)]
        out = br.handle(rows, FakeRedis(), now=NOW, append=Appender(), max_per_run=3)
        self.assertEqual(3, len(out["answered"]))
        self.assertEqual(7, out["capped"])

    def test_a_malformed_flood_does_not_become_a_flood_of_refusal_rows(self):
        """Otherwise it is the same denial of service with our name on the rows."""
        rows = [row(req="!!") for _ in range(6)]
        app = Appender()
        br.handle(rows, FakeRedis(), now=NOW, append=app)
        self.assertEqual([], app.rows)

    def test_the_oldest_request_is_served_first(self):
        """So a flood cannot starve the request that has waited longest."""
        rows = [row(req="REQ-NEWER", at="2026-10-06T13:59:00Z"),
                row(req="REQ-OLDER", at="2026-10-06T13:40:00Z")]
        out = br.handle(rows, FakeRedis(), now=NOW, append=Appender(), max_per_run=1)
        self.assertEqual("REQ-OLDER", out["answered"][0]["req_id"])

    def test_an_id_shorter_than_four_characters_is_not_an_id(self):
        """Found by the test above using three-character fixtures. The floor is deliberate - a
        one-character request id is not something to key idempotency on - and now it is stated."""
        out = br.handle([row(req="AB")], FakeRedis(), now=NOW, append=Appender())
        self.assertIn("not an id", " ".join(out["refused"][0]["why"]))


class TheBounds(unittest.TestCase):
    def test_a_stale_request_is_refused(self):
        out = br.handle([row(at="2026-10-06T13:00:00Z")], FakeRedis(), now=NOW, append=Appender())
        self.assertIn("older than 30 minutes", " ".join(out["refused"][0]["why"]))

    def test_a_future_stamped_request_is_refused(self):
        out = br.handle([row(at="2026-10-06T23:00:00Z")], FakeRedis(), now=NOW, append=Appender())
        self.assertIn("stamped in the future", " ".join(out["refused"][0]["why"]))

    def test_a_row_with_no_readable_timestamp_is_refused(self):
        out = br.handle([row(at="Open")], FakeRedis(), now=NOW, append=Appender())
        self.assertIn("staleness cannot be judged", " ".join(out["refused"][0]["why"]))

    def test_an_already_answered_request_does_nothing(self):
        app = Appender()
        out = br.handle([row(), result_row()], FakeRedis(), now=NOW, append=app)
        self.assertEqual([], out["answered"])
        self.assertEqual([], app.rows)

    def test_idempotency_is_read_off_the_board_not_local_state(self):
        """A Cloud Run job keeps no disk that survives it; a watermark that can be lost re-answers."""
        self.assertEqual({"REQ-0001"}, br.answered([result_row()]))

    def test_two_requests_with_one_id_are_answered_once(self):
        out = br.handle([row(), row(row_id="AYA-DUP")], FakeRedis(), now=NOW, append=Appender())
        self.assertEqual(1, len(out["answered"]))

    def test_a_non_request_row_is_ignored(self):
        self.assertIsNone(br.parse_request(row(action="DISPATCH")))
        self.assertIsNone(br.parse_request(["short"]))
        self.assertIsNone(br.parse_request("not a row"))


class WhatTheResultSays(unittest.TestCase):
    def test_no_nonce_or_key_name_reaches_the_board(self):
        fake = FakeRedis()
        app = Appender()
        br.handle([row()], fake, now=NOW, append=app)
        blob = app.blob()
        self.assertNotIn(br.PROBE_NAMESPACE, blob)
        for value in fake.store.values():
            self.assertNotIn(value, blob)

    def test_a_client_error_reports_its_type_and_never_its_message(self):
        """A client's error text can quote what it was sent."""
        app = Appender()
        out = br.handle([row()], FakeRedis(fail="setex"), now=NOW, append=app)
        self.assertEqual(["REQ-0001"], out["errors"])
        self.assertIn("failed_with=RuntimeError", app.blob())
        self.assertNotIn("hunter2", app.blob())

    def test_a_mismatched_read_back_is_FAILED_not_OK(self):
        out = br.handle([row()], FakeRedis(corrupt=True), now=NOW, append=Appender())
        self.assertEqual("FAILED", out["answered"][0]["status"])

    def test_a_missing_ttl_is_FAILED(self):
        out = br.handle([row()], FakeRedis(drop_ttl=True), now=NOW, append=Appender())
        self.assertEqual("FAILED", out["answered"][0]["status"])

    def test_a_key_left_behind_is_FAILED(self):
        out = br.handle([row()], FakeRedis(keep_after_del=True), now=NOW, append=Appender())
        self.assertEqual("FAILED", out["answered"][0]["status"])

    def test_no_value_written_to_the_board_contains_a_pipe(self):
        """The payload has no escaping: a literal pipe splits the row."""
        app = Appender()
        br.handle([row()], FakeRedis(), now=NOW, append=app)
        for spec in app.rows:
            for key in ("gist", "row_id", "source_tag", "target_surface", "action_type"):
                self.assertNotIn("|", spec[key], key)
            body = spec["payload"].split("text=", 1)[1]
            self.assertNotIn("|", body)

    def test_a_pipe_in_a_value_is_refused_at_construction(self):
        req = {"req_id": "R", "claimed_sender": "aya", "action": "x", "row_id": "R", "at": NOW}
        with self.assertRaises(AssertionError):
            br.row_for(br.RESULT_ACTION, req, "has a | pipe", "gist")


class FieldParsing(unittest.TestCase):
    def test_a_field_is_read_from_the_payload(self):
        self.assertEqual("REQ-9", br.field("BCB|v=1|req=REQ-9|do=x", "req"))

    def test_an_absent_field_is_empty(self):
        self.assertEqual("", br.field("BCB|v=1", "req"))

    def test_a_field_name_that_is_a_substring_of_another_is_not_confused(self):
        self.assertEqual("", br.field("BCB|v=1|xreq=no", "req"))


if __name__ == "__main__":
    unittest.main()
