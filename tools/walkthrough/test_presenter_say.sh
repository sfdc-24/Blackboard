#!/bin/bash
# Behavioral regression test for presenter_say.sh's fail-closed playback boundary.
#
# This runs the actual production script and its actual zoom_bound_count.sh helper.
# Only the external audio commands are replaced with deterministic PATH stubs, so a
# source-order grep cannot make a broken production call path look covered.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PRESENTER="${HERE}/presenter_say.sh"
ROOT="$(mktemp -d /tmp/test-presenter-say-XXXXXX)"
BIN="${ROOT}/bin"
PAPLAY_LOG="${ROOT}/paplay.log"
REAL_AWK="$(command -v awk)"
mkdir -p "${BIN}"
trap 'rm -rf "${ROOT}"' EXIT

pass=0
fail=0

pass_case() {
    pass=$((pass + 1))
    echo "PASS $1"
}

fail_case() {
    fail=$((fail + 1))
    echo "FAIL $1: $2"
}

check_equal() {
    if [ "$2" = "$3" ]; then
        pass_case "$1 ($3)"
    else
        fail_case "$1" "expected '$3' got '$2'"
    fi
}

check_nonzero() {
    if [ "$2" -ne 0 ]; then
        pass_case "$1 (exit $2)"
    else
        fail_case "$1" "expected non-zero exit, got 0"
    fi
}

check_contains() {
    if grep -Fq -- "$2" "$3"; then
        pass_case "$1"
    else
        fail_case "$1" "missing '$2' in $3"
    fi
}

cat > "${BIN}/espeak-ng" <<'STUB'
#!/bin/bash
set -u
wav=''
while [ "$#" -gt 0 ]; do
    if [ "$1" = '-w' ]; then
        shift
        wav="${1:-}"
    fi
    shift
done
[ -n "${wav}" ] || { echo 'missing -w output' >&2; exit 90; }
head -c 2048 /dev/zero > "${wav}"
STUB

cat > "${BIN}/pactl" <<'STUB'
#!/bin/bash
set -u
mode="${PRESENTER_TEST_MODE:-}"
if [ "${mode}" = 'error' ]; then
    echo 'controlled pactl failure' >&2
    exit 7
fi
case "$*" in
    'list short sources')
        printf '94\tvmic_src\tmodule-remap-source.c\ts16le 1ch 44100Hz\tRUNNING\n'
        ;;
    'list source-outputs')
        if [ "${mode}" = 'one' ]; then
            cat <<'OUTPUT'
Source Output #7
    Source: 94
    Properties:
        application.name = "Zoom Workplace"
        application.process.binary = "zoom"
OUTPUT
        fi
        ;;
    *)
        echo "unexpected pactl arguments: $*" >&2
        exit 8
        ;;
esac
STUB

cat > "${BIN}/awk" <<'STUB'
#!/bin/bash
set -u
: "${PRESENTER_REAL_AWK:?PRESENTER_REAL_AWK is required}"
case "${PRESENTER_TEST_MODE:-}" in
    empty|leading-zero|malformed|nine-digit|ten-digit|oversized)
        for arg in "$@"; do
            case "${arg}" in
                idx=*)
                    case "${PRESENTER_TEST_MODE}" in
                        empty) ;;
                        leading-zero) echo '01' ;;
                        malformed) echo 'not-a-count' ;;
                        nine-digit) echo '999999999' ;;
                        ten-digit) echo '1000000000' ;;
                        oversized) echo '9999999999999999999999999999999999999999' ;;
                    esac
                    exit 0
                    ;;
            esac
        done
        ;;
esac
exec "${PRESENTER_REAL_AWK}" "$@"
STUB

cat > "${BIN}/paplay" <<'STUB'
#!/bin/bash
set -u
: "${PAPLAY_LOG:?PAPLAY_LOG is required}"
printf '%s\n' "$*" >> "${PAPLAY_LOG}"
exit 0
STUB

