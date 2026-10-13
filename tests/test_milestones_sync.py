"""scripts/milestones_sync.py: the live chart is rewritten from the repo copy, never over a newer one."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import milestones_sync as ms                                              # noqa: E402
import redis_gov as gov                                                   # noqa: E402
from test_redis_gov import ACL, FakeRedis, run                            # noqa: E402

SRC = "gantt\n  title Milestones\n"


def repo_file(version="2026-10-08.1", source=SRC, sha=None, extra=None):
    keys = {
        ms.META: {"fields": {"version": version, "overall_pct": "39"}},
        ms.PREFIX + "redis": {"fields": {"name": "Redis", "pct": "50"}},
        ms.PREFIX + "mermaid": {"fields": {"kind": "gantt", "source": source,
                                           "sha256": sha or hashlib.sha256(source.encode()).hexdigest()}},
    }
    keys.update(extra or {})
    return json.dumps({"_status": "DURABLE COPY", "keys": keys}).encode("utf-8")


class TheRepoCopyIsCheckedBeforeAnythingIsWritten(unittest.TestCase):
    def test_a_good_file_parses(self):
        self.assertEqual(3, len(ms.parse(repo_file())))

    def test_the_shipped_durable_copy_parses(self):
        raw = (ROOT / "docs" / "milestones" / "milestones-v1.json").read_bytes() \
            if (ROOT / "docs" / "milestones" / "milestones-v1.json").exists() else None
        if raw is None:
            self.skipTest("the durable copy is on main (PR 344), not on this branch")
        self.assertEqual(10, len(ms.parse(raw)))

    def test_every_shipped_milestone_has_a_target_and_an_actual_and_neither_is_made_up(self):
        """The owner, 2026-10-13: milestones "each with target date and actual date (plan vs actual)". A date
        that was never recorded stays empty: a done item whose date was only ever a plan has no actual."""
        path = ROOT / "docs" / "milestones" / "milestones-v1.json"
        if not path.exists():
            self.skipTest("the durable copy is on main (PR 344), not on this branch")
        hashes = ms.parse(path.read_bytes())
        items = [i for fields in hashes.values() if "milestones" in fields for i in json.loads(fields["milestones"])]
        self.assertEqual(int(hashes[ms.META]["milestones_total"]), len(items))
        for item in items:
            self.assertEqual({"actual", "change", "date", "date_kind", "id", "owner", "status", "target", "title"},
                             set(item), item.get("id"))
            want = {"actual": ("", item["date"]), "prop": (item["date"], ""), "agreed": (item["date"], ""),
                    "": ("", "")}[item["date_kind"]]
            self.assertEqual(want, (item["target"], item["actual"]), item["id"])
            if item["actual"]:
                self.assertEqual("done", item["status"], "%s has an actual date and is not done" % item["id"])
        self.assertIn("never filled in from the other one", hashes[ms.META]["item_dates"])
        self.assertEqual(("2026-10-08", 2), ms.version_of(hashes[ms.META]["version"]))

    def test_the_shipped_copy_is_written_whole_through_the_governor_so_every_hash_fits_its_cap(self):
        path = ROOT / "docs" / "milestones" / "milestones-v1.json"
        if not path.exists():
            self.skipTest("the durable copy is on main (PR 344), not on this branch")
        conn = FakeRedis()
        out = ms.sync(conn, ACL, ms.parse(path.read_bytes()), "0" * 40)
        self.assertEqual(("write", [], 10), (out["decision"], out["failed"], len(out["written"])))
        self.assertEqual(out["written"][-1], ms.META, "meta is written last")
        self.assertIn('"target"', conn.data["proj:milestones:v1:redis"]["milestones"])

    def test_a_source_that_does_not_match_its_sha_refuses_the_whole_file(self):
        with self.assertRaises(ms.BadSource):
            ms.parse(repo_file(sha="0" * 64))

    def test_keys_outside_the_prefix_non_strings_and_no_version_are_refused(self):
        for raw in (repo_file(extra={"proj:other": {"fields": {"a": "b"}}}),
                    repo_file(extra={ms.PREFIX + "x": {"fields": {"a": 1}}}),
                    repo_file(version="next week"),
                    b"not json"):
            with self.assertRaises(ms.BadSource):
                ms.parse(raw)


class NeverOverANewerChart(unittest.TestCase):
    def test_missing_older_and_same_are_written_newer_is_held(self):
        repo = ms.parse(repo_file("2026-10-08.2"))
        self.assertEqual("write", ms.decide(repo, {}))
        self.assertEqual("write", ms.decide(repo, {"version": "2026-10-08.1"}))
        self.assertEqual("write", ms.decide(repo, {"version": "2026-10-08.2"}))
        self.assertEqual("hold", ms.decide(repo, {"version": "2026-10-08.10"}), "10 is after 2, not before")
        self.assertEqual("hold", ms.decide(repo, {"version": "2026-10-09.1"}))


class TheWriteIsGovernedAndAudited(unittest.TestCase):
    def test_a_missing_chart_is_written_whole_meta_last_one_audit_each(self):
        conn = FakeRedis()
        out = ms.sync(conn, ACL, ms.parse(repo_file()), "abc123def456")
        self.assertEqual(("write", []), (out["decision"], out["failed"]))
        self.assertEqual(ms.META, out["written"][-1], "meta is written last")
        self.assertEqual(SRC, conn.hgetall(ms.PREFIX + "mermaid")["source"])
        self.assertEqual(2592000, conn.ttls[ms.META])
        audits = [e for e in conn.audit() if e["actor"] == ms.PRINCIPAL]
        self.assertEqual(3, len(audits))
        self.assertTrue(all(e["via"] == "sync/abc123def456" for e in audits))

    def test_a_newer_live_chart_is_not_touched(self):
        conn = FakeRedis()
        run(conn, "grok", "hput", ms.META, val=__import__("base64").b64encode(
            json.dumps({"version": "2026-10-09.1"}).encode()).decode(), enc="b64")
        out = ms.sync(conn, ACL, ms.parse(repo_file()), "abc")
        self.assertEqual(("hold", []), (out["decision"], out["written"]))
        self.assertEqual("2026-10-09.1", conn.hgetall(ms.META)["version"])

    def test_the_sync_principal_can_write_nothing_else(self):
        conn = FakeRedis()
        refused = run(conn, ms.PRINCIPAL, "set", "proj:costcontrol:x", val="y")
        self.assertIn("may not write", refused["refused"])
        refused = run(conn, ms.PRINCIPAL, "get", "conf:anything")
        self.assertIn("may not read", refused["refused"])


if __name__ == "__main__":
    unittest.main()
