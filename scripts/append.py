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

from bus import fetch, load_env, read_board

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


def main():
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
    conflicts = sorted(_conflicting_keys(payload))
    if conflicts:
        raise SystemExit(
            "BCB_PAYLOAD_AMBIGUOUS: these keys are written more than once with "
            "different values: " + ", ".join(conflicts) + ".\n"
            "BCB-1 has no escaping, so a '|' inside a value starts a new key as "
            "far as any reader is concerned. Rewrite the value without pipes "
            "(say 'v=1 then v=999' rather than 'v=1|v=999'). Refusing to append."
        )
    # tries=1 is deliberately explicit even though fetch() now enforces the same
    # rule for every append. The v1 bus does not dedup, and a googleusercontent
    # 404 on the redirect hop can be raised client-side AFTER the row has already
    # landed. On 2026-09-08 the old transport default replayed
    # WRK-vmccc-xray-blocker-20260908T1630Z onto the live board. Never let an
    # append retry itself; the read-back below is what closes the loop.
    try:
        body = fetch(
            env["BUS_URL"],
            {
                "action": "append",
                "secret": env["BUS_SECRET"],
                "title": TITLE,
                "sheetRow": row,
            },
            tries=1,
        )
        print("APPEND response:", body[:300])
    except Exception as exc:  # noqa: BLE001 - the transport failure is not the answer
        print("APPEND raised %s: %s" % (type(exc).__name__, exc))
        print("This does NOT mean the row is absent. Reading back to find out.")

    # read back — ok:true is not proof, and an exception is not proof of absence
    # (ISSUE 020 and its inverse). Reads are idempotent, so retry those freely.
    for attempt in range(3):
        try:
            obj = read_board(env)
        except Exception as exc:  # noqa: BLE001
            print("read-back attempt %d failed: %s" % (attempt + 1, exc))
            continue
        hits = [r for r in obj["rows"] if r and r[0] == spec["row_id"]]
        if len(hits) == 1:
            r = hits[0]
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
