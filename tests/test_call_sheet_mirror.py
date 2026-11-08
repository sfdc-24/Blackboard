"""The call-sheet mirror: Redis call events and live turns become sheet rows. Offline.

The sheet is a projection. A missing turn text is withheld, not reconstructed, and nothing
here writes a voice lane. CALL_SHEET_MIRROR is exactly "on". Anything else does nothing.
"""
import datetime
import hashlib
import fnmatch
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import board_row                                                         # noqa: E402
import serve_requests as sr                                               # noqa: E402
import signature                                                          # noqa: E402

NOW = datetime.datetime(2026, 11, 8, 13, 0, 0, tzinfo=datetime.timezone.utc)
LIVE = "conf-0123456789abcdef-0123abcd"
ENDED = "conf-fedcba9876543210-abcd0123"
EXACT = "conf-1111111111111111-22222222"
OVER = "conf-aaaaaaaaaaaaaaaa-bbbbbbbb"
HANDLE = {"answered": [], "refused": [], "capped": 0}


def _id_key(entry_id):
    ms, sep, seq = str(entry_id).partition("-")
    if not sep:
        return (0, 0)
    return (int(ms), int(seq or 0))


class FakeRedis:
    """Streams, hashes, strings and one set. Exclusive xrange, which is how a cursor is held."""

    def __init__(self):
        self.strings, self.hashes, self.streams, self.sets = {}, {}, {}, {}
        self.ttls, self.ops, self.writes = {}, [], []

    def get(self, key):
        self.ops.append(("get", key))
        return self.strings.get(key)

    def set(self, key, value, nx=False, ex=None):
        self.ops.append(("set", key))
        self.writes.append(("set", key))
        self.strings[key] = value
        return True

    def hgetall(self, key):
        self.ops.append(("hgetall", key))
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.ops.append(("hset", key))
        self.writes.append(("hset", key))
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        return 1

    def xadd(self, key, fields, **kwargs):
        self.ops.append(("xadd", key))
        self.writes.append(("xadd", key))
        seq = len(self.streams.get(key, [])) + 1
        entry_id = "2000-%d" % seq
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    def add(self, key, entry_id, fields):
        """Plant an entry the chair already wrote, without counting it as this worker's write."""
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))

    def xrange(self, key, min="-", max="+", count=None):
        self.ops.append(("xrange", key))
        items = list(self.streams.get(key, []))
        if min and min != "-":
            if str(min).startswith("("):
                cut = _id_key(str(min)[1:])
                items = [item for item in items if _id_key(item[0]) > cut]
            else:
                cut = _id_key(min)
                items = [item for item in items if _id_key(item[0]) >= cut]
        if count is not None:
            items = items[:int(count)]
        return items

    def scan_iter(self, match=None, count=None):
        self.ops.append(("scan_iter", match))
        for key in list(self.hashes):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def sadd(self, key, member):
        self.ops.append(("sadd", key))
        self.writes.append(("sadd", key))
        self.sets.setdefault(key, set()).add(member)
        return 1

    def sismember(self, key, member):
        self.ops.append(("sismember", key))
        return member in self.sets.get(key, ())

    def expire(self, key, seconds):
        self.ops.append(("expire", key))
        self.writes.append(("expire", key))
        self.ttls[key] = seconds
        return 1


class Board:
    def __init__(self):
        self.rows, self.appends, self.hidden, self.present_calls = {}, [], set(), []

    def present(self, rid):
        """The stored payload, or None. Hidden means the read-back has not seen the row."""
        self.present_calls.append(rid)
        if rid not in self.rows or rid in self.hidden:
            return None
        return self.rows[rid].get("payload")

    def append(self, spec):
        self.appends.append(spec)
        if spec["row_id"] not in self.hidden:
            self.rows[spec["row_id"]] = spec


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_pass(conn, board, rows=None):
    with mock.patch.object(sr.bus_request, "heartbeat"), \
            mock.patch.object(sr.bus_reconcile, "board_rows", return_value=rows or []), \
            mock.patch.object(sr.bus_request, "handle", return_value=dict(HANDLE)), \
            mock.patch.object(sr, "_append_row", side_effect=lambda env, renew, spec: board.append(spec)), \
            mock.patch.object(sr, "_row_present", side_effect=lambda env, rid: board.present(rid)), \
            mock.patch.dict(sr.os.environ, {"CALL_SHEET_MIRROR": "on"}):
        return sr.one_pass(conn, {}, "bus-requests-test", with_git=False, quiet=True, renew=lambda: True)


