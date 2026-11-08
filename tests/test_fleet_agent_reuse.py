"""A stable request id is verified only with its own payload (Codex on #478, CODEX-RESULT-RV-BB478-20261108T0146Z).

No network: the board read and the append are fakes. A retry of the same request under its stable id is
verified without a second append; a different request under that id is refused and nothing is appended;
a read-back that finds the id under a different payload is not VERIFIED. A post is unchanged.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet_agent  # noqa: E402

HEADER = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload",
          "Status", "Project", "Gist", "Extra"]


def board(*rows):
    return {"rows": [HEADER] + [list(r) for r in rows]}


class Board:
    """The board as the append sees it: rows before, then the rows after any append."""

    def __init__(self, before, after=None):
        self.before, self.after = before, after
        self.appends = []

    def read(self, quiet=False):
        if self.appends and self.after is not None:
            return self.after
        return self.before

    def fetch(self, url, body, tries=1):
        self.appends.append(body["sheetRow"])
        return '{"ok":true}'


def run(fake, rid, row, reuse_check):
    out = io.StringIO()
    with mock.patch.object(fleet_agent, "read_board", fake.read), \
            mock.patch.object(fleet_agent, "_bus_fetch", fake.fetch), \
            mock.patch.object(fleet_agent, "_bus_load_env", lambda: {"BUS_URL": "https://x", "BUS_SECRET": "s"}), \
            redirect_stdout(out):
        code = fleet_agent.append_and_verify(rid, row, reuse_check=reuse_check)
    return code, out.getvalue()


class StableIds(unittest.TestCase):
    RID = "GEMINI-REDIS-0123456789ab"

    def request(self, key="fleet:inbox:grok"):
        return fleet_agent.request_row("gemini", "xadd", key, val="hello", rid=self.RID)[1]

    def test_a_new_request_is_appended_and_verified_by_its_own_payload(self):
        row = self.request()
        fake = Board(board(), after=board(row))
        code, out = run(fake, self.RID, row, True)
        self.assertEqual(0, code, out)
        self.assertEqual(1, len(fake.appends))
        self.assertIn("VERIFIED on the board", out)

    def test_a_retry_of_the_same_request_is_verified_without_a_second_append(self):
        row = self.request()
        fake = Board(board(row))
        code, out = run(fake, self.RID, row, True)
        self.assertEqual(0, code, out)
        self.assertEqual([], fake.appends)
        self.assertIn("not appended again", out)

    def test_a_different_request_under_the_same_id_is_refused_and_nothing_is_appended(self):
        first, second = self.request("fleet:inbox:grok"), self.request("fleet:inbox:codex")
        self.assertNotEqual(first[fleet_agent.PAYLOAD_COLUMN], second[fleet_agent.PAYLOAD_COLUMN])
        fake = Board(board(first))
        code, out = run(fake, self.RID, second, True)
        self.assertEqual(2, code, out)
        self.assertEqual([], fake.appends)
        self.assertNotIn("VERIFIED", out)

    def test_a_read_back_that_finds_the_id_under_another_payload_is_not_verified(self):
        mine, other = self.request("fleet:inbox:grok"), self.request("fleet:inbox:codex")
        fake = Board(board(), after=board(other, mine))
        code, out = run(fake, self.RID, mine, True)
        self.assertEqual(1, code, out)
        self.assertIn("NOT VERIFIED", out)

    def test_no_board_read_before_the_append_means_no_append(self):
        fake = Board(None)
        code, out = run(fake, self.RID, self.request(), True)
        self.assertEqual(1, code, out)
        self.assertEqual([], fake.appends)

    def test_a_post_with_its_own_unique_id_is_unchanged(self):
        row = ["CLAUDE-CLI-X-20261108T0150Z", "t", "claude-code-cli", "ALL", "RESULT", "text", "", "", "", ""]
        stored = list(row)
        stored[fleet_agent.PAYLOAD_COLUMN] = "text as the board keeps it"
        fake = Board(board(), after=board(stored))
        code, out = run(fake, row[0], row, False)
        self.assertEqual(0, code, out)
        self.assertEqual(1, len(fake.appends), "a post is appended without a first read")

    def test_the_request_subcommand_asks_for_the_reuse_check(self):
        import inspect
        self.assertIn("append_and_verify(rid, row, reuse_check=True)", inspect.getsource(fleet_agent.cmd_request))


if __name__ == "__main__":
    unittest.main()
