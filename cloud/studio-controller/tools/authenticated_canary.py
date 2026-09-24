"""Explicit, redacted API canary for the authenticated Studio Lead path.

This is controller acceptance, not browser or served-page acceptance.  It is
deliberately disconnected from the Studio application and has no credential,
deployment, settings, route, voice, Salesforce-write, or environment lookup.
Live execution requires ``--live`` and three non-echoing console inputs: email,
an operator-supplied Lead expectation, and the emailed OTP.
"""
from __future__ import annotations

import argparse
import getpass
import json
import math
import re
import secrets
import sys
import time
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx


RECEIPT_SCHEMA = "studio-authenticated-lead-canary.v1"
FIXED_UTTERANCE = "How many leads do we have?"
WHOLE_RUN_SECONDS = 180.0
REQUEST_SECONDS = 15.0
MAX_RESPONSE_BYTES = 512 * 1024
# A one-byte raw iterator exposes each slow-trickle byte to the elapsed-budget
# check instead of letting httpx's chunker accumulate several source reads.
# The total accumulated response remains capped at 512 KiB.
STREAM_CHUNK_BYTES = 1
EXPECTED_ORIGIN = "https://www.sfdc24.com"

_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
# The one host this runner may send a sign-in to: the lead-canary TAG of our
# studio controller. Cloud Run writes a tag url as TAG---SERVICE-HASH-REGION,
# and HASH is per project. Matching "lead-canary" anywhere in a label also
# admitted an untagged service merely NAMED lead-canary-..., another service's
# tag, and the same names in any other project, which would then receive the
# operator's email and OTP (Gemini's review of #231). So the whole host is fixed.
LEAD_CANARY_HOST = "lead-canary---sfdc24-studio-controller-yzet4vuplq-uc.a.run.app"
_OPAQUE = re.compile(r"[A-Za-z0-9._:-]{1,512}\Z")
_OTP = re.compile(r"[0-9]{6}\Z")
_LEAD_TEXT = re.compile(
    r"^(?P<org_label>[^()\r\n]{1,120}) \((?P<org_type>[^()\r\n]{1,80})\) "
    r"has (?P<total>[0-9]+) leads in total; (?P<site_total>[0-9]+) came "
    r"from the sfdc24\.com site, (?P<site_recent>[0-9]+) of them in the "
    r"last 7 days\. Live count from the Lead object at "
    r"(?P<observed_at>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}Z)\.$"
)
_EVENT_KEYS = frozenset({
    "session_id", "generation", "seq", "op_id", "type", "task_id",
    "task_revision", "artifact_version", "turn_id", "payload",
})
_FAILURE_STAGES = frozenset({
    "configuration", "operator_input", "expected_lead", "auth_start",
    "auth_verify", "session", "baseline_sse", "lead_command", "lead_sse",
    "replay", "replay_sse", "stop", "terminal_sse", "stop_replay",
    "stopped_refusal", "deadline", "internal",
})


class CanaryFailure(RuntimeError):
    """A closed failure whose text cannot contain remote or secret data."""

    def __init__(self, stage: str) -> None:
        self.stage = stage if stage in _FAILURE_STAGES else "internal"
        super().__init__("authenticated Studio canary failed")


