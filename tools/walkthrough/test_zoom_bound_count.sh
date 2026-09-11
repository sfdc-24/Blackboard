#!/bin/bash
# Regression test for the Zoom-binding check, driven by committed pactl fixtures.
#
# This exists because the same check has now been wrong twice, in opposite directions,
# and both versions shipped because neither was ever run against real pactl output:
#
#   v1  counted every source-output           -> passed with Zoom absent
#   v2  matched a NAME against a NUMERIC field -> failed with Zoom correctly bound
#
# The second was "verified" by grepping the script for its new variable name. That
# checks the text of a fix rather than its behaviour, which is exactly how a broken
# fix ships under a green test.
#
# The fixtures are real pactl shapes, including the detail that makes v1 wrong: a
# `parec` stream bound to the SAME source. A check that cannot tell Zoom from the
# recorder measuring it is not a check.
#
# Runs anywhere. No box, no Zoom, no audio hardware.
HERE="$(cd "$(dirname "$0")" && pwd)"
COUNTER="${HERE}/zoom_bound_count.sh"
FIX="${HERE}/fixtures"
pass=0; fail=0
check() {
    if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1 ($3)"
    else fail=$((fail+1)); echo "FAIL $1: expected '$3' got '$2'"; fi
}

echo "== Zoom bound to vmic_src: the case v2 got wrong =="
got=$(bash "${COUNTER}" vmic_src "${FIX}/pactl_zoom_bound.txt")
check "counts the Zoom stream bound to vmic_src" "$got" "1"

echo
echo "== and it does NOT count the recorder on the same source: the case v1 got wrong =="
# The bound fixture holds a parec stream on source 94 as well. If the count were 2,
# the check would be satisfied by the harness measuring itself.
check "parec on the same source is not counted as Zoom" "$got" "1"

echo
echo "== Zoom bound SOMEWHERE ELSE must not count =="
got=$(bash "${COUNTER}" vmic_src "${FIX}/pactl_zoom_elsewhere.txt")
check "Zoom on another source reports zero" "$got" "0"

echo
echo "== a source that does not exist reports zero rather than erroring =="
got=$(bash "${COUNTER}" no_such_source "${FIX}/pactl_zoom_bound.txt")
check "unknown source reports zero" "$got" "0"

echo
echo "== the name really is resolved to an index =="
# vmic.monitor is index 87 and carries the remap stream, not Zoom. If the parser were
# matching names as substrings, 'vmic' would smear across both and this would be wrong.
got=$(bash "${COUNTER}" vmic.monitor "${FIX}/pactl_zoom_bound.txt")
check "vmic.monitor has no Zoom stream" "$got" "0"

echo
echo "== usage errors are loud =="
bash "${COUNTER}" >/dev/null 2>&1
check "no argument exits non-zero" "$( [ $? -ne 0 ] && echo nonzero || echo zero )" "nonzero"

echo
echo "RESULT passed=$pass failed=$fail"
[ "$fail" -gt 0 ] && exit 1
exit 0
