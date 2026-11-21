#!/usr/bin/env python3
"""scripts/gemini_parity.py: the parity test for gemini's direct Redis path, run offline here, for BOTH accounts.

GEMINI-RULING-DIRECT-REDIS-OK-20261121T0153Z, PARITY TEST. Nothing here mints a token or opens a socket. The "bridge"
is the service's own door, cloud/redis-tool-bridge/op.py handle(), over the committed callers.json and redis_gov
on the governed suite's FakeRedis, reached through scripts/bridge_client.call() with a fake opener: so the
outcome each check sees is classified by the client code the live run uses. The "Sheet" is a list of board rows,
and the lane is what the board-to-Redis mirror's own functions (serve_requests) make of those rows.

Run: python -m pytest tests/test_gemini_parity.py
"""
import datetime
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "redis-tool-bridge"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))
sys.path.insert(0, str(ROOT / "tests"))

import bridge_client  # noqa: E402
import gemini_parity as gp  # noqa: E402
import op as bridge_op  # noqa: E402
import redis_gov  # noqa: E402
import serve_requests as sr  # noqa: E402
from test_redis_gov import FakeRedis  # noqa: E402

NOW = datetime.datetime(2026, 11, 21, 2, 0, 0, tzinfo=datetime.timezone.utc)
URL = gp.DEFAULT_URL
TOKEN = "aaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc"


class Answer:
    def __init__(self, body):
        self.status, self._raw = 200, json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self._raw


class TheDoor:
    """An opener whose server is op.handle itself, for one verified account, over one FakeRedis."""

    def __init__(self, email, conn):
        self.email, self.conn, self.log, self.requests = email, conn, [], []

    def open(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode("utf-8")))
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        code, body = bridge_op.handle(self.email, request.data, lambda: self.conn, self.log.append)
        if code == 200:
            return Answer(body)
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(json.dumps(body).encode("utf-8")))


