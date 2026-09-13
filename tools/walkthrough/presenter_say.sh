#!/bin/bash
# Speak into the live Zoom meeting from the headless presenter box.
#
# WHY THIS EXISTS
#   The rig could share a screen but had no voice, so a walkthrough was a silent
#   slideshow with the narration happening somewhere the audience could not hear.
#   Mr Salam's words: "this is something that you will have to figure out how to
#   speak in zoom."
#
# HOW THE AUDIO ACTUALLY GETS THERE
#   The box has no sound hardware at all. The chain is:
#
#     espeak-ng  ->  sink 'vmic'  ->  vmic.monitor  ->  remap  ->  source 'vmic_src'
#                                                                        |
#                                                             Zoom captures this
#
#   Zoom will not offer a bare monitor as a microphone, which is why the remap step
#   exists - module-remap-source turns the monitor into something Zoom lists and
#   selects as a real input device (shown as SFDC24-VirtualMic).
#
# THE TRAP THAT COST ME A ROUND OF DEBUGGING
#   Zoom's SPEAKER must NOT point at 'vmic'. If it does, Zoom's own output re-enters
#   its own microphone and every other participant hears an echo of themselves. The
#   silent control proved it: with the speaker on vmic the "silence" recording had
#   peak 1239, and after moving Zoom's output to the separate 'zspk' sink the same
#   control read peak 0. Keep them separate.
#
# AND THE LESSON THAT MATTERS MOST HERE
#   'paplay returned 0' is not proof that anyone heard anything. The first test
#   returned 0 while Zoom had NO capture stream open and every word went into the
#   void. Proof is recording FROM the source Zoom captures and measuring the samples
#   against a silent control. scratchpad/prove_voice.sh does exactly that.
set -u

SINK="${PRESENTER_SINK:-vmic}"
VOICE="${PRESENTER_VOICE:-en-gb}"
SPEED="${PRESENTER_SPEED:-140}"
PITCH="${PRESENTER_PITCH:-45}"

if [ "$#" -lt 1 ]; then
    echo "usage: presenter_say.sh \"text to speak\"" >&2
    echo "       text may also be piped on stdin" >&2
    exit 2
fi

TEXT="$*"
if [ "${TEXT}" = "-" ]; then
    TEXT="$(cat)"
fi

if [ -z "${TEXT// /}" ]; then
    echo "refusing to speak an empty string" >&2
    exit 2
fi

WAV="$(mktemp /tmp/presenter-say-XXXXXX.wav)"
trap 'rm -f "${WAV}"' EXIT

espeak-ng -v "${VOICE}" -s "${SPEED}" -p "${PITCH}" -w "${WAV}" "${TEXT}" 2>/dev/null
BYTES=$(stat -c %s "${WAV}" 2>/dev/null || echo 0)
if [ "${BYTES}" -lt 1000 ]; then
    echo "synthesis produced ${BYTES} bytes - refusing to claim it spoke" >&2
    exit 1
fi

# IS *ZOOM* LISTENING, not "is anything listening".
#
# This counted ALL source-outputs, which is a check that passes for the wrong reason:
# prove_voice.sh's own parec creates a source-output, so the count reads 1 and the
# script reports success with Zoom absent entirely. A listener check satisfied by the
# test harness measuring itself is not a listener check.
#
# So: require a capture stream whose application really is the Zoom client AND whose
# source really is our virtual mic. Both halves matter - Zoom bound to some other
# device hears nothing from us either.
# THE COUNT COMES FROM A TESTED PARSER, NOT FROM AN AWK INVENTED HERE.
#
# My previous inline version matched the source NAME against each record, but pactl
# prints `Source: 94` - a numeric index. It matched nothing and reported zero for a
# correctly bound Zoom, so the script refused to speak. The version before that
# counted every source-output, which the test harness's own parec satisfied.
#
# Two opposite errors in the same check, neither ever run against real pactl output.
# zoom_bound_count.sh resolves the name to its index and is driven by committed
# fixtures in the regression test, so it is exercised without a live box.
#
# AND `|| echo 0` WAS ITSELF A THIRD WAY TO LIE.
#
# The counter is committed mode 100644, so executing it directly gives EACCES - and
# `|| echo 0` turned that permission error into "Zoom is not listening". A script that
# cannot run and a meeting nobody is in produced the identical answer. The test called
# `bash <file>` and so never touched this path at all.
#
# Invoked through `bash` explicitly, so the file mode cannot decide whether the check
# happens; and the exit status is kept, because 2 means "could not look" and must not
# be flattened into 0 meaning "looked and saw none".
SOURCE_NAME="${PRESENTER_SOURCE:-vmic_src}"
COUNTER="$(dirname "$0")/zoom_bound_count.sh"
if [ ! -r "${COUNTER}" ]; then
    echo "cannot read ${COUNTER} - refusing to guess whether Zoom is listening" >&2
    exit 1
fi
ZOOM_BOUND=$(bash "${COUNTER}" "${SOURCE_NAME}")
COUNTER_RC=$?
if [ "${COUNTER_RC}" -ne 0 ]; then
    echo "the binding check could not run (exit ${COUNTER_RC}). That is NOT the same as" >&2
    echo "'nobody is listening' - refusing to speak rather than report a guess." >&2
    exit 1
fi

paplay --device="${SINK}" "${WAV}"
RC=$?

echo "spoke_bytes=${BYTES} sink=${SINK} source=${SOURCE_NAME} paplay_rc=${RC} zoom_capture_streams=${ZOOM_BOUND}"
if [ "${ZOOM_BOUND}" -eq 0 ]; then
    echo "WARNING: no ZOOM capture stream is bound to ${SOURCE_NAME}, so the meeting" >&2
    echo "         heard NOTHING. Pick the microphone inside Zoom: the chevron beside" >&2
    echo "         the Audio button -> SFDC24-VirtualMic." >&2
    exit 1
fi

# Even with Zoom bound, this proves the audio reached Zoom's input - not that a human
# heard it. Say so rather than letting the exit code imply more than it knows.
echo "note: this confirms Zoom is reading ${SOURCE_NAME}; it is not receiver-side proof."
exit ${RC}
