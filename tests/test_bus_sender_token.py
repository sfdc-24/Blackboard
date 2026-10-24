"""scripts/bus.py sender helpers: the client half of gas-bus/Code.js senderCheck_.

Run: python3 tests/test_bus_sender_token.py
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import bus  # noqa: E402

TOKEN = "aaaa.bbbb.cccc"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SenderToken(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("BUS_SENDER_TOKEN")
        self.seen = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BUS_SENDER_TOKEN", None)
        else:
            os.environ["BUS_SENDER_TOKEN"] = self._old

    def opener(self, body=TOKEN.encode(), boom=False):
        def _open(req, timeout=None):
            self.seen.append(req)
            if boom:
                raise OSError("no metadata server")
            return FakeResp(body)
        return _open

    def test_off_by_default_and_no_call(self):
        os.environ.pop("BUS_SENDER_TOKEN", None)
        self.assertIsNone(bus.sender_token(opener=self.opener()))
        self.assertEqual(self.seen, [])
        p = {"action": "append"}
        self.assertIs(bus.with_sender_token(p, opener=self.opener()), p)

    def test_metadata_mints_for_the_bus_audience(self):
        os.environ["BUS_SENDER_TOKEN"] = "metadata"
        self.assertEqual(bus.sender_token(opener=self.opener()), TOKEN)
        req = self.seen[0]
        self.assertIn("audience=sfdc24-blackboard-bus", req.full_url)
        self.assertEqual(req.get_header("Metadata-flavor"), "Google")

    def test_with_sender_token_copies_never_mutates(self):
        os.environ["BUS_SENDER_TOKEN"] = "metadata"
        p = {"action": "append", "secret": "s"}
        out = bus.with_sender_token(p, opener=self.opener())
        self.assertEqual(out["id_token"], TOKEN)
        self.assertNotIn("id_token", p)

    def test_no_metadata_server_is_none_not_an_error(self):
        os.environ["BUS_SENDER_TOKEN"] = "metadata"
        self.assertIsNone(bus.sender_token(opener=self.opener(boom=True)))

    def test_a_non_jwt_answer_is_not_sent(self):
        os.environ["BUS_SENDER_TOKEN"] = "metadata"
        self.assertIsNone(bus.sender_token(opener=self.opener(body=b"<html>nope</html>")))


class SenderCheckReader(unittest.TestCase):
    def test_reads_each_stamp(self):
        base = [""] * 11
        self.assertEqual(bus.sender_check(base + ["sender=unverified"]), ("unverified", ""))
        self.assertEqual(bus.sender_check(base + ["sender=verified;who=a@b"]), ("verified", "who=a@b"))
        self.assertEqual(bus.sender_check(base + ["sender=mismatch;claimed=aya;from=grok"]),
                         ("mismatch", "claimed=aya;from=grok"))

    def test_old_rows_are_unstamped(self):
        self.assertEqual(bus.sender_check([""] * 12), ("unstamped", ""))
        self.assertEqual(bus.sender_check([""] * 10), ("unstamped", ""))
        self.assertEqual(bus.sender_check(None), ("unstamped", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
