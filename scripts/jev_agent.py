#!/usr/bin/env python3
"""Jev (TypeSafe System One) on the board: typed decisions, never text.

WHAT JEV IS (CR-20261103-001, verified on console.typesafe.ai 2026-11-03): TypeSafe's
System-1 model, jev-latest. It does NOT generate text. Input is unstructured state; output
is one typed decision with a calibrated probability. Three question types: yesno, score
(min..max), choice (up to 255 options). About $0.042 per 1M input tokens, output free.

The owner's rulings of 2026-11-03 (COWORK-CHROME-JEV-OWNER-RULINGS, -DISCRETIONARY-USE,
-30DAY-GATE) bound this adapter:
  - DATA BOUNDARY: board rows and PR metadata ONLY. No client content, no conference
    transcripts, no screenshots. This adapter only ever sends the board row text the
    waker hands it, capped at CONTEXT_CHARS.
  - BUDGET: $25/mo working budget; the TypeSafe balance is the hard stop (auto-recharge
    off). At these prices a full pass costs well under a cent.

HOW TO ASK JEV, in the request row's text (pipe-free, the board's field separator):
    jev:yesno: <question>
    jev:score:<min>..<max>: <question>
    jev:choice:(<option>; <option>; ...): <question>
A row addressed to jev with NO spec is answered as yesno("Is this request within Jev's
typed-decision scope...") plus a usage hint, so the asker learns the shape instead of
getting silence.

THE WIRE SHAPE (v1, measured on the live API 2026-11-03 against docs.typesafe.ai/api):
    POST /v1/systemone  {"model": "jev-latest", "state": <text>,
                         "questions": {"q": {"type": "noul", "instructions": <q>}}}
    -> {"model": "jev-1.14.0", "answers": {"q": {"type": "noul", "noul": 0.98}}, "usage": {...}}
  noul = yes/no, answered as P(yes). choice: criteria {name: description}, 1..255, answered
  with choice, confidence, probabilities. score: criteria = ordered list of 1..10 levels,
  answered with the EXPECTED level index (a float), confidence and probabilities. So a
  jev:score range wider than 10 values is split into 10 equal bands, and the reply names
  the most likely band. A response without answers.q fails loudly with its top-level keys.

D-18: the key is read from the environment (JEV_API_KEY, Secret Manager on Cloud Run) or
.env at run time. Never argv, never logged, never printed. `check` reports presence only.

USAGE
  python scripts/jev_agent.py check
  python scripts/jev_agent.py say "jev:yesno: Is the sky blue?"
"""
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(REPO, ".env")

SYSTEMONE = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT = 30
CONTEXT_CHARS = 20000          # the data-boundary cap on what one call may carry
SCORE_SPAN_MAX = 10 ** 9
CHOICES_MAX = 255

_YESNO = re.compile(r"jev:yesno:\s*(.+)", re.S)
_SCORE = re.compile(r"jev:score:\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*:\s*(.+)", re.S)
_CHOICE = re.compile(r"jev:choice:\s*\(([^)]+)\)\s*:\s*(.+)", re.S)

NO_SPEC_QUESTION = ("Is this request something Jev can answer with one typed decision "
                    "(yes/no, a score, or a choice) about board rows or PR metadata?")
USAGE_HINT = ("Jev answers typed decisions only. Ask with jev:yesno: <q>, "
              "jev:score:<min>..<max>: <q>, or jev:choice:(a; b; c): <q>.")


