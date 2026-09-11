#!/bin/bash
# Prove the synthesised voice actually reaches the source Zoom captures.
#
# WHAT THIS REPLACES
#   The first version used only `set -u`, left stale /tmp WAVs in place, and checked
#   no producer's exit status. Its Python helper printed UNREADABLE and returned 0. So
#   a run where the recorder never started, or where it measured yesterday's file,
#   reported success. The tool built to stop me claiming a false proof could produce
#   one. codex found it; the repair is that every step is now checked and the
#   measurement is an assertion.
#
# WHAT IT STILL DOES NOT PROVE, STATED PLAINLY
#   That a HUMAN HEARD IT. This proves the local chain: synthesis reaches the
#   PulseAudio source that Zoom is bound to. Whether Zoom encoded and transmitted it,
#   and whether the far end played it, needs a receiver-side confirmation. Do not
#   quote this script as evidence the meeting heard anything - quote it as evidence
#   the audio is where Zoom must read it from.
set -uo pipefail

SRC="${PRESENTER_SOURCE:-vmic_src}"
SINK="${PRESENTER_SINK:-vmic}"
WORK="$(mktemp -d /tmp/prove-voice-XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT

CONTROL="${WORK}/control.wav"
SPEECH="${WORK}/speech.wav"
VOICE="${WORK}/voice.wav"

fail() { echo "FAIL $*" >&2; exit 1; }

command -v parec     >/dev/null 2>&1 || fail "parec is missing"
command -v paplay    >/dev/null 2>&1 || fail "paplay is missing"
command -v espeak-ng >/dev/null 2>&1 || fail "espeak-ng is missing"

pactl list short sources 2>/dev/null | grep -q "[[:space:]]${SRC}[[:space:]]" \
    || fail "source ${SRC} does not exist - run presenter_voice_up.sh first"

# A fresh working directory per run, so a stale file cannot be measured by accident.
echo "=== control: record ${SRC} with nothing playing ==="
timeout 5 parec --device="${SRC}" --file-format=wav "${CONTROL}"
RC=$?
# timeout kills parec at the deadline, which is how it is meant to end: 124 is
# expected, anything else is the recorder failing.
[ "${RC}" -eq 0 ] || [ "${RC}" -eq 124 ] || fail "control recorder exited ${RC}"
[ -s "${CONTROL}" ] || fail "control recording is empty - the recorder produced nothing"

echo "=== synthesise ==="
espeak-ng -v en-gb -s 140 -p 45 -w "${VOICE}" \
    "Testing the presenter voice. One. Two. Three. Four. Five." 2>/dev/null \
    || fail "espeak-ng failed"
[ -s "${VOICE}" ] || fail "synthesis produced no audio"

echo "=== record ${SRC} while speaking into ${SINK} ==="
timeout 9 parec --device="${SRC}" --file-format=wav "${SPEECH}" &
REC=$!
sleep 1
paplay --device="${SINK}" "${VOICE}" || fail "paplay failed"
wait "${REC}"
RC=$?
[ "${RC}" -eq 0 ] || [ "${RC}" -eq 124 ] || fail "speech recorder exited ${RC}"
[ -s "${SPEECH}" ] || fail "speech recording is empty"

echo "=== measure: the numbers are the verdict ==="
python3 "$(dirname "$0")/measure_wav.py" --control "${CONTROL}" --speech "${SPEECH}"
