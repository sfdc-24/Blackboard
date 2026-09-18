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
#   Consequence: the toolbar must be ON SCREEN to be croppable.
#
# THE PEEK, added 2026-09-18 — WHY THIS FILE HAD TO CHANGE
#   grok-bot's overlay_repark.sh now holds as_toolbar off-capture at -3000,-3000
#   every 2 seconds, because a Zoom toolbar sitting on top of a client demo is
#   the entire impression. That is the right call for the viewer and it makes
#   this check impossible: an off-screen window is not in a root capture.
#
#   So this raises a flag file, waits for the repark loop to co-operate and put
#   the toolbar back at 0,0, takes its reading, and lowers the flag again. The
#   flag is a FILE and not an environment variable on purpose: the loop reads its
#   environment once at start, so an exported variable can never reach a loop
#   that is already running.
#
#   If anything here fails, the flag is still cleared - an EXIT trap guarantees
#   it. A stuck flag would leave the Zoom toolbar parked over the shared screen
#   for the rest of the meeting, which is the exact defect we just removed.
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
PEEK_FLAG="${OVERLAY_PEEK_FLAG:-/tmp/overlay_mute_peek}"
PEEK_WAIT="${PRESENTER_PEEK_WAIT:-6}"
SHOT="$(mktemp /tmp/mute-XXXXXX.png)"

# The flag must come down whatever happens below, including a failed capture or
# an interrupt. Leaving it up re-parks Zoom's toolbar over the shared screen.
cleanup() { rm -f "$SHOT"; rm -f "$PEEK_FLAG"; }
trap cleanup EXIT INT TERM

tb=$(timeout 8 xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null | head -1)
if [ -z "$tb" ]; then
  echo "mute=UNKNOWN reason=no-as_toolbar - Zoom is not sharing, so its toolbar does not exist"
  exit 2
fi

read_geometry() {
  X=""; Y=""; WIDTH=""; HEIGHT=""
  eval "$(timeout 8 xdotool getwindowgeometry --shell "$tb" 2>/dev/null | grep -E '^(X|Y|WIDTH|HEIGHT)=')"
}

read_geometry
case "${WIDTH:-}${HEIGHT:-}" in ''|*[!0-9]*) echo "mute=UNKNOWN reason=no-geometry"; exit 2 ;; esac

# ---- raise the toolbar if the repark loop is holding it off-capture ----------
PEEKED=0
if [ "${X:-0}" -lt 0 ] || [ "${Y:-0}" -lt 0 ]; then
  touch "$PEEK_FLAG" 2>/dev/null
  PEEKED=1
  # Nudge it ourselves too, so a single reading still works when the repark loop
  # is not running at all. Belt and braces, and cheap.
  xdotool windowmove "$tb" 0 0 2>/dev/null || true
  waited=0
  while [ "$waited" -lt "$PEEK_WAIT" ]; do
    sleep 1
    waited=$((waited + 1))
    read_geometry
    case "${X:-}" in ''|*[!0-9-]*) continue ;; esac
    if [ "${X:-0}" -ge 0 ] && [ "${Y:-0}" -ge 0 ]; then break; fi
  done
fi

if [ "${X:-0}" -lt 0 ] || [ "${Y:-0}" -lt 0 ]; then
  echo "mute=UNKNOWN reason=toolbar-still-off-screen at ${X},${Y} after ${PEEK_WAIT}s peek"
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
  echo "mute=MUTED red_pixels=$RED threshold=$THRESHOLD toolbar=$tb at ${X},${Y} peeked=$PEEKED"
  exit 1
fi
echo "mute=OPEN red_pixels=$RED threshold=$THRESHOLD toolbar=$tb at ${X},${Y} peeked=$PEEKED"
exit 0