def _key():
    value = os.environ.get("JEV_API_KEY", "").strip()
    if not value and os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            if line.strip().startswith("JEV_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    return value


def _bands(lo, hi):
    """At most 10 ordered (low, high) bands covering lo..hi: one per value when it fits."""
    width = hi - lo + 1
    n = min(10, width)
    return [(lo + width * i // n, lo + width * (i + 1) // n - 1) for i in range(n)]


def _band_label(band):
    low, high = band
    return str(low) if low == high else "%d to %d" % (low, high)


def row_text(prompt):
    """Only the board row out of a waker prompt. agent_waker wraps the row as
    doctrine + a newline-dashes-newline marker + row + a newline-dashes marker. The doctrine
    itself quotes the spec syntax (jev:choice:(a; b; c):), so parsing the whole prompt
    would answer the doctrine's example instead of the row, and would send prose that is
    not board data to TypeSafe. A bare string (the CLI's say) is its own row."""
    text = str(prompt or "")
    marker = "\n---\n"
    start = text.find(marker)
    if start < 0:
        return text
    body = text[start + len(marker):]
    end = body.rfind("\n---")
    return body[:end] if end >= 0 else body


def parse_spec(text):
    """(question dict, found spec?) from a request's text. The whole capped text is the
    state Jev reads; the question is what follows the spec."""
    text = str(text or "")[:CONTEXT_CHARS]
    m = _SCORE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < hi and hi - lo <= SCORE_SPAN_MAX:
            return {"type": "score", "question": m.group(3).strip(), "min": lo, "max": hi,
                    "context": text}, True
    m = _CHOICE.search(text)
    if m:
        options = [part.strip() for part in m.group(1).split(";") if part.strip()]
        if 2 <= len(options) <= CHOICES_MAX and len(set(options)) == len(options):
            return {"type": "choice", "question": m.group(2).strip(), "options": options,
                    "context": text}, True
    m = _YESNO.search(text)
    if m:
        return {"type": "yesno", "question": m.group(1).strip(), "context": text}, True
    return {"type": "yesno", "question": NO_SPEC_QUESTION, "context": text}, False


def wire_question(question):
    """The System One question object for one parsed spec."""
    if question["type"] == "choice":
        return {"type": "choice", "instructions": question["question"],
                "criteria": {name: name for name in question["options"]}}
    if question["type"] == "score":
        return {"type": "score", "instructions": question["question"],
                "criteria": [_band_label(b) for b in _bands(question["min"], question["max"])]}
    return {"type": "noul", "instructions": question["question"]}


def _probability(value):
    """A finite numeric probability, excluding bool (a subclass of int)."""
    if type(value) not in (int, float):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def read_answer(question, answer):
    """(decision text, probability) from a validated System One answer."""
    if not isinstance(answer, dict):
        return None, None
    kind = question["type"]
    expected = {"yesno": "noul", "choice": "choice", "score": "score"}.get(kind)
    if answer.get("type") != expected:
        return None, None

    if kind == "yesno":
        p_yes = _probability(answer.get("noul"))
        if p_yes is None:
            return None, None
        return ("yes" if p_yes >= 0.5 else "no"), (p_yes if p_yes >= 0.5 else 1 - p_yes)

    confidence = answer.get("confidence")
    if confidence is not None:
        confidence = _probability(confidence)
        if confidence is None:
            return None, None

    probabilities = answer.get("probabilities")
    if probabilities is not None and not isinstance(probabilities, dict):
        return None, None

    if kind == "choice":
        chosen = answer.get("choice")
        options = question["options"]
        if not isinstance(chosen, str) or chosen not in options:
            return None, None
        if probabilities is not None:
            if any(name not in options or _probability(p) is None
                   for name, p in probabilities.items()):
                return None, None
        probability = confidence
        if probability is None and probabilities is not None:
            probability = _probability(probabilities.get(chosen))
        return (chosen, probability) if probability is not None else (None, None)

    if kind == "score":
        bands = _bands(question["min"], question["max"])
        score = answer.get("score")
        if type(score) not in (int, float) or not math.isfinite(float(score)):
            return None, None
        if not 0 <= score <= len(bands) - 1:
            return None, None
        best, best_p = None, None
        if probabilities is not None:
            for level, p in probabilities.items():
                if (not isinstance(level, str) or not level.isdecimal()
                        or str(int(level)) != level or int(level) >= len(bands)):
                    return None, None
                p = _probability(p)
                if p is None:
                    return None, None
                if best_p is None or p > best_p:
                    best, best_p = int(level), p
        if best is None:
            best, best_p = int(round(score)), confidence
        return (_band_label(bands[best]), best_p) if best_p is not None else (None, None)
    return None, None


def ask(prompt, model=None):
    """One typed decision from Jev. Returns (text, route) or (None, reason).

    The reply text names the question type, the decision, its probability and the model
    version that answered, so the row Jev's waker posts records a DECISION, not prose.
    """
    key = _key()
    if not key:
        return None, "no JEV_API_KEY in the environment or .env"
    question, had_spec = parse_spec(row_text(prompt))
    body = {"model": model or MODEL, "state": question.get("context") or question["question"],
            "questions": {"q": wire_question(question)}}
    request = urllib.request.Request(
        SYSTEMONE, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        # Provider error bodies can echo the submitted row or credentials.
        return None, "systemone HTTP %s" % error.code
    except Exception as error:  # noqa: BLE001 - the waker reports, never crashes
        return None, "systemone %s" % type(error).__name__
    if not isinstance(data, dict):
        return None, "systemone answered a non-object"
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    decision, probability = read_answer(question, answers.get("q"))
    if decision is None:
        return None, ("systemone shape mismatch; response keys: %s"
                      % ", ".join(sorted(str(k) for k in data)[:10]))
    parts = ["Jev decision (%s): %s" % (question["type"], decision)]
    if probability is not None:
        parts.append("probability %.3f" % probability)
    parts.append("model %s" % (data.get("model") or model or MODEL))
    if question["type"] == "score":
        parts.append("range %d..%d" % (question["min"], question["max"]))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if isinstance(usage.get("input_tokens"), int):
        parts.append("input tokens %d" % usage["input_tokens"])
    if not had_spec:
        parts.append(USAGE_HINT)
    parts.append("Jev gives typed decisions with calibrated probabilities; it does not "
                 "write text, and this sentence is its adapter's, not its own.")
    return " / ".join(p.replace("|", "/") for p in parts), "systemone api-key"


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "check":
        key = _key()
        print("JEV_API_KEY: %s" % ("present (%d chars)" % len(key) if key else "MISSING"))
        return 0 if key else 1
    if len(sys.argv) >= 3 and sys.argv[1] == "say":
        text, route = ask(sys.argv[2])
        print(text if text else "FAILED: %s" % route)
        return 0 if text else 1
    print(__doc__.split("USAGE")[1].strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
