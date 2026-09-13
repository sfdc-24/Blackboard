#!/bin/bash
# Decide whether a suite's output actually represents a passing run.
#
# WHY THIS IS A FILE AND NOT THREE COPIES IN A WORKFLOW
#   walkthrough-tools.yml carried this check inline, twice, and BOTH copies
#   accepted `RESULT passed=0 failed=0` as success - a suite that asserted
#   nothing at all was indistinguishable from one that asserted everything.
#   Adding a third step would have made three copies of the same bug.
#
#   This rig has been here before. zoom_bound_count.sh exists because the same
#   count was written inline twice and was wrong both times, in opposite
#   directions, and the fix was to move it somewhere it could be driven by
#   fixtures. Same lesson, same remedy.
#
# WHAT IT REQUIRES, AND WHY EACH ONE
#   exactly one RESULT line   a suite that died early emits none, and one that
#                             loops emits several; both must be distinguishable
#                             from a clean run, and neither is a pass
#   failed=0                  the obvious half
#   passed >= 1               the half that was missing. Zero assertions is not
#                             a green run, it is a run that did not happen, and
#                             it is what a suite reports when its setup fails
#                             quietly or every check is skipped
#
# Found by chatgpt-codex-connector reviewing the walkthrough CI on PR83.
#
# Reads the suite's output on stdin so a caller never has to re-run the suite
# to check it, and names the suite in every message so a red CI step says which.
set -uo pipefail

LABEL="${1:-suite}"

output="$(cat)"

if [ -z "${output//[[:space:]]/}" ]; then
    echo "${LABEL}: produced NO output at all - refusing to call that a pass" >&2
    exit 1
fi

result_lines="$(printf '%s\n' "${output}" | grep -c '^RESULT passed=' || true)"
if [ "${result_lines}" -ne 1 ]; then
    echo "${LABEL}: expected exactly one RESULT line, got ${result_lines}." >&2
    echo "  none means the suite died before reporting; more than one means it" >&2
    echo "  ran twice or printed a RESULT it did not mean." >&2
    exit 1
fi

line="$(printf '%s\n' "${output}" | grep '^RESULT passed=' | head -1)"

# Anchored and fully specified: a trailing field this does not know about is a
# contract change, and silently ignoring it is how a guard stops guarding.
if ! printf '%s' "${line}" | grep -qE '^RESULT passed=[0-9]+ failed=[0-9]+( skipped=[0-9]+)?$'; then
    echo "${LABEL}: RESULT line is not in the expected shape: ${line}" >&2
    exit 1
fi

passed="$(printf '%s' "${line}" | sed -n 's/^RESULT passed=\([0-9]*\).*/\1/p')"
failed="$(printf '%s' "${line}" | sed -n 's/.* failed=\([0-9]*\).*/\1/p')"

if [ "${failed}" -ne 0 ]; then
    echo "${LABEL}: reported ${failed} failing assertions" >&2
    exit 1
fi

# THE ONE THAT WAS MISSING.
if [ "${passed}" -lt 1 ]; then
    echo "${LABEL}: reported ZERO passing assertions." >&2
    echo "  'passed=0 failed=0' is not a green suite - it is a suite that never" >&2
    echo "  ran. A setup step that fails quietly produces exactly this line." >&2
    exit 1
fi

echo "  ok ${LABEL}: ${passed} assertions, 0 failures"
