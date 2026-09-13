#!/bin/bash
# Drive assert_suite_result.sh with the outputs it has to tell apart.
#
# The case that matters is `RESULT passed=0 failed=0`. Both inline copies of
# this check in walkthrough-tools.yml accepted it, so a suite that asserted
# nothing scored the same as one that asserted everything. Every other case
# here is a regression guard around the fix for that one.
set -uo pipefail

RIG="${1:?usage: test_assert_suite_result.sh <path to tools/walkthrough>}"
RIG="$(cd "${RIG}" && pwd)"
SUT="${RIG}/assert_suite_result.sh"
pass=0
fail=0

ok()  { pass=$((pass+1)); echo "  PASS  $1"; }
bad() { fail=$((fail+1)); echo "  FAIL  $1"; }

# Feed $2 on stdin; assert the exit status is $1 ("ok" = 0, "no" = non-zero).
expect() {
    local want="$1" name="$2" input="$3" rc
    printf '%s\n' "${input}" | bash "${SUT}" "${name}" >/dev/null 2>&1
    rc=$?
    if [ "${want}" = "ok" ]; then
        [ "${rc}" -eq 0 ] && ok "${name}" || bad "${name} (wanted accept, rc=${rc})"
    else
        [ "${rc}" -ne 0 ] && ok "${name}" || bad "${name} (wanted REFUSE, rc=0)"
    fi
}

echo "=== suite-result assertion, executed ==="
echo

expect ok "a real pass is accepted" \
    "  PASS  something
RESULT passed=13 failed=0"

# ---- the bug this file exists for ------------------------------------------
expect no "passed=0 failed=0 is REFUSED, not treated as green" \
    "RESULT passed=0 failed=0"

expect no "a reported failure is refused" \
    "RESULT passed=12 failed=1"

expect no "no RESULT line at all is refused (a suite that died early)" \
    "  PASS  something
  PASS  something else"

expect no "two RESULT lines are refused" \
    "RESULT passed=3 failed=0
RESULT passed=4 failed=0"

expect no "empty output is refused" ""

expect no "a malformed RESULT line is refused" \
    "RESULT passed=many failed=0"

# The ORDER suites append `skipped=N`, and refusing that shape would make this
# unusable for them - but only the documented shape is allowed, so an unknown
# trailing field is still a contract change and still refused.
expect ok "the skipped= form used by the ORDER suites is accepted" \
    "RESULT passed=482 failed=0 skipped=17"

expect no "an unknown trailing field is refused rather than ignored" \
    "RESULT passed=10 failed=0 flaky=3"

# A pass must still be a pass when the suite printed to stderr as well; the
# caller pipes combined output in and this must not be confused by noise.
expect ok "surrounding noise does not prevent a clean accept" \
    "warning: tput: unknown terminal
  PASS  a thing
RESULT passed=1 failed=0
"

echo
echo "RESULT passed=${pass} failed=${fail}"
[ "${fail}" -eq 0 ] || exit 1
exit 0
