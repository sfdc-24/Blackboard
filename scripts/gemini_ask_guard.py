#!/usr/bin/env python3
"""Refuse to post a Gemini-addressed question unless the row's phase is ASK.

WHY
  Gemini's doorbell and worker wake on phase=ASK. A question posted as RESULT,
  DISPATCH, or NOTE can sit unanswered because the worker never treats it as a
  request. This guard fails closed at the writer, before the row reaches the
  append-only board.

  The sanctioned Python writer is scripts/append.py (see
  scripts/README-board-clients.md). The fleet's local bus_cli.py is a
  convenience copy of a post helper and is not in this repository; the rule
  lives here so every in-repo append goes through it.

WHEN IT FIRES
  All three of the following are true, and --allow-gemini-non-ask was not
  passed to scripts/append.py:

  1. Addressed to Gemini. The BCB `to=` field contains a token that equals
     `gemini` or starts with `gemini-` (case-insensitive). Tokens are split
     on commas and whitespace. Only `to=` is consulted; `cc=` is ignored.
     Tokens such as `not-gemini` or `geminid` do not match.

  2. The row expects an answer. The first matching clause wins:

       a. Explicit flag: a BCB field `expects_answer` is present and its
          value, stripped and lowercased, is not one of: empty, `0`, `n`,
          `no`, `false`, `off`. So `expects_answer=yes`, `true`, `1`, `y`,
          `on`, or any other non-falsey value counts. A bare
          `expects_answer=` does not.

       b. Deadline marker: a BCB field `answer_by` or `answer-by` is present
          and non-empty, OR the payload contains the two-word phrase
          `answer by` as a whole-word match (case-insensitive). Fleet prompts
          often write "answer by 18:00Z" in prose rather than as a field.
          "answered by" does not match.

       c. Question form: a BCB field `ask` or `question` is present and
          non-empty, OR a `?` appears in a BCB field among `text`, `what`,
          `subject`, `gist`, or in the free-text tail after the field head,
          after `http://` and `https://` URLs have been stripped. A payload
          whose only `?` characters live inside those URLs does not count.

  3. The BCB `phase=` value, stripped, is not `ASK` (case-insensitive). A
     missing `phase=` is treated as not ASK.

OVERRIDE
  `--allow-gemini-non-ask` on scripts/append.py skips the refusal and prints
  a one-line notice that the guard was overridden. Use it for an approved
  exception (for example a status note to Gemini that happens to contain a
  question mark). There is no payload-field override: a row cannot waive
  the guard itself.

EXIT
  On refusal, scripts/append.py prints a warning, exits non-zero with
  GEMINI_ASK_REQUIRED, and does not call the bus. Nothing is posted.
"""
from __future__ import annotations

import re
from typing import Optional

# Keep the field-head split in step with scripts/board_row.py:split_fields.
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")
_PAIR = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=([^|]*)")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_ANSWER_BY_PHRASE = re.compile(r"\banswer by\b", re.IGNORECASE)
_FALSEY = frozenset({"", "0", "n", "no", "false", "off"})
_QUESTION_FIELDS = ("text", "what", "subject", "gist")
_TOKEN_SPLIT = re.compile(r"[,\s]+")


def split_fields(text: str) -> tuple[str, str]:
    """('k=v|k=v|', free text). The free text may itself contain pipes."""
    segs = (text or "").split("|")
    i = 1 if segs and segs[0] == "BCB" else 0
    while i < len(segs) - 1 and _FIELD.match(segs[i]):
        i += 1
    head = "|".join(segs[:i]) + ("|" if i else "")
    return head, "|".join(segs[i:])


def field_map(payload: str) -> tuple[dict[str, str], str]:
    """First-value map of BCB fields (lowercased keys) and the free-text tail."""
    head, free = split_fields(payload)
    out: dict[str, str] = {}
    for match in _PAIR.finditer(head):
        key = match.group(1).lower()
        if key not in out:
            out[key] = match.group(2).strip()
    return out, free


def addressed_to_gemini(payload: str) -> bool:
    fields, _free = field_map(payload)
    for token in _TOKEN_SPLIT.split(fields.get("to", "")):
        token = token.strip()
        if not token:
            continue
        low = token.lower()
        if low == "gemini" or low.startswith("gemini-"):
            return True
    return False


def _strip_urls(text: str) -> str:
    return _URL.sub(" ", text or "")


def expects_answer(payload: str) -> tuple[bool, str]:
    """Return (True, clause) when the payload expects an answer, else (False, '')."""
    fields, free = field_map(payload)
    if "expects_answer" in fields:
        value = fields["expects_answer"].strip().lower()
        if value not in _FALSEY:
            return True, "expects_answer"
    for key in ("answer_by", "answer-by"):
        if fields.get(key, "").strip():
            return True, "answer_by"
    if _ANSWER_BY_PHRASE.search(payload or ""):
        return True, "answer by"
    if fields.get("ask", "").strip() or fields.get("question", "").strip():
        return True, "ask/question"
    for key in _QUESTION_FIELDS:
        if "?" in _strip_urls(fields.get(key, "")):
            return True, "question-mark"
    if "?" in _strip_urls(free):
        return True, "question-mark"
    return False, ""


def phase_is_ask(payload: str) -> bool:
    fields, _free = field_map(payload)
    return fields.get("phase", "").strip().lower() == "ask"


def phase_value(payload: str) -> str:
    fields, _free = field_map(payload)
    return fields.get("phase", "").strip()


def refusal_message(payload: str) -> Optional[str]:
    """Warning text if this row must be refused, else None."""
    if not addressed_to_gemini(payload):
        return None
    expects, clause = expects_answer(payload)
    if not expects:
        return None
    if phase_is_ask(payload):
        return None
    phase = phase_value(payload) or "(missing)"
    return (
        "GEMINI_ASK_REQUIRED: this row is addressed to gemini and expects an "
        "answer (reason: %s) but phase=%s, not ASK. Refusing to post. "
        "Pass --allow-gemini-non-ask to override. Nothing was posted."
        % (clause, phase)
    )
