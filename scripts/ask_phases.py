"""The ask-type phases (R-STATUS) and the default-Category rule they drive.

ASK_PHASES is the ONE set every writer, the reconciler and the lint rule share.
Before R-STATUS every write defaulted Category (column G) to "OPEN", including
replies and notes, which is why 14,156 of 21,227 rows showed OPEN while the
real open-ask backlog was 2,846 (docs/BOARD-STATUS-FRAMEWORK.md, "Why the
board looked all-open"). Rule 5: Category stays a free label; Status (the
reconciler's column M) is the only authoritative open/closed signal. Writers
only need to stop handing out a wrong default - they do not need to compute
Status themselves.
"""
import re

ASK_PHASES = frozenset({"ASK", "REQUEST", "DISPATCH", "REVIEW_REQUEST", "TASK"})

_PHASE_RE = re.compile(r"(?:^|\|)phase=([^|]*)")


def phase_of(payload):
    """The BCB phase= value out of a payload string, upper-cased, or ""."""
    m = _PHASE_RE.search(str(payload or ""))
    return m.group(1).strip().upper() if m else ""


def is_ask_phase(phase):
    return str(phase or "").strip().upper() in ASK_PHASES


def default_category(payload):
    """"OPEN" when the payload's own phase= is an ask-type phase, else "" (a
    free label, not a signal) - the Rule-5 default for a caller that did not
    pass an explicit Category."""
    return "OPEN" if is_ask_phase(phase_of(payload)) else ""


def default_category_for_phase(phase):
    """Same rule, for a caller that already has `phase` as its own variable
    (not embedded in a payload string yet) - avoids parsing it back out."""
    return "OPEN" if is_ask_phase(phase) else ""