def _mirror(conn, board=None):
    board = board or Board()
    with mock.patch.dict(sr.os.environ, {"CALL_SHEET_MIRROR": "on"}):
        sr.mirror_call_sheet(conn, NOW, {}, append=board.append, present=board.present)
    return board


class CallEvents(unittest.TestCase):
    def test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet(self):
        conn, board = FakeRedis(), Board()
        start_at, end_at = "2026-11-08T12:10:00Z", "2026-11-08T12:40:00Z"
        conn.add(sr.CALLS_KEY, "1000-0", {"event": "start", "call_id": LIVE, "epoch": "7", "at": start_at, "reason": ""})
        conn.add(sr.CALLS_KEY, "1000-1", {"event": "end", "call_id": LIVE, "epoch": "7", "at": end_at, "reason": "hangup"})
        _run_pass(conn, board)
        by_id = {spec["row_id"]: spec for spec in board.appends}
        self.assertEqual(["CALLSTART-" + LIVE, "CALLEND-" + LIVE], list(by_id))
        start, end = by_id["CALLSTART-" + LIVE], by_id["CALLEND-" + LIVE]
        self.assertEqual(
            "BCB|v=1|id=CALLSTART-%s|phase=RESULT|class=CALL|from=conference-chair|to=ALL|"
            "msg_type=CALL_START|call_id=%s|epoch=7|started_at=%s" % (LIVE, LIVE, start_at), start["payload"])
        self.assertEqual(
            "BCB|v=1|id=CALLEND-%s|phase=RESULT|class=CALL|from=conference-chair|to=ALL|"
            "msg_type=CALL_END|call_id=%s|epoch=7|ended_at=%s|reason=hangup" % (LIVE, LIVE, end_at), end["payload"])
        for spec in (start, end):
            self.assertEqual(("conference-chair", "ALL", "RESULT"),
                             (spec["source_tag"], spec["target_surface"], spec["action_type"]))
            self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
            self.assertEqual([], board_row.problems(spec["payload"]))
        self.assertEqual("1000-1", conn.get(sr.CALLS_CURSOR), "the cursor rests on the entry just read back")

        marked = len(board.present_calls)
        _run_pass(conn, board)
        self.assertEqual(marked, len(board.present_calls), "a second loop does not read the entries again")
        self.assertEqual(2, len(board.appends))

        conn.strings.pop(sr.CALLS_CURSOR)
        _run_pass(conn, board)
        self.assertEqual(2, len(board.appends), "the row id is already on the board, so nothing is appended")
        self.assertEqual("1000-1", conn.get(sr.CALLS_CURSOR))

        new_id = "conf-bbbbbbbbbbbbbbbb-cccccccc"
        conn.add(sr.CALLS_KEY, "1001-0", {"event": "start", "call_id": new_id, "epoch": "7",
                                          "at": start_at, "reason": ""})
        board.hidden.add("CALLSTART-" + new_id)
        _run_pass(conn, board)
        self.assertIn("CALLSTART-" + new_id, [spec["row_id"] for spec in board.appends])
        self.assertEqual("1000-1", conn.get(sr.CALLS_CURSOR), "no read-back, so the cursor stays")

    def test_a_failed_row_is_logged_by_id_and_size_not_by_its_words(self):
        conn, board = FakeRedis(), Board()
        conn.add(sr.CALLS_KEY, "1000-0", {
            "event": "end", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:40:00Z",
            "reason": "swordfish-secret"})

        def explode(env, renew, spec):
            raise SystemExit("swordfish-secret")

        with mock.patch.object(sr.bus_request, "heartbeat"), \
                mock.patch.object(sr.bus_reconcile, "board_rows", return_value=[]), \
                mock.patch.object(sr.bus_request, "handle", return_value=dict(HANDLE)), \
                mock.patch.object(sr, "_append_row", side_effect=explode), \
                mock.patch.object(sr, "_row_present", side_effect=lambda env, rid: None), \
                mock.patch.dict(sr.os.environ, {"CALL_SHEET_MIRROR": "on"}), \
                self.assertLogs("bus-requests", level="WARNING") as caught:
            out = sr.one_pass(conn, {}, "bus-requests-test", with_git=False, quiet=True, renew=lambda: True)
        self.assertEqual(HANDLE["answered"], out["answered"])
        text = "\n".join(caught.output)
        self.assertNotIn("swordfish", text)
        self.assertIn("CALLEND-" + LIVE, text)
        self.assertIn("size=", text)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))

    def test_the_same_id_and_payload_is_already_done(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        board.rows[spec["row_id"]] = spec
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        _mirror(conn, board)
        self.assertEqual([], board.appends)
        self.assertEqual("1000-0", conn.get(sr.CALLS_CURSOR))

    def test_the_same_id_with_a_different_payload_is_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        board.rows[spec["row_id"]] = dict(spec, payload=spec["payload"] + "|note=swordfish-secret")
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING") as caught:
            _mirror(conn, board)
        text = "\n".join(caught.output)
        self.assertEqual([], board.appends)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))
        self.assertIn(spec["row_id"], text)
        self.assertIn("conflict", text)
        self.assertNotIn("swordfish", text)


