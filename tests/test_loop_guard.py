#!/usr/bin/env python3
"""L-114's mechanism: scripts/loop_guard.py refuses the shapes a loop is made of.

Each test is one of the ways the fleet could go round: the same pair answering each other, a chain
of answers to answers, and the guard losing the ability to count. Nothing here touches the board.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import loop_guard  # noqa: E402


class LoopGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._state = loop_guard.os.environ.get("SFDC24_STATE_DIR")
        loop_guard.os.environ["SFDC24_STATE_DIR"] = self.dir.name
        self.addCleanup(self.restore)
        self.now = 1_790_000_000.0

    def restore(self):
        if self._state is None:
            loop_guard.os.environ.pop("SFDC24_STATE_DIR", None)
        else:
            loop_guard.os.environ["SFDC24_STATE_DIR"] = self._state

    def post(self, to="codex", project="CONFERENCE", rid=None, answers="", owner_asked="", at=None):
        self.now = self.now if at is None else at
        rid = rid or ("R%d" % int(self.now))
        return loop_guard.allow("claude-code-cli", to, project, rid, answers, owner_asked, self.now)

    def test_ordinary_work_is_never_in_the_way(self):
        for i in range(loop_guard.CAP - 1):
            allowed, why = self.post(rid="R%d" % i, at=self.now + i * 60)
            self.assertTrue(allowed, why)

    def test_the_same_pair_going_round_is_stopped(self):
        for i in range(loop_guard.CAP):
            self.assertTrue(self.post(rid="R%d" % i, at=self.now + i * 60)[0])
        allowed, why = self.post(rid="over", at=self.now + 60)
        self.assertFalse(allowed)
        self.assertIn("LOOP GUARD", why)
        self.assertIn("what is wrong is the shape", why)

    def test_another_target_or_another_project_is_its_own_count(self):
        for i in range(loop_guard.CAP):
            self.assertTrue(self.post(rid="R%d" % i, at=self.now + i * 60)[0])
        self.assertTrue(self.post(to="grok", rid="other-target", at=self.now + 60)[0])
        self.assertTrue(self.post(project="SITE", rid="other-project", at=self.now + 60)[0])

    def test_the_window_lets_the_count_fall_away(self):
        for i in range(loop_guard.CAP):
            self.assertTrue(self.post(rid="R%d" % i, at=self.now + i)[0])
        self.assertFalse(self.post(rid="over", at=self.now + 10)[0])
        self.assertTrue(self.post(rid="later", at=self.now + loop_guard.WINDOW + 60)[0], "the window never cleared")

    def test_a_chain_of_answers_to_answers_is_stopped(self):
        last = ""
        for i in range(loop_guard.MAX_CHAIN):
            rid = "C%d" % i
            allowed, why = self.post(to="grok", rid=rid, answers=last, at=self.now + i)
            self.assertTrue(allowed, why)
            last = rid
        allowed, why = self.post(to="grok", rid="deep", answers=last, at=self.now + 1)
        self.assertFalse(allowed)
        self.assertIn("deep in the last", why)

    def test_a_chain_that_came_from_elsewhere_starts_at_one(self):
        allowed, _ = self.post(answers="SOMETHING-ANOTHER-AGENT-SENT")
        self.assertTrue(allowed)
        self.assertEqual(1, loop_guard.chain_of("NOT-MINE", []))
        self.assertEqual(1, loop_guard.chain_of("", []))

    def test_a_chain_that_points_at_itself_does_not_hang(self):
        recent = [{"rid": "A", "answers": "B"}, {"rid": "B", "answers": "A"}]
        self.assertLessEqual(loop_guard.chain_of("A", recent), loop_guard.MAX_CHAIN + 3)

    def test_his_word_lets_one_through_and_is_written_down(self):
        for i in range(loop_guard.CAP):
            self.post(rid="R%d" % i, at=self.now + i * 60)
        allowed, why = self.post(rid="asked", owner_asked="keep working through with codex", at=self.now + 60)
        self.assertTrue(allowed)
        self.assertEqual("", why)
        last = json.loads(Path(self.dir.name, "post_ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("keep working through with codex", last["owner_asked"])

    def test_a_refused_post_is_still_counted(self):
        for i in range(loop_guard.CAP):
            self.post(rid="R%d" % i, at=self.now + i * 60)
        self.post(rid="refused-one", at=self.now + 60)
        lines = Path(self.dir.name, "post_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(loop_guard.CAP + 1, len(lines))
        self.assertIn("refused", json.loads(lines[-1]))

    def test_a_guard_that_cannot_count_refuses(self):
        loop_guard.os.environ["SFDC24_STATE_DIR"] = str(Path(self.dir.name, "a-file"))
        Path(self.dir.name, "a-file").write_text("not a directory", encoding="utf-8")
        allowed, why = self.post(rid="nowhere")
        self.assertFalse(allowed)
        self.assertIn("cannot count", why)

    def test_a_ledger_that_cannot_be_read_does_not_block_the_work(self):
        Path(self.dir.name, "post_ledger.jsonl").write_text("{not json\n\x00", encoding="utf-8")
        self.assertTrue(self.post(rid="after-bad-ledger")[0])


if __name__ == "__main__":
    unittest.main()
