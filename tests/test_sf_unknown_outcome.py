"""Offline fault injection for ambiguous mutating Salesforce deploy starts."""
import copy
import unittest

from tests.test_studio_sf_build import ASK, SCRATCH_ID, Harness
from workers import sf_build as B


class UnknownDeployOutcome(unittest.TestCase):
    def prepared(self):
        h = Harness()
        h.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        h.do("ask", text=ASK)
        h.do("options")
        h.do("pick", option_id="b")
        h.do("build")
        return h

    def after_acceptance(self, org, *, error=None, returned=None):
        original = org.deploy

        def deploy(zip_bytes, *, check_only, purge_on_delete=False):
            original(zip_bytes, check_only=check_only, purge_on_delete=purge_on_delete)
            if error:
                raise error
            return returned

        org.deploy = deploy

    def assert_held(self, h, org, target, deploys=1):
        pending = h.sf()["pending"]
        self.assertEqual("outcome_unknown", pending["dispatch_state"])
        self.assertEqual(target, pending["target"])
        self.assertEqual(org.org_id, pending["org_id"])
        self.assertEqual(org.alias, pending["alias"])
        self.assertEqual(32, len(pending["attempt_id"]))
        self.assertEqual(64, len(pending["payload_sha256"]))
        self.assertTrue(pending["plan_hash"])
        self.assertTrue(pending["package_hash"])
        self.assertEqual(deploys, sum(not d["check_only"] for d in org.deploys))
        return pending

    def test_accepted_then_timeout_is_held_and_cannot_be_reissued_after_restart(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        qid = h.sf()["pending"]["question_id"]
        self.after_acceptance(org, error=TimeoutError("accepted, output lost"))
        h.do("confirm", answer="yes", question_id=qid)
        pending = self.assert_held(h, org, "scratch")
        self.assertNotIn("deploy_id", pending)
        self.assertEqual([], h.sf()["builds"])
        self.assertIsNone(h.sf()["built_plan"])
        self.assertIsNone(h.sf()["test"])
        status = h.do("status")
        self.assertIn("outcome is unknown", str(status["events"]))
        self.assertNotIn("Nothing is running", str(status["events"]))
        for action, args in (("confirm", {"answer": "yes", "question_id": qid}),
                             ("build", {}), ("test", {}), ("promote", {}),
                             ("revise", {"text": "change"}),
                             ("attach_scratch", {"org_id": SCRATCH_ID, "alias": "another"})):
            with self.subTest(action=action), self.assertRaises(B.LaneRefused):
                h.do(action, **args)
        another = B.SfBuildLane(load=lambda sid: h.repo.load(sid).state,
                                commit=h.controller.commit_lane, targets=h.targets,
                                architect=h.architect, sink=h.sink)
        with self.assertRaises(B.LaneRefused):
            another.handle(h.sid, {"action": "confirm", "answer": "yes", "question_id": qid})
        self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))
        self.assertEqual(pending["attempt_id"], h.sf()["pending"]["attempt_id"])

    def test_wait_timeout_after_known_id_holds_and_reconciles_that_job(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        real_wait = org.wait
        def lost_wait(job, budget):
            raise TimeoutError("accepted job, status output lost")
        org.wait = lost_wait
        h.do("confirm", answer="yes")
        pending = self.assert_held(h, org, "scratch")
        self.assertEqual("0Af000000000003AAA", pending["deploy_id"])
        self.assertEqual([], h.sf()["builds"])
        org.wait = real_wait
        h.do("status")
        self.assertEqual(1, len(h.sf()["builds"]))
        self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))

    def test_missing_and_malformed_ids_hold_without_guessing(self):
        for value in (None, "bad-id"):
            with self.subTest(value=value):
                h = self.prepared()
                org = h.targets.orgs["scratch"]
                self.after_acceptance(org, returned=value)
                h.do("confirm", answer="yes")
                self.assertNotIn("deploy_id", self.assert_held(h, org, "scratch"))
                before = list(org.calls)
                self.assertIn("outcome is unknown", str(h.do("status")["events"]))
                self.assertEqual(before, org.calls)

    def fail_post_dispatch_saves(self, h, failures):
        real_commit = h.lane.commit_fn
        remaining = failures
        def fail_writes(sid, change):
            def checked(state):
                nonlocal remaining
                pending = (state.get("sf_build") or {}).get("pending") or {}
                before = pending.get("dispatch_state")
                drafts = change(state)
                if before == "outcome_unknown" and drafts is not None and remaining:
                    remaining -= 1
                    raise RuntimeError("save unavailable")
                return drafts
            return real_commit(sid, checked)
        h.lane.commit_fn = fail_writes
        return real_commit

    def test_job_id_save_failure_holds_then_reconciles_only_existing_job(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        self.fail_post_dispatch_saves(h, 1)
        h.do("confirm", answer="yes")
        pending = self.assert_held(h, org, "scratch")
        self.assertEqual("0Af000000000003AAA", pending["deploy_id"])
        self.assertEqual([], h.sf()["builds"])
        h.do("status")
        self.assertEqual(1, len(h.sf()["builds"]))
        self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))

    def test_failed_unknown_save_leaves_dispatch_fence_without_id(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        real_commit = self.fail_post_dispatch_saves(h, 2)
        with self.assertRaisesRegex(B.LaneRefused, "outcome is unknown"):
            h.do("confirm", answer="yes")
        self.assertEqual("outcome_unknown", h.sf()["pending"]["dispatch_state"])
        self.assertNotIn("deploy_id", h.sf()["pending"])
        self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))
        h.lane.commit_fn = real_commit
        self.assertIn("outcome is unknown", str(h.do("status")["events"]))
        with self.assertRaises(B.LaneRefused):
            h.do("confirm", answer="yes")

    def test_promotion_and_undo_use_the_same_hold(self):
        h = Harness()
        h.to_display()
        h.do("test")
        h.do("promote")
        dev = h.targets.orgs["devorg"]
        self.after_acceptance(dev, error=TimeoutError("accepted promotion"))
        h.do("confirm", answer="yes")
        self.assert_held(h, dev, "devorg")
        self.assertEqual([], [b for b in h.sf()["builds"] if b["target"] == "devorg"])
        with self.assertRaises(B.LaneRefused):
            h.do("promote")
        scratch = Harness()
        scratch.to_display()
        org = scratch.targets.orgs["scratch"]
        scratch.do("undo")
        self.after_acceptance(org, error=TimeoutError("accepted undo"))
        scratch.do("confirm", answer="yes")
        self.assert_held(scratch, org, "scratch", deploys=2)
        self.assertNotIn("undone_at", scratch.sf()["builds"][-1])

    def test_changed_plan_or_target_cannot_settle_known_job(self):
        for tamper in ("plan", "org"):
            with self.subTest(tamper=tamper):
                h = self.prepared()
                org = h.targets.orgs["scratch"]
                org.result = dict(org.result, done=False)
                h.do("confirm", answer="yes")
                self.assertEqual("running", h.sf()["pending"]["dispatch_state"])
                if tamper == "plan":
                    record = h.repo.load(h.sid)
                    state = record.state
                    state["sf_build"]["plan"] = copy.deepcopy(state["sf_build"]["plan"])
                    state["sf_build"]["plan"]["title"] = "changed after dispatch"
                    h.repo.save(h.sid, state, record.token)
                else:
                    org.org_id = "00D000000000009AAA"
                org.result = dict(org.result, done=True)
                h.do("status")
                self.assertEqual("outcome_unknown", h.sf()["pending"]["dispatch_state"])
                self.assertEqual([], h.sf()["builds"])
                self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))

    def after_fence(self, h, mutate):
        real_commit = h.lane.commit_fn
        fired = [False]
        def commit(sid, change):
            result = real_commit(sid, change)
            if not fired[0] and (h.sf().get("pending") or {}).get("dispatch_state") == "outcome_unknown":
                fired[0] = True
                mutate()
            return result
        h.lane.commit_fn = commit
        return fired

    def test_stop_or_expiry_after_fence_prevents_dispatch(self):
        for ending in ("stop", "expiry"):
            with self.subTest(ending=ending):
                h = self.prepared()
                org = h.targets.orgs["scratch"]
                def end_session():
                    if ending == "stop":
                        h.controller.execute(h.sid, {"command_id": "stop-before-dispatch", "session_id": h.sid,
                                                     "type": "stop", "expected_version": h.state()["artifact_version"]})
                    else:
                        expired = h.state()["expires_at"] + 1
                        h.controller.clock = lambda: expired
                fired = self.after_fence(h, end_session)
                with self.assertRaises(Exception):
                    h.do("confirm", answer="yes")
                self.assertTrue(fired[0])
                self.assertEqual("outcome_unknown", h.sf()["pending"]["dispatch_state"])
                self.assertEqual(0, sum(not d["check_only"] for d in org.deploys))
                self.assertEqual([], h.sf()["builds"])

    def test_changed_binding_after_fence_prevents_dispatch(self):
        for field in ("attempt_id", "target", "plan"):
            with self.subTest(field=field):
                h = self.prepared()
                org = h.targets.orgs["scratch"]
                def tamper():
                    record = h.repo.load(h.sid)
                    state = record.state
                    sf = state["sf_build"]
                    if field == "plan":
                        sf["plan"]["title"] = "changed before dispatch"
                    else:
                        sf["pending"][field] = "devorg" if field == "target" else "older-attempt"
                    h.repo.save(h.sid, state, record.token)
                fired = self.after_fence(h, tamper)
                with self.assertRaises(B.LaneRefused):
                    h.do("confirm", answer="yes")
                self.assertTrue(fired[0])
                self.assertEqual("outcome_unknown", h.sf()["pending"]["dispatch_state"])
                self.assertEqual(0, sum(not d["check_only"] for d in org.deploys))

    def test_stop_and_expiry_preserve_the_hold(self):
        for ending in ("stop", "expiry"):
            with self.subTest(ending=ending):
                h = self.prepared()
                org = h.targets.orgs["scratch"]
                self.after_acceptance(org, error=TimeoutError("accepted"))
                h.do("confirm", answer="yes")
                attempt = h.sf()["pending"]["attempt_id"]
                if ending == "stop":
                    h.controller.execute(h.sid, {"command_id": "stop-unknown", "session_id": h.sid,
                                                 "type": "stop", "expected_version": h.state()["artifact_version"]})
                else:
                    h.controller.clock = lambda: h.state()["expires_at"] + 1
                with self.assertRaises(Exception):
                    h.do("status")
                self.assertEqual(attempt, h.sf()["pending"]["attempt_id"])
                self.assertEqual("outcome_unknown", h.sf()["pending"]["dispatch_state"])
                self.assertEqual(1, sum(not d["check_only"] for d in org.deploys))


    def test_stale_settlement_cannot_record_a_second_build(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        org.result = dict(org.result, done=False)
        h.do("confirm", answer="yes")
        pending = copy.deepcopy(h.sf()["pending"])
        org.result = dict(org.result, done=True)
        h.do("status")
        self.assertEqual(1, len(h.sf()["builds"]))
        with self.assertRaises(B.LaneRefused):
            h.lane._deployed(h.sid, "scratch", org, org.wait(pending["deploy_id"]),
                             pending["attempt_id"], pending["deploy_id"])
        self.assertEqual(1, len(h.sf()["builds"]))

    def test_fence_is_saved_before_deploy_and_pre_dispatch_rejection_is_safe(self):
        h = self.prepared()
        org = h.targets.orgs["scratch"]
        original = org.deploy
        observed = []

        def inspect_fence(zip_bytes, *, check_only, purge_on_delete=False):
            pending = h.sf()["pending"]
            observed.append((pending["dispatch_state"], pending["org_id"],
                             pending["alias"], pending["payload_sha256"]))
            return original(zip_bytes, check_only=check_only, purge_on_delete=purge_on_delete)

        org.deploy = inspect_fence
        h.do("confirm", answer="yes")
        self.assertEqual("outcome_unknown", observed[0][0])
        self.assertEqual(org.org_id, observed[0][1])
        self.assertEqual(org.alias, observed[0][2])
        self.assertEqual(64, len(observed[0][3]))
        rejected = self.prepared()
        rejected.targets.forbid.add("scratch")
        rejected.do("confirm", answer="yes")
        self.assertIsNone(rejected.sf()["pending"])
        self.assertEqual(0, sum(not d["check_only"] for d in rejected.targets.orgs["scratch"].deploys))


if __name__ == "__main__":
    unittest.main()