class CallTurns(unittest.TestCase):
    def test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none(self):
        conn, board = FakeRedis(), Board()
        said = "2026-11-08T12:50:00Z"
        long_text = "a|b" + ("c" * 998)                                 # 1001 chars, one pipe
        conn.hashes[sr.CALL_KEY % LIVE] = {
            "state": "live", "epoch": "7", "started_at": said, "ended_at": "", "reason": ""}
        conn.hashes[sr.CALL_KEY % ENDED] = {
            "state": "ended", "epoch": "7", "started_at": said, "ended_at": said, "reason": "hangup"}
        conn.add(sr.TURNS_KEY % LIVE, "4000-0", {
            "turn_key": "t-001", "to": "grok", "epoch": "7", "said_at": said, "text": long_text})
        conn.add(sr.TURNS_KEY % LIVE, "4000-1", {
            "turn_key": "t-other", "to": "cursor", "epoch": "7", "said_at": said, "text": "nope"})
        conn.add(sr.TURNS_KEY % ENDED, "4000-0", {
            "turn_key": "t-ended", "to": "grok", "epoch": "7", "said_at": said, "text": "gone"})
        with self.assertLogs("bus-requests", level="WARNING") as caught:
            _mirror(conn, board)
        logged = "\n".join(caught.output)
        self.assertNotIn("nope", logged)
        self.assertNotIn("gone", logged)
        self.assertNotIn("a|b", logged)
        self.assertNotIn("ccc", logged)
        self.assertEqual(["CALLTURN-t-001"], [spec["row_id"] for spec in board.appends])
        spec = board.appends[0]
        full = "a/b" + ("c" * 998)
        self.assertEqual("grok", spec["target_surface"])
        self.assertEqual("ASK", spec["action_type"])
        self.assertEqual(full[:1000], sr._field(spec["payload"], "text"))
        self.assertNotIn("a|b", spec["payload"])
        self.assertEqual("1", sr._field(spec["payload"], "truncated"))
        self.assertEqual(str(len(full)), sr._field(spec["payload"], "chars"))
        self.assertEqual(hashlib.sha256(full.encode("utf-8")).hexdigest(), sr._field(spec["payload"], "sha256"))
        self.assertEqual([], board_row.problems(spec["payload"]))
        self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
        self.assertNotIn("gone", spec["payload"])
        self.assertNotIn("nope", " ".join(spec["payload"] for spec in board.appends))

        marked = len(board.present_calls)
        _mirror(conn, board)
        self.assertEqual(marked, len(board.present_calls))
        self.assertEqual(1, len(board.appends))
        self.assertEqual([], [key for op, key in conn.writes if str(key).startswith("fleet:voice-say:")])

    def test_a_turn_without_text_records_the_class_and_withheld(self):
        """turn_class=other carries chars, turn_class=command carries verb and target_chars.
        The envelope class stays CALL. The old class= field and a words field are not the row."""
        conn, board = FakeRedis(), Board()
        said = "2026-11-08T12:55:00Z"
        conn.hashes[sr.CALL_KEY % LIVE] = {
            "state": "live", "epoch": "7", "started_at": said, "ended_at": "", "reason": ""}
        conn.hashes[sr.CALL_KEY % ENDED] = {
            "state": "ended", "epoch": "7", "started_at": said, "ended_at": said, "reason": "hangup"}
        conn.add(sr.TURNS_KEY % LIVE, "5000-0", {
            "turn_key": "t-other", "to": "grok", "epoch": "7", "said_at": said,
            "turn_class": "other", "class": "not-this", "chars": "40", "words": "swordfish-secret"})
        conn.add(sr.TURNS_KEY % LIVE, "5000-1", {
            "turn_key": "t-cmd", "to": "gemini", "epoch": "7", "said_at": said,
            "turn_class": "command", "class": "not-this", "verb": "status", "target_chars": "12",
            "words": "secretphrase"})
        conn.add(sr.TURNS_KEY % LIVE, "5000-2", {
            "turn_key": "t-said", "to": "grok", "epoch": "7", "said_at": said, "text": "hello",
            "turn_class": "command", "class": "not-this", "verb": "status", "target_chars": "4"})
        conn.add(sr.TURNS_KEY % ENDED, "5000-0", {
            "turn_key": "t-dead", "to": "grok", "epoch": "7", "said_at": said,
            "turn_class": "other", "chars": "9", "words": "gone-words"})
        _mirror(conn, board)
        by_id = {spec["row_id"]: spec for spec in board.appends}
        self.assertEqual(["CALLTURN-t-other", "CALLTURN-t-cmd", "CALLTURN-t-said"], list(by_id))
        other = (
            "BCB|v=1|id=CALLTURN-t-other|phase=ASK|class=CALL|from=conference-chair|to=grok|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-other|said_at=%s|withheld=1|"
            "turn_class=other|chars=40" % (LIVE, said))
        command = (
            "BCB|v=1|id=CALLTURN-t-cmd|phase=ASK|class=CALL|from=conference-chair|to=gemini|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-cmd|said_at=%s|withheld=1|"
            "turn_class=command|verb=status|target_chars=12" % (LIVE, said))
        said_row = (
            "BCB|v=1|id=CALLTURN-t-said|phase=ASK|class=CALL|from=conference-chair|to=grok|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-said|said_at=%s|text=hello|"
            "turn_class=command|verb=status|target_chars=4" % (LIVE, said))
        self.assertEqual(other, by_id["CALLTURN-t-other"]["payload"])
        self.assertEqual(command, by_id["CALLTURN-t-cmd"]["payload"])
        self.assertEqual(said_row, by_id["CALLTURN-t-said"]["payload"])
        joined = other + command + said_row
        self.assertNotIn("not-this", joined)
        self.assertNotIn("swordfish", joined)
        self.assertNotIn("secretphrase", joined)
        self.assertNotIn("gone-words", joined)
        self.assertNotIn("|text=", other)
        self.assertNotIn("|text=", command)
        for spec in by_id.values():
            self.assertEqual([], board_row.problems(spec["payload"]))
            self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
        self.assertNotIn("CALLTURN-t-dead", by_id)


