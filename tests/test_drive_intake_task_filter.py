"""drive-intake's task filter (cloud/drive-intake/task_filter.py), the owner's spec of 2026-10-16 08:28 ET.

Fail-closed: a doc in the folder is dispatched as a TASK only when it passes all five gates, and
anything ambiguous is a NOTE that is never claimed, receipted, queued or rung. Each gate has a test
that fails when the gate is removed. Offline by construction: every outside call is a double.
"""
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests import test_drive_intake as base  # noqa: E402

di = base.di
tf = di.tf

NOW = datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc).timestamp()      # 08:30 ET
TODAY = "2026-10-16"
TOKEN = "s3cret-confirm"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def body(action='{"action":"rehearsal_call","callee":"abdus","caller":"claude"}', ok=TODAY, confirm=None,
         extra=""):
    lines = ["Morning check.", ""]
    if action is not None:
        lines.append(action)
    if ok is not None:
        lines.append("TASK-OK: " + ok)
    if confirm is not None:
        lines.append("CONFIRM: " + confirm)
    return "\n".join(lines) + "\n" + extra


class Docs(base.Docs):
    def __init__(self, text=None, title="LOU CALL GO #7", created=NOW - 60, **kw):
        super().__init__(text=body() if text is None else text, **kw)
        self.title, self.created = title, created

    def read(self, file_id):
        out = super().read(file_id)
        out["title"] = self.title
        out["created_time"] = iso(self.created) if isinstance(self.created, float) else self.created
        return out


class Renamer:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, file_id, title):
        self.calls.append((file_id, title))
        if self.fail:
            raise ConnectionError("drive said no")


def deps(docs=None, **over):
    base_over = dict(docs=docs or Docs(), now=NOW, filter_tasks=True, confirm_token=TOKEN, renamer=Renamer(),
                     at="2026-10-16T12:30:00Z")
    base_over.update(over)
    return base.deps(**base_over)


def run(d):
    return di.decide(base.hint(), d)


def nothing_happened(test, d):
    test.assertEqual({}, d.claims.states)
    test.assertEqual([], d.receipts.lines)
    test.assertEqual([], d.queue.puts)
    test.assertEqual([], d.ring.calls)


class G1TitleTest(unittest.TestCase):
    def test_allowlisted_prefixes_are_tasks(self):
        self.assertEqual("LOU CALL GO", tf.prefix_of("LOU CALL GO #7"))
        self.assertEqual("TASK:", tf.prefix_of("TASK: summarise the board"))

    def test_everything_else_is_a_note_and_nothing_is_written(self):
        for title in ("lou call go #7", " LOU CALL GO", "Claude -> Grok: standing by for call-go",
                      "GROK -> CLAUDE: feedback", "CONVERSPAN MASTER PLAN", "DRAFT LOU CALL GO",
                      "RESULT: x", "PLAN", "HOLD", "CONSUMED task-1 t", "STALE (not dialed) LOU CALL GO #6",
                      "task: lower case", "GATE3 FIRE: not yet allowlisted", "", None):
            d = deps(Docs(title=title))
            out = run(d)
            self.assertEqual(("note", "title_not_allowlisted"), (out["status"], out.get("reason")), repr(title))
            nothing_happened(self, d)

    def test_a_note_is_finished_so_the_poll_does_not_keep_it(self):
        self.assertIn("note", di.FINISHED)


class G2BodyTest(unittest.TestCase):
    def assert_note(self, text, reason, title="LOU CALL GO #7"):
        d = deps(Docs(text=text, title=title))
        out = run(d)
        self.assertEqual(("note", reason), (out["status"], out.get("reason")))
        nothing_happened(self, d)

    def test_no_action_line(self):
        self.assert_note(body(action=None), "action_line_count")

    def test_two_action_lines(self):
        self.assert_note(body(extra='{"action":"rehearsal_call","callee":"abdus"}\n'), "action_line_count")

    def test_an_action_line_that_does_not_parse(self):
        self.assert_note(body(action='{"action":"rehearsal_call", callee: abdus}'), "action_line_unparsed")

    def test_json_inside_prose_is_not_a_standalone_line(self):
        self.assert_note(body(action='Please run {"action":"rehearsal_call","callee":"abdus"} now'),
                         "action_line_count")

    def test_the_action_must_match_the_prefix(self):
        self.assert_note(body(), "action_mismatch", title="TASK: call him")
        self.assert_note(body(action='{"action":"task"}'), "action_mismatch")

    def test_unknown_keys_on_a_rehearsal_call(self):
        self.assert_note(body(action='{"action":"rehearsal_call","callee":"abdus","phone":"+15555550100"}'),
                         "action_extra_keys")

    def test_task_ok_missing_stale_or_doubled(self):
        self.assert_note(body(ok=None), "task_ok_count")
        self.assert_note(body(ok="2026-10-15"), "task_ok_not_today")
        self.assert_note(body(extra="TASK-OK: %s\n" % TODAY), "task_ok_count")

    def test_task_ok_is_the_toronto_day_not_the_utc_day(self):
        late = datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc).timestamp()      # 22:00 ET on the 16th
        self.assertEqual("task", tf.classify({"title": "TASK: x", "created_time": iso(late - 60)},
                                             body(action='{"action":"task"}'), late)["kind"])

    def test_lines_below_receipts_do_not_count(self):
        text = body(action=None) + "\nRECEIPTS\n" + '{"action":"rehearsal_call","callee":"abdus"}\n'
        self.assert_note(text, "action_line_count")