chmod +x "${BIN}/espeak-ng" "${BIN}/pactl" "${BIN}/awk" "${BIN}/paplay"

run_case() {
    local mode="$1"
    CASE_OUT="${ROOT}/${mode}.out"
    CASE_ERR="${ROOT}/${mode}.err"
    : > "${PAPLAY_LOG}"

    if PATH="${BIN}:${PATH}" \
        PAPLAY_LOG="${PAPLAY_LOG}" \
        PRESENTER_REAL_AWK="${REAL_AWK}" \
        PRESENTER_TEST_MODE="${mode}" \
        bash "${PRESENTER}" "behavioral playback probe" \
        > "${CASE_OUT}" 2> "${CASE_ERR}"; then
        CASE_RC=0
    else
        CASE_RC=$?
    fi
    CASE_PAPLAY_CALLS="$(wc -l < "${PAPLAY_LOG}" | tr -d '[:space:]')"
}

echo '== zero Zoom bindings refuse before playback =='
run_case zero
check_nonzero 'zero bindings return non-zero' "${CASE_RC}"
check_equal 'zero bindings never invoke paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'zero bindings explain that Zoom would hear nothing' 'heard NOTHING' "${CASE_ERR}"

echo
echo '== a counter error is not flattened into zero and never plays =='
run_case error
check_nonzero 'counter error returns non-zero' "${CASE_RC}"
check_equal 'counter error never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'counter error is reported as an unavailable measurement' 'binding check could not run' "${CASE_ERR}"

echo
echo '== malformed successful counter output still fails closed =='
run_case malformed
check_nonzero 'malformed count returns non-zero' "${CASE_RC}"
check_equal 'malformed count never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'malformed count is reported as invalid' 'invalid count' "${CASE_ERR}"

echo
echo '== empty successful counter output still fails closed =='
run_case empty
check_nonzero 'empty count returns non-zero' "${CASE_RC}"
check_equal 'empty count never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'empty count is reported as invalid' 'invalid count' "${CASE_ERR}"

echo
echo '== leading-zero successful counter output is not canonical =='
run_case leading-zero
check_nonzero 'leading-zero count returns non-zero' "${CASE_RC}"
check_equal 'leading-zero count never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'leading-zero count is reported as invalid' 'invalid count' "${CASE_ERR}"

echo
echo '== oversized successful counter output cannot overflow into playback =='
run_case oversized
check_nonzero 'oversized count returns non-zero' "${CASE_RC}"
check_equal 'oversized count never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'oversized count is reported as invalid' 'invalid count' "${CASE_ERR}"

echo
echo '== the maximum nine-digit count remains valid =='
run_case nine-digit
check_equal 'nine-digit count returns success' "${CASE_RC}" '0'
check_equal 'nine-digit count invokes paplay exactly once' "${CASE_PAPLAY_CALLS}" '1'
check_contains 'nine-digit success reports the measured binding' 'zoom_capture_streams=999999999' "${CASE_OUT}"

echo
echo '== the first ten-digit count is refused =='
run_case ten-digit
check_nonzero 'ten-digit count returns non-zero' "${CASE_RC}"
check_equal 'ten-digit count never invokes paplay' "${CASE_PAPLAY_CALLS}" '0'
check_contains 'ten-digit count is reported as invalid' 'invalid count' "${CASE_ERR}"

echo
echo '== one Zoom binding permits exactly one playback =='
run_case one
check_equal 'one binding returns success' "${CASE_RC}" '0'
check_equal 'one binding invokes paplay exactly once' "${CASE_PAPLAY_CALLS}" '1'
check_contains 'success reports the measured Zoom binding' 'zoom_capture_streams=1' "${CASE_OUT}"
check_contains 'paplay receives the configured sink' '--device=vmic' "${PAPLAY_LOG}"

echo
echo "RESULT passed=${pass} failed=${fail}"
if [ "${fail}" -gt 0 ]; then
    exit 1
fi
exit 0
