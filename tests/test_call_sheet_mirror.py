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

    def eval(self, script, numkeys, *args):
        """The stale-end script: compare-and-set on state=live and the epoch, then hash and stream
        together. A raised error applies nothing."""
        self.ops.append(("eval", args[0] if args else ""))
        self.writes.append(("eval", args[0] if args else ""))
        if getattr(self, "eval_raises", None):
            error = self.eval_raises
            self.eval_raises = None
            raise error
        if "HSET" not in script or "XADD" not in script:
            raise AssertionError("the end is not one script")
        keys, argv = args[:numkeys], args[numkeys:]
        call_key, calls_key = keys
        call_id, epoch, at, ttl, _maxlen, reason = argv
        current = dict(self.hashes.get(call_key, {}))
        # Follow the script's compare-and-set. A mutant that drops "state is live" closes again.
        if "state') ~= 'live'" in script and current.get("state") != "live":
            return 0
        if "epoch') ~=" in script and str(current.get("epoch")) != str(epoch):
            return 0
        current.update(state="ended", epoch=str(epoch), ended_at=at, reason=reason)
        self.hashes[call_key] = current
        self.ttls[call_key] = int(ttl)
        self.xadd(calls_key, {"event": "end", "call_id": call_id, "epoch": str(epoch),
                              "at": at, "reason": reason})
        return 1

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
        self.rows, self.appends, self.hidden, self.present_calls, self.copies = {}, [], set(), [], {}

    def present(self, rid):
        """The read-back hits for this id, or None. Hidden means the row was not seen.
        A hit carries payload, source_tag and the gateway sender stamp."""
        self.present_calls.append(rid)
        if rid in self.hidden:
            return None
        if rid in self.copies:
            return list(self.copies[rid])
        row = self.rows.get(rid)
        if row is None:
            return None
        return [row]

    def append(self, spec):
        self.appends.append(spec)
        if spec["row_id"] not in self.hidden:
            stored = dict(spec)
            stored.setdefault("sender", gateway_stamp(spec))
            self.rows[spec["row_id"]] = stored