class G3FreshTest(unittest.TestCase):
    def test_older_than_ten_minutes_is_a_note(self):
        d = deps(Docs(created=NOW - 601))
        out = run(d)
        self.assertEqual(("note", "stale"), (out["status"], out.get("reason")))
        nothing_happened(self, d)

    def test_inside_ten_minutes_is_admitted(self):
        self.assertEqual("dispatched", run(deps(Docs(created=NOW - 599)))["status"])

    def test_missing_or_far_future_created_time_is_a_note(self):
        self.assertEqual("no_created_time", run(deps(Docs(created="")))["reason"])
        self.assertEqual("stale", run(deps(Docs(created=NOW + 120)))["reason"])

    def test_a_delivery_already_under_way_is_finished_even_after_ten_minutes(self):
        ring = base.Ring(fail_first=1)
        docs = Docs()
        d = deps(docs, ring=ring)
        self.assertEqual("queued_not_rung", run(d)["status"])
        d.now = NOW + 1200                     # twenty minutes later the ring works
        self.assertEqual("dispatched", run(d)["status"])
        self.assertEqual(1, len(d.queue.puts))


def task_docs(**kw):
    return Docs(title="TASK: filter self-test", text=body(action='{"action":"task"}'), **kw)


class Go7FormatTest(unittest.TestCase):
    """The exact doc the board told Claude to write for the 8:44 ET call (GROK-LOU-CALL-0844-FORMAT)."""
    TEXT = ('{"action":"rehearsal_call","callee":"abdus","caller":"claude"}\n'
            "TASK-OK: 2026-10-16\n"
            "CONFIRM: abdus-0844\n")

    def test_it_passes_every_gate_and_is_dry_run_here_without_the_deploy_token(self):
        d = deps(Docs(title="LOU CALL GO #7", text=self.TEXT), confirm_token="")
        out = run(d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual(("rehearsal_call", "dry_run"), (d.queue.puts[0][1]["action"], d.queue.puts[0][1]["mode"]))

    def test_it_is_never_renamed_so_the_box_call_watcher_still_finds_it(self):
        d = deps(Docs(title="LOU CALL GO #7", text=self.TEXT))
        out = run(d)
        self.assertEqual("recorded_not_renamed", out["consumed"])
        self.assertEqual([], d.renamer.calls)
        self.assertTrue(d.claims.processed_has(base.FILE))


class G4ConsumedTest(unittest.TestCase):
    def test_dispatch_records_the_doc_and_renames_it_consumed(self):
        d = deps(task_docs())
        out = run(d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("renamed", out["consumed"])
        self.assertTrue(d.claims.processed_has(base.FILE))
        self.assertEqual([(base.FILE, "CONSUMED %s 2026-10-16T12:30:00Z" % out["task"])], d.renamer.calls)
        self.assertEqual("title_not_allowlisted",
                         tf.classify({"title": d.renamer.calls[0][1]}, body(), NOW)["reason"])

    def test_an_edited_consumed_doc_is_never_admitted_again(self):
        docs = Docs()
        d = deps(docs)
        run(d)
        docs.text = body(extra="one more line\n")     # a new request revision, the title unchanged
        out = run(d)
        self.assertEqual("consumed", out["status"])
        self.assertEqual(1, len(d.queue.puts))
        self.assertIn("consumed", di.FINISHED)

    def test_a_failed_rename_is_said_and_the_set_still_guards(self):
        d = deps(task_docs(), renamer=Renamer(fail=True))
        out = run(d)
        self.assertEqual(("dispatched", "rename_failed:ConnectionError"), (out["status"], out["consumed"]))
        self.assertTrue(d.claims.processed_has(base.FILE))
        self.assertEqual("no_renamer", run(deps(task_docs(), renamer=None))["consumed"])


class G5CostlyTest(unittest.TestCase):
    def test_callee_must_be_on_the_allowlist_never_a_number(self):
        for callee in ('"+16475550100"', '"Abdus"', '"someone"', '""', "null", "7"):
            d = deps(Docs(text=body(action='{"action":"rehearsal_call","callee":%s}' % callee)))
            self.assertEqual("note", run(d)["status"], callee)
            nothing_happened(self, d)

    def test_without_confirm_it_is_dry_run_and_uses_no_rate_slot(self):
        d = deps()
        out = run(d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("dry_run", d.queue.puts[0][1]["mode"])
        self.assertEqual("rehearsal_call", d.queue.puts[0][1]["action"])
        self.assertNotIn("live_last:rehearsal_call", d.claims.beats)

    def test_a_wrong_token_or_no_deploy_token_is_dry_run(self):
        d = deps(Docs(text=body(confirm="guess")))
        run(d)
        self.assertEqual("dry_run", d.queue.puts[0][1]["mode"])
        d = deps(Docs(text=body(confirm=TOKEN)), confirm_token="")
        run(d)
        self.assertEqual("dry_run", d.queue.puts[0][1]["mode"])

    def test_the_right_token_is_live(self):
        d = deps(Docs(text=body(confirm=TOKEN)))
        self.assertEqual("dispatched", run(d)["status"])
        self.assertEqual("live", d.queue.puts[0][1]["mode"])

    def test_two_confirm_lines_is_a_note(self):
        d = deps(Docs(text=body(confirm=TOKEN, extra="CONFIRM: other\n")))
        self.assertEqual(("note", "confirm_count"), (run(d)["status"], run(d).get("reason")))

    def test_a_general_task_spends_so_it_is_dry_run_without_confirm(self):
        d = deps(Docs(title="TASK: summarise", text=body(action='{"action":"task","what":"summarise"}')))
        run(d)
        self.assertEqual("dry_run", d.queue.puts[0][1]["mode"])

    def test_one_live_dial_per_180_seconds(self):
        claims = di.MemoryClaims()
        first = deps(Docs(text=body(confirm=TOKEN)), claims=claims)
        self.assertEqual("dispatched", run(first)["status"])
        other = "2pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4"
        second = deps(Docs(text=body(confirm=TOKEN, extra="second\n")), claims=claims, now=NOW + 100)
        out = di.decide(base.hint(file_id=other), second)
        self.assertEqual(("rate_limited", "gap"), (out["status"], out.get("reason")))
        self.assertEqual([], second.queue.puts)
        third = deps(Docs(text=body(confirm=TOKEN, extra="third\n"), created=NOW + 150), claims=claims,
                     now=NOW + 181)
        self.assertEqual("dispatched", di.decide(base.hint(file_id=other), third)["status"])

    def test_ten_live_dials_per_toronto_day(self):
        claims = di.MemoryClaims()
        for n in range(10):
            self.assertEqual("ok", claims.live_reserve("rehearsal_call", NOW + n * 200, 180, 10, TODAY))
        self.assertEqual("day_cap", claims.live_reserve("rehearsal_call", NOW + 5000, 180, 10, TODAY))
        self.assertEqual("ok", claims.live_reserve("rehearsal_call", NOW + 5000, 180, 10, "2026-10-17"))

    def test_redis_reservation_is_one_atomic_call_on_fleet_keys(self):
        class Client:
            def __init__(self):
                self.calls = []

            def eval(self, *args):
                self.calls.append(args)
                return b"gap"
        c = Client()
        self.assertEqual("gap", di.RedisClaims(c).live_reserve("rehearsal_call", NOW, 180, 10, TODAY))
        script, nkeys, k1, k2 = c.calls[0][:4]
        self.assertEqual((di.RATE_LUA, 2), (script, nkeys))
        self.assertEqual(("fleet:intake:live:rehearsal_call:last", "fleet:intake:live:rehearsal_call:day:" + TODAY),
                         (k1, k2))


class WiringTest(unittest.TestCase):
    def test_a_deploy_always_filters_and_no_env_turns_it_off(self):
        for env in ({}, {"DRIVE_INTAKE_TASK_FILTER": "off"}, {"DRIVE_INTAKE_FILTER": "0"}):
            self.assertTrue(di.deps_from_env(env).filter_tasks, env)
        self.assertEqual("", di.deps_from_env({}).confirm_token)
        self.assertEqual("x", di.deps_from_env({"DRIVE_INTAKE_CONFIRM_TOKEN": "x"}).confirm_token)

    def test_the_image_carries_the_filter(self):
        dockerfile = (REPO / "cloud" / "drive-intake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY cloud/drive-intake/task_filter.py /app/task_filter.py", dockerfile)

    def test_the_inbox_entry_carries_the_mode(self):
        class Client:
            def eval(self, *args):
                self.args = args
                return 1
        c = Client()
        tuple_ = di.correlate(base.FILE, body(), "r1", "claude-api")
        di.InboxQueue(c).put(tuple_, di.task_gist(body(), {"action": "rehearsal_call", "mode": "dry_run",
                                                         "prefix": "LOU CALL GO"}), "t")
        flat = list(c.args[6:-1])
        fields = dict(zip(flat[::2], flat[1::2]))
        self.assertEqual(("rehearsal_call", "dry_run", "LOU CALL GO"),
                         (fields["action"], fields["mode"], fields["prefix"]))


if __name__ == "__main__":
    unittest.main()
