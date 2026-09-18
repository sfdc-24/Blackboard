#!/bin/bash
# Play a recording into the live Zoom meeting. One job, and every guard kept.
#
# WHY THIS FILE EXISTS AT ALL
#   It is presenter_say.sh with the synthesiser removed, and it exists because
#   deleting presenter_say.sh on 2026-09-18 took the voice down with it. That
#   script was described to the product lead as "the espeak path", and the
#   ruling to delete it was correct on those facts. The facts were incomplete:
#   roughly ninety-five percent of that file was PLAYBACK and the guards around
#   it, and only the small else-branch was espeak. say_in_zoom.ps1 - the good
#   nova path - depended on the part that got deleted.
#
#   So the split is now honest. This plays. Nothing here synthesises anything,
#   which means espeak cannot come back through this door.
#
# HOW THE AUDIO ACTUALLY GETS THERE
#   The box has no sound hardware at all. The chain is:
#
#     WAV -> paplay -> sink 'vmic' -> vmic.monitor -> remap -> source 'vmic_src'
#                                                                     |
#                                                          Zoom captures this
#
#   Zoom will not offer a bare monitor as a microphone, which is why the remap
#   step exists - module-remap-source turns the monitor into something Zoom
#   lists and selects as a real input device (shown as SFDC24-VirtualMic).
#
# THE TRAP THAT COST A ROUND OF DEBUGGING
#   Zoom's SPEAKER must NOT point at 'vmic'. If it does, Zoom's own output
#   re-enters its own microphone and every other participant hears an echo of
#   themselves. The silent control proved it: with the speaker on vmic the
#   "silence" recording had peak 1239, and after moving Zoom's output to the
#   separate 'zspk' sink the same control read peak 0. Keep them separate.
#
# AND THE LESSON THAT MATTERS MOST
#   'paplay returned 0' is not proof that anyone heard anything. The first test
#   returned 0 while Zoom had NO capture stream open and every word went into
#   the void. This reports bytes and bindings, and says plainly what it does not
#   know.
#
# THE KEY STAYS ON THE LAPTOP. Synthesis happens there and only a WAV is shipped.
# A WAV is not a credential; an API key is, and this box is disposable.
#
# USAGE
#   PRESENTER_WAV=/tmp/x.wav presenter_play.sh
#   presenter_play.sh /tmp/x.wav
set -u

SINK="${PRESENTER_SINK:-vmic}"

# Accept the recording either way. say_in_zoom.ps1 has used the environment
# variable, and a bare path is the obvious thing for a human to type. Taking
# both costs nothing and removes a class of "worked by hand, failed in the
# script" confusion.
WAV="${PRESENTER_WAV:-}"
if [ -z "${WAV}" ] && [ "$#" -ge 1 ]; then
    WAV="$1"
fi

if [ -z "${WAV}" ]; then
    echo "usage: presenter_play.sh /path/to.wav   (or set PRESENTER_WAV)" >&2
    echo "       this script PLAYS a recording; it does not synthesise one." >&2
    exit 2
fi

if [ ! -f "${WAV}" ]; then
    echo "no such recording: ${WAV}" >&2
    exit 2
fi

# RIFF, or it is not a WAV. A truncated or half-shipped download makes paplay
# play nothing and return 0 - a silent success, which is the exact shape of
# failure this rig has already been bitten by twice.
if [ "$(head -c 4 "${WAV}" 2>/dev/null)" != "RIFF" ]; then
    echo "${WAV} does not begin with RIFF - that is not a WAV, refusing" >&2
    exit 1
fi

BYTES=$(stat -c %s "${WAV}" 2>/dev/null || echo 0)
if [ "${BYTES}" -lt 1000 ]; then
    echo "recording is ${BYTES} bytes - refusing to claim it spoke" >&2
    exit 1
fi

# IS *ZOOM* LISTENING, not "is anything listening".
#
# An earlier version counted ALL source-outputs, which passes for the wrong
# reason: the test harness's own parec creates a source-output, so the count
# reads 1 and the script reports success with Zoom absent entirely. A listener
# check satisfied by the test measuring itself is not a listener check.
#
# THE COUNT COMES FROM A TESTED PARSER, NOT FROM AN AWK INVENTED HERE. One
# earlier inline version matched the source NAME against each record, but pactl
# prints `Source: 94`, a numeric index; it matched nothing and reported zero for
# a correctly bound Zoom. Two opposite errors in the same check, neither ever
# run against real pactl output. zoom_bound_count.sh resolves the name to its
# index and is driven by committed fixtures.
#
# AND `|| echo 0` WAS ITSELF A THIRD WAY TO LIE: the counter is mode 100644, so
# executing it directly gives EACCES, and `|| echo 0` turned that permission
# error into "Zoom is not listening". Invoked through `bash` explicitly so the
# file mode cannot decide whether the check happens, and the exit status is
# kept, because 2 means "could not look" and must not become 0.
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

# IS THE CLIENT MUTED? Asked BEFORE playing, because speaking while muted burns
# the audio and writes a spoke_bytes line that reads exactly like success.
#
# This is the check that was missing on 2026-09-12, when the rig spoke into a
# live meeting with paplay_rc=0, zoom_capture_streams=1, and a muted client. The
# binding check above is necessary and was never sufficient: Zoom's mute sits
# AFTER the point it measures, and nothing in PulseAudio can see it - verified
# by toggling a live client twice while pactl reported Corked:no Mute:no.
MUTE="$(dirname "$0")/presenter_mute_state.sh"
if [ -r "${MUTE}" ]; then
    MUTE_OUT=$(bash "${MUTE}" 2>&1)
    MUTE_RC=$?
    case "${MUTE_RC}" in
      1)
        echo "REFUSING TO SPEAK - the Zoom client is MUTED." >&2
        echo "  ${MUTE_OUT}" >&2
        echo "  Speaking now would consume the audio and report success. Unmute in Zoom" >&2
        echo "  (the Audio button, or alt+a) and run this again." >&2
        exit 1
        ;;
      0) : ;;   # open, carry on
      *)
        # NOT a refusal. as_toolbar only exists while sharing, and speaking
        # without a share is legitimate. But say plainly that the guard did not
        # run, rather than letting silence imply it passed.
        echo "WARNING: could not determine mute state, so this is unguarded:" >&2
        echo "  ${MUTE_OUT}" >&2
        ;;
    esac
else
    echo "WARNING: ${MUTE} not present - speaking without a mute guard" >&2
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

# Even with Zoom bound, this proves the audio reached Zoom's input - not that a
# human heard it. Say so rather than letting the exit code imply more.
echo "note: this confirms Zoom is reading ${SOURCE_NAME}; it is not receiver-side proof."
exit ${RC}
