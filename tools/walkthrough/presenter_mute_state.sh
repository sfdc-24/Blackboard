#!/bin/bash
# Is the Zoom client MUTED? Answer it, or refuse to answer. Never guess.
#
# WHY THIS EXISTS, and why it is NOT a pactl check
#   On 2026-09-12 the rig spoke into a live meeting and Mr Salam heard nothing.
#   Every instrument reported success:
#
#     spoke_bytes=657644 sink=vmic source=vmic_src paplay_rc=0 zoom_capture_streams=1
#
#   All of it true, and all of it useless. Zoom was reading the microphone and
#   discarding the audio at its own mute switch, which sits AFTER everything
#   being measured.
#
#   MEASURED, not assumed. With the client definitively muted - red slash on the
#   Audio button, on the participant thumbnail and on the sharing bar - PulseAudio
#   still reports the Zoom source-output as `Corked: no` and `Mute: no`, and
#   zoom_bound_count still returns 1. Toggled twice to be certain. Zoom's mute is
#   entirely internal: NOTHING in the audio graph can see it. So this reads the
#   only thing that knows, which is Zoom's own toolbar.
#
# THE SIGNAL, calibrated on a live client over two full toggle cycles
#     muted   : 108 strongly-red pixels in the Audio button region
#     unmuted :   0
#   A threshold of 20 sits in the middle of a gap of 108. A measurement with a
#   margin, not a pixel guess.
#
# ROOT CAPTURE ONLY - `import -window <id>` IS BANNED HERE
#   Every successful capture on this rig has used `import -window root`. An
#   attempt to grab the toolbar window directly wedged the X server for fourteen
#   minutes: the window argument was mis-quoted, import could not resolve it,
#   fell back to INTERACTIVE mode waiting for a mouse click that never came, and
#   held an X server GRAB the whole time. Every later xdotool blocked on connect,
#   and killing the import only made the parent loop spawn another.
#
#   Consequences encoded below: root capture, a hard timeout on every import, and
#   the toolbar must be ON SCREEN to be croppable - which is why
#   join_and_share.sh parks it along the bottom edge rather than off-screen.
#
# EXIT CODES, and the middle one is the entire point
#   0  OPEN     - not muted, safe to speak
#   1  MUTED    - speaking now produces silence at the far end
#   2  UNKNOWN  - could not determine
#
#   2 MUST NEVER be treated as 0. "I could not look" and "I looked and it is
#   fine" are different answers; collapsing them is the mistake that cost a
#   meeting.
set -uo pipefail
export DISPLAY="${DISPLAY:-:99}"

THRESHOLD="${PRESENTER_MUTE_THRESHOLD:-20}"
SHOT="$(mktemp /tmp/mute-XXXXXX.png)"
trap 'rm -f "$SHOT"' EXIT

tb=$(timeout 8 xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null | head -1)
if [ -z "$tb" ]; then
  echo "mute=UNKNOWN reason=no-as_toolbar - Zoom is not sharing, so its toolbar does not exist"
  exit 2
fi

X=""; Y=""; WIDTH=""; HEIGHT=""
eval "$(timeout 8 xdotool getwindowgeometry --shell "$tb" 2>/dev/null | grep -E '^(X|Y|WIDTH|HEIGHT)=')"
case "${WIDTH:-}${HEIGHT:-}" in ''|*[!0-9]*) echo "mute=UNKNOWN reason=no-geometry"; exit 2 ;; esac

# The toolbar has to be ON the root window to appear in a root capture. If it has
# been parked off-screen this check cannot see it - and says so, rather than
# reporting a comfortable zero.
if [ "$X" -lt 0 ] || [ "$Y" -lt 0 ]; then
  echo "mute=UNKNOWN reason=toolbar-off-screen at ${X},${Y} - move it on-screen or this cannot be read"
  exit 2
fi

# Hard timeout. An import that hangs holds an X server grab and takes the whole
# display with it; a mute check that wedges a live meeting is far worse than one
# that returns 2.
if ! timeout 10 import -window root "$SHOT" 2>/dev/null; then
  pkill -x import 2>/dev/null
  echo "mute=UNKNOWN reason=root-capture-failed-or-timed-out"
  exit 2
fi
[ -s "$SHOT" ] || { echo "mute=UNKNOWN reason=empty-capture"; exit 2; }

# The Audio button is the leftmost control. Crop generously around it, positioned
# from the toolbar's live geometry rather than remembered coordinates.
CX=$((X + 8)); CY=$((Y + 5))
RED=$(convert "$SHOT" -crop 70x60+${CX}+${CY} +repage txt:- 2>/dev/null \
      | awk -F'[(,)]' 'NR>1 {r=$3+0; g=$4+0; b=$5+0; if (r>120 && r-g>50 && r-b>50) n++} END {print n+0}')
case "$RED" in ''|*[!0-9]*) echo "mute=UNKNOWN reason=pixel-count-unreadable"; exit 2 ;; esac

if [ "$RED" -gt "$THRESHOLD" ]; then
  echo "mute=MUTED red_pixels=$RED threshold=$THRESHOLD toolbar=$tb at ${X},${Y}"
  exit 1
fi
echo "mute=OPEN red_pixels=$RED threshold=$THRESHOLD toolbar=$tb at ${X},${Y}"
exit 0
