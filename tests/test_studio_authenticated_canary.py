"""Offline contract tests for the authenticated Studio Lead API canary."""
from __future__ import annotations

import io
import json
import sys
import unittest
import warnings
from collections import Counter
from pathlib import Path
from unittest import mock

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

from tools import authenticated_canary as canary
from app.core import StudioController
from app.workers.synthetic import SyntheticWorker


TARGET = "https://studio-lead-canary-abc-uc.a.run.app"
ORIGIN = "https://www.sfdc24.com"
EMAIL = "operator@example.com"
OTP = "123456"
OPERATOR_TOKEN = "operator-token-private"
SESSION_TOKEN = "session-token-private"
SESSION_ID = "s-private-session"
LEAD_COMMAND_ID = "canary-lead-fixed"
STOP_COMMAND_ID = "canary-stop-fixed"
EXPECTED_LEAD = {
    "org_label": "Test dev org",
    "org_type": "Developer Edition",
    "total": 26,
    "site_total": 4,
    "site_recent": 2,
}
ROOT_ARTIFACT = {"id": "screen-home", "kind": "screen", "label": "Homepage", "children": []}


def event(seq, kind, payload, *, artifact_version=1):
    return {
        "session_id": SESSION_ID,
        "generation": 1,
        "seq": seq,
        "op_id": "op-%d" % seq,
        "type": kind,
        "task_id": "task-1",
        "task_revision": 1,
        "artifact_version": artifact_version,
        "turn_id": "turn-%d" % seq,
        "payload": payload,
    }


SESSION_EVENT = event(
    1, "session.started", {"title": "Authenticated Lead canary"},
    artifact_version=0,
)
SNAPSHOT_EVENT = event(2, "artifact.snapshot", {"root": ROOT_ARTIFACT})
QUESTION_EVENT = event(3, "question.asked", {"question": {"question_id": "q-cta"}})
LEAD_EVENT = event(4, "confirm", {
    "text": (
        "Test dev org (Developer Edition) has 26 leads in total; 4 came from "
        "the sfdc24.com site, 2 of them in the last 7 days. Live count from "
        "the Lead object at 2026-09-24T01:02:03Z."
    ),
    "artifact_ids": ["screen-home"],
})
STOP_EVENT = event(5, "session.ended", {"reason": "You ended this session."})


def sse(*items):
    return "".join(
        "id: %d\nevent: %s\ndata: %s\n\n" % (
            item["seq"], item["type"],
            json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
        for item in items
    )


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, before_chunk=None):
        self.chunks = list(chunks)
        self.before_chunk = before_chunk
        self.yielded = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            if self.before_chunk is not None:
                self.before_chunk(self.yielded)
            yield chunk

    def close(self):
        self.closed = True


class InitialSessionRepository:
    def __init__(self):
        self.saved = None

    def admit(self, daily_cap, session_id):
        return 1

    def create(self, state):
        self.saved = state


class DeterministicIds:
    def __init__(self):
        self.value = 0

    def __call__(self, prefix):
        self.value += 1
        return "%s-%d" % (prefix, self.value)