def _canonical_https_origin(value: str) -> tuple[str, str]:
    if type(value) is not str:
        raise CanaryFailure("configuration")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise CanaryFailure("configuration") from None
    host = parsed.hostname or ""
    canonical = "https://" + host
    if (
        value != canonical
        or parsed.scheme != "https"
        or not _HOST.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise CanaryFailure("configuration")
    return canonical, host


def _configuration(*, live: bool, target: str, origin: str) -> tuple[str, str]:
    if live is not True:
        raise CanaryFailure("configuration")
    target, host = _canonical_https_origin(target)
    origin, _ = _canonical_https_origin(origin)
    if origin != EXPECTED_ORIGIN or not host.endswith(".a.run.app"):
        raise CanaryFailure("configuration")
    # Only the lead-canary TAG url. The untagged *.a.run.app url is the
    # service's primary address and serves whatever revision holds public
    # traffic (claude-code-cli review of #229: an override marker for untagged
    # hosts accepted exactly that production url), so there is no override.
    if host != LEAD_CANARY_HOST:
        raise CanaryFailure("configuration")
    return target, origin


def _closed(value, keys: frozenset[str], stage: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise CanaryFailure(stage)
    return value


def _pairs_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_bytes(raw: bytes, stage: str):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CanaryFailure(stage)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_object)
    except Exception:
        raise CanaryFailure(stage) from None


def _identifier(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(16)


def _wall_time(clock: Callable[[], float], stage: str) -> float:
    try:
        value = float(clock())
    except Exception:
        raise CanaryFailure(stage) from None
    if not math.isfinite(value) or value < 0:
        raise CanaryFailure(stage)
    return value


class _Run:
    def __init__(self, *, client: httpx.Client, target: str, origin: str,
                 clock: Callable[[], float], identifier: Callable[[str], str],
                 whole_run_seconds: float, request_seconds: float) -> None:
        if getattr(client, "follow_redirects", None) is not False:
            raise CanaryFailure("configuration")
        if (
            type(whole_run_seconds) not in (int, float)
            or type(request_seconds) not in (int, float)
            or not 0 < float(whole_run_seconds) <= WHOLE_RUN_SECONDS
            or not 0 < float(request_seconds) <= REQUEST_SECONDS
        ):
            raise CanaryFailure("configuration")
        self.client = client
        self.target = target
        self.origin = origin
        self.clock = clock
        self.identifier = identifier
        try:
            self.deadline = float(clock()) + float(whole_run_seconds)
        except Exception:
            raise CanaryFailure("configuration") from None
        self.request_seconds = float(request_seconds)

    def _remaining(self) -> float:
        try:
            remaining = self.deadline - float(self.clock())
        except Exception:
            raise CanaryFailure("deadline") from None
        if remaining <= 0:
            raise CanaryFailure("deadline")
        return remaining

    def request(self, method: str, path: str, stage: str, *, headers: dict,
                body: dict | None = None, expected_status: int = 200) -> httpx.Response:
        remaining = self._remaining()
        started_at = self.deadline - remaining
        request_deadline = min(
            self.deadline, started_at + self.request_seconds,
        )
        expected_url = self.target + path
        try:
            with self.client.stream(
                method, expected_url,
                headers={
                    **headers,
                    "Origin": self.origin,
                    "Accept-Encoding": "identity",
                },
                json=body,
                timeout=request_deadline - started_at,
            ) as response:
                # Synchronous HTTPX timeouts bound individual blocking I/O
                # operations rather than pre-empting the active header phase
                # at this elapsed-time checkpoint. Check immediately after
                # final headers, between raw bytes, and after EOF; documentation
                # states that cooperative checks do not guarantee total runtime.
                self._request_remaining(request_deadline, stage)
                try:
                    actual_url = str(response.request.url)
                except Exception:
                    raise CanaryFailure(stage) from None
                if (
                    response.history
                    or response.status_code != expected_status
                    or actual_url != expected_url
                ):
                    raise CanaryFailure(stage)
                content_encoding = response.headers.get("content-encoding")
                if (
                    content_encoding is not None
                    and content_encoding.lower().strip() != "identity"
                ):
                    raise CanaryFailure(stage)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError):
                        raise CanaryFailure(stage) from None
                    if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                        raise CanaryFailure(stage)
                content = bytearray()
                # Compressed responses are refused above, so bounded raw chunks
                # are also bounded decoded chunks for this closed canary.
                for chunk in response.iter_raw(chunk_size=STREAM_CHUNK_BYTES):
                    self._request_remaining(request_deadline, stage)
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise CanaryFailure(stage)
                    content.extend(chunk)
                self._request_remaining(request_deadline, stage)
                buffered = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=response.request,
                )
        except CanaryFailure:
            raise
        except Exception:
            raise CanaryFailure(stage) from None
        return buffered

    def _request_remaining(self, deadline: float, stage: str) -> float:
        try:
            remaining = min(self.deadline, deadline) - float(self.clock())
        except Exception:
            raise CanaryFailure(stage) from None
        if remaining <= 0:
            raise CanaryFailure(stage)
        return remaining

    def json_request(self, method: str, path: str, stage: str, *, headers: dict,
                     body: dict | None = None, expected_status: int = 200):
        response = self.request(
            method, path, stage, headers=headers, body=body,
            expected_status=expected_status,
        )
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise CanaryFailure(stage)
        return _json_bytes(response.content, stage)