def stamp(minutes_ago):
    return (NOW - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sheet():
    """Board rows: [Row_ID, Timestamp, Source_Tag, Target_Surface, Action_Type, Payload]."""
    rows = []
    for n, target in enumerate(("gemini", "ALL", "claude-code-cli;gemini", "fleet", "Gemini", "grok, gemini", "ALL")):
        rows.append(["CCC-ASK-%d" % n, stamp(100 - n * 10), "claude-code-cli", target, "AYA_REQ",
                     "BCB|v=1|id=CCC-ASK-%d|phase=DISPATCH|from=claude-code-cli|text=asked %d" % (n, n)])
    rows.append(["GEMINI-WAKE-OWN-1", stamp(55), "gemini", "ALL", "NOTE", "BCB|v=1|phase=RESULT|text=mine"])
    rows.append(["GEMINI-WAKE-OWN-2", stamp(65), "gemini", "claude-code-cli;ALL", "NOTE", "BCB|v=1|phase=DONE|text=mine"])
    rows.append(["CCC-TO-GROK", stamp(45), "claude-code-cli", "grok", "NOTE", "BCB|v=1|phase=NOTE|text=not gemini's"])
    rows.append(["RECON-LOOP", stamp(35), "bus-reconciler", "gemini", "AYA_RESULT", "BCB|v=1|phase=AYA_RESULT|text=loop"])
    return rows


def lane_of(rows):
    """What the mirror writes on fleet:inbox:gemini for these rows, with the mirror's own functions, ms stream ids."""
    acl = redis_gov.load_acl()
    aliases = dict(acl.get("aliases") or {})
    aliases.update(sr.inbox_wake_aliases())
    out = []
    for row in sorted(rows, key=lambda r: r[1]):
        if not sr._safe("row_id", row[0]) or row[2].lower() in sr.LOOP_SOURCES:
            continue
        if "gemini" not in sr.addressed_agents(row[3], list(acl["principals"]), aliases):
            continue
        ms = int(gp.when(row[1]).timestamp() * 1000) + 500          # mirrored half a second after the row
        out.append(("%d-0" % ms, sr.mirror_fields(row[2], row[0], row[1], row[5])))
    # A lane entry that is not a board row (the call bridge's ask, {by, text, at}): not compared.
    out.append(("%d-0" % int((NOW - datetime.timedelta(minutes=5)).timestamp() * 1000),
                {"by": "claude-code-cli", "text": "a call ask", "at": stamp(5)}))
    return out


class TheMirrorRuleIsTheMirrorsOwn(unittest.TestCase):
    """The parity is only as good as its idea of 'addressed to gemini'. It is restated from serve_requests; held here."""

    def test_the_address_rule_matches_addressed_agents_for_gemini(self):
        acl = redis_gov.load_acl()
        aliases = dict(acl.get("aliases") or {})
        aliases.update(sr.inbox_wake_aliases())
        for target in ("gemini", "Gemini", " GEMINI ", "ALL", "all", "fleet", "Fleet", "claude-code-cli", "grok;gemini",
                       "codex, gemini", "", " ", "geminix", "gemini-cli", "aya", "all;grok", "claude-code-cli,,", "sheet"):
            with self.subTest(target=target):
                self.assertEqual("gemini" in sr.addressed_agents(target, list(acl["principals"]), aliases),
                                 gp.mirror_addresses(target, aliases=aliases), target)
        self.assertFalse(any(value == "gemini" for value in aliases.values()), "an alias for gemini: pass it on")

    def test_the_constants_and_the_entry_are_the_mirrors(self):
        self.assertEqual(sr.LOOP_SOURCES, gp.LOOP_SOURCES)
        self.assertEqual(sr._EXPAND, gp.EXPAND)
        for name in gp._SAFE:
            self.assertEqual(sr._SAFE[name].pattern, gp._SAFE[name].pattern, name)
        for row in sheet():
            entry = gp.mirror_entry(row)
            if entry:
                fields = sr.mirror_fields(row[2], row[0], row[1], row[5])
                self.assertEqual({k: fields[k] for k in ("by", "row_id", "at", "phase")}, entry)
        self.assertEqual({}, gp.mirror_entry(["RECON", stamp(1), "bus-reconciler", "gemini", "X", ""]))
        self.assertEqual({}, gp.mirror_entry(["bad id!", stamp(1), "grok", "gemini", "X", ""]))
        self.assertEqual({}, gp.mirror_entry(["CCC-1", stamp(1), "grok", "claude-code-cli", "X", ""]))

    def test_own_rows_are_the_doorbells_own_rows(self):
        import redis_doorbell
        self.assertIs(redis_doorbell.is_own, gp.redis_doorbell.is_own)
        self.assertTrue(redis_doorbell.is_own("gemini", {"by": "gemini", "row_id": "GEMINI-X"}))
        self.assertFalse(redis_doorbell.is_own("gemini", {"by": "claude-code-cli", "row_id": "CCC-X"}))


class AParityRun(unittest.TestCase):
    """The whole run, per account, through the real door and redis_gov."""

    def go(self, surface, rows=None, lane=None, argv=(), conn=None, door=None, minter=None):
        account = gp.SURFACES[surface][0]
        conn = conn or FakeRedis()
        conn.data[gp.LANE] = list(lane if lane is not None else lane_of(rows or sheet()))
        door = door or TheDoor(account, conn)
        read = []
        said = []

        def read_sheet(since):
            read.append(since)
            return [r for r in (rows or sheet()) if r[1] > since]
        code = gp.main(["--surface", surface] + list(argv), minter=minter or (lambda url, sa: TOKEN),
                       caller=lambda url, token, request: bridge_client.call(url, token, request, opener=door),
                       read_sheet=read_sheet, now=NOW, say=said.append)
        return code, said, conn, door, read

    def test_both_accounts_pass_all_four_checks_with_7_of_7_and_own_rows_skipped_on_both_sides(self):
        for surface in ("laptop", "waker"):
            with self.subTest(surface=surface):
                code, said, conn, door, read = self.go(surface)
                text = "\n".join(said)
                self.assertEqual(0, code, text)
                self.assertIn("check lane: PASS 7 of 7 rows match", text)
                self.assertIn("own rows skipped: lane 2, Sheet 2", text)
                self.assertIn("check kv: PASS set then get fleet:pilot:gemini:20261121T020000Z", text)
                self.assertIn("check lane:architect: PASS", text)
                self.assertIn("check refusals: PASS xadd lane:build is 403 refused (gemini does not write lane:build)", text)
                self.assertIn("check refusals: PASS set gov:test is 403 refused (that namespace is protected", text)
                self.assertTrue(said[-1].startswith("PASS: gemini parity as %s, 4 of 4" % gp.SURFACES[surface][0]))
                self.assertEqual("gemini-parity %s 20261121T020000Z" % surface,
                                 conn.data["fleet:pilot:gemini:20261121T020000Z"])
                self.assertEqual(["GEMINI-PARITY-%s-20261121T020000Z" % surface.upper()],
                                 [f.get("ref") for _, f in conn.data["lane:architect"]])
                self.assertNotIn("lane:build", conn.data)
                self.assertNotIn("gov:test", conn.data)
                self.assertTrue(all(r.get("expect") == "gemini" for r in door.requests))
                audit = [f for _, f in conn.data["gov:audit"]]
                self.assertEqual({"bridge:" + gp.SURFACES[surface][0]}, {a["via"] for a in audit})
                self.assertEqual({"gemini"}, {a["actor"] for a in audit})
                self.assertNotIn(TOKEN, text)
                self.assertNotIn("asked 3", text, "no Sheet payload is printed")

    def test_a_row_the_mirror_did_not_copy_fails_the_lane_and_names_it(self):
        # Six rows asked for, so the window still spans the row that is missing (seven would stop short of it).
        lane = [e for e in lane_of(sheet()) if e[1].get("row_id") != "CCC-ASK-4"]
        code, said, *_ = self.go("waker", lane=lane, argv=["--rows", "6"])
        text = "\n".join(said)
        self.assertEqual(1, code)
        self.assertIn("check lane: FAIL", text)
        self.assertIn("missing_on_lane: CCC-ASK-4", text)

    def test_a_field_that_differs_fails_the_lane(self):
        lane = [(i, dict(f, phase="RESULT") if f.get("row_id") == "CCC-ASK-5" else f) for i, f in lane_of(sheet())]
        code, said, *_ = self.go("laptop", lane=lane)
        self.assertEqual(1, code)
        self.assertIn("differ: CCC-ASK-5", "\n".join(said))

    def test_own_rows_skipped_differently_on_the_two_sides_fail_the_lane(self):
        extra = ("%d-0" % int((NOW - datetime.timedelta(minutes=50)).timestamp() * 1000),
                 {"by": "gemini", "row_id": "GEMINI-WAKE-NOT-ON-SHEET", "at": stamp(50), "phase": "DONE"})
        code, said, *_ = self.go("waker", lane=lane_of(sheet()) + [extra])
        text = "\n".join(said)
        self.assertEqual(1, code)
        self.assertIn("own rows skipped: lane 3, Sheet 2 (NOT the same rows)", text)
        self.assertIn("own only on the lane: GEMINI-WAKE-NOT-ON-SHEET", text)

    def test_too_few_rows_on_the_lane_fails_and_says_why(self):
        code, said, *_ = self.go("laptop", lane=lane_of(sheet())[:3])
        self.assertEqual(1, code)
        self.assertIn("fewer than 7 board rows", "\n".join(said))

    def test_a_widened_scope_is_a_loud_failure(self):
        conn = FakeRedis()
        real = TheDoor(gp.SURFACES["waker"][0], conn)

        class Widened(TheDoor):
            def open(self, request, timeout=None):
                body = json.loads(request.data.decode("utf-8"))
                if body["key"] in ("lane:build", "gov:test"):
                    return Answer({"ok": True, "op": body["op"], "key": body["key"], "principal": "gemini"})
                return real.open(request, timeout)
        code, said, *_ = self.go("waker", conn=conn, door=Widened(gp.SURFACES["waker"][0], conn))
        text = "\n".join(said)
        self.assertEqual(1, code)
        self.assertIn("check refusals: FAIL xadd lane:build WAS ACCEPTED (200)", text)
        self.assertIn("check refusals: FAIL set gov:test WAS ACCEPTED (200)", text)

    def test_a_502_is_a_failure_of_that_check_in_closed_words(self):
        conn = FakeRedis()
        real = TheDoor(gp.SURFACES["laptop"][0], conn)
        hostile = "the secret plan is at dawn"

        class Lost(TheDoor):
            def open(self, request, timeout=None):
                if json.loads(request.data.decode("utf-8"))["op"] == "set":
                    raise urllib.error.HTTPError(request.full_url, 502, "x", {}, io.BytesIO(
                        json.dumps({"error": "outcome unknown", "type": hostile}).encode("utf-8")))
                return real.open(request, timeout)
        code, said, *_ = self.go("laptop", conn=conn, door=Lost(gp.SURFACES["laptop"][0], conn))
        text = "\n".join(said)
        self.assertEqual(1, code)
        self.assertIn("check kv: FAIL Unknown (HTTP 502 failed upstream)", text)
        self.assertNotIn(hostile, text)

    def test_an_unbound_account_sends_nothing(self):
        minted = []
        with mock.patch.object(bridge_client, "load_callers", lambda path=None: {}):
            code, said, conn, door, read = self.go("waker", minter=lambda *a: minted.append(a) or TOKEN)
        self.assertEqual(2, code)
        self.assertEqual(([], [], []), (minted, door.requests, read))
        self.assertIn("is not bound to gemini", said[-1])

    def test_a_dry_run_mints_nothing_and_sends_nothing(self):
        minted = []
        code, said, conn, door, read = self.go("laptop", argv=["--dry-run"], minter=lambda *a: minted.append(a) or TOKEN)
        self.assertEqual(0, code)
        self.assertEqual(([], [], []), (minted, door.requests, read))
        self.assertIn("DRY RUN", said[-1])

    def test_each_surface_mints_its_own_way_when_no_minter_is_given(self):
        for surface, name in (("laptop", "mint"), ("waker", "mint_metadata")):
            other = "mint_metadata" if name == "mint" else "mint"
            asked = []
            with mock.patch.object(bridge_client, name, lambda url, sa: asked.append((url, sa)) or TOKEN), \
                    mock.patch.object(bridge_client, other, lambda *a: self.fail("the wrong minter for " + surface)):
                conn = FakeRedis()
                conn.data[gp.LANE] = lane_of(sheet())
                door = TheDoor(gp.SURFACES[surface][0], conn)
                code = gp.main(["--surface", surface], caller=lambda u, t, r: bridge_client.call(u, t, r, opener=door),
                               read_sheet=lambda since: [r for r in sheet() if r[1] > since], now=NOW,
                               say=lambda line: None)
            self.assertEqual((0, [(URL, gp.SURFACES[surface][0])]), (code, asked), surface)


class TheJobRunsItAsItsOwnAccount(unittest.TestCase):
    def test_main_py_parity_is_the_waker_surface_and_gemini_s_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_waker_main_parity", ROOT / "cloud" / "agent-waker" / "main.py")
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
        seen = []
        with mock.patch.object(gp, "main", lambda argv: seen.append(argv) or 0):
            main.AGENT = "gemini"
            self.assertEqual(0, main.parity(["--rows", "7"]))
            main.AGENT = "claude-api"
            self.assertEqual(2, main.parity([]))
        self.assertEqual([["--surface", "waker", "--rows", "7"]], seen)

    def test_the_image_carries_the_bridge_client_its_callers_file_and_the_parity_script(self):
        docker = (ROOT / "cloud" / "agent-waker" / "Dockerfile").read_text(encoding="utf-8")
        for name in ("bridge_client", "redis_gov", "redis_doorbell", "gemini_parity"):
            self.assertIn("scripts/%s.py" % name, docker)
        self.assertIn("COPY cloud/redis-tool-bridge/callers.json /app/cloud/redis-tool-bridge/callers.json", docker)
        self.assertEqual(Path(bridge_client.CALLERS_FILE).relative_to(ROOT).as_posix(), "cloud/redis-tool-bridge/callers.json",
                         "the client reads callers.json at <repo>/cloud/redis-tool-bridge, which is /app/... in the image")


if __name__ == "__main__":
    unittest.main(verbosity=2)
