"""hput: replace a whole hash from one b64 JSON object, in one transaction with one audit entry.

For the milestone chart (Mr. Salam, 2026-10-08 18:05Z: "keep the repo copy, Redis as the live copy"),
whose largest hash is a 7101-byte Mermaid source - over the 2048-byte cap every other value keeps.
"""
import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import redis_gov as gov                                                   # noqa: E402
from test_redis_gov import ACL, FakeRedis, run                            # noqa: E402

KEY = "proj:milestones:v1:mermaid"


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


class HputReplacesAWholeHash(unittest.TestCase):
    def test_the_whole_hash_is_written_and_audited_once(self):
        conn = FakeRedis()
        source = "gantt\n" + "x" * 7000
        out = run(conn, "grok", "hput", KEY, val=b64({"kind": "gantt", "source": source}), enc="b64")
        self.assertTrue(out["ok"], out)
        self.assertEqual({"kind": "gantt", "source": source}, conn.hgetall(KEY))
        self.assertEqual(2592000, conn.ttls[KEY], "proj: keeps its 30-day TTL")
        entries = [e for e in conn.audit() if e["key"] == KEY]
        self.assertEqual(1, len(entries))
        self.assertEqual(gov.sha8(gov.canonical({"kind": "gantt", "source": source})), entries[0]["new_sha"])

    def test_replace_not_merge(self):
        conn = FakeRedis()
        run(conn, "grok", "hput", KEY, val=b64({"a": "1", "stale": "x"}), enc="b64")
        run(conn, "grok", "hput", KEY, val=b64({"a": "2"}), enc="b64")
        self.assertEqual({"a": "2"}, conn.hgetall(KEY))
        last = [e for e in conn.audit() if e["key"] == KEY][-1]
        self.assertEqual(gov.sha8(gov.canonical({"a": "1", "stale": "x"})), last["old_sha"])

    def test_the_8KB_cap_is_only_for_the_milestone_prefix(self):
        conn = FakeRedis()
        big = {"source": "y" * 6000}
        self.assertTrue(run(conn, "grok", "hput", KEY, val=b64(big), enc="b64")["ok"])
        out = run(conn, "grok", "hput", "proj:other", val=b64(big), enc="b64")
        self.assertIn("over 2048 bytes", out["refused"])
        out = run(conn, "grok", "hput", KEY, val=b64({"source": "z" * 9000}), enc="b64")
        self.assertIn("over 8192 bytes", out["refused"])

    def test_the_shape_is_closed(self):
        conn = FakeRedis()
        bad = {
            "not b64": dict(val=json.dumps({"a": "1"}), enc=""),
            "not json": dict(val=base64.b64encode(b"nope").decode(), enc="b64"),
            "a list": dict(val=b64(["a"]), enc="b64"),
            "empty": dict(val=b64({}), enc="b64"),
            "a number value": dict(val=b64({"a": 1}), enc="b64"),
            "a bad field name": dict(val=b64({"a b": "1"}), enc="b64"),
            "too many fields": dict(val=b64({"f%d" % n: "1" for n in range(51)}), enc="b64"),
        }
        for name, kw in bad.items():
            out = run(conn, "grok", "hput", KEY, **kw)
            self.assertFalse(out["ok"], name)
            self.assertIn("refused", out, name)
        self.assertEqual({}, conn.hgetall(KEY))
        out = run(conn, "grok", "hput", KEY, field="x", val=b64({"a": "1"}), enc="b64")
        self.assertIn("takes no field", out["refused"])

    def test_protected_and_unwritable_namespaces_still_refuse(self):
        conn = FakeRedis()
        self.assertIn("protected", run(conn, "grok", "hput", "gov:x", val=b64({"a": "1"}), enc="b64")["refused"])
        self.assertIn("may not write",
                      run(conn, "grok", "hput", "other:x", val=b64({"a": "1"}), enc="b64")["refused"])

    def test_a_string_key_is_not_overwritten_as_a_hash(self):
        conn = FakeRedis()
        conn.set(KEY, "a string")
        out = run(conn, "grok", "hput", KEY, val=b64({"a": "1"}), enc="b64")
        self.assertIn("holds a string", out["refused"])
        self.assertEqual("a string", conn.get(KEY))

    def test_the_shipped_acl_carries_the_milestone_cap(self):
        self.assertEqual(8192, gov.value_cap(ACL, KEY))
        self.assertEqual(2048, gov.value_cap(ACL, "proj:anything-else"))


if __name__ == "__main__":
    unittest.main()