class StaleSweep(unittest.TestCase):
    def test_the_stale_sweep_closes_a_call_after_fifty_minutes_and_not_before(self):
        conn = FakeRedis()
        exact_at = _stamp(NOW - datetime.timedelta(minutes=50))
        over_at = _stamp(NOW - datetime.timedelta(minutes=50, seconds=1))
        conn.hashes[sr.CALL_KEY % EXACT] = {
            "state": "live", "epoch": "3", "started_at": exact_at, "ended_at": "", "reason": ""}
        conn.hashes[sr.CALL_KEY % OVER] = {
            "state": "live", "epoch": "4", "started_at": over_at, "ended_at": "", "reason": ""}
        _mirror(conn)
        exact = conn.hashes[sr.CALL_KEY % EXACT]
        over = conn.hashes[sr.CALL_KEY % OVER]
        self.assertEqual("live", exact["state"], "fifty minutes on the dot is not stale")
        self.assertEqual("", exact["reason"])
        self.assertEqual({"state": "ended", "ended_at": _stamp(NOW), "reason": "stale",
                          "epoch": "4", "started_at": over_at}, over)
        self.assertEqual([("2000-1", {"event": "end", "call_id": OVER, "epoch": "4",
                                      "at": _stamp(NOW), "reason": "stale"})],
                         conn.streams.get(sr.CALLS_KEY))
        self.assertEqual([sr.CALLS_KEY, sr.CALL_KEY % OVER],
                         [key for op, key in conn.writes if op in ("xadd", "hset")])


class TheFlag(unittest.TestCase):
    def test_the_flag_off_means_nothing_runs(self):
        cases = (None, "", "ON", "true", "1", "yes")
        for value in cases:
            conn, board = FakeRedis(), Board()
            conn.add(sr.CALLS_KEY, "1000-0", {
                "event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""})
            env = {} if value is None else {"CALL_SHEET_MIRROR": value}
            with mock.patch.object(sr.bus_request, "heartbeat"), \
                    mock.patch.object(sr.bus_reconcile, "board_rows", return_value=[]), \
                    mock.patch.object(sr.bus_request, "handle", return_value=dict(HANDLE)), \
                    mock.patch.object(sr, "_append_row", side_effect=lambda env, renew, spec: board.append(spec)), \
                    mock.patch.object(sr, "_row_present", side_effect=lambda env, rid: board.present(rid)), \
                    mock.patch.dict(sr.os.environ, env, clear=False):
                if value is None:
                    sr.os.environ.pop("CALL_SHEET_MIRROR", None)
                sr.one_pass(conn, {}, "bus-requests-test", with_git=False, quiet=True, renew=lambda: True)
            self.assertEqual([], conn.ops, value)
            self.assertEqual([], board.appends, value)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