def _opaque(value, stage: str, *, limit: int = 512) -> str:
    if type(value) is not str or len(value) > limit or not _OPAQUE.fullmatch(value):
        raise CanaryFailure(stage)
    return value


def _event(value, session_id: str, stage: str) -> dict:
    _closed(value, _EVENT_KEYS, stage)
    event_type = value["type"]
    artifact_version = value["artifact_version"]
    if (
        value["session_id"] != session_id
        or type(value["seq"]) is not int
        or value["seq"] < 1
        or type(value["generation"]) is not int
        or value["generation"] < 1
        or type(artifact_version) is not int
        or artifact_version < 0
        or type(value["payload"]) is not dict
    ):
        raise CanaryFailure(stage)
    for key in ("op_id", "type", "task_id", "turn_id"):
        _opaque(value[key], stage)
    # The controller emits the initial session.started event before creating
    # artifact version 1. No other accepted event may carry version zero, and
    # session.started may not claim a later artifact version.
    if (artifact_version == 0) != (event_type == "session.started"):
        raise CanaryFailure(stage)
    return value


def _command_result(value, *, command_id: str, session_id: str,
                    stage: str) -> dict:
    _closed(value, frozenset({
        "command_id", "session_id", "artifact_version", "events", "problems",
    }), stage)
    if (
        value["command_id"] != command_id
        or value["session_id"] != session_id
        or type(value["artifact_version"]) is not int
        or value["artifact_version"] < 1
        or type(value["events"]) is not list
        or type(value["problems"]) is not list
        or value["problems"] != []
    ):
        raise CanaryFailure(stage)
    for item in value["events"]:
        _event(item, session_id, stage)
    return value


def _expected_lead(value) -> dict:
    keys = frozenset({"org_label", "org_type", "total", "site_total", "site_recent"})
    _closed(value, keys, "expected_lead")
    if (
        type(value["org_label"]) is not str
        or not 1 <= len(value["org_label"]) <= 120
        or value["org_label"] != value["org_label"].strip()
        or any(char in value["org_label"] for char in "\r\n()")
        or type(value["org_type"]) is not str
        or not 1 <= len(value["org_type"]) <= 80
        or value["org_type"] != value["org_type"].strip()
        or any(char in value["org_type"] for char in "\r\n()")
    ):
        raise CanaryFailure("expected_lead")
    counts = (value["total"], value["site_total"], value["site_recent"])
    if any(type(item) is not int or item < 0 for item in counts):
        raise CanaryFailure("expected_lead")
    if not value["site_recent"] <= value["site_total"] <= value["total"]:
        raise CanaryFailure("expected_lead")
    return dict(value)