def gateway_stamp(spec):
    """What gas-bus senderCheck_ stamps for a bus-reconciler post with its ID token: column C against from=."""
    claimed, said = spec["source_tag"], sr._field(spec["payload"], "from")
    if said and claimed and said != claimed:
        return "sender=mismatch;claimed=%s;from=%s" % (claimed, said)
    return sr.VERIFIED_STAMP


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
            "BCB|v=1|id=CALLSTART-%s|phase=RESULT|class=CALL|from=bus-reconciler|to=ALL|"
            "msg_type=CALL_START|call_id=%s|epoch=7|started_at=%s|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, LIVE, start_at), start["payload"])
        self.assertEqual(
            "BCB|v=1|id=CALLEND-%s|phase=RESULT|class=CALL|from=bus-reconciler|to=ALL|"
            "msg_type=CALL_END|call_id=%s|epoch=7|ended_at=%s|reason=hangup|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, LIVE, end_at), end["payload"])
        for spec in (start, end):
            self.assertEqual(("bus-reconciler", "ALL", "RESULT"),
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

    def test_the_call_code_the_end_acks_and_each_join_reach_the_board(self):
        # conference #405 (T-02): the chair streams start with code, join per binder (first join only), end with acks.
        conn, board = FakeRedis(), Board()
        start_at, join_at, end_at = "2026-11-09T13:30:00Z", "2026-11-09T13:30:05Z", "2026-11-09T13:50:00Z"
        conn.add(sr.CALLS_KEY, "1000-0", {"event": "start", "call_id": LIVE, "epoch": "7", "at": start_at,
                                          "code": "SFDC-2026-11-09-0830"})
        conn.add(sr.CALLS_KEY, "1000-1", {"event": "join", "call_id": LIVE, "epoch": "7", "persona": "grok",
                                          "at": join_at})
        conn.add(sr.CALLS_KEY, "1000-2", {"event": "join", "call_id": LIVE, "epoch": "7", "persona": "claude",
                                          "at": join_at})
        conn.add(sr.CALLS_KEY, "1000-3", {"event": "leave", "call_id": LIVE, "epoch": "7", "persona": "grok",
                                          "at": end_at})
        conn.add(sr.CALLS_KEY, "1000-4", {"event": "end", "call_id": LIVE, "epoch": "7", "at": end_at,
                                          "reason": "hangup", "acks": "grok,claude"})
        _run_pass(conn, board)
        by_id = {spec["row_id"]: spec for spec in board.appends}
        self.assertEqual(["CALLSTART-" + LIVE, "CALLJOIN-%s-grok" % LIVE, "CALLJOIN-%s-claude" % LIVE,
                          "CALLEND-" + LIVE], list(by_id), "a leave is not a row")
        self.assertEqual(
            "BCB|v=1|id=CALLSTART-%s|phase=RESULT|class=CALL|from=bus-reconciler|to=ALL|"
            "msg_type=CALL_START|call_id=%s|epoch=7|started_at=%s|code=SFDC-2026-11-09-0830|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, LIVE, start_at),
            by_id["CALLSTART-" + LIVE]["payload"])
        self.assertEqual(
            "BCB|v=1|id=CALLJOIN-%s-grok|phase=RESULT|class=CALL|from=bus-reconciler|to=ALL|"
            "msg_type=CALL_JOIN|call_id=%s|epoch=7|persona=grok|joined_at=%s|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, LIVE, join_at),
            by_id["CALLJOIN-%s-grok" % LIVE]["payload"])
        self.assertEqual(
            "BCB|v=1|id=CALLEND-%s|phase=RESULT|class=CALL|from=bus-reconciler|to=ALL|"
            "msg_type=CALL_END|call_id=%s|epoch=7|ended_at=%s|reason=hangup|acks=claude,grok|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, LIVE, end_at),
            by_id["CALLEND-" + LIVE]["payload"])
        for spec in by_id.values():
            self.assertEqual(("bus-reconciler", "ALL", "RESULT"),
                             (spec["source_tag"], spec["target_surface"], spec["action_type"]))
            self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
            self.assertEqual([], board_row.problems(spec["payload"]))
            self.assertEqual(sr.VERIFIED_STAMP, board.rows[spec["row_id"]]["sender"], spec["row_id"])
        self.assertEqual("1000-4", conn.get(sr.CALLS_CURSOR), "the leave was passed, and the end read back")

    def test_an_older_chair_without_code_or_acks_writes_the_rows_as_before(self):
        start = sr._event_spec({"event": "start", "call_id": LIVE, "epoch": "7", "at": "t"})
        end = sr._event_spec({"event": "end", "call_id": LIVE, "epoch": "7", "at": "t", "reason": "r"})
        self.assertNotIn("code=", start["payload"])
        self.assertNotIn("acks=", end["payload"])
        blank = sr._event_spec({"event": "end", "call_id": LIVE, "epoch": "7", "at": "t", "reason": "r", "acks": ""})
        self.assertEqual(end["payload"], blank["payload"], "an End with no binder present carries no acks=")

    def test_a_code_or_persona_that_is_not_one_is_never_copied(self):
        for code in ("SFDC-2026-11-09-083", "sfdc-2026-11-09-0830", "SFDC-2026-11-09-0830-1",
                     "SFDC-2026-11-09-0830|to=grok", "SFDC-2026-11-09-0830 x", "swordfish"):
            spec = sr._event_spec({"event": "start", "call_id": LIVE, "epoch": "7", "at": "t", "code": code})
            self.assertNotIn("code=", spec["payload"], code)
            self.assertNotIn("swordfish", spec["payload"])
        spec = sr._event_spec({"event": "start", "call_id": LIVE, "epoch": "7", "at": "t",
                               "code": "SFDC-2026-11-09-0830-2"})
        self.assertIn("|code=SFDC-2026-11-09-0830-2|", spec["payload"], "a second call in the same minute")
        end = sr._event_spec({"event": "end", "call_id": LIVE, "epoch": "7", "at": "t", "reason": "r",
                              "acks": "grok,swordfish,grok, gemini ,|to=all"})
        self.assertIn("|acks=gemini,grok|", end["payload"], "known personas only, once each, sorted")
        self.assertNotIn("swordfish", end["payload"])
        for persona in ("", "swordfish", "Grok", "grok|x", "owner"):
            self.assertIsNone(sr._event_spec({"event": "join", "call_id": LIVE, "epoch": "7", "persona": persona,
                                              "at": "t"}), persona)
        self.assertIsNone(sr._event_spec({"event": "join", "call_id": "nope", "epoch": "7", "persona": "grok",
                                          "at": "t"}))
        self.assertIsNone(sr._event_spec({"event": "join", "call_id": LIVE, "epoch": "", "persona": "grok",
                                          "at": "t"}))
        self.assertIsNone(sr._event_spec({"event": "leave", "call_id": LIVE, "epoch": "7", "persona": "grok",
                                          "at": "t"}))

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
        board.rows[spec["row_id"]] = dict(spec, sender=sr.VERIFIED_STAMP)
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        _mirror(conn, board)
        self.assertEqual([], board.appends)
        self.assertEqual("1000-0", conn.get(sr.CALLS_CURSOR))

    def test_every_row_the_mirror_writes_is_one_the_gateway_verifies(self):
        conn, board = FakeRedis(), Board()
        conn.add(sr.CALLS_KEY, "1000-0", {"event": "start", "call_id": LIVE, "epoch": "7",
                                          "at": "2026-11-08T12:10:00Z", "reason": ""})
        _mirror(conn, board)
        for row in board.rows.values():
            self.assertEqual(sr.VERIFIED_STAMP, row["sender"], row["row_id"])
        self.assertEqual("1000-0", conn.get(sr.CALLS_CURSOR), "its own row reads back as done")

    def test_a_legacy_row_from_before_the_fix_is_passed_not_reposted(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        old = spec["payload"].replace("|from=bus-reconciler|", "|from=conference-chair|", 1)
        board.rows[spec["row_id"]] = dict(spec, payload=old, sender=sr.LEGACY_STAMP)
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING") as caught:
            _mirror(conn, board)
        self.assertEqual([], board.appends, "never re-posted")
        self.assertEqual("1000-0", conn.get(sr.CALLS_CURSOR), "the cursor moves past it")
        self.assertIn("legacy", "\n".join(caught.output))

    def test_a_legacy_stamp_on_any_other_payload_is_still_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        old = spec["payload"].replace("|from=bus-reconciler|", "|from=conference-chair|", 1) + "|note=x"
        board.rows[spec["row_id"]] = dict(spec, payload=old, sender=sr.LEGACY_STAMP)
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING"):
            _mirror(conn, board)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))

    def test_a_legacy_stamp_from_another_source_is_still_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        old = spec["payload"].replace("|from=bus-reconciler|", "|from=conference-chair|", 1)
        board.rows[spec["row_id"]] = dict(spec, payload=old, source_tag="grok", sender=sr.LEGACY_STAMP)
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING"):
            _mirror(conn, board)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))

    def test_two_identical_copies_are_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = dict(sr._event_spec(fields), sender=sr.VERIFIED_STAMP)
        board.copies[spec["row_id"]] = [spec, dict(spec)]
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING") as caught:
            _mirror(conn, board)
        text = "\n".join(caught.output)
        self.assertEqual([], board.appends)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))
        self.assertIn(spec["row_id"], text)
        self.assertIn("conflict", text)

    def test_a_copy_from_another_sender_is_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        board.rows[spec["row_id"]] = dict(spec, source_tag="grok",
                                          sender="sender=verified;who=grok@sfdc24.iam.gserviceaccount.com")
        conn.add(sr.CALLS_KEY, "1000-0", fields)
        with self.assertLogs("bus-requests", level="WARNING") as caught:
            _mirror(conn, board)
        text = "\n".join(caught.output)
        self.assertEqual([], board.appends)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))
        self.assertIn(spec["row_id"], text)
        self.assertNotIn("grok@sfdc24", text)

    def test_the_same_id_with_a_different_payload_is_a_conflict(self):
        conn, board = FakeRedis(), Board()
        fields = {"event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""}
        spec = sr._event_spec(fields)
        board.rows[spec["row_id"]] = dict(spec, sender=sr.VERIFIED_STAMP,
                                          payload=spec["payload"] + "|note=swordfish-secret")
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
            "BCB|v=1|id=CALLTURN-t-other|phase=ASK|class=CALL|from=bus-reconciler|to=grok|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-other|said_at=%s|withheld=1|"
            "turn_class=other|chars=40|relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, said))
        command = (
            "BCB|v=1|id=CALLTURN-t-cmd|phase=ASK|class=CALL|from=bus-reconciler|to=gemini|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-cmd|said_at=%s|withheld=1|"
            "turn_class=command|verb=status|target_chars=12|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, said))
        said_row = (
            "BCB|v=1|id=CALLTURN-t-said|phase=ASK|class=CALL|from=bus-reconciler|to=grok|"
            "msg_type=CALL_TURN|call_id=%s|epoch=7|turn_key=t-said|said_at=%s|text=hello|"
            "turn_class=command|verb=status|target_chars=4|"
            "relayer=bus-reconciler|claimed_author=conference-chair" % (LIVE, said))
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

    def test_the_box_reads_a_bus_reconciler_row_at_its_turn_substring(self):
        """conference#375: the box takes a turn only from a row stamped for bus-reconciler,
        and only when msg_type=CALL_TURN is immediately followed by call_id=<room>."""
        conn, board = FakeRedis(), Board()
        said = "2026-11-08T12:55:00Z"
        conn.add(sr.CALLS_KEY, "1000-0", {
            "event": "start", "call_id": LIVE, "epoch": "7", "at": said, "reason": ""})
        conn.hashes[sr.CALL_KEY % LIVE] = {
            "state": "live", "epoch": "7", "started_at": said, "ended_at": "", "reason": ""}
        conn.add(sr.TURNS_KEY % LIVE, "5000-0", {
            "turn_key": "t-box", "to": "grok", "epoch": "7", "said_at": said, "text": "hello",
            "turn_class": "other", "chars": "5"})
        _mirror(conn, board)
        by_id = {spec["row_id"]: spec for spec in board.appends}
        start, turn = by_id["CALLSTART-" + LIVE], by_id["CALLTURN-t-box"]
        for spec in (start, turn):
            self.assertEqual("bus-reconciler", spec["source_tag"])
            self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
        self.assertIn("|msg_type=CALL_TURN|call_id=%s|" % LIVE, turn["payload"])
        self.assertEqual("bus-reconciler", sr._field(turn["payload"], "from"), "from= is the poster (the gateway's rule)")
        self.assertEqual("conference-chair", sr._field(turn["payload"], "claimed_author"))
        self.assertEqual("bus-reconciler", sr._field(turn["payload"], "relayer"))


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
        self.assertEqual([sr.CALLS_KEY], [key for op, key in conn.writes if op == "xadd"])

    def test_a_stale_end_is_one_write_and_a_failed_script_writes_neither(self):
        conn, board = FakeRedis(), Board()
        over_at = _stamp(NOW - datetime.timedelta(minutes=50, seconds=1))
        conn.hashes[sr.CALL_KEY % OVER] = {
            "state": "live", "epoch": "4", "started_at": over_at, "ended_at": "", "reason": ""}
        conn.eval_raises = RuntimeError("noscript")
        _mirror(conn, board)
        self.assertEqual("live", conn.hashes[sr.CALL_KEY % OVER]["state"])
        self.assertIsNone(conn.streams.get(sr.CALLS_KEY))
        self.assertEqual([], [spec for spec in board.appends if spec["row_id"].startswith("CALLEND")])
        _mirror(conn, board)                                   # the script lands: one end
        self.assertEqual("ended", conn.hashes[sr.CALL_KEY % OVER]["state"])
        self.assertEqual(1, len(conn.streams[sr.CALLS_KEY]))
        _mirror(conn, board)                                   # the end becomes one CALLEND row
        ends = [spec for spec in board.appends if spec["row_id"] == "CALLEND-" + OVER]
        self.assertEqual(1, len(ends))
        _mirror(conn, board)                                   # a later pass does not end it again
        self.assertEqual(1, len(conn.streams[sr.CALLS_KEY]))
        self.assertEqual(1, len([spec for spec in board.appends if spec["row_id"] == "CALLEND-" + OVER]))

    def test_losing_the_lease_mid_pass_writes_no_cursor_and_no_state(self):
        conn, board = FakeRedis(), Board()
        over_at = _stamp(NOW - datetime.timedelta(minutes=50, seconds=1))
        conn.hashes[sr.CALL_KEY % OVER] = {
            "state": "live", "epoch": "4", "started_at": over_at, "ended_at": "", "reason": ""}
        conn.add(sr.CALLS_KEY, "1000-0", {
            "event": "start", "call_id": LIVE, "epoch": "7", "at": "2026-11-08T12:10:00Z", "reason": ""})
        calls = {"n": 0}

        def renew():
            calls["n"] += 1
            return calls["n"] < 2                              # the pass starts; the next fence fails

        with mock.patch.dict(sr.os.environ, {"CALL_SHEET_MIRROR": "on"}):
            with self.assertRaises(sr.LockLost):
                sr.mirror_call_sheet(conn, NOW, {}, renew=renew, append=board.append, present=board.present)
        self.assertIsNone(conn.strings.get(sr.CALLS_CURSOR))
        self.assertEqual("live", conn.hashes[sr.CALL_KEY % OVER]["state"])
        self.assertEqual([], [key for op, key in conn.writes if op in ("xadd", "hset", "set", "eval")])


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
