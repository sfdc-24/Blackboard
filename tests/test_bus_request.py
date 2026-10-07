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

    def test_a_pipe_in_a_value_is_cleaned_rather_than_crashing_the_worker(self):
        """This used to assert, and the assertion was the blocker. row_for() publishes
        attacker-supplied values - the request id and, once, the claimed sender - so an invariant
        check there turned one malformed row into an AssertionError that took the whole worker down
        before any well-formed request behind it ran. Data gets cleaned; invariants get asserted."""
        req = {"req_id": "R", "claimed_sender": "aya", "action": "x", "row_id": "R", "at": NOW}
        row = br.row_for(br.RESULT_ACTION, req, "has a | pipe", "gi|st")
        self.assertNotIn("|", row["gist"])
        self.assertEqual(1, row["payload"].count("text="))
        # The payload's own separators survive; only the VALUES were cleaned.
        self.assertTrue(row["payload"].startswith("BCB|v=1|id="))
        self.assertIn("has a   pipe", row["payload"])

    def test_safe_strips_newlines_and_control_bytes_too(self):
        """A raw newline inside a JSON string is what broke an append earlier the same day, and a
        control byte once compiled itself into a regex. One cleaner, every published value."""
        self.assertEqual("a b", br.safe("a\nb"))
        self.assertEqual("a b", br.safe("a\tb"))
        self.assertEqual("ab", br.safe("a\x00b"))
        self.assertEqual("", br.safe(None))

    def test_an_unrecognised_sender_never_becomes_the_reply_address(self):
        """The claim decided where our own answer went, so an unrecognised, pipe-bearing tag was
        both a malformed row and a sender choosing our addressing."""
        req = {"req_id": "R", "claimed_sender": "bad|sender", "action": "x", "row_id": "R", "at": NOW}
        row = br.row_for(br.RESULT_ACTION, req, "text", "gist")
        self.assertEqual("ALL", row["target_surface"])
        self.assertNotIn("bad", row["payload"])


class TheBlockersFromPr323(unittest.TestCase):
    """Each of these is a finding Copilot raised on PR 323, written as the test that was missing."""

    def _req_row(self, req_id, action="redis-synthetic-probe", sender="aya", ts=None):
        payload = "BCB|v=1|req=%s|do=%s|from=%s|to=bus-reconciler|text=t" % (req_id, action, sender)
        return [req_id, ts or br.stamp(NOW), sender, "bus-reconciler", br.REQUEST_ACTION,
                payload, "OPEN", "Blackboard", "a gist", ""]

    def test_a_malformed_sender_does_not_abort_the_requests_behind_it(self):
        """THE BLOCKER: source_tag='bad|sender' put a pipe in the refusal text, row_for asserted,
        and the AssertionError ended the run - so a later valid request never got its probe, and the
        unanswered row could repeat the failure on every pass."""
        rows = [self._req_row("BAD-1", sender="bad|sender"), self._req_row("GOOD-1")]
        appended = []
        out = br.handle(rows, FakeRedis(), now=NOW, append=appended.append)
        self.assertEqual(["GOOD-1"], [a["req_id"] for a in out["answered"]])
        self.assertEqual(1, len(out["refused"]))
        # Diagnostics keep the unrecognised tag; nothing published does.
        self.assertEqual("bad|sender", out["refused"][0]["claimed_sender"])
        self.assertTrue(all("bad|sender" not in r["payload"] for r in appended))

    def test_an_unrecognised_sender_gets_no_board_row_at_all(self):
        rows = [self._req_row("X-1", sender="nobody-we-know")]
        appended = []
        br.handle(rows, FakeRedis(), now=NOW, append=appended.append)
        self.assertEqual([], appended, "answering an unrecognised claim is a flood we author")

    def test_refusals_count_against_the_same_budget_as_probes(self):
        """THE BLOCKER: distinct valid ids with a rejected action produced one board write and one
        read-back each, regardless of max_per_run."""
        rows = [self._req_row("REQ-%03d" % i, action="not-allowlisted") for i in range(10)]
        appended = []
        out = br.handle(rows, FakeRedis(), now=NOW, append=appended.append, max_per_run=3)
        self.assertEqual(3, len(appended), "the cap must bound refusal rows too")
        self.assertEqual(7, out["capped"])

    def test_a_mixed_flood_cannot_exceed_the_budget_in_total(self):
        rows = ([self._req_row("BAD-%03d" % i, action="nope") for i in range(5)]
                + [self._req_row("OKAY-%03d" % i) for i in range(5)])
        appended = []
        out = br.handle(rows, FakeRedis(), now=NOW, append=appended.append, max_per_run=2)
        # Each answered request writes two rows (receipt + result); each refusal writes one. The
        # budget counts REQUESTS, so at most two of either were served.
        self.assertLessEqual(len(out["answered"]) + len([r for r in out["refused"]
                                                         if "already" not in r["why"][0]]), 7)
        self.assertGreaterEqual(out["capped"], 1)
        self.assertLessEqual(len(appended), 4)

    def test_a_receipt_only_request_resumes_instead_of_wedging(self):
        """THE BLOCKER: a receipt that landed while its result did not left the request pending AND
        its Row_ID taken. The next pass replayed the receipt, append.py exited 2 on the duplicate,
        and serve_requests propagated that before the probe could run - so the request could never
        be finished by a retry."""
        req = self._req_row("RESUME-1")
        expected = br.row_for(br.RECEIPT_ACTION,
                              {"req_id": "RESUME-1", "claimed_sender": "aya"}, "", "")["row_id"]
        receipt_row = [expected, br.stamp(NOW), br.WORKER_TAG, "aya", br.RECEIPT_ACTION,
                       "BCB|v=1|id=%s|phase=%s|answers=RESUME-1|text=t" % (expected,
                                                                           br.RECEIPT_ACTION),
                       "OPEN", "Blackboard", "receipt", ""]
        appended = []
        out = br.handle([req, receipt_row], FakeRedis(), now=NOW, append=appended.append)
        self.assertEqual(["RESUME-1"], out["receipts_reused"])
        self.assertEqual(["RESUME-1"], [a["req_id"] for a in out["answered"]])
        kinds = [r["action_type"] for r in appended]
        self.assertEqual([br.RESULT_ACTION], kinds, "the receipt must not be written twice")

    def test_two_receipts_for_one_request_fail_closed(self):
        """Ambiguity is not something to reason past: two receipts, or one under an id this worker
        would not have written, means stop."""
        req = self._req_row("AMBIG-1")

        def receipt(row_id):
            return [row_id, br.stamp(NOW), br.WORKER_TAG, "aya", br.RECEIPT_ACTION,
                    "BCB|v=1|id=%s|answers=AMBIG-1|text=t" % row_id,
                    "OPEN", "Blackboard", "receipt", ""]

        appended = []
        out = br.handle([req, receipt("A"), receipt("B")], FakeRedis(), now=NOW,
                        append=appended.append)
        self.assertEqual([], out["answered"])
        self.assertEqual([], appended)
        self.assertIn("refusing to guess", out["refused"][0]["why"][0])


class FieldParsing(unittest.TestCase):
    def test_a_field_is_read_from_the_payload(self):
        self.assertEqual("REQ-9", br.field("BCB|v=1|req=REQ-9|do=x", "req"))

    def test_an_absent_field_is_empty(self):
        self.assertEqual("", br.field("BCB|v=1", "req"))

    def test_a_field_name_that_is_a_substring_of_another_is_not_confused(self):
        self.assertEqual("", br.field("BCB|v=1|xreq=no", "req"))


if __name__ == "__main__":
    unittest.main()
