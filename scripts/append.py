#!/usr/bin/env python3
"""Append one board row from a JSON file, then read it back.

Usage: python append.py rowfile.json
The JSON file is an object with keys: row_id, source_tag, target_surface,
action_type, payload, category, project_tag, gist, subgist.
Timestamp is stamped here in UTC.
"""
import datetime
import json
import re
import sys

import board_row
import signature
from bus import fetch, load_env, read_rows, with_sender_token

TITLE = "Blackboard - Alpha DB"

# The keys that carry authority. Kept in step with the same list in
# scripts/open_for_me.py: a key the reader will refuse to act on ambiguously is
# a key the writer must refuse to publish ambiguously.
AUTHORITY_KEYS = ("v", "id", "from", "to", "pr", "verdict", "hold",
                  "clears", "supersedes",
                  "exact_head", "reviewed_head", "head", "merged_head", "new_head")


def _conflicting_keys(payload):
    """Authority keys written more than once with DIFFERENT values."""
    bad = set()
    for key in AUTHORITY_KEYS:
        values = {m.strip() for m in re.findall(
            r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", payload)}
        if len(values) > 1:
            bad.add(key)
    return bad


# Repo names a pull-request number may follow. The owner prefix is optional:
# "Blackboard #484" and "sfdc-24/Blackboard #484" are the same citation.
_REPO_NAMES = ("sfdc24-site", "conference", "Blackboard")
_PR_NUMBER = re.compile(r"#(\d{1,6})\b")
_REPO_PREFIX = re.compile(
    r"(?<![A-Za-z0-9-])(?:sfdc-24/)?"
    r"(?:%s)\s*$" % "|".join(re.escape(name) for name in _REPO_NAMES),
    re.IGNORECASE)
# Work-order ids, measured against the ids this repo actually writes.
#   TASK ID: G30 and TASK ID: C77 are one letter and exactly two digits.
#   TASK ID: CL-RG1 is two or three letters, a hyphen, two or three letters, then digits.
# One-digit labels are not this shape. G2, R2, P0, T0 fill the docs and the
# rows as section numbers; a matcher that caught them would refuse ordinary
# status rows. Doctrine ids are letter, hyphen, digits (D-18, L-82, T-02) and
# linter codes run to three digits (E402). Neither is a work-order id.
# A row id is a longer hyphenated token (GROK-ACK-C77-...-20261112T1647Z,
# CODEX-RESULT-RV-BB478-20261108T0146Z). The hyphen is part of that token, so
# the C77 inside it is not a bare work-order id. id=, answers= and req= values
# are those row ids even when the value is short. A hex SHA (7 or more hex
# characters) and a timestamp are not work-order ids either.
_WORK_ORDER = re.compile(
    r"(?<![A-Za-z0-9-])([A-Z]\d{2}|[A-Z]{2,3}-[A-Z]{2,3}\d{1,4})(?![A-Za-z0-9-])")
_TASK_PREFIX = re.compile(r"TASK ID:\s*$")
_ID_FIELD = re.compile(r"(?:^|\|)(?:id|answers|req)=([^|]*)", re.IGNORECASE)
_ID_CHAR = re.compile(r"[A-Za-z0-9._-]")
_SHA = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{8}T\d{4,6}Z")


def _exempt_spans(payload):
    """id=, answers= and req= values. A row id lives there, whatever shape it has."""
    return [(m.start(1), m.end(1)) for m in _ID_FIELD.finditer(payload or "")]


def _inside(spans, start, end):
    return any(s <= start and end <= e for s, e in spans)


def _enclosing_token(payload, start, end):
    """The identifier token that contains [start, end), hyphen included."""
    i, j = start, end
    while i > 0 and _ID_CHAR.fullmatch(payload[i - 1]):
        i -= 1
    while j < len(payload) and _ID_CHAR.fullmatch(payload[j]):
        j += 1
    return payload[i:j], i, j


def _row_id_token(token):
    """A row id, a hex SHA, or a timestamp. Not a work-order id and not a PR number."""
    if not token:
        return False
    if _SHA.fullmatch(token):
        return True
    if _TIMESTAMP.search(token):
        return True
    # GROK-ACK-C77-RESULT-20261112T1647Z and the same shape without a clock:
    # several segments, longer than CL-RG1. The clock case is already caught above.
    parts = [p for p in token.split("-") if p]
    return len(parts) >= 3 and len(token) >= 12


def _citation_problems(payload):
    """Bare PR numbers and bare work-order ids. Empty means the payload may be appended.

    Each string names the offending token. Checked on the original payload, before fit()
    shortens it, for the same reason as _conflicting_keys: a violation past the summary
    cutoff would otherwise ride out in the spill while the posted row looked clean.
    """
    payload = payload or ""
    exempt = _exempt_spans(payload)
    problems = []
    seen = set()

    def add(code, token, detail):
        key = (code, token)
        if key in seen:
            return
        seen.add(key)
        problems.append("%s: %s" % (code, detail))

    for match in _PR_NUMBER.finditer(payload):
        if _inside(exempt, match.start(), match.end()):
            continue
        # The '#' is the citation, not part of a row id. "Blackboard#484" has the
        # repo name immediately before it; "notes#484" does not. Both are decided
        # by the prefix, so a longer token on the left must not hide the number.
        if _REPO_PREFIX.search(payload[:match.start()]):
            continue
        shown = match.group(0)
        add("PR_REFERENCE_BARE", shown,
            "'%s' names a pull request without its repository. Write the repo "
            "name then the number (conference %s, Blackboard %s, sfdc24-site %s)."
            % (shown, shown, shown, shown))

    for match in _WORK_ORDER.finditer(payload):
        if _inside(exempt, match.start(), match.end()):
            continue
        token, i, j = _enclosing_token(payload, match.start(), match.end())
        if i < match.start() or j > match.end() or _row_id_token(token):
            continue
        if _TASK_PREFIX.search(payload[:match.start()]):
            continue
        shown = match.group(1)
        add("WORK_ORDER_BARE", shown,
            "'%s' is a work-order id without the prefix 'TASK ID:'. "
            "Write 'TASK ID: %s'." % (shown, shown))
    return problems


def main(require_sender_identity=False):
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python scripts/append.py <row.json>\n"
            "The JSON object needs: row_id, source_tag, payload, and optionally\n"
            "target_surface, action_type, category, project_tag, gist, subgist."
        )
    env = load_env()
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    if not isinstance(spec, dict):
        raise SystemExit(f"{sys.argv[1]} must contain one JSON object")
    for required in ("row_id", "source_tag", "payload"):
        if not isinstance(spec.get(required), str) or not spec[required].strip():
            raise SystemExit(f"{sys.argv[1]} needs non-empty string field: {required}")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = [
        spec["row_id"],
        ts,
        spec["source_tag"],
        spec.get("target_surface", "ALL"),
        spec.get("action_type", "APPEND"),
        spec["payload"],
        spec.get("category", "OPEN"),
        spec.get("project_tag", ""),
        spec.get("gist", ""),
        spec.get("subgist", ""),
    ]

    # REFUSE AN AMBIGUOUS PAYLOAD BEFORE IT REACHES THE BOARD.
    #
    # BCB-1 is pipe-delimited with no escaping, so a `|` inside a VALUE is
    # indistinguishable from the start of a new key. Writing prose such as
    # "duplicate v=1|v=999 parsing" into a semantics field therefore publishes a
    # row that declares v=1 to a first-value reader and v=999 to a last-value
    # one. Two readers disagreeing about a row that can lift a safety hold is
    # the whole problem, and intent does not enter into it.
    #
    # Found by running the new reader against the live board: five rows were
    # flagged and THREE OF THEM WERE MINE, written while describing this very
    # defect. The reader was right; the writer was wrong. Fail here, at the
    # writer, the way scripts/bus.ps1 fails on a malformed timestamp - a row
    # already on an append-only board cannot be taken back.
    payload = spec["payload"]
    if not payload.startswith("BCB|"):
        raise SystemExit("payload must be a BCB envelope starting with 'BCB|'")
    # The ambiguity guard reads the ORIGINAL payload, before fit() replaces the tail with a summary.
    # A conflicting authority key past the summary cutoff would otherwise ride out inside the spill
    # and the posted row would look clean. Refusing here also means a rejected row never spills.
    conflicts = sorted(_conflicting_keys(payload))
    if conflicts:
        raise SystemExit(
            "BCB_PAYLOAD_AMBIGUOUS: these keys are written more than once with "
            "different values: " + ", ".join(conflicts) + ".\n"
            "BCB-1 has no escaping, so a '|' inside a value starts a new key as "
            "far as any reader is concerned. Rewrite the value without pipes "
            "(say 'v=1 then v=999' rather than 'v=1|v=999'). Refusing to append."
        )
    # A pull-request number is the repo name then the number (conference #428,
    # Blackboard #484, sfdc24-site #391). A work-order id carries the prefix
    # "TASK ID:" (TASK ID: G30, TASK ID: C77). One poster already writes that
    # way; the others reach this function, so the refusal has to live here.
    # Same rule as the ambiguity guard: the original payload, before fit().
    citations = _citation_problems(payload)
    if citations:
        raise SystemExit(
            "CITATION_REFUSED: refusing to append.\n  - " + "\n  - ".join(citations))
    # WHO WROTE THIS, AND HOW DID IT GET HERE.
    #
    # Mr Salam, 2026-10-06: "signs should reflect who its from; claude mobile or claude code cli
    # makes a difference, also good to know who relayed the message, direct or by aya".
    #
    # Checked at the WRITER for the same reason the ambiguity check is: the board is append-only and
    # a row that misattributes itself cannot be taken back. See scripts/signature.py for the three
    # checks and for what this deliberately does NOT claim - it makes a signature unambiguous and
    # traceable, never true, because this board has no authenticated sender.
    #
    # NOTE ON MERGE ORDER: this refuses every row if scripts/agent_roster.json is missing, which is
    # correct - a guard that cannot run has not passed - but it means append.py and the roster must
    # land together.
    signature_problems = signature.check(spec.get("source_tag"), payload)
    if signature_problems:
        raise SystemExit(
            "ROW_ATTRIBUTION_UNCLEAR: refusing to append.\n  - "
            + "\n  - ".join(signature_problems))
    # NEVER TRIM SILENTLY (scripts/board_row.py). write=False only names the spill object. The
    # bytes go out in commit(), after the append below, so the store is not on the post.
    spill = board_row.store_spill()
    try:
        payload, fitted = board_row.fit("", payload, key=spec["row_id"], spill=spill, write=False)
    except board_row.RowRejected as exc:
        raise SystemExit(str(exc))
    if fitted["truncated"]:
        print("LONG PAYLOAD: %d chars -> truncated=1 (not stored)" % fitted["chars"])
    row[5] = payload
    # tries=1 is deliberately explicit even though fetch() now enforces the same
    # rule for every append. The v1 bus does not dedup, and a googleusercontent
    # 404 on the redirect hop can be raised client-side AFTER the row has already
    # landed. On 2026-09-08 the old transport default replayed
    # WRK-vmccc-xray-blocker-20260908T1630Z onto the live board. Never let an
    # append retry itself; the read-back below is what closes the loop.
    # A reconciler receipt must carry the workload's own existing metadata
    # identity. This opt-in is an in-process argument, NOT a field supplied in the
    # row file. Missing identity stops before append; never persist/log its token.
    outbound = {"action": "append", "secret": env["BUS_SECRET"], "title": TITLE, "sheetRow": row}
    if require_sender_identity:
        outbound = with_sender_token(outbound, require_metadata=True)
    try:
        body = fetch(env["BUS_URL"], outbound, tries=1)
        if require_sender_identity:
            print("APPEND response received (authenticated request; body not logged)")
        else:
            print("APPEND response:", body[:300])
    except Exception as exc:  # noqa: BLE001 - the transport failure is not the answer
        # An authenticated transport error/response may echo the credential sent.
        print("APPEND raised %s" % type(exc).__name__ if require_sender_identity
              else "APPEND raised %s: %s" % (type(exc).__name__, exc))
        print("This does NOT mean the row is absent. Reading back to find out.")

    if fitted.get("deferred"):
        try:
            board_row.commit(fitted, spill)
            print("LONG PAYLOAD: %d chars stored at %s" % (fitted["chars"], fitted["pointer"]))
        except Exception as exc:  # noqa: BLE001
            print("SPILL_UNCONFIRMED: %s: %s" % (type(exc).__name__, exc))
            print("The row is already posted. A later commit of the same text reuses the object.")

    # read back — ok:true is not proof, and an exception is not proof of absence
    # (ISSUE 020 and its inverse). Reads are idempotent, so retry those freely.
    #
    # THE READ-BACK IS FILTERED, AND THAT IS A CORRECTNESS FIX, NOT A SPEED TWEAK.
    # This loop used to call read_board(), pulling the whole sheet up to three
    # times. On 2026-09-19 the board was 2951 rows / 4.3 MB, the Azure lane took
    # over 120 s per unfiltered read, and appending row
    # VMCCC-SFDC-LEADS-UI-20260919T0750Z died on a timeout *inside this loop* —
    # AFTER the row had already landed. The operator therefore saw a failure for
    # a successful append, which is precisely the state that tempts a blind
    # re-run, and a blind re-run is how a duplicate reaches an append-only
    # board. A verification step slow enough to time out is a verification step
    # that produces the bug it exists to prevent.
    #
    # match= narrows the transport only. It is a case-insensitive substring over
    # the WHOLE row, so a later row that merely QUOTES this row_id in its payload
    # comes back too — measured at 3 hits for one id on 2026-09-19. The exact
    # Row_ID comparison below stays the authority and the duplicate check keeps
    # its original meaning; counting match hits instead would invent duplicates
    # and exit 2 on a healthy append.
    for attempt in range(3):
        try:
            obj = read_rows(env, match=spec["row_id"])
        except Exception as exc:  # noqa: BLE001
            print("read-back attempt %d failed: %s" % (attempt + 1, exc))
            continue
        hits = [r for r in obj["rows"] if r and r[0] == spec["row_id"]]
        if len(hits) == 1:
            r = hits[0]
            if len(r) < 10 or any(str(r[i] or "") != str(row[i] or "") for i in range(10) if i != 1):
                raise SystemExit("READ-BACK CONFLICT: row id exists with different content; do not retry")
            if require_sender_identity and (len(r) <= 11 or r[11] !=
                    "sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com"):
                raise SystemExit("READ-BACK UNAUTHENTICATED: reconciler receipt has no verified server stamp")
            print("READ-BACK OK: row present exactly once, %d cells" % len(r))
            print("  ts=%s from=%s cat=%s tag=%s" % (r[1], r[2], r[6], r[7]))
            print("  payload starts: %s" % str(r[5])[:80])
            return
        if len(hits) > 1:
            print("READ-BACK: DUPLICATE — %d copies of %s are on the board." % (len(hits), spec["row_id"]))
            print("  Do NOT re-run. Post a correction row naming the duplicate.")
            sys.exit(2)
        print("read-back attempt %d: row_id not found yet" % (attempt + 1))
    print("READ-BACK FAILED: row_id absent after 3 reads. Check the board by hand")
    print("before re-running — a blind re-run is how duplicates happen.")
    sys.exit(1)


if __name__ == "__main__":
    main()
