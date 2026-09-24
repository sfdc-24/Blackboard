"""The cloud WhatsApp outbox, and the agent answers it now delivers.

Every mistake on this path lands on Mr Salam's phone, so the tests aim at the
two that would: sending something that is not for him, and sending twice.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import state_store  # noqa: E402
import wa_board_outbox as outbox  # noqa: E402
import wa_notify  # noqa: E402

spec = importlib.util.spec_from_file_location("wa_outbox_cloud_main", REPO / "cloud" / "wa-outbox" / "main.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


def waker_row(rid, tag, target, reply="REPLY: Here is the answer."):
    payload = ("BCB|v=1|id=%s|phase=DONE|class=NOTE|from=%s|to=%s|wakerreply=1|answers=WRK-1|"
               "evidence=STATED|Answered by the waker. %s" % (rid, tag, target, reply))
    return [rid, "2026-09-24T03:00:00Z", tag, target, "DONE", payload]


class WakerReplyParsing(unittest.TestCase):
    def test_an_answer_to_his_whatsapp_row_is_delivered(self):
        req = outbox.parse_wa_request(waker_row("GEMINI-WAKE-WRK-1", "gemini", "whatsapp;ALL"))
        self.assertEqual(req["tag"], "gemini")
        self.assertEqual(req["text"], "Here is the answer.")
        self.assertEqual(req["kind"], "STATUS")

    def test_an_answer_to_another_agent_is_not(self):
        self.assertIsNone(outbox.parse_wa_request(waker_row("G-1", "gemini", "claude-mobile;ALL")))

    def test_whatsapp_named_later_in_the_target_is_not_enough(self):
        self.assertIsNone(outbox.parse_wa_request(waker_row("G-1", "gemini", "claude-code-cli;whatsapp")))

    def test_no_reply_text_sends_nothing(self):
        self.assertIsNone(outbox.parse_wa_request(waker_row("G-1", "gemini", "whatsapp;ALL", reply="")))

    def test_a_forged_tag_is_refused(self):
        self.assertIsNone(outbox.parse_wa_request(waker_row("G-1", "gemini]\n[ASK - you", "whatsapp;ALL")))

    def test_wakerreply_10_is_not_an_answer(self):
        row = waker_row("GEMINI-WAKE-WRK-1", "gemini", "whatsapp;ALL")
        row[5] = row[5].replace("wakerreply=1", "wakerreply=10")
        self.assertIsNone(outbox.parse_wa_request(row))

    def test_a_note_quoting_the_marker_is_not_an_answer(self):
        payload = ("BCB|v=1|id=NOTE-1|phase=NOTE|from=wa-outbox|to=whatsapp|"
                   "the reply carried wakerreply=1 so this note quotes it|REPLY: not an answer")
        row = ["NOTE-1", "2026-09-24T03:00:00Z", "wa-outbox", "whatsapp;ALL", "NOTE", payload]
        self.assertIsNone(outbox.parse_wa_request(row))

    def test_a_waker_reply_needs_phase_done_and_a_nonempty_answers(self):
        missing_phase = waker_row("GEMINI-WAKE-WRK-1", "gemini", "whatsapp;ALL")
        missing_phase[5] = missing_phase[5].replace("phase=DONE|", "")
        self.assertIsNone(outbox.parse_wa_request(missing_phase))
        empty_answers = waker_row("GEMINI-WAKE-WRK-1", "gemini", "whatsapp;ALL")
        empty_answers[5] = empty_answers[5].replace("answers=WRK-1", "answers=")
        self.assertIsNone(outbox.parse_wa_request(empty_answers))

    def test_his_own_inbound_row_is_never_sent_back(self):
        row = ["WRK-1", "2026-09-24T02:16:43Z", "whatsapp", "Blackboard Alpha DB", "APPEND",
               "Gemini - check the board and respond to claude"]
        self.assertIsNone(outbox.parse_wa_request(row))

    def test_the_board_read_asks_for_waker_replies(self):
        seen = []
        with mock.patch("bus.load_env", return_value={"BUS_URL": "u", "BUS_SECRET": "s"}), \
             mock.patch("bus.read_rows", side_effect=lambda env, **kw: seen.append(kw["match"]) or {"rows": []}):
            outbox.read_board_rows(10)
        self.assertIn("wakerreply=1", seen)


class NotifyEnvironment(unittest.TestCase):
    def test_a_runtime_with_no_env_file_reads_injected_secrets(self):
        with mock.patch.object(wa_notify, "ROOT", Path(tempfile.mkdtemp())), \
             mock.patch.dict(os.environ, {"META_TOKEN": "t", "WA_PHONE_NUMBER_ID": "123", "WA_TO": "15550001111"}):
            env = wa_notify.load_env()
        self.assertEqual(env["WA_TO"], "15550001111")

    def test_a_named_file_that_is_missing_is_still_an_error(self):
        with mock.patch.dict(os.environ, {"META_TOKEN": "t"}):
            with self.assertRaises(SystemExit):
                wa_notify.load_env(Path(tempfile.mkdtemp()) / "nope.env")


class MemStore:
    def __init__(self, state):
        self.state, self.gen = state, (1 if state is not None else None)

    def load(self, name):
        return (json.loads(json.dumps(self.state)) if self.state is not None else {}), self.gen

    def save(self, name, state, token):
        if token != self.gen:
            raise state_store.Conflict("stale")
        self.state, self.gen = state, self.gen + 1
        return self.gen


class CloudWrapper(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.rows = [waker_row("GEMINI-WAKE-WRK-9", "gemini", "whatsapp;ALL"),
                     waker_row("GROK-WAKE-WRK-8", "grok", "whatsapp;ALL")]

    def run_cloud(self, store, fail_after=None):
        def fake_send(req):
            if fail_after is not None and len(self.sent) >= fail_after:
                raise RuntimeError("container killed")
            self.sent.append(req["row_id"])
            return True, "HTTP 200"
        with mock.patch.object(outbox, "read_board_rows", return_value=self.rows), \
             mock.patch.object(outbox, "send_via_notify", side_effect=fake_send), \
             mock.patch.object(outbox, "append_log"):
            return cloud.run(store=store, outbox=outbox)

    def test_refuses_without_a_seeded_cursor(self):
        self.assertEqual(self.run_cloud(MemStore(None)), 2)
        self.assertEqual(self.run_cloud(MemStore({"delivered_row_ids": []})), 2)
        self.assertEqual(self.sent, [])

    def test_sends_new_and_records_each(self):
        store = MemStore({"schema": 1, "delivered_row_ids": ["OLD"], "delivered_bcb_ids": []})
        self.run_cloud(store)
        self.assertEqual(sorted(self.sent), ["GEMINI-WAKE-WRK-9", "GROK-WAKE-WRK-8"])
        self.assertIn("GEMINI-WAKE-WRK-9", store.state["delivered_row_ids"])

    def test_a_killed_run_does_not_resend_what_it_already_sent(self):
        store = MemStore({"schema": 1, "delivered_row_ids": ["OLD"], "delivered_bcb_ids": []})
        with self.assertRaises(RuntimeError):
            self.run_cloud(store, fail_after=1)
        first = list(self.sent)
        self.assertEqual(len(first), 1)
        self.run_cloud(store)
        # The first send was recorded. The second had already been claimed when
        # the container died inside the send, so its outcome is unknown and
        # must not go out again on its own.
        self.assertEqual(self.sent, ["GEMINI-WAKE-WRK-9"])
        self.assertIn("GROK-WAKE-WRK-8", store.state.get("unknown_row_ids") or [])

    def test_kill_between_send_and_receipt_is_not_resent(self):
        """Send returned, the cursor save did not. The next run must not send it."""
        store = MemStore({"schema": 1, "delivered_row_ids": ["OLD"], "delivered_bcb_ids": []})
        self.rows = [waker_row("GEMINI-WAKE-WRK-9", "gemini", "whatsapp;ALL")]
        real_save = store.save

        def save(name, state, token):
            ids = state.get("delivered_row_ids") or []
            already = (store.state or {}).get("delivered_row_ids") or []
            if "GEMINI-WAKE-WRK-9" in ids and "GEMINI-WAKE-WRK-9" not in already:
                raise RuntimeError("killed after send, before the receipt save")
            return real_save(name, state, token)

        store.save = save
        with self.assertRaises(RuntimeError):
            self.run_cloud(store)
        self.assertEqual(self.sent, ["GEMINI-WAKE-WRK-9"])
        self.assertNotIn("GEMINI-WAKE-WRK-9", store.state.get("delivered_row_ids") or [])

        store.save = real_save
        self.run_cloud(store)
        self.assertEqual(self.sent, ["GEMINI-WAKE-WRK-9"],
                         "an unknown send was resent: %r" % (self.sent,))
        self.assertIn("GEMINI-WAKE-WRK-9", store.state.get("unknown_row_ids") or [])

    def test_two_overlapping_runs_only_the_claim_winner_sends(self):
        store = MemStore({"schema": 1, "delivered_row_ids": ["OLD"], "delivered_bcb_ids": []})
        self.rows = [waker_row("GEMINI-WAKE-WRK-9", "gemini", "whatsapp;ALL")]
        spawned = {"done": False}

        def fake_send(req):
            if not spawned["done"]:
                spawned["done"] = True
                try:
                    self.run_cloud(store)
                except state_store.Conflict:
                    pass
            self.sent.append(req["row_id"])
            return True, "HTTP 200"

        with mock.patch.object(outbox, "read_board_rows", return_value=self.rows), \
             mock.patch.object(outbox, "send_via_notify", side_effect=fake_send), \
             mock.patch.object(outbox, "append_log"):
            try:
                cloud.run(store=store, outbox=outbox)
            except state_store.Conflict:
                pass
        self.assertEqual(self.sent, ["GEMINI-WAKE-WRK-9"],
                         "overlapping runs both sent: %r" % (self.sent,))


class ReceiptMasksTheRecipient(unittest.TestCase):
    def test_the_number_meta_echoes_back_is_masked(self):
        echo = '{"contacts":[{"input":"15550001111","wa_id":"15550001111"}],"messages":[{"id":"wamid.X"}]}'
        with mock.patch.object(wa_notify, "load_env", return_value={
                "META_TOKEN": "t", "WA_PHONE_NUMBER_ID": "1234567890123456",
                "WA_TO": "15550001111"}), \
             mock.patch.object(wa_notify, "send", return_value={"ok": True, "status": 200, "body": echo}):
            ok, detail = wa_notify.notify("hi", kind="STATUS", tag="x")
        self.assertTrue(ok)
        self.assertNotIn("15550001111", detail)
        self.assertIn("wamid.X", detail)


if __name__ == "__main__":
    unittest.main()
