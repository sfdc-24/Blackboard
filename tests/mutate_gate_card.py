#!/usr/bin/env python3
"""Mutation testing for the same-SHA gate card. Is each guard load-bearing?

WHY THIS EXISTS AT ALL
  scripts/gate_card.py has one job: REFUSE while anything is open. Across
  the review of #316 its suite went from 30-odd tests to 137, and every
  round I reported "mutants die for this round's fixes" from a harness that
  lived in a scratchpad and that nobody but me could run. On 2026-10-03 that
  harness lied: two mutants deleted the same number of characters, so the
  mutated files had IDENTICAL SIZE, and Python validates a .pyc by mtime and
  size - one mutant's bytecode was served to the next and the run named the
  wrong mutant's victims. Fixing it immediately exposed a guard with no test
  behind it at all (the three required-check items were chained to each
  other, and every existing case had only one of them live).

  So the claim "every fix is checked by a mutant" belongs in the repository,
  runnable by a reviewer, and not in my working notes. A green count is not
  evidence; the repo's own CI comments say so, and this tool exists because
  a merge was handed over while a BLOCKER was open.

  The stakes are specific to a REFUSAL tool: a permissive bug prints a card
  over an open blocker, and an over-strict bug makes the gate unsatisfiable
  so it gets worked around by hand. Both show up here as a mutant that lives.

HOW
  scripts/ and tests/ are copied into a scratch tree per mutant and the edit
  is applied to the COPY, so a half-applied edit can never leave the real
  script mutated. Every mutant is anchor-checked: a pattern that matches
  nothing reports ANCHOR-MISS rather than a false survivor, because an edit
  that never applied and a guard that is genuinely untested look identical
  in the output otherwise. Each mutant also names the test that must fail,
  so a mutant caught by some unrelated assertion is visible as such.

  Bytecode is off (-B is in the CI invocation, and PYTHONDONTWRITEBYTECODE
  is set here) and each mutant gets a fresh tree. That is not belt and
  braces: it is the specific failure described above.

USAGE
  python3 -B tests/mutate_gate_card.py
  Exit 0 = every guard below is load-bearing. Exit 1 = at least one survivor
  or anchor-miss, both of which mean the suite is weaker than it looks.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SUITE = "tests/test_gate_card.py"
TARGETS = ("scripts", "tests")

GATE = "scripts/gate_card.py"

# (name, file, find, replace, why-it-should-break, the test that must fail)
MUTANTS = [
    (
        "agreeing-no-app-runs-refuse",
        GATE,
        "            if outcomes_agree(runs):\n",
        "            if False:\n",
        "two green runs of one name with no app recorded refuse although "
        "there is nothing to pick between them: an unsatisfiable gate",
        "test_runs_that_agree_need_no_lineage",
    ),
    (
        "agreeing-unorderable-attempts-refuse",
        GATE,
        "            if outcomes_agree(attempts):\n",
        "            if False:\n",
        "two green attempts with no start times refuse for the same reason",
        "test_runs_that_agree_need_no_lineage",
    ),
    (
        "outcomes-agree-always-true",
        GATE,
        '    return len({(str(r.get("status") or ""), str(r.get("conclusion") or ""))\n'
        "                for r in runs}) <= 1",
        "    return True",
        "runs that DISAGREE are treated as needing no lineage, so a failing "
        "run is read as superseded and the card prints",
        "test_an_unclear_name_refuses_even_when_it_is_not_required",
    ),
    (
        "ambiguity-filtered-to-required-names",
        GATE,
        "    ambiguous = list(unclear)",
        "    ambiguous = [(n, w) for n, w in unclear if n in required_checks]",
        "a non-required check whose current run cannot be established is "
        "dropped, and the card prints over a failing run",
        "test_an_unclear_name_refuses_even_when_it_is_not_required",
    ),
    (
        "one-fixed-reason-for-both-causes",
        GATE,
        "for name, why in sorted(ambiguous):",
        'for name, why in [(n, "no app is recorded") for n, _ in sorted(ambiguous)]:',
        "the refusal names a cause that does not apply, sending the reader "
        "to look for a missing app id when every run carries one",
        "test_the_refusal_names_the_reason_that_actually_applies",
    ),
    (
        "verdict-line-accepts-a-blockquote",
        GATE,
        '    r"^(?:\\*\\*|__)?(?:NO-GO|GO)',
        '    r"^>?\\s*(?:\\*\\*|__)?(?:NO-GO|GO)',
        "a QUOTED verdict inside an approval becomes a verdict, so quoting "
        "the previous NO-GO sinks the new GO",
        "test_a_quoted_no_go_sinks_bare_and_does_not_sink_blockquoted",
    ),
    (
        "lineage-back-to-app-and-name",
        GATE,
        '                (str(run.get("app_id")), workflow_run_id(run)), []).append(run)',
        '                (str(run.get("app_id")), ""), []).append(run)',
        "every GitHub Actions workflow shares one app id, so a failing check "
        "in a second workflow is discarded as a superseded attempt",
        "test_two_workflows_one_app_are_two_checks_by_workflow_run",
    ),
    (
        "group-on-check-suite-id",
        GATE,
        '                (str(run.get("app_id")), workflow_run_id(run)), []).append(run)',
        '                (str(run.get("app_id")), str(run.get("check_suite_id") or "")),'
        " []).append(run)",
        'the repair Cursor ruled against: "Re-run all jobs" creates a new '
        "suite, so a green re-run can never clear the old failure",
        "test_two_workflows_one_app_are_two_checks_by_workflow_run",
    ),
    (
        "details-url-ignored",
        GATE,
        '    for key in ("html_url", "details_url"):',
        '    for key in ("html_url",):',
        "a check run whose linked URL is not a job URL loses its lineage, "
        "though details_url carries it",
        "test_details_url_is_read_when_the_linked_url_carries_no_run",
    ),
    (
        "not-green-chained-to-missing",
        GATE,
        '    if not_green:\n        out.open.append(\n'
        '            "the required check(s) did not pass',
        '    elif not_green:\n        out.open.append(\n'
        '            "the required check(s) did not pass',
        "with two required names, one missing and one failing, the refusal "
        "stops naming the failing one",
        "test_a_missing_required_check_does_not_hide_a_failing_required_one",
    ),
    (
        "unlinkable-chained-to-the-others",
        GATE,
        '    if unlinkable:\n        out.open.append(\n'
        '            "the required check(s) " + ", ".join(unlinkable)',
        '    elif unlinkable:\n        out.open.append(\n'
        '            "the required check(s) " + ", ".join(unlinkable)',
        "a green required run with no URL is lost entirely - it appears "
        "nowhere in the aggregate line, so the handoff cannot be linked and "
        "nothing says so",
        "test_a_failing_required_check_does_not_hide_an_unlinkable_one",
    ),
    (
        "aggregate-chained-to-the-required-items",
        GATE,
        '    rest = [b for b in bad if b not in set(not_green)]\n    if ci == "success":',
        '    rest = [b for b in bad if b not in set(not_green)]\n'
        "    if missing or not_green or unlinkable:\n        pass\n"
        '    elif ci == "success":',
        "a missing required check consumes the branch and a failing check "
        "the same read SAW is never printed: the refusal lists the first "
        "open item instead of every one",
        "test_a_missing_required_check_does_not_hide_a_failing_one",
    ),
    (
        "aggregate-repeats-the-required-line",
        GATE,
        "    rest = [b for b in bad if b not in set(not_green)]",
        "    rest = list(bad)",
        "the same failing run is named twice, which is noise in the one "
        "output a reader has to act on",
        "test_the_aggregate_line_does_not_repeat_the_required_line",
    ),
    (
        "green-aggregate-closed-over-an-open-item",
        GATE,
        "        if not (missing or not_green or unlinkable):",
        "        if True:",
        '"CI on this SHA: success" is recorded as closed beside "required-ci '
        'never ran": a card contradicting its own refusal',
        "test_a_green_aggregate_is_not_closed_while_a_required_item_is_open",
    ),
    (
        "cursor-sink-case-sensitive",
        GATE,
        'VERDICT_TOKEN = re.compile(r"\\b(NO-GO|GO)\\b", re.IGNORECASE)',
        'VERDICT_TOKEN = re.compile(r"\\b(NO-GO|GO)\\b")',
        "a lowercase **no-go** opens a verdict line, yields no token and "
        "becomes SILENCE, so an older GO stays latest and the card prints "
        "over the withdrawal",
        "test_a_lowercase_withdrawal_sinks_an_older_go_end_to_end",
    ),
    (
        "codex-body-sink-case-sensitive",
        GATE,
        'NO_GO_ANYWHERE = re.compile(r"\\bNO-GO\\b", re.IGNORECASE)',
        'NO_GO_ANYWHERE = re.compile(r"\\bNO-GO\\b")',
        "the same hole in the Codex reader: a lowercase no-go cannot sink "
        "a receipt",
        "test_a_lowercase_no_go_sinks_a_codex_receipt",
    ),
    (
        "lift-widened-to-any-spelling",
        GATE,
        'if tokens == {"GO"} and shas == {named_sha} and "GO" in spellings:',
        'if tokens == {"GO"} and shas == {named_sha}:',
        "the asymmetry is the invariant: text may never LIFT a verdict to "
        "GO on a loose spelling, only sink one. A lowercase go line would "
        "approve, and 'go on <sha>' is prose as often as a verdict",
        "test_lifting_to_go_still_needs_the_exact_token",
    ),
]


def run_suite(tree: Path):
    """Run the gate-card suite inside a scratch tree. Returns (rc, text)."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-B", "-W", "error", SUITE],
        cwd=str(tree), capture_output=True, text=True, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-gate-card-"))
    for name in TARGETS:
        shutil.copytree(ROOT / name, tree / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    return tree


def main() -> int:
    print("BASELINE: the unmutated suite must pass, or no verdict below means "
          "anything.")
    tree = stage()
    try:
        rc, text = run_suite(tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if rc != 0:
        print("  BASELINE FAILED - fix the suite before trusting any mutant.")
        print(text[-2500:])
        return 1
    ran = [ln for ln in text.splitlines() if ln.startswith("Ran ")]
    print("  baseline ok  %s" % (ran[0] if ran else "?"))

    killed, survived, missed = [], [], []
    for name, relpath, find, repl, why, must_fail in MUTANTS:
        tree = stage()
        try:
            target = tree / relpath
            src = target.read_text(encoding="utf-8")
            if src.count(find) != 1:
                missed.append(name)
                print("\nANCHOR-MISS  %s" % name)
                print("  pattern appears %dx in %s - this mutant tested NOTHING."
                      % (src.count(find), relpath))
                print("  Fix the pattern; do not read this as a passing guard.")
                continue
            target.write_text(src.replace(find, repl, 1), encoding="utf-8")
            if find in target.read_text(encoding="utf-8"):
                missed.append(name)
                print("\nANCHOR-MISS  %s" % name)
                print("  the edit did not land on disk - this mutant tested NOTHING.")
                continue
            rc, text = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)

        named_test_failed = must_fail in text and rc != 0
        if rc == 0:
            survived.append(name)
            print("\nSURVIVED     %s" % name)
            print("  predicted breakage: %s" % why)
            print("  The suite passed with the guard REMOVED, so nothing in it")
            print("  constrains this behaviour. The guard is an untested claim.")
        elif not named_test_failed:
            killed.append(name)
            print("\nkilled       %s  (suite failed, but not via %s)"
                  % (name, must_fail))
            print("  counted as killed; the predicted test was not the one that "
                  "caught it.")
        else:
            killed.append(name)
            print("\nkilled       %s  via %s" % (name, must_fail))

    print("\n%d killed, %d survived, %d anchor-miss, of %d mutants"
          % (len(killed), len(survived), len(missed), len(MUTANTS)))
    if survived or missed:
        print("NOT CLEAN. A survivor is a guard no test can fail; an anchor-miss")
        print("is a mutant that never ran. Both mean the suite is weaker than its")
        print("green count suggests.")
        return 1
    print("Every guard above is load-bearing: removing it breaks the suite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
