#!/usr/bin/env python3
"""Run the collision rules over a board dump and log what fired.

WHAT THIS IS FOR
  Lane arbitration - "who owns this dispatch, and who claimed it after" - is a
  question the board cannot answer today, and three surfaces taking one fix in
  six minutes on 2026-09-15 is what that costs. It is also a question that needs
  no language model: it is a join over claims by target and timestamp. Moving it
  to rules moves it off the expensive surface AND makes the answer inspectable,
  because CLIPS can say which facts it matched.

WHY CLIPSDOS AND NOT clipspy
  clipspy is not installed for Python 3.14 on this laptop and installing it is
  not mine to decide. CLIPSDOS.exe -f2 <file> was MEASURED to run a rule file
  non-interactively, fire rules, print to stdout and exit 0. So this uses the
  binary that is proven to work here, through a file, with no new dependency.
  CLIPSIDE.exe is the GUI and is not scriptable - it is not used.

WHAT IT WILL NOT DO
  It never writes to the Blackboard and takes no corrective action. A COLLISION
  finding cannot recall a duplicate that already dispatched, and the admission
  and fencing contract that would make acting on one safe does not exist yet.
  Output goes to a local JSONL log for review. That is the whole contract.

USAGE
  python governor.py --board <dump.json> [--log outcomes.jsonl] [--print]
  python governor.py --board <dump.json> --expect-collisions 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from board_facts import emit_facts, load_rows  # noqa: E402

DEFAULT_CLIPS = r"C:\Program Files\SSS\CLIPS 6.4.2\CLIPSDOS.exe"
RULES = HERE / "collision.clp"


def build_program(fact_lines):
    """The rules, then the facts, then reset/run/exit.

    Order matters and not for style: (reset) asserts deffacts, so the deffacts
    must already be defined when it runs, and (run) must follow (reset) or the
    agenda is empty and the program prints nothing while still exiting 0 -
    a silent pass that looks exactly like "no collisions".
    """
    parts = [RULES.read_text(encoding="utf-8"), ""]
    if fact_lines:
        parts.append("(deffacts board-state")
        parts.extend(fact_lines)
        parts.append(")")
    parts.extend(["", "(reset)", "(run)", "(exit)", ""])
    return "\n".join(parts)


def run_clips(program_text, clips_exe):
    """Run one CLIPS program from a temp file. Returns (stdout, stderr, rc)."""
    if not os.path.isfile(clips_exe):
        raise SystemExit(f"CLIPS binary not found: {clips_exe}")
    # delete=False then unlink by hand: on Windows the child cannot open a file
    # this process still holds, so the handle must be closed before CLIPS runs.
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".clp", delete=False, encoding="utf-8", newline="\n"
    )
    try:
        handle.write(program_text)
        handle.close()
        proc = subprocess.run(
            [clips_exe, "-f2", handle.name],
            capture_output=True, text=True, timeout=120,
        )
        return proc.stdout, proc.stderr, proc.returncode
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def parse_output(stdout):
    """Pull the three contract lines out of CLIPS' chatter.

    Anything that is not a contract line is kept as `noise` rather than
    discarded, because a CLIPS syntax error appears there and exit 0 does not
    tell you about it. A governor that silently reported zero collisions
    because its rule file failed to parse would be worse than no governor.
    """
    owners, collisions, unanswered, noise = [], [], [], []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("OWNER|"):
            _, target, tag, row, ts = line.split("|", 4)
            owners.append({"target": target, "tag": tag, "row_id": row, "ts": int(ts)})
        elif line.startswith("COLLISION|"):
            _, target, o_tag, o_row, c_tag, c_row, gap = line.split("|", 6)
            collisions.append({
                "target": target,
                "owner_tag": o_tag, "owner_row": o_row,
                "collider_tag": c_tag, "collider_row": c_row,
                "gap_seconds": int(gap),
            })
        elif line.startswith("UNANSWERED|"):
            _, bcb_id, phase, from_tag, row, quiet = line.split("|", 5)
            unanswered.append({
                "bcb_id": bcb_id, "phase": phase, "from_tag": from_tag,
                "row_id": row, "quiet_minutes": int(quiet),
            })
        else:
            noise.append(line)
    return owners, collisions, unanswered, noise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", required=True, help="bus action=read JSON dump")
    ap.add_argument("--log", default=str(HERE / "outcomes.jsonl"))
    ap.add_argument("--clips", default=DEFAULT_CLIPS)
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--keep-program", help="write the generated .clp here for inspection")
    ap.add_argument("--expect-collisions", type=int, default=None,
                    help="exit non-zero unless exactly this many collisions are found")
    # DISPLAY ONLY - the log always keeps every finding.
    #   answers= adoption was 0% / 37% / 73% across W36 / W37 / W38, so the
    #   oldest unanswered asks are substantially an adoption gap rather than
    #   ignored work. Capping the PRINTED list keeps the actionable rows visible
    #   without deleting the noisy ones from the record, which would be hiding
    #   evidence rather than ranking it.
    ap.add_argument("--max-age-days", type=float, default=None,
                    help="only PRINT unanswered asks quieter than this many days")
    args = ap.parse_args(argv)

    rows, refusals = load_rows(args.board)
    fact_lines, counts = emit_facts(rows)
    program = build_program(fact_lines)
    if args.keep_program:
        Path(args.keep_program).write_text(program, encoding="utf-8", newline="\n")

    stdout, stderr, rc = run_clips(program, args.clips)
    owners, collisions, unanswered, noise = parse_output(stdout)
    # Loudest first: an ask that has been quiet for a day outranks one quiet for
    # an hour, and a reviewer reads the top of a list, not the middle.
    unanswered.sort(key=lambda u: u["quiet_minutes"], reverse=True)

    outcome = {
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "board": os.path.abspath(args.board),
        "rows_parsed": len(rows),
        "rows_refused": len(refusals),
        "refusals": [{"index": i, "reason": r} for i, r in refusals],
        "facts": counts,
        "clips_rc": rc,
        "clips_stderr": stderr.strip(),
        "clips_unparsed_output": noise,
        "owners": owners,
        "collisions": collisions,
        "unanswered": unanswered,
    }
    with open(args.log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, sort_keys=True) + "\n")

    if args.show:
        print(json.dumps(outcome, indent=2, sort_keys=True))
    else:
        print(f"rows={len(rows)} refused={len(refusals)} "
              f"claims={counts['claim_facts']} asks={counts['ask_facts']} "
              f"answers={counts['answered_facts']} owners={len(owners)} "
              f"collisions={len(collisions)} unanswered={len(unanswered)} rc={rc}")
        for c in collisions:
            print(f"  COLLISION {c['target']}: {c['owner_tag']} then "
                  f"{c['collider_tag']} +{c['gap_seconds']}s")
        shown = unanswered
        if args.max_age_days is not None:
            cap = args.max_age_days * 24 * 60
            shown = [u for u in unanswered if u["quiet_minutes"] <= cap]
            hidden = len(unanswered) - len(shown)
            if hidden:
                print(f"  ({hidden} unanswered asks older than "
                      f"{args.max_age_days:g}d not printed; all are in the log)")
        for u in shown[:15]:
            hours = u["quiet_minutes"] / 60.0
            print(f"  UNANSWERED {u['bcb_id']} ({u['phase']} from "
                  f"{u['from_tag']}) quiet {hours:.1f}h")
        if len(shown) > 15:
            print(f"  ... and {len(shown) - 15} more in the log")

    if rc != 0:
        print(f"CLIPS exited {rc}; stderr: {stderr.strip()[:400]}", file=sys.stderr)
        return 2
    if args.expect_collisions is not None and len(collisions) != args.expect_collisions:
        print(f"EXPECTATION FAILED: {len(collisions)} collisions, "
              f"expected {args.expect_collisions}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
