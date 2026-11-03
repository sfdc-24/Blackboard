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

THE WIRE SHAPE IS ASSUMED (v0). The REST endpoint is published (POST /v1/systemone,
Bearer auth, model jev-latest) but the field names are not in the assessment doc. This
adapter sends the body below and reads the answer tolerantly (several candidate keys).
If TypeSafe's real shape differs, the reply SAYS SO with the response's top-level keys
(never the key, never more than an excerpt), so the first live call corrects v0 honestly
instead of pretending.

D-18: the key is read from the environment (JEV_API_KEY, Secret Manager on Cloud Run) or
.env at run time. Never argv, never logged, never printed. `check` reports presence only.

USAGE
  python scripts/jev_agent.py check
  python scripts/jev_agent.py say "jev:yesno: Is the sky blue?"
"""
import json
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


def parse_spec(text):
    """(question dict, found spec?) from a request's text. The question carries the whole
    remaining text as its question; context is what came before the spec."""
    text = str(text or "")[:CONTEXT_CHARS]
    m = _SCORE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < hi and hi - lo <= SCORE_SPAN_MAX:
            return {"type": "score", "question": m.group(3).strip(), "min": lo, "max": hi}, True
    m = _CHOICE.search(text)
    if m:
        options = [part.strip() for part in m.group(1).split(";") if part.strip()]
        if 2 <= len(options) <= CHOICES_MAX:
            return {"type": "choice", "question": m.group(2).strip(), "options": options}, True
    m = _YESNO.search(text)
    if m:
        return {"type": "yesno", "question": m.group(1).strip()}, True
    return {"type": "yesno", "question": NO_SPEC_QUESTION, "context": text}, False


def _decision_of(data):
    """(decision, probability) read tolerantly from the assumed-v0 response shape."""
    body = data.get("decision") if isinstance(data.get("decision"), dict) else data
    decision = None
    for key in ("decision", "answer", "value", "choice", "score", "result", "label"):
        if key in body and not isinstance(body[key], dict):
            decision = body[key]
            break
    probability = None
    for key in ("probability", "confidence", "p", "calibrated_probability"):
        if isinstance(body.get(key), (int, float)):
            probability = float(body[key])
            break
    return decision, probability


def ask(prompt, model=None):
    """One typed decision from Jev. Returns (text, route) or (None, reason).

    The reply text names the question type, the decision, the calibrated probability and
    the model, so the row Jev's waker posts is a faithful record of a DECISION, not prose.
    """
    key = _key()
    if not key:
        return None, "no JEV_API_KEY in the environment or .env"
    question, had_spec = parse_spec(prompt)
    body = {"model": model or MODEL, "question": question}
    request = urllib.request.Request(
        SYSTEMONE, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return None, "systemone HTTP %s: %s" % (error.code, detail or error.reason)
    except Exception as error:  # noqa: BLE001 - the waker reports, never crashes
        return None, "systemone %s: %s" % (type(error).__name__, str(error)[:160])
    if not isinstance(data, dict):
        return None, "systemone answered a non-object"
    decision, probability = _decision_of(data)
    if decision is None:
        # The assumed v0 shape missed. Say so, with the keys only: the first live call
        # is allowed to teach us the real shape, not to be papered over.
        return None, ("systemone shape v0 mismatch; response keys: %s"
                      % ", ".join(sorted(str(k) for k in data)[:10]))
    parts = ["Jev decision (%s): %s" % (question["type"], decision)]
    if probability is not None:
        parts.append("calibrated probability %.3f" % probability)
    parts.append("model %s" % (model or MODEL))
    if question["type"] == "score":
        parts.append("range %d..%d" % (question["min"], question["max"]))
    if not had_spec:
        parts.append(USAGE_HINT)
    parts.append("Jev gives typed decisions with calibrated probabilities; it does not "
                 "write text, and this sentence is its adapter's, not its own.")
    return " | ".join(parts).replace("|", "/"), "systemone api-key"


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