class Scenario:
    def __init__(self, fail_stage=None):
        self.fail_stage = fail_stage
        self.calls = []
        self.stage_counts = Counter()
        self.utterances = []
        self.stop_commands = []
        self.event_calls = 0
        self.lead_event = json.loads(json.dumps(LEAD_EVENT))
        self.stop_event = json.loads(json.dumps(STOP_EVENT))
        self.baseline_events = json.loads(json.dumps([
            SESSION_EVENT, SNAPSHOT_EVENT, QUESTION_EVENT,
        ]))
        self.replay_sse_events = []
        self.replay_mutation = False
        self.stop_replay_mutation = False
        self.omit_lead_sse = False
        self.omit_terminal_sse = False
        self.start_extra = False
        self.start_expires_in = 600
        self.verify_scope = "operator"
        self.verify_expires_at = 9999999999
        self.session_expires_at = 9999999999
        self.session_events_url = "/v1/session/%s/events" % SESSION_ID
        self.stop_problems = []
        self.refusal_status = 410
        self.refusal_body = {"detail": "session stopped"}

    def _response(self, request, stage, *, body=None, text=None, media=None,
                  status=200):
        self.stage_counts[stage] += 1
        if self.fail_stage == stage:
            return httpx.Response(
                503,
                headers={"Content-Type": "text/plain"},
                stream=httpx.ByteStream(
                    b'private provider detail token=session-secret'
                ),
                request=request,
            )
        if body is not None:
            encoded = json.dumps(
                body, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return httpx.Response(
                status,
                headers={"Content-Type": "application/json"},
                stream=httpx.ByteStream(encoded),
                request=request,
            )
        return httpx.Response(
            status,
            headers={"Content-Type": media},
            stream=httpx.ByteStream((text or "").encode("utf-8")),
            request=request,
        )

    def route(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/v1/auth/start":
            body = {
                "challenge_id": "challenge-private",
                "expires_in": self.start_expires_in,
            }
            if self.start_extra:
                body["unexpected"] = True
            return self._response(request, "auth_start", body=body)
        if path == "/v1/auth/verify":
            return self._response(request, "auth_verify", body={
                "token": OPERATOR_TOKEN, "expires_at": self.verify_expires_at,
                "scope": self.verify_scope,
            })
        if path == "/v1/session":
            return self._response(request, "session", body={
                "session_id": SESSION_ID,
                "generation": 1,
                "artifact_version": 1,
                "expires_at": self.session_expires_at,
                "max_session_seconds": 600,
                "daily_admission_number": 1,
                "token": SESSION_TOKEN,
                "events_url": self.session_events_url,
            })
        if path.endswith("/commands"):
            body = json.loads(request.content)
            if body.get("type") == "utterance":
                self.utterances.append(body)
                stage = "lead_command" if len(self.utterances) == 1 else "replay"
                result = {
                    "command_id": body["command_id"],
                    "session_id": SESSION_ID,
                    "artifact_version": 1,
                    "events": [self.lead_event],
                    "problems": [],
                }
                if stage == "replay" and self.replay_mutation:
                    result["artifact_version"] = 2
                return self._response(request, stage, body=result)
            if body.get("type") == "stop":
                self.stop_commands.append(body)
                stage = "stop" if len(self.stop_commands) == 1 else "stop_replay"
                result = {
                    "command_id": body["command_id"],
                    "session_id": SESSION_ID,
                    "artifact_version": 1,
                    "events": [self.stop_event],
                    "problems": list(self.stop_problems),
                }
                if stage == "stop_replay" and self.stop_replay_mutation:
                    result["artifact_version"] = 2
                return self._response(request, stage, body=result)
            return self._response(
                request, "stopped_refusal", status=self.refusal_status,
                body=self.refusal_body,
            )
        if path.endswith("/events"):
            self.event_calls += 1
            if self.event_calls == 1:
                return self._response(
                    request, "baseline_sse",
                    text=sse(*self.baseline_events),
                    media="text/event-stream",
                )
            if self.event_calls == 2:
                items = [] if self.omit_lead_sse else [self.lead_event]
                return self._response(
                    request, "lead_sse", text=sse(*items), media="text/event-stream",
                )
            if self.event_calls == 3:
                return self._response(
                    request, "replay_sse", text=sse(*self.replay_sse_events),
                    media="text/event-stream",
                )
            items = [] if self.omit_terminal_sse else [self.stop_event]
            return self._response(
                request, "terminal_sse", text=sse(*items), media="text/event-stream",
            )
        raise AssertionError("unexpected route")


def fixed_identifier(prefix):
    values = {
        "canary-client": "canary-client-fixed",
        "canary-session": "canary-session-fixed",
        "canary-lead": LEAD_COMMAND_ID,
        "canary-item": "canary-item-fixed",
        "canary-stop": STOP_COMMAND_ID,
        "canary-stopped": "canary-stopped-fixed",
    }
    return values[prefix]


def run_scenario(scenario, **overrides):
    overrides.setdefault("wall_clock", lambda: 1790211840)
    expected = overrides.pop("expected_lead", EXPECTED_LEAD)
    with httpx.Client(
        transport=httpx.MockTransport(scenario.route),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        return canary.run_authenticated_canary(
            client=client,
            target=TARGET,
            origin=ORIGIN,
            expected_lead=expected,
            email_reader=lambda: EMAIL,
            otp_reader=lambda: OTP,
            identifier=fixed_identifier,
            **overrides,
        )


class ConfigurationTests(unittest.TestCase):
    def test_live_exact_https_canary_target_and_origin_are_required(self):
        self.assertEqual(
            (TARGET, ORIGIN),
            canary._configuration(live=True, target=TARGET, origin=ORIGIN),
        )
        bad_targets = (
            "http://studio-lead-canary-abc-uc.a.run.app",
            TARGET + "/",
            TARGET + "/path",
            TARGET + "?x=1",
            TARGET + "#fragment",
            "https://user@studio-lead-canary-abc-uc.a.run.app",
            "https://studio-lead-canary-abc-uc.a.run.app:443",
            "https://STUDIO-lead-canary-abc-uc.a.run.app",
            "https://localhost",
            "https://127.0.0.1",
        )
        for target in bad_targets:
            with self.subTest(target=target), self.assertRaises(canary.CanaryFailure):
                canary._configuration(live=True, target=target, origin=ORIGIN)
        for origin in ("http://www.sfdc24.com", ORIGIN + "/", ORIGIN + "/path"):
            with self.subTest(origin=origin), self.assertRaises(canary.CanaryFailure):
                canary._configuration(live=True, target=TARGET, origin=origin)
        with self.assertRaises(canary.CanaryFailure):
            canary._configuration(live=False, target=TARGET, origin=ORIGIN)

    def test_public_target_is_never_allowed_and_untagged_run_app_needs_marker(self):
        for target in (
            "https://sfdc24.com",
            "https://www.sfdc24.com",
            "https://studio.sfdc24.com",
            "https://notcanary.sfdc24.com",
        ):
            with self.subTest(target=target), self.assertRaises(canary.CanaryFailure):
                canary._configuration(live=True, target=target, origin=ORIGIN)
            with self.subTest(target=target), self.assertRaises(canary.CanaryFailure):
                canary._configuration(
                    live=True, target=target, origin=ORIGIN,
                    safety_marker=canary.LIVE_SAFETY_MARKER,
                )
        target = "https://studio-abc-uc.a.run.app"
        with self.assertRaises(canary.CanaryFailure):
            canary._configuration(live=True, target=target, origin=ORIGIN)
        self.assertEqual(
            (target, ORIGIN),
                    canary._configuration(
                        live=True, target=target, origin=ORIGIN,
                        safety_marker=canary.LIVE_SAFETY_MARKER,
                    ),
                )
        with self.assertRaises(canary.CanaryFailure):
            canary._configuration(
                live=True, target=target, origin=ORIGIN,
                safety_marker=canary.LIVE_SAFETY_MARKER + "-almost",
            )


class LifecycleTests(unittest.TestCase):
    def test_exact_authenticated_lead_replay_stop_and_terminal_flow(self):
        scenario = Scenario()
        receipt = run_scenario(scenario)
        self.assertEqual({
            "schema": canary.RECEIPT_SCHEMA,
            "status": "passed",
            "evidence_level": "controller_api_canary",
            "generation": 1,
            "baseline_seq": 3,
            "lead_seq": 4,
            "terminal_seq": 5,
            "lead_total": 26,
            "sfdc24_total": 4,
            "sfdc24_last_7_days": 2,
            "observed_at": "2026-09-24T01:02:03Z",
            "operator_expectation_match_passed": True,
            "expectation_source": "operator_supplied_lead_expectation",
            "authentication_verified": True,
            "lead_result_committed": True,
            "lead_sse_exact": True,
            "lead_replay_exact": True,
            "lead_replay_sse_quiet": True,
            "stop_committed": True,
            "stop_replay_exact": True,
            "terminal_sse_exact": True,
            "stopped_refused_410": True,
        }, receipt)
        self.assertEqual(12, len(scenario.calls))
        self.assertEqual(2, len(scenario.utterances))
        self.assertEqual(scenario.utterances[0], scenario.utterances[1])
        self.assertEqual(2, len(scenario.stop_commands))
        self.assertEqual(scenario.stop_commands[0], scenario.stop_commands[1])
        self.assertEqual(canary.FIXED_UTTERANCE, scenario.utterances[0]["transcript"])
        self.assertEqual(Counter({
            "auth_start": 1, "auth_verify": 1, "session": 1,
            "baseline_sse": 1, "lead_command": 1, "lead_sse": 1,
            "replay": 1, "replay_sse": 1, "stop": 1,
            "terminal_sse": 1, "stop_replay": 1, "stopped_refusal": 1,
        }), scenario.stage_counts)
        for request in scenario.calls:
            self.assertEqual(ORIGIN, request.headers["Origin"])
            self.assertEqual("identity", request.headers["Accept-Encoding"])
            self.assertEqual("https", request.url.scheme)
            self.assertEqual("studio-lead-canary-abc-uc.a.run.app", request.url.host)
        self.assertNotIn("Authorization", scenario.calls[0].headers)
        self.assertNotIn("Authorization", scenario.calls[1].headers)
        self.assertEqual("Bearer " + OPERATOR_TOKEN, scenario.calls[2].headers["Authorization"])
        for request in scenario.calls[3:]:
            self.assertEqual("Bearer " + SESSION_TOKEN, request.headers["Authorization"])
        streams = [request for request in scenario.calls if request.url.path.endswith("/events")]
        self.assertEqual(
            [None, "3", "4", "4"],
            [request.headers.get("Last-Event-ID") for request in streams],
        )

    def test_each_remote_stage_fails_once_without_retry_or_follow_on_calls(self):
        stages = (
            "auth_start", "auth_verify", "session", "baseline_sse",
            "lead_command", "lead_sse", "replay", "replay_sse", "stop",
            "terminal_sse", "stop_replay", "stopped_refusal",
        )
        for stage in stages:
            scenario = Scenario(fail_stage=stage)
            with self.subTest(stage=stage), self.assertRaises(canary.CanaryFailure) as caught:
                run_scenario(scenario)
            self.assertEqual(stage, caught.exception.stage)
            self.assertEqual(1, scenario.stage_counts[stage])
            self.assertNotIn("private", str(caught.exception))

    def test_transport_exception_is_sanitized_and_not_retried(self):
        calls = []

        def explode(request):
            calls.append(request)
            raise httpx.ConnectError(
                "secret-email token session-id provider-host", request=request
            )

        with httpx.Client(
            transport=httpx.MockTransport(explode), follow_redirects=False,
            trust_env=False,
        ) as client:
            with self.assertRaises(canary.CanaryFailure) as caught:
                canary.run_authenticated_canary(
                    client=client, target=TARGET, origin=ORIGIN,
                    expected_lead=EXPECTED_LEAD,
                    email_reader=lambda: EMAIL, otp_reader=lambda: OTP,
                    identifier=fixed_identifier, wall_clock=lambda: 1790211840,
                )
        self.assertEqual(1, len(calls))
        self.assertEqual("auth_start", caught.exception.stage)
        self.assertEqual("authenticated Studio canary failed", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_redirect_is_a_refusal_and_is_never_followed(self):
        calls = []

        def redirect(request):
            calls.append(request)
            return httpx.Response(
                302, headers={"Location": "https://evil.example/steal"}, request=request
            )

        with httpx.Client(
            transport=httpx.MockTransport(redirect), follow_redirects=False,
            trust_env=False,
        ) as client:
            with self.assertRaises(canary.CanaryFailure):
                canary.run_authenticated_canary(
                    client=client, target=TARGET, origin=ORIGIN,
                    expected_lead=EXPECTED_LEAD,
                    email_reader=lambda: EMAIL, otp_reader=lambda: OTP,
                    identifier=fixed_identifier, wall_clock=lambda: 1790211840,
                )
        self.assertEqual(1, len(calls))
        self.assertEqual("studio-lead-canary-abc-uc.a.run.app", calls[0].url.host)

    def test_identity_body_limit_stops_stream_early_and_closes_it(self):
        half = canary.MAX_RESPONSE_BYTES // 2
        stream = TrackingStream((b"a" * half, b"b" * (half + 1), b"unread"))
        route_calls = []

        def route(request):
            route_calls.append(request)
            return httpx.Response(200, stream=stream, request=request)

        with httpx.Client(
            transport=httpx.MockTransport(route), follow_redirects=False,
            trust_env=False,
        ) as client:
            run = canary._Run(
                client=client, target=TARGET, origin=ORIGIN, clock=lambda: 0.0,
                identifier=fixed_identifier, whole_run_seconds=30.0,
                request_seconds=7.0,
            )
            with self.assertRaises(canary.CanaryFailure) as caught:
                run.request("GET", "/oversized", "auth_start", headers={})
        self.assertEqual("auth_start", caught.exception.stage)
        self.assertEqual(1, len(route_calls))
        self.assertEqual(2, stream.yielded)
        self.assertTrue(stream.closed)

    def test_length_and_encoding_refusals_close_without_consuming_body(self):
        cases = (
            ({"Content-Length": str(canary.MAX_RESPONSE_BYTES + 1)}, "length"),
            ({"Content-Encoding": "gzip"}, "encoding"),
        )
        for headers, label in cases:
            stream = TrackingStream((b"private", b"unread"))

            def route(request, *, stream=stream, headers=headers):
                return httpx.Response(
                    200, headers=headers, stream=stream, request=request,
                )

            with self.subTest(label=label), httpx.Client(
                transport=httpx.MockTransport(route), follow_redirects=False,
                trust_env=False,
            ) as client:
                run = canary._Run(
                    client=client, target=TARGET, origin=ORIGIN,
                    clock=lambda: 0.0, identifier=fixed_identifier,
                    whole_run_seconds=30.0, request_seconds=7.0,
                )
                with self.assertRaises(canary.CanaryFailure):
                    run.request("GET", "/refused", "auth_start", headers={})
            self.assertEqual(0, stream.yielded)
            self.assertTrue(stream.closed)

    def test_elapsed_budget_stops_slow_drip_at_chunk_boundary_and_closes_it(self):
        now = [0.0]

        def advance(_count):
            now[0] += 0.6

        stream = TrackingStream([b"x"] * 8, before_chunk=advance)
        route_calls = []

        def route(request):
            route_calls.append(request)
            return httpx.Response(200, stream=stream, request=request)

        with httpx.Client(
            transport=httpx.MockTransport(route), follow_redirects=False,
            trust_env=False,
        ) as client:
            run = canary._Run(
                client=client, target=TARGET, origin=ORIGIN,
                clock=lambda: now[0], identifier=fixed_identifier,
                whole_run_seconds=30.0, request_seconds=1.0,
            )
            with self.assertRaises(canary.CanaryFailure) as caught:
                run.request("GET", "/slow", "auth_start", headers={})
        self.assertEqual("auth_start", caught.exception.stage)
        self.assertEqual(1, len(route_calls))
        self.assertEqual(2, stream.yielded)
        self.assertTrue(stream.closed)

    def test_whole_run_and_request_budgets_bound_each_io_timeout(self):
        scenario = Scenario()
        now = [10.0]

        def clock():
            value = now[0]
            now[0] += 0.001
            return value

        run_scenario(
            scenario, clock=clock, whole_run_seconds=30.0, request_seconds=7.0,
        )
        observed = [request.extensions["timeout"] for request in scenario.calls]
        self.assertEqual(12, len(observed))
        for timeout in observed:
            self.assertEqual({"connect", "read", "write", "pool"}, set(timeout))
            self.assertTrue(all(0 < value <= 7.0 for value in timeout.values()))
        self.assertLessEqual(
            max(observed[-1].values()), max(observed[0].values())
        )

    def test_expiry_while_waiting_for_otp_makes_no_verification_call(self):
        scenario = Scenario()
        now = [0.0]

        def otp():
            now[0] = 31.0
            return OTP

        with httpx.Client(
            transport=httpx.MockTransport(scenario.route), follow_redirects=False,
            trust_env=False,
        ) as client:
            with self.assertRaises(canary.CanaryFailure) as caught:
                canary.run_authenticated_canary(
                    client=client, target=TARGET, origin=ORIGIN,
                    expected_lead=EXPECTED_LEAD,
                    email_reader=lambda: EMAIL, otp_reader=otp,
                    identifier=fixed_identifier, clock=lambda: now[0],
                    wall_clock=lambda: 1790211840, whole_run_seconds=30.0,
                )
        self.assertEqual("deadline", caught.exception.stage)
        self.assertEqual(Counter({"auth_start": 1}), scenario.stage_counts)

    def test_injected_client_must_disable_redirects(self):
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            follow_redirects=True,
        ) as client:
            with self.assertRaises(canary.CanaryFailure) as caught:
                canary.run_authenticated_canary(
                    client=client, target=TARGET, origin=ORIGIN,
                    expected_lead=EXPECTED_LEAD,
                    email_reader=lambda: EMAIL, otp_reader=lambda: OTP,
                    identifier=fixed_identifier, wall_clock=lambda: 1790211840,
                )
        self.assertEqual("configuration", caught.exception.stage)


class ExactEvidenceTests(unittest.TestCase):
    def test_real_controller_version_zero_start_and_version_one_snapshot_are_accepted(self):
        repository = InitialSessionRepository()
        controller = StudioController(
            repository,
            SyntheticWorker(),
            clock=lambda: 1000,
            id_factory=DeterministicIds(),
        )
        state, admission = controller.create_session("Authenticated Lead canary")
        self.assertEqual(1, admission)
        self.assertEqual(
            [("session.started", 0), ("artifact.snapshot", 1)],
            [
                (item["type"], item["artifact_version"])
                for item in state["events"][:2]
            ],
        )
        request = httpx.Request("GET", TARGET + "/events")
        response = httpx.Response(
            200,
            text=sse(*state["events"]),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )
        parsed = canary._parse_sse(
            response, state["session_id"], "baseline_sse"
        )
        root_id, generation, version, cursor = canary._baseline(parsed, state)
        self.assertEqual(state["artifact"]["id"], root_id)
        self.assertEqual(state["generation"], generation)
        self.assertEqual(1, version)
        self.assertEqual(state["last_seq"], cursor)

    def test_version_zero_is_rejected_for_non_start_events_and_start_must_be_zero(self):
        invalid = (
            event(1, "session.started", {"title": "x"}, artifact_version=1),
            event(1, "artifact.snapshot", {"root": ROOT_ARTIFACT}, artifact_version=0),
            event(1, "confirm", {"text": "x", "artifact_ids": []}, artifact_version=0),
            event(1, "session.ended", {"reason": "x"}, artifact_version=0),
        )
        for item in invalid:
            with self.subTest(kind=item["type"]), self.assertRaises(
                    canary.CanaryFailure):
                canary._event(item, SESSION_ID, "baseline_sse")

    def test_closed_remote_shapes_and_live_lead_answer_are_required(self):
        cases = []
        extra = Scenario()
        extra.start_extra = True
        cases.append((extra, "auth_start"))
        ttl = Scenario()
        ttl.start_expires_in = 601
        cases.append((ttl, "auth_start"))
        scope = Scenario()
        scope.verify_scope = "session"
        cases.append((scope, "auth_verify"))
        expired_operator = Scenario()
        expired_operator.verify_expires_at = 1790211840
        cases.append((expired_operator, "auth_verify"))
        events_url = Scenario()
        events_url.session_events_url = "/wrong"
        cases.append((events_url, "session"))
        expired_session = Scenario()
        expired_session.session_expires_at = 1790211840
        cases.append((expired_session, "session"))
        no_snapshot = Scenario()
        no_snapshot.baseline_events = [SESSION_EVENT, QUESTION_EVENT]
        cases.append((no_snapshot, "baseline_sse"))
        two_snapshots = Scenario()
        two_snapshots.baseline_events.append(json.loads(json.dumps(SNAPSHOT_EVENT)))
        two_snapshots.baseline_events[-1]["seq"] = 3
        cases.append((two_snapshots, "baseline_sse"))
        mixed_generation = Scenario()
        mixed_generation.baseline_events[1]["generation"] = 2
        cases.append((mixed_generation, "baseline_sse"))
        unavailable = Scenario()
        unavailable.lead_event["payload"]["text"] = (
            "The live Lead count is unavailable right now. No estimate was substituted."
        )
        cases.append((unavailable, "lead_command"))
        missing = Scenario()
        missing.omit_lead_sse = True
        cases.append((missing, "lead_sse"))
        changed = Scenario()
        changed.replay_mutation = True
        cases.append((changed, "replay"))
        replay_event = Scenario()
        replay_event.replay_sse_events = [LEAD_EVENT]
        cases.append((replay_event, "replay_sse"))
        stop_problem = Scenario()
        stop_problem.stop_problems = ["private provider pending"]
        cases.append((stop_problem, "stop"))
        no_terminal = Scenario()
        no_terminal.omit_terminal_sse = True
        cases.append((no_terminal, "terminal_sse"))
        stop_changed = Scenario()
        stop_changed.stop_replay_mutation = True
        cases.append((stop_changed, "stop_replay"))
        wrong_refusal = Scenario()
        wrong_refusal.refusal_body = {"detail": "private different refusal"}
        cases.append((wrong_refusal, "stopped_refusal"))
        for scenario, stage in cases:
            with self.subTest(stage=stage), self.assertRaises(canary.CanaryFailure) as caught:
                run_scenario(scenario)
            self.assertEqual(stage, caught.exception.stage)

    def test_operator_expectation_counts_inequalities_and_fresh_utc_are_required(self):
        wrong_expected = dict(EXPECTED_LEAD, total=27)
        with self.assertRaises(canary.CanaryFailure) as caught:
            run_scenario(Scenario(), expected_lead=wrong_expected)
        self.assertEqual("lead_command", caught.exception.stage)

        invalid_expected = dict(EXPECTED_LEAD, site_recent=5)
        scenario = Scenario()
        with self.assertRaises(canary.CanaryFailure) as caught:
            run_scenario(scenario, expected_lead=invalid_expected)
        self.assertEqual("expected_lead", caught.exception.stage)
        self.assertEqual([], scenario.calls)

        for wall_now in (1790212204, 1790211700):
            with self.subTest(wall_now=wall_now), self.assertRaises(
                    canary.CanaryFailure) as caught:
                run_scenario(Scenario(), wall_clock=lambda value=wall_now: value)
            self.assertEqual("lead_command", caught.exception.stage)

    def test_lead_event_must_name_the_baseline_root_and_generation(self):
        wrong_root = Scenario()
        wrong_root.lead_event["payload"]["artifact_ids"] = ["other-screen"]
        with self.assertRaises(canary.CanaryFailure) as caught:
            run_scenario(wrong_root)
        self.assertEqual("lead_command", caught.exception.stage)

        wrong_generation = Scenario()
        wrong_generation.lead_event["generation"] = 2
        with self.assertRaises(canary.CanaryFailure) as caught:
            run_scenario(wrong_generation)
        self.assertEqual("lead_command", caught.exception.stage)

    def test_sse_parser_rejects_malformed_duplicate_or_unbound_envelopes(self):
        bad_streams = (
            "event: confirm\ndata: {}\n\n",
            "id: 4\nid: 4\nevent: confirm\ndata: {}\n\n",
            "id: 4\nevent: progress\ndata: %s\n\n" % json.dumps(LEAD_EVENT),
            "id: 4\nevent: confirm\ndata: {\"seq\":4,\"seq\":4}\n\n",
        )
        request = httpx.Request("GET", TARGET + "/events")
        for value in bad_streams:
            response = httpx.Response(
                200, text=value, headers={"Content-Type": "text/event-stream"},
                request=request,
            )
            with self.subTest(value=value), self.assertRaises(canary.CanaryFailure):
                canary._parse_sse(response, SESSION_ID, "lead_sse")

    def test_email_and_otp_validation_happen_without_echo_or_network(self):
        for email, code in (("Operator@example.com", OTP), (" operator@example.com", OTP),
                            (EMAIL, "12345"), (EMAIL, "secret")):
            scenario = Scenario()
            with self.subTest(email=email, code=code), self.assertRaises(canary.CanaryFailure):
                run_scenario(scenario) if (email, code) == (EMAIL, OTP) else self._bad_input(
                    scenario, email, code
                )
            if email != EMAIL:
                self.assertEqual([], scenario.calls)

    @staticmethod
    def _bad_input(scenario, email, code):
        with httpx.Client(
            transport=httpx.MockTransport(scenario.route), follow_redirects=False,
            trust_env=False,
        ) as client:
            return canary.run_authenticated_canary(
                client=client, target=TARGET, origin=ORIGIN,
                expected_lead=EXPECTED_LEAD,
                email_reader=lambda: email, otp_reader=lambda: code,
                identifier=fixed_identifier, wall_clock=lambda: 1790211840,
            )


class CliAndCiTests(unittest.TestCase):
    def test_cli_uses_non_echoing_readers_and_emits_only_closed_redacted_receipt(self):
        scenario = Scenario()
        captured = {}
        transport_policies = []

        def transport_factory(**kwargs):
            transport_policies.append(kwargs)
            return httpx.HTTPTransport(**kwargs)

        def factory(**kwargs):
            captured.update(kwargs)
            supplied = kwargs.pop("transport")
            supplied.close()
            kwargs["transport"] = httpx.MockTransport(scenario.route)
            return httpx.Client(**kwargs)

        prompts = []
        values = iter((EMAIL, json.dumps(EXPECTED_LEAD), OTP))

        def secret_reader(prompt):
            prompts.append(prompt)
            return next(values)

        output = io.StringIO()
        code = canary.main(
            ["--live", "--target", TARGET, "--origin", ORIGIN],
            client_factory=factory, secret_reader=secret_reader, output=output,
            wall_clock=lambda: 1790211840,
            transport_factory=transport_factory,
        )
        self.assertEqual(0, code, output.getvalue())
        self.assertEqual([
            "Operator email: ",
            "Expected Lead JSON (org_label, org_type, total, site_total, site_recent): ",
            "Email verification code: ",
        ], prompts)
        receipt = json.loads(output.getvalue())
        self.assertEqual("passed", receipt["status"])
        self.assertEqual("controller_api_canary", receipt["evidence_level"])
        self.assertEqual(26, receipt["lead_total"])
        self.assertTrue(receipt["operator_expectation_match_passed"])
        rendered = output.getvalue()
        for secret in (
            EMAIL, OTP, OPERATOR_TOKEN, SESSION_TOKEN, SESSION_ID,
            LEAD_COMMAND_ID, STOP_COMMAND_ID, LEAD_EVENT["payload"]["text"],
        ):
            self.assertNotIn(secret, rendered)
        self.assertIs(captured["follow_redirects"], False)
        self.assertIs(captured["trust_env"], False)
        self.assertIsInstance(captured["transport"], httpx.HTTPTransport)
        self.assertEqual([{"retries": 0, "trust_env": False}], transport_policies)

    def test_getpass_warning_at_every_prompt_fails_closed_as_operator_input(self):
        expected_json = json.dumps(EXPECTED_LEAD)
        for fail_at in (1, 2, 3):
            scenario = Scenario()
            prompt_count = [0]

            def unsafe_reader(_prompt):
                prompt_count[0] += 1
                if prompt_count[0] == fail_at:
                    warnings.warn(
                        "private terminal fallback would echo",
                        canary.getpass.GetPassWarning,
                    )
                return (EMAIL, expected_json, OTP)[prompt_count[0] - 1]

            def factory(**kwargs):
                supplied = kwargs.pop("transport")
                supplied.close()
                kwargs["transport"] = httpx.MockTransport(scenario.route)
                return httpx.Client(**kwargs)

            output = io.StringIO()
            with self.subTest(fail_at=fail_at), mock.patch.object(
                    canary.getpass, "getpass", unsafe_reader):
                code = canary.main(
                    ["--live", "--target", TARGET, "--origin", ORIGIN],
                    client_factory=factory, output=output,
                )
            self.assertEqual(1, code)
            self.assertEqual({
                "schema": canary.RECEIPT_SCHEMA,
                "status": "failed",
                "failed_check": "operator_input",
            }, json.loads(output.getvalue()))
            self.assertNotIn("private terminal", output.getvalue())
            expected_network_calls = 1 if fail_at == 3 else 0
            self.assertEqual(expected_network_calls, len(scenario.calls))

    def test_successfully_read_malformed_expectation_is_expected_lead_failure(self):
        calls = []
        values = iter((EMAIL, "{}"))
        output = io.StringIO()
        code = canary.main(
            ["--live", "--target", TARGET, "--origin", ORIGIN],
            client_factory=lambda **kwargs: calls.append(kwargs),
            secret_reader=lambda _prompt: next(values),
            output=output,
        )
        self.assertEqual(1, code)
        self.assertEqual([], calls)
        self.assertEqual({
            "schema": canary.RECEIPT_SCHEMA,
            "status": "failed",
            "failed_check": "expected_lead",
        }, json.loads(output.getvalue()))

    def test_cli_without_live_never_prompts_or_constructs_a_client(self):
        calls = []
        output = io.StringIO()
        code = canary.main(
            ["--target", TARGET, "--origin", ORIGIN],
            client_factory=lambda **kwargs: calls.append(kwargs),
            secret_reader=lambda prompt: calls.append(prompt), output=output,
        )
        self.assertEqual(1, code)
        self.assertEqual([], calls)
        self.assertEqual({
            "schema": canary.RECEIPT_SCHEMA,
            "status": "failed",
            "failed_check": "configuration",
        }, json.loads(output.getvalue()))

    def test_cli_failure_output_cannot_echo_remote_or_operator_data(self):
        def factory(**kwargs):
            supplied = kwargs.pop("transport")
            supplied.close()
            kwargs["transport"] = httpx.MockTransport(Scenario("auth_start").route)
            return httpx.Client(**kwargs)

        output = io.StringIO()
        values = iter((EMAIL, json.dumps(EXPECTED_LEAD), OTP))
        code = canary.main(
            ["--live", "--target", TARGET, "--origin", ORIGIN],
            client_factory=factory, secret_reader=lambda prompt: next(values),
            output=output, wall_clock=lambda: 1790211840,
        )
        self.assertEqual(1, code)
        self.assertEqual({
            "schema": canary.RECEIPT_SCHEMA,
            "status": "failed",
            "failed_check": "auth_start",
        }, json.loads(output.getvalue()))
        for secret in (EMAIL, OTP, OPERATOR_TOKEN, SESSION_TOKEN, "provider detail"):
            self.assertNotIn(secret, output.getvalue())

    def test_source_is_disconnected_and_has_no_secret_or_browser_persistence_surface(self):
        source_path = ROOT / "cloud" / "studio-controller" / "tools" / "authenticated_canary.py"
        source = source_path.read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in (
            "os.environ", "os.getenv", "subprocess", "selenium", "playwright",
            "browser_state", "storage_state", "cookiejar", "gmail", "input(",
            "--email", "--otp", "--code", "--token", "--session-id",
            "--command-id",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("getpass.getpass", source)
        for runtime in ("main.py", "core.py", "settings.py"):
            text = (ROOT / "cloud" / "studio-controller" / "app" / runtime).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("authenticated_canary", text)

    def test_ci_requires_and_network_isolates_the_canary_suite_without_false_green(self):
        workflow = (ROOT / ".github" / "workflows" / "python-suites.yml").read_text(
            encoding="utf-8"
        )
        test_path = "tests/test_studio_authenticated_canary.py"
        source_path = "cloud/studio-controller/tools/authenticated_canary.py"
        self.assertGreaterEqual(workflow.count(test_path), 5)
        self.assertGreaterEqual(workflow.count(source_path), 3)
        self.assertRegex(
            workflow,
            r"test -f cloud/studio-controller/tools/authenticated_canary\.py[\s\S]+"
            r"test -f tests/test_studio_authenticated_canary\.py[\s\S]+"
            r"sudo unshare -n -- \"\$python_bin\" -m unittest "
            r"tests\.test_studio_authenticated_canary",
        )
        self.assertIn("[ ! -f tests/test_studio_authenticated_canary.py ]", workflow)


if __name__ == "__main__":
    unittest.main()