def _parse_lead_text(value, expected: dict, wall_now: float) -> dict:
    if type(value) is not str:
        raise CanaryFailure("lead_command")
    match = _LEAD_TEXT.fullmatch(value)
    if match is None:
        raise CanaryFailure("lead_command")
    try:
        observed = datetime.strptime(
            match["observed_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        raise CanaryFailure("lead_command") from None
    actual = {
        "org_label": match["org_label"],
        "org_type": match["org_type"],
        "total": int(match["total"]),
        "site_total": int(match["site_total"]),
        "site_recent": int(match["site_recent"]),
    }
    if (
        actual != expected
        or not actual["site_recent"] <= actual["site_total"] <= actual["total"]
        or not 0 <= wall_now - observed.timestamp() <= 300
    ):
        raise CanaryFailure("lead_command")
    return {
        "lead_total": actual["total"],
        "sfdc24_total": actual["site_total"],
        "sfdc24_last_7_days": actual["site_recent"],
        "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _lead_event(result: dict, session_id: str, expected: dict,
                root_id: str, wall_now: float) -> tuple[dict, dict]:
    matches = [item for item in result["events"] if item["type"] == "confirm"]
    if len(result["events"]) != 1 or len(matches) != 1:
        raise CanaryFailure("lead_command")
    event = matches[0]
    payload = event["payload"]
    if (
        set(payload) != {"text", "artifact_ids"}
        or payload["artifact_ids"] != [root_id]
        or event["session_id"] != session_id
    ):
        raise CanaryFailure("lead_command")
    return event, _parse_lead_text(payload["text"], expected, wall_now)


def _parse_sse(response: httpx.Response, session_id: str, stage: str) -> list[dict]:
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "text/event-stream":
        raise CanaryFailure(stage)
    try:
        text = response.content.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise CanaryFailure(stage) from None
    events = []
    for block in text.split("\n\n"):
        if not block.strip() or all(line.startswith(":") for line in block.splitlines()):
            continue
        fields = {}
        for line in block.splitlines():
            if not line or line.startswith(":") or ": " not in line:
                raise CanaryFailure(stage)
            name, value = line.split(": ", 1)
            if name not in {"id", "event", "data"} or name in fields:
                raise CanaryFailure(stage)
            fields[name] = value
        if set(fields) != {"id", "event", "data"}:
            raise CanaryFailure(stage)
        item = _json_bytes(fields["data"].encode("utf-8"), stage)
        _event(item, session_id, stage)
        if fields["id"] != str(item["seq"]) or fields["event"] != item["type"]:
            raise CanaryFailure(stage)
        events.append(item)
    return events


def _ordered_generation(events: list[dict], generation: int, stage: str) -> None:
    if (
        any(item["generation"] != generation for item in events)
        or [item["seq"] for item in events] != sorted({item["seq"] for item in events})
    ):
        raise CanaryFailure(stage)


def _baseline(events: list[dict], created: dict) -> tuple[str, int, int, int]:
    _ordered_generation(events, created["generation"], "baseline_sse")
    snapshots = [item for item in events if item["type"] == "artifact.snapshot"]
    if len(snapshots) != 1 or not events:
        raise CanaryFailure("baseline_sse")
    snapshot = snapshots[0]
    root = snapshot["payload"].get("root") if set(snapshot["payload"]) == {"root"} else None
    root_id = root.get("id") if type(root) is dict else None
    if (
        type(root_id) is not str
        or not root_id
        or snapshot["artifact_version"] != created["artifact_version"]
    ):
        raise CanaryFailure("baseline_sse")
    return (
        root_id, created["generation"], snapshot["artifact_version"],
        max(item["seq"] for item in events),
    )


def run_authenticated_canary(
    *,
    client: httpx.Client,
    target: str,
    origin: str,
    expected_lead: dict,
    email_reader: Callable[[], str],
    otp_reader: Callable[[], str],
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    identifier: Callable[[str], str] = _identifier,
    whole_run_seconds: float = WHOLE_RUN_SECONDS,
    request_seconds: float = REQUEST_SECONDS,
) -> dict:
    """Exercise one authenticated, read-only Lead aggregate session.

    The second command POST is the required stable-receipt replay, not a retry.
    No request is automatically retried.
    """
    run = _Run(
        client=client, target=target, origin=origin, clock=clock,
        identifier=identifier, whole_run_seconds=whole_run_seconds,
        request_seconds=request_seconds,
    )
    expected = _expected_lead(expected_lead)
    try:
        email = email_reader()
    except Exception:
        raise CanaryFailure("operator_input") from None
    run._remaining()
    if (
        type(email) is not str
        or email != email.strip().lower()
        or not 3 <= len(email) <= 320
        or email.count("@") != 1
    ):
        raise CanaryFailure("operator_input")

    client_key = identifier("canary-client")
    creation_id = identifier("canary-session")
    lead_command_id = identifier("canary-lead")
    item_id = identifier("canary-item")
    stop_command_id = identifier("canary-stop")
    stopped_probe_id = identifier("canary-stopped")
    for value in (
        client_key, creation_id, lead_command_id, item_id, stop_command_id,
        stopped_probe_id,
    ):
        _opaque(value, "internal", limit=80)

    started = run.json_request(
        "POST", "/v1/auth/start", "auth_start", headers={},
        body={"email": email, "client_key": client_key},
    )
    _closed(started, frozenset({"challenge_id", "expires_in"}), "auth_start")
    challenge_id = _opaque(started["challenge_id"], "auth_start")
    if type(started["expires_in"]) is not int or started["expires_in"] != 600:
        raise CanaryFailure("auth_start")

    try:
        code = otp_reader()
    except Exception:
        raise CanaryFailure("operator_input") from None
    run._remaining()
    if type(code) is not str or not _OTP.fullmatch(code):
        raise CanaryFailure("operator_input")

    verified = run.json_request(
        "POST", "/v1/auth/verify", "auth_verify", headers={},
        body={
            "challenge_id": challenge_id, "email": email, "code": code,
            "client_key": client_key,
        },
    )
    _closed(verified, frozenset({"token", "expires_at", "scope"}), "auth_verify")
    operator_token = _opaque(verified["token"], "auth_verify", limit=8192)
    if (
        verified["scope"] != "operator"
        or type(verified["expires_at"]) is not int
        or verified["expires_at"] <= _wall_time(wall_clock, "auth_verify")
    ):
        raise CanaryFailure("auth_verify")

    created = run.json_request(
        "POST", "/v1/session", "session",
        headers={"Authorization": "Bearer " + operator_token},
        body={"title": "Authenticated Lead canary", "creation_id": creation_id},
    )
    _closed(created, frozenset({
        "session_id", "generation", "artifact_version", "expires_at",
        "max_session_seconds", "daily_admission_number", "token", "events_url",
    }), "session")
    session_id = _opaque(created["session_id"], "session")
    session_token = _opaque(created["token"], "session", limit=8192)
    events_url = "/v1/session/%s/events" % session_id
    if (
        created["events_url"] != events_url
        or type(created["artifact_version"]) is not int
        or created["artifact_version"] < 1
        or type(created["generation"]) is not int
        or created["generation"] < 1
        or type(created["expires_at"]) is not int
        or created["expires_at"] <= _wall_time(wall_clock, "session")
        or type(created["max_session_seconds"]) is not int
        or created["max_session_seconds"] <= 0
        or type(created["daily_admission_number"]) is not int
        or created["daily_admission_number"] <= 0
    ):
        raise CanaryFailure("session")
    session_headers = {"Authorization": "Bearer " + session_token}
    command_path = "/v1/session/%s/commands" % session_id

    baseline_stream = run.request(
        "GET", events_url + "?once=true", "baseline_sse",
        headers=session_headers,
    )
    baseline_events = _parse_sse(baseline_stream, session_id, "baseline_sse")
    root_id, generation, artifact_version, baseline_cursor = _baseline(
        baseline_events, created
    )

    lead_command = {
        "command_id": lead_command_id,
        "session_id": session_id,
        "type": "utterance",
        "expected_version": artifact_version,
        "item_id": item_id,
        "transcript": FIXED_UTTERANCE,
    }
    first = run.json_request(
        "POST", command_path, "lead_command", headers=session_headers,
        body=lead_command,
    )
    _command_result(
        first, command_id=lead_command_id, session_id=session_id,
        stage="lead_command",
    )
    if first["artifact_version"] != artifact_version:
        raise CanaryFailure("lead_command")
    lead_event, lead_observation = _lead_event(
        first, session_id, expected, root_id,
        _wall_time(wall_clock, "lead_command"),
    )
    if (
        lead_event["generation"] != generation
        or lead_event["artifact_version"] != artifact_version
        or lead_event["seq"] <= baseline_cursor
    ):
        raise CanaryFailure("lead_command")

    lead_stream = run.request(
        "GET", events_url + "?once=true", "lead_sse",
        headers={**session_headers, "Last-Event-ID": str(baseline_cursor)},
    )
    lead_events = _parse_sse(lead_stream, session_id, "lead_sse")
    _ordered_generation(lead_events, generation, "lead_sse")
    if lead_events != [lead_event]:
        raise CanaryFailure("lead_sse")
    lead_cursor = lead_event["seq"]

    replay = run.json_request(
        "POST", command_path, "replay", headers=session_headers,
        body=lead_command,
    )
    if replay != first:
        raise CanaryFailure("replay")

    replay_stream = run.request(
        "GET", events_url + "?once=true", "replay_sse",
        headers={**session_headers, "Last-Event-ID": str(lead_cursor)},
    )
    if _parse_sse(replay_stream, session_id, "replay_sse") != []:
        raise CanaryFailure("replay_sse")

    stop_command = {
        "command_id": stop_command_id,
        "session_id": session_id,
        "type": "stop",
        "expected_version": artifact_version,
    }
    stopped = run.json_request(
        "POST", command_path, "stop", headers=session_headers,
        body=stop_command,
    )
    _command_result(
        stopped, command_id=stop_command_id, session_id=session_id, stage="stop",
    )
    if len(stopped["events"]) != 1 or stopped["events"][0]["type"] != "session.ended":
        raise CanaryFailure("stop")
    terminal_event = stopped["events"][0]
    if (
        stopped["artifact_version"] != artifact_version
        or terminal_event["payload"] != {"reason": "You ended this session."}
        or terminal_event["generation"] != generation
        or terminal_event["artifact_version"] != artifact_version
        or terminal_event["seq"] <= lead_cursor
    ):
        raise CanaryFailure("stop")

    terminal_stream = run.request(
        "GET", events_url + "?once=true", "terminal_sse",
        headers={**session_headers, "Last-Event-ID": str(lead_cursor)},
    )
    terminal_events = _parse_sse(terminal_stream, session_id, "terminal_sse")
    _ordered_generation(terminal_events, generation, "terminal_sse")
    if terminal_events != [terminal_event]:
        raise CanaryFailure("terminal_sse")

    stop_replay = run.json_request(
        "POST", command_path, "stop_replay", headers=session_headers,
        body=stop_command,
    )
    if stop_replay != stopped:
        raise CanaryFailure("stop_replay")

    refusal = run.json_request(
        "POST", command_path, "stopped_refusal", headers=session_headers,
        body={
            "command_id": stopped_probe_id,
            "session_id": session_id,
            "type": "pause",
            "expected_version": artifact_version,
        },
        expected_status=410,
    )
    if refusal != {"detail": "session stopped"}:
        raise CanaryFailure("stopped_refusal")

    return {
        "schema": RECEIPT_SCHEMA,
        "status": "passed",
        "evidence_level": "controller_api_canary",
        "generation": generation,
        "baseline_seq": baseline_cursor,
        "lead_seq": lead_cursor,
        "terminal_seq": terminal_event["seq"],
        **lead_observation,
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
    }


def _failure_receipt(stage: str) -> dict:
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "failed",
        "failed_check": stage if stage in _FAILURE_STAGES else "internal",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit authenticated Studio Lead API canary."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--target", default="")
    parser.add_argument("--origin", default="")
    return parser


def _read_secret(reader, prompt: str, stage: str):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return reader(prompt)
    except Exception:
        raise CanaryFailure(stage) from None


def main(argv=None, *, client_factory=None, secret_reader=None, output=None,
         wall_clock=None, transport_factory=None) -> int:
    args = _parser().parse_args(argv)
    out = output or sys.stdout
    reader = secret_reader or getpass.getpass
    try:
        target, origin = _configuration(
            live=args.live, target=args.target, origin=args.origin,
        )
        email = _read_secret(reader, "Operator email: ", "operator_input")
        expected_raw = _read_secret(
            reader,
            "Expected Lead JSON (org_label, org_type, total, site_total, site_recent): ",
            "operator_input",
        )
        if type(expected_raw) is not str:
            raise CanaryFailure("expected_lead")
        expected = _expected_lead(
            _json_bytes(expected_raw.encode("utf-8"), "expected_lead")
        )
        factory = client_factory or httpx.Client
        make_transport = transport_factory or httpx.HTTPTransport
        transport = make_transport(retries=0, trust_env=False)
        with factory(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(REQUEST_SECONDS),
        ) as client:
            receipt = run_authenticated_canary(
                client=client,
                target=target,
                origin=origin,
                expected_lead=expected,
                email_reader=lambda: email,
                otp_reader=lambda: _read_secret(
                    reader, "Email verification code: ", "operator_input"
                ),
                wall_clock=wall_clock or time.time,
            )
        code = 0
    except CanaryFailure as exc:
        receipt = _failure_receipt(exc.stage)
        code = 1
    except Exception:
        receipt = _failure_receipt("internal")
        code = 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), file=out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
