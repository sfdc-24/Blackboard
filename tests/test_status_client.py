"""scripts/status_client.py against a fake gateway. No network."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import status_client as sc  # noqa: E402

ENV = {"BUS_URL": "https://example.invalid/bus", "BUS_SECRET": "s3cr3t"}


class FakeGateway:
    def __init__(self):
        self.calls = []

    def ok(self, **extra):
        def fetch(url, data):
            self.calls.append((url, data))
            body = {"ok": True}
            body.update(extra)
            return json.dumps(body)
        return fetch

    def refuses(self, error="refused"):
        def fetch(url, data):
            self.calls.append((url, data))
            return json.dumps({"ok": False, "error": error})
        return fetch

    def answers_garbage(self):
        def fetch(url, data):
            self.calls.append((url, data))
            return "not json"
        return fetch


class TheWrite(unittest.TestCase):
    def test_sends_the_secret_and_title_and_the_updates(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.ok(written=1, misses=[]))
        client.write([{"row_id": "R1", "values": ["OPEN", "", "", "", "", ""]}])
        url, body = gw.calls[0]
        self.assertEqual(ENV["BUS_URL"], url)
        self.assertEqual("s3cr3t", body["secret"])
        self.assertEqual("status_write", body["action"])
        self.assertEqual(sc.TITLE, body["title"])
        self.assertEqual([{"row_id": "R1", "values": ["OPEN", "", "", "", "", ""]}], body["updates"])

    def test_empty_updates_makes_no_call_at_all(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.ok(written=99))
        result = client.write([])
        self.assertEqual({"written": 0, "misses": []}, result)
        self.assertEqual([], gw.calls)

    def test_written_and_misses_are_returned(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.ok(written=3, misses=["R9"]))
        result = client.write([{"row_id": "R1", "values": ["OPEN"] * 6}])
        self.assertEqual({"written": 3, "misses": ["R9"]}, result)

    def test_more_than_500_updates_is_split_into_batches(self):
        gw = FakeGateway()

        def fetch(url, data):
            gw.calls.append((url, data))
            return json.dumps({"ok": True, "written": len(data["updates"]), "misses": []})
        client = sc.StatusClient(ENV, fetch=fetch)
        updates = [{"row_id": "R%d" % i, "values": ["OPEN"] * 6} for i in range(620)]
        result = client.write(updates)
        self.assertEqual(2, len(gw.calls))
        self.assertEqual(500, len(gw.calls[0][1]["updates"]))
        self.assertEqual(120, len(gw.calls[1][1]["updates"]))
        self.assertEqual(620, result["written"])

    def test_a_refusal_raises_and_names_the_action(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.refuses(error="updates[0] missing row_id"))
        with self.assertRaises(sc.StatusClientError) as ctx:
            client.write([{"row_id": "R1", "values": ["OPEN"] * 6}])
        self.assertIn("status_write", str(ctx.exception))
        self.assertIn("missing row_id", str(ctx.exception))

    def test_non_json_answer_raises_rather_than_crashing_on_json_parse(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.answers_garbage())
        with self.assertRaises(sc.StatusClientError):
            client.write([{"row_id": "R1", "values": ["OPEN"] * 6}])


class TheSetup(unittest.TestCase):
    def test_sends_status_setup_and_returns_the_report(self):
        gw = FakeGateway()
        report = {"headers": True, "validation": True, "protection": True, "filter_view": True}
        client = sc.StatusClient(ENV, fetch=gw.ok(report=report))
        self.assertEqual(report, client.setup())
        self.assertEqual("status_setup", gw.calls[0][1]["action"])

    def test_a_refused_setup_raises(self):
        gw = FakeGateway()
        client = sc.StatusClient(ENV, fetch=gw.refuses(error="M1 already holds something else"))
        with self.assertRaises(sc.StatusClientError):
            client.setup()


if __name__ == "__main__":
    unittest.main()
