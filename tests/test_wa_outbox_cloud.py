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
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(set(self.sent)), 2, "a message was sent twice")


if __name__ == "__main__":
    unittest.main()
