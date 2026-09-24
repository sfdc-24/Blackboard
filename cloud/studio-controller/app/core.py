"""Deterministic reducer and command controller for SFDC24 Studio."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
import uuid

from .state import StateConflict, StudioRepository
from .artifacts import apply_ops


class CommandError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


def _command_fingerprint(command: dict) -> str:
    """Bind an idempotency key to one exact, validated command payload."""
    canonical = json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_question(state: dict, question_id: str) -> dict:
    for question in state.get("questions", []):
        if question.get("question_id") == question_id:
            return question
    raise CommandError("unknown question_id", 404)


def _mark_batch_member(state: dict, question_id: str) -> None:
    """A form question answered outside its form (by voice) leaves the form.
    The member is recorded, never deleted, so the batch stays traceable; the
    form closes when every member is answered."""
    for batch in (state.get("batches") or {}).values():
        if batch.get("status") != "open" or question_id not in (batch.get("question_ids") or []):
            continue
        answered = batch.setdefault("answered_ids", [])
        if question_id not in answered:
            answered.append(question_id)
        if set(answered) >= set(batch.get("question_ids") or []):
            batch["status"] = "answered"


def _answer(question: dict, answer: dict, artifact_version: int) -> dict:
    if question.get("status") != "open":
        raise CommandError("question is not open", 409)
    option_id = answer.get("option_id")
    freeform = (answer.get("freeform_answer") or "").strip()
    if bool(option_id) == bool(freeform):
        raise CommandError("provide exactly one of option_id or freeform_answer")
    if option_id and option_id not in {item.get("option_id") for item in question.get("options", [])}:
        raise CommandError("option_id does not belong to this question")
    question["status"] = "answered"
    question["answer_source"] = answer.get("answer_source") or "tap"
    question["artifact_version_before"] = artifact_version
    if option_id:
        question["selected_option"] = option_id
    else:
        question["freeform_answer"] = freeform[:600]
    return question


def reduce_event(state: dict, event: dict) -> bool:
    """Apply one committed event only when all ordering fences agree.

    This is also the authoritative replay rule used by tests and repair tools.
    A foreign session, older generation/revision, duplicate sequence/op, or
    non-contiguous artifact version is ignored without mutating state.
    """
    if event.get("session_id") != state.get("session_id"):
        return False
    generation = int(event.get("generation") or 0)
    current_generation = int(state.get("generation") or 1)
    if generation < current_generation:
        return False
    seq = int(event.get("seq") or 0)
    current_seq = int(state.get("last_seq") or 0)
    if generation == current_generation and seq != current_seq + 1:
        return False
    if generation > current_generation and (seq != 1 or event.get("type") != "artifact.snapshot"):
        return False
    if event.get("op_id") in set(state.get("seen_ops") or []):
        return False
    if int(event.get("task_revision") or 0) < int(state.get("task_revision") or 1):
        return False
    event_type = event.get("type")
    event_version = int(event.get("artifact_version") or 0)
    current_version = int(state.get("artifact_version") or 0)
    if event_type == "artifact.patch":
        if event_version != current_version + 1:
            return False
    elif event_type == "artifact.snapshot":
        if event_version < current_version:
            return False
    elif event_version != current_version:
        return False

    # Only mutate after every fence passes. A rejected new-generation event
    # must not reset the current cursor on its way out.
    if generation > current_generation:
        state["generation"] = generation
        state["last_seq"] = 0
    if event_type == "artifact.patch":
        state["artifact"] = apply_ops(state["artifact"], event.get("payload", {}).get("ops") or [])
        state["artifact_version"] = event_version
    elif event_type == "artifact.snapshot":
        state["artifact"] = copy.deepcopy(event.get("payload", {}).get("root"))
        state["artifact_version"] = event_version
    state["task_revision"] = max(int(state.get("task_revision") or 1), int(event.get("task_revision") or 1))
    state["last_seq"] = seq
    state.setdefault("seen_ops", []).append(event["op_id"])
    state["seen_ops"] = state["seen_ops"][-400:]
    return True


class StudioController:
    def __init__(self, repository: StudioRepository, worker, *, clock=time.time,
                 id_factory=None, max_seconds: int = 600, daily_cap: int = 20,
                 max_events: int = 200, max_commands: int = 100,
                 inflight_lease_seconds: int = 90):
        self.repository = repository
        self.worker = worker
        self.clock = clock
        self.id_factory = id_factory or (lambda prefix: prefix + "-" + uuid.uuid4().hex)
        self.max_seconds = max_seconds
        self.daily_cap = daily_cap
        self.max_events = max_events
        self.max_commands = max_commands
        self.inflight_lease_seconds = inflight_lease_seconds

    def _event(self, state: dict, event_type: str, payload: dict, *,
               turn_id: str | None = None, task_revision: int | None = None,
               artifact_version: int | None = None) -> dict:
        state["last_seq"] += 1
        event = {
            "session_id": state["session_id"],
            "generation": state["generation"],
            "seq": state["last_seq"],
            "op_id": self.id_factory("op"),
            "type": event_type,
            "task_id": state["task_id"],
            "task_revision": task_revision or state["task_revision"],
            "artifact_version": state["artifact_version"] if artifact_version is None else artifact_version,
            "turn_id": turn_id or state["turn_id"],
            "payload": copy.deepcopy(payload),
        }
        state.setdefault("events", []).append(event)
        state["events"] = state["events"][-self.max_events:]
        state.setdefault("seen_ops", []).append(event["op_id"])
        state["seen_ops"] = state["seen_ops"][-400:]
        return event

    def create_session(self, title: str = "Live prototype", subject: str = "",
                       creation_id: str = "") -> tuple[dict, int]:
        if creation_id and not ID_RE.fullmatch(creation_id):
            raise CommandError("creation_id must be a contract id")
        now = int(self.clock())
        if creation_id:
            stable = hashlib.sha256((subject + "\x00" + creation_id).encode("utf-8")).hexdigest()[:32]
            session_id = "s-" + stable
        else:
            session_id = self.id_factory("s")
        admitted = self.repository.admit(self.daily_cap, session_id)
        artifact = self.worker.initial_artifact()
        questions = self.worker.initial_questions()
        state = {
            "schema_version": 1,
            "session_id": session_id,
            "created_at": now,
            "expires_at": now + self.max_seconds,
            "generation": 1,
            "last_seq": 0,
            "task_id": self.id_factory("task"),
            "task_revision": 1,
            "artifact_version": 0,
            "turn_id": "turn-0",
            "turn_seq": 0,
            "artifact": copy.deepcopy(artifact),
            "questions": copy.deepcopy(questions),
            "batches": {},
            "transcript": [],
            "events": [],
            "seen_ops": [],
            "commands": {},
            "active_command": None,
            "paused": False,
            "stopped": False,
            "operator_subject": subject,
            "voice_item_ids": [],
        }
        self._event(state, "session.started", {"title": title[:600]})
        state["artifact_version"] = 1
        self._event(state, "artifact.snapshot", {"root": artifact}, artifact_version=1)
        if questions:
            state["turn_seq"] = 1
            state["turn_id"] = "turn-1"
            if len(questions) == 1:
                self._event(state, "question.asked", {"question": questions[0]})
            else:
                batch_id = self.id_factory("batch")
                state["batches"][batch_id] = {
                    "status": "open",
                    "question_ids": [question["question_id"] for question in questions],
                }
                self._event(state, "decision.batch", {
                    "batch_id": batch_id,
                    "title": "A few quick decisions",
                    "questions": questions,
                })
        try:
            self.repository.create(state)
            return copy.deepcopy(state), admitted
        except StateConflict:
            if not creation_id:
                raise
            existing = self.repository.load(session_id).state
            if existing.get("operator_subject") != subject:
                raise StateConflict("creation_id belongs to another operator")
            return copy.deepcopy(existing), admitted

    def _assert_live(self, state: dict) -> None:
        if int(state.get("expires_at") or 0) <= int(self.clock()):
            raise CommandError("session expired", 410)
        if state.get("stopped"):
            raise CommandError("session stopped", 410)

    def _validate_command(self, state: dict, command: dict, *, stateful: bool = True) -> None:
        required = {"command_id", "session_id", "type", "expected_version"}
        if not required.issubset(command):
            raise CommandError("command is missing required fields")
        allowed = required | {
            "answers", "batch_id", "question_id", "option_id", "freeform_answer",
            "answer_source", "transcript", "item_id",
        }
        if set(command) - allowed:
            raise CommandError("command has unknown fields: " + ", ".join(sorted(set(command) - allowed)))
        if not all(ID_RE.fullmatch(str(command.get(key) or "")) for key in ("command_id", "session_id")):
            raise CommandError("command_id and session_id must be contract ids")
        if command["session_id"] != state["session_id"]:
            raise CommandError("session_id mismatch", 403)
        if command["type"] not in {
            "answer", "answer_batch", "change_decision", "decide_later", "pause",
            "resume", "stop", "utterance",
        }:
            raise CommandError("unknown command type")
        if not isinstance(command["expected_version"], int) or isinstance(command["expected_version"], bool):
            raise CommandError("expected_version must be an integer")
        kind = command["type"]
        if command.get("answer_source") not in {None, "tap", "voice", "typed"}:
            raise CommandError("answer_source is not allowed")
        if kind == "answer":
            if not ID_RE.fullmatch(str(command.get("question_id") or "")):
                raise CommandError("answer requires a contract question_id")
            if command.get("option_id") and not ID_RE.fullmatch(str(command["option_id"])):
                raise CommandError("option_id must be a contract id")
            if len(str(command.get("freeform_answer") or "")) > 600:
                raise CommandError("freeform_answer exceeds 600 characters")
            if bool(command.get("option_id")) == bool((command.get("freeform_answer") or "").strip()):
                raise CommandError("answer requires exactly one option_id or freeform_answer")
        elif kind == "answer_batch":
            if not ID_RE.fullmatch(str(command.get("batch_id") or "")):
                raise CommandError("answer_batch requires a contract batch_id")
            answers = command.get("answers")
            if not isinstance(answers, list) or not 1 <= len(answers) <= 4:
                raise CommandError("answer_batch requires 1 to 4 answers")
            for answer in answers:
                if not isinstance(answer, dict) or set(answer) - {"question_id", "option_id", "freeform_answer"}:
                    raise CommandError("answer_batch item has unknown fields")
                if not ID_RE.fullmatch(str(answer.get("question_id") or "")):
                    raise CommandError("answer_batch item requires a contract question_id")
                if answer.get("option_id") and not ID_RE.fullmatch(str(answer["option_id"])):
                    raise CommandError("answer_batch option_id must be a contract id")
                if len(str(answer.get("freeform_answer") or "")) > 600:
                    raise CommandError("answer_batch freeform_answer exceeds 600 characters")
                if bool(answer.get("option_id")) == bool((answer.get("freeform_answer") or "").strip()):
                    raise CommandError("answer_batch item requires exactly one answer")
            if stateful:
                batch = (state.get("batches") or {}).get(command["batch_id"])
                if not batch or batch.get("status") != "open":
                    raise CommandError("answer_batch does not name an open decision batch", 409)
                # A form question already answered by voice is no longer on
                # the form: the submission names exactly the ones still open.
                remaining = set(batch.get("question_ids") or []) - set(batch.get("answered_ids") or [])
                if {item["question_id"] for item in answers} != remaining:
                    raise CommandError("answer_batch questions do not match the named batch", 409)
        elif kind in {"change_decision", "decide_later"}:
            if not ID_RE.fullmatch(str(command.get("question_id") or "")):
                raise CommandError("command requires a contract question_id")
        elif kind == "utterance":
            transcript = command.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip():
                raise CommandError("utterance requires a completed transcript")
            if len(transcript) > 600:
                raise CommandError("utterance transcript exceeds 600 characters")
            if not ITEM_ID_RE.fullmatch(str(command.get("item_id") or "")):
                raise CommandError("utterance requires a contract item_id")

    def _recover_stale_inflight(self, session_id: str, state: dict, token):
        active = state.get("active_command")
        if not active:
            return state, token
        receipt = state.get("commands", {}).get(active) or {}
        started_at = int(receipt.get("started_at") or 0)
        if receipt.get("status") != "inflight" or int(self.clock()) - started_at < self.inflight_lease_seconds:
            return state, token
        receipt.update({
            "status": "failed",
            "error": "command outcome is unknown after controller interruption",
            "finished_at": int(self.clock()),
            "outcome_unknown": True,
        })
        state["active_command"] = None
        token = self.repository.save(session_id, state, token)
        return state, token

    def execute(self, session_id: str, command: dict) -> dict:
        record = self.repository.load(session_id)
        state = record.state
        # Validate the closed command shape first, but defer mutable decision
        # state until after a matching completed receipt can be replayed.
        self._validate_command(state, command, stateful=False)
        if command["type"] == "stop":
            return self.stop_session(session_id, command)
        state, current_token = self._recover_stale_inflight(session_id, state, record.token)
        command_id = str(command["command_id"])
        fingerprint = _command_fingerprint(command)
        prior = state.get("commands", {}).get(command_id)
        if prior:
            if not hmac.compare_digest(str(prior.get("fingerprint") or ""), fingerprint):
                raise CommandError("command_id is already bound to a different payload", 409)
            if prior.get("status") == "completed":
                return copy.deepcopy(prior["result"])
            if prior.get("status") == "failed":
                raise CommandError("command has already failed; use a new command_id", 409)
            raise CommandError("command is already in progress", 409)
        self._validate_command(state, command)
        self._assert_live(state)
        if state.get("active_command"):
            raise CommandError("another command is in progress", 409)
        if state.get("paused") and command["type"] not in {"resume", "stop"}:
            raise CommandError("session is paused", 409)
        if len(state.get("commands", {})) >= self.max_commands:
            raise CommandError("session command limit reached", 429)
        if command["expected_version"] != state["artifact_version"]:
            raise CommandError("stale expected_version", 409)

        # Reserve before any provider call. A retry can observe this record and
        # must not call the worker twice after an uncertain client timeout.
        state.setdefault("commands", {})[command_id] = {
            "status": "inflight", "started_at": int(self.clock()),
            "expected_version": command["expected_version"],
            "fingerprint": fingerprint,
        }
        state["active_command"] = command_id
        reserved_token = self.repository.save(session_id, state, current_token)

        working = copy.deepcopy(state)
        try:
            result = self._run_reserved(working, command)
        except Exception as exc:
            # Persist a terminal failure without replaying the worker. The same
            # command_id returns this failure; recovery requires a new command.
            state["commands"][command_id] = {
                "status": "failed", "error": "worker failure: " + type(exc).__name__,
                "finished_at": int(self.clock()), "fingerprint": fingerprint,
            }
            state["active_command"] = None
            try:
                self.repository.save(session_id, state, reserved_token)
            except StateConflict:
                pass
            raise

        working["commands"][command_id] = {
            "status": "completed", "finished_at": int(self.clock()),
            "fingerprint": fingerprint, "result": copy.deepcopy(result)
        }
        working["active_command"] = None
        self.repository.save(session_id, working, reserved_token)
        return result

    def stop_session(self, session_id: str, command: dict) -> dict:
        """Fail-safe stop that fences any late worker commit with CAS.

        Stop is deliberately not blocked by an in-flight command, receipt cap,
        pause state, or a stale artifact version. It is the visitor's emergency
        brake and must remain available whenever the session record exists.
        """
        command_id = str(command["command_id"])
        fingerprint = _command_fingerprint(command)
        for _ in range(self.repository.attempts):
            record = self.repository.load(session_id)
            state = record.state
            self._validate_command(state, command)
            prior = state.get("commands", {}).get(command_id)
            if prior:
                if not hmac.compare_digest(str(prior.get("fingerprint") or ""), fingerprint):
                    raise CommandError("command_id is already bound to a different payload", 409)
                if prior.get("status") == "completed":
                    return copy.deepcopy(prior["result"])
                raise CommandError("stop command is not replayable", 409)
            if state.get("stopped"):
                raise CommandError("session stopped", 410)

            candidate = copy.deepcopy(state)
            active = candidate.get("active_command")
            if active:
                receipt = candidate.get("commands", {}).get(active) or {}
                receipt.update({
                    "status": "failed",
                    "error": "command fenced by visitor stop",
                    "finished_at": int(self.clock()),
                    "outcome_unknown": True,
                })
                candidate["active_command"] = None
            candidate["stopped"] = True
            candidate["paused"] = False
            candidate["turn_seq"] += 1
            candidate["turn_id"] = "turn-%d" % candidate["turn_seq"]
            event = self._event(candidate, "session.ended", {"reason": "You ended this session."})
            result = {
                "command_id": command_id,
                "session_id": candidate["session_id"],
                "artifact_version": candidate["artifact_version"],
                "events": [copy.deepcopy(event)],
                "problems": [],
            }
            candidate.setdefault("commands", {})[command_id] = {
                "status": "completed",
                "finished_at": int(self.clock()),
                "fingerprint": fingerprint,
                "result": copy.deepcopy(result),
            }
            try:
                self.repository.save(session_id, candidate, record.token)
                return result
            except StateConflict:
                continue
        raise StateConflict("stop is busy; retry with the same command_id")

    def begin_voice(self, session_id: str, voice_id: str, ends_at: int) -> dict:
        """Reserve the session's single voice call before provider contact."""
        record = self.repository.load(session_id)
        state = record.state
        self._assert_live(state)
        if state.get("active_command"):
            raise CommandError("a prototype command is in progress", 409)
        voice = state.get("voice_call") or {}
        if voice:
            raise CommandError("this Studio session already has a voice call", 409)
        state["voice_call"] = {
            "voice_id": voice_id,
            "status": "opening",
            "started_at": int(self.clock()),
            "ends_at": int(ends_at),
        }
        self.repository.save(session_id, state, record.token)
        return copy.deepcopy(state)

    def activate_voice(self, session_id: str, voice_id: str, call_id: str) -> dict:
        record = self.repository.load(session_id)
        state = record.state
        self._assert_live(state)
        voice = state.get("voice_call") or {}
        if voice.get("voice_id") != voice_id or voice.get("status") != "opening":
            raise CommandError("voice reservation is no longer current", 409)
        voice["call_id"] = call_id
        voice["status"] = "active"
        voice["activated_at"] = int(self.clock())
        self.repository.save(session_id, state, record.token)
        return copy.deepcopy(state)

    def mark_voice_unknown(self, session_id: str, voice_id: str) -> None:
        """Close admission after an ambiguous provider transport failure."""
        record = self.repository.load(session_id)
        state = record.state
        voice = state.get("voice_call") or {}
        if voice.get("voice_id") != voice_id or voice.get("status") != "opening":
            return
        voice.update({
            "status": "unknown",
            "failed_at": int(self.clock()),
            "failure": "provider request outcome unknown",
        })
        self.repository.save(session_id, state, record.token)

    def reconcile_voice_hangup(
            self, session_id: str, voice_id: str, call_id: str, reason: str) -> bool:
        """Durably settle the exact call only after provider hangup is confirmed.

        Activation can fail after its state write committed but before the
        caller received the result. Re-read under CAS and accept either the
        original opening reservation or that already-active exact call. The
        sweep index may be removed only after this transition returns true.
        """
        for _ in range(self.repository.attempts):
            record = self.repository.load(session_id)
            state = record.state
            voice = state.get("voice_call") or {}
            if voice.get("voice_id") != voice_id:
                return False
            status = voice.get("status")
            if status == "ended":
                return voice.get("call_id") == call_id
            if status == "active" and voice.get("call_id") != call_id:
                return False
            if status not in {"opening", "active"}:
                return False
            voice.update({
                "call_id": call_id,
                "status": "ended",
                "ended_at": int(self.clock()),
                "end_reason": reason,
            })
            try:
                self.repository.save(session_id, state, record.token)
                return True
            except StateConflict:
                continue
        raise StateConflict("voice hangup reconciliation is busy; retry cleanup")

    def request_voice_end(self, session_id: str, call_id: str, reason: str) -> None:
        record = self.repository.load(session_id)
        state = record.state
        voice = state.get("voice_call") or {}
        if voice.get("call_id") != call_id or voice.get("status") != "active":
            return
        voice.update({
            "close_requested": True,
            "close_reason": reason,
            "retry_at": int(self.clock()),
        })
        self.repository.save(session_id, state, record.token)

    def fail_voice(self, session_id: str, voice_id: str) -> bool:
        """Release the exact opening after its owned index entry is gone."""
        for _ in range(self.repository.attempts):
            record = self.repository.load(session_id)
            state = record.state
            voice = state.get("voice_call") or {}
            if not voice:
                return True
            if voice.get("voice_id") != voice_id or voice.get("status") != "opening":
                return False
            state.pop("voice_call", None)
            try:
                self.repository.save(session_id, state, record.token)
                return True
            except StateConflict:
                continue
        raise StateConflict("voice release is busy; keep the reservation closed")

    def finish_voice(self, session_id: str, call_id: str, reason: str) -> None:
        record = self.repository.load(session_id)
        state = record.state
        voice = state.get("voice_call") or {}
        if voice.get("call_id") != call_id or voice.get("status") != "active":
            return
        voice.update({"status": "ended", "ended_at": int(self.clock()), "end_reason": reason})
        self.repository.save(session_id, state, record.token)

    def _run_reserved(self, state: dict, command: dict) -> dict:
        kind = command["type"]
        state["turn_seq"] += 1
        state["turn_id"] = "turn-%d" % state["turn_seq"]
        events: list[dict] = []
        trigger = None
        answered_questions: list[dict] = []

        if kind == "answer":
            question = _find_question(state, command.get("question_id", ""))
            before = state["artifact_version"]
            answered = _answer(question, command, before)
            trigger = {"kind": "answer", "question_id": question["question_id"]}
            if command.get("option_id"):
                trigger["option_id"] = command["option_id"]
            else:
                trigger["freeform_answer"] = command.get("freeform_answer", "")
            state["transcript"].append({"role": "visitor", "text": command.get("option_id") or command.get("freeform_answer", "")})
            answered_questions.append(answered)
        elif kind == "answer_batch":
            answers = command.get("answers") or []
            if not 1 <= len(answers) <= 4:
                raise CommandError("answer_batch requires 1 to 4 answers")
            normalized = []
            seen = set()
            for raw in answers:
                qid = raw.get("question_id", "")
                if qid in seen:
                    raise CommandError("duplicate question in answer_batch")
                seen.add(qid)
                item = dict(raw)
                item["answer_source"] = command.get("answer_source") or "tap"
                answered = _answer(_find_question(state, qid), item, state["artifact_version"])
                answered_questions.append(answered)
                normalized.append({key: item[key] for key in ("question_id", "option_id", "freeform_answer") if item.get(key)})
            trigger = {"kind": "answer_batch", "batch_id": command["batch_id"], "answers": normalized}
            state["batches"][command["batch_id"]]["status"] = "answered"
        elif kind == "utterance":
            item_id = command["item_id"]
            if item_id in set(state.get("voice_item_ids") or []):
                return {
                    "command_id": command["command_id"],
                    "session_id": state["session_id"],
                    "artifact_version": state["artifact_version"],
                    "events": [],
                    "problems": [],
                    "deduplicated": True,
                }
            transcript = command["transcript"].strip()
            state.setdefault("voice_item_ids", []).append(item_id)
            # Retain every item that can be admitted under this session's
            # command bound; a fixed 100-item window permits old items to
            # invoke providers again when max_commands is configured higher.
            state["voice_item_ids"] = state["voice_item_ids"][-self.max_commands:]
            state["transcript"].append({"role": "visitor", "text": transcript})
            trigger = {"kind": "utterance", "text": transcript, "item_id": item_id}
        elif kind == "change_decision":
            question = _find_question(state, command.get("question_id", ""))
            if question.get("status") not in {"answered", "assumed", "deferred"}:
                raise CommandError("only a completed decision can be changed", 409)
            state["task_revision"] += 1
            question["status"] = "superseded"
            events.append(self._event(state, "question.superseded", {
                "question_id": question["question_id"],
                "superseded_by_revision": state["task_revision"],
            }))
            replacement = copy.deepcopy(question)
            replacement["question_id"] = question["question_id"] + "-r%d" % state["task_revision"]
            replacement["parent_question_id"] = question["question_id"]
            replacement["status"] = "open"
            replacement["reason"] = "You asked to change this decision."
            replacement["prompt"] = replacement["prompt"].rstrip("?") + " instead?"
            for key in ("selected_option", "freeform_answer", "answer_source", "artifact_version_before", "artifact_version_after"):
                replacement.pop(key, None)
            state["questions"].append(replacement)
            events.append(self._event(state, "question.asked", {"question": replacement}))
        elif kind == "decide_later":
            question = _find_question(state, command.get("question_id", ""))
            if question.get("status") != "open":
                raise CommandError("question is not open", 409)
            question["status"] = "deferred"
            events.append(self._event(state, "question.answered", {"question": copy.deepcopy(question)}))
        elif kind == "pause":
            state["paused"] = True
            events.append(self._event(state, "progress", {
                "artifact_ids": [state["artifact"]["id"]], "text": "Session paused",
            }))
        elif kind == "resume":
            state["paused"] = False
            events.append(self._event(state, "progress", {
                "artifact_ids": [state["artifact"]["id"]], "text": "Session resumed",
            }))
        elif kind == "stop":
            state["stopped"] = True
            events.append(self._event(state, "session.ended", {"reason": "You ended this session."}))

        problems = []
        if trigger is not None:
            worker_result = self.worker.on_turn(copy.deepcopy(state), trigger)
            problems = list(worker_result.get("problems") or [])
            # A spoken turn can answer the open question ("book a call, then").
            # The worker only NAMES the answer; the controller owns the record,
            # so it is re-checked here against the live question and recorded
            # with answer_source "voice". A stale or invalid claim is a problem,
            # never a crash and never an answer.
            resolves = worker_result.get("resolves") or {}
            if trigger["kind"] == "utterance" and resolves.get("question_id"):
                item = {key: resolves[key] for key in ("option_id", "freeform_answer") if resolves.get(key)}
                item["answer_source"] = "voice"
                try:
                    question = _find_question(state, resolves["question_id"])
                    answered_questions.append(_answer(question, item, state["artifact_version"]))
                    _mark_batch_member(state, resolves["question_id"])
                except CommandError as exc:
                    problems.append("resolves %r refused: %s" % (resolves["question_id"], exc))
            answers_emitted = False

            def emit_answers():
                nonlocal answers_emitted
                if answers_emitted:
                    return
                for answered in answered_questions:
                    answered["artifact_version_after"] = state["artifact_version"]
                    events.append(self._event(
                        state, "question.answered", {"question": copy.deepcopy(answered)}
                    ))
                answers_emitted = True

            for draft in worker_result.get("events") or []:
                event_type = draft.get("type")
                payload = draft.get("payload") or {}
                if event_type not in {"artifact.patch", "confirm", "question.asked", "decision.batch", "progress"}:
                    problems.append("worker event type %r refused" % event_type)
                    continue
                if not isinstance(payload, dict):
                    problems.append("worker event %r payload is not an object" % event_type)
                    continue
                if event_type == "artifact.patch":
                    state["artifact"] = apply_ops(state["artifact"], payload.get("ops") or [])
                    state["artifact_version"] += 1
                elif event_type in {"confirm", "question.asked", "decision.batch"}:
                    # Keep the decision record and prototype on one committed
                    # artifact_version before announcing confirmation/next ask.
                    emit_answers()
                event = self._event(state, event_type, payload)
                events.append(event)
                if event_type == "question.asked":
                    state["questions"].append(copy.deepcopy(payload["question"]))
                elif event_type == "decision.batch":
                    questions = copy.deepcopy(payload.get("questions") or [])
                    state["questions"].extend(questions)
                    state.setdefault("batches", {})[payload["batch_id"]] = {
                        "status": "open",
                        "question_ids": [question["question_id"] for question in questions],
                    }
            emit_answers()

        return {
            "command_id": command["command_id"],
            "session_id": state["session_id"],
            "artifact_version": state["artifact_version"],
            "events": copy.deepcopy(events),
            "problems": problems,
        }

    def events_after(self, session_id: str, after_seq: int) -> tuple[list[dict], bool, dict]:
        """Read a replay window or atomically begin a repair generation.

        The first event of every repair generation is the contract's closed
        artifact.snapshot shape at seq 1. Open decisions are re-announced only
        after that snapshot so the browser can rebuild its conversation UI.
        """
        for _ in range(self.repository.attempts):
            record = self.repository.load(session_id)
            state = record.state
            events = state.get("events") or []
            first = int(events[0]["seq"]) if events else int(state.get("last_seq") or 0) + 1
            repair = after_seq < first - 1 or after_seq > int(state.get("last_seq") or 0)
            if not repair:
                return (
                    [copy.deepcopy(event) for event in events if int(event["seq"]) > after_seq],
                    False,
                    state,
                )

            candidate = copy.deepcopy(state)
            candidate["generation"] = int(candidate.get("generation") or 1) + 1
            candidate["last_seq"] = 0
            candidate["events"] = []
            self._event(candidate, "artifact.snapshot", {"root": copy.deepcopy(candidate["artifact"])})
            for question in candidate.get("questions") or []:
                if question.get("status") == "open":
                    self._event(candidate, "question.asked", {"question": copy.deepcopy(question)})
            try:
                self.repository.save(session_id, candidate, record.token)
                return copy.deepcopy(candidate["events"]), True, candidate
            except StateConflict:
                continue
        raise StateConflict("snapshot repair is busy; reconnect")
