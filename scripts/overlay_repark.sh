#!/bin/bash
# overlay_repark.sh — durable Zoom share black-box fix (grok-bot product lead 2026-09-18).
# Zoom recreates as_toolbar / annotate_toolbar / float video after every join+share.
#
# WHY A LOOP AND NOT A ONE-SHOT
#   Measured on 2026-09-18: join_and_share.sh moved zoom_linux_float_video_window
#   off-capture at 08:22Z, and by 08:41Z Zoom had put it back at 0,0 over the
#   shared page with nobody touching anything. A one-shot park is correct for
#   about twenty minutes. Only a loop holds.
#
# THE PEEK IS A FLAG FILE, NOT AN ENVIRONMENT VARIABLE — AND THAT IS A FIX
#   The original design read OVERLAY_MUTE_PEEK from the environment. A loop reads
#   its environment ONCE, at start, so exporting that variable later has no
#   effect on a loop that is already running; the peek could never fire in
#   production. It is now a flag file, which a separate process can create and
#   remove while this loop is mid-flight.
#
#   This matters because parking as_toolbar off-capture BREAKS the mute guard on
#   purpose. presenter_mute_state.sh reads Zoom's own toolbar because Zoom's mute
#   is internal: with the client definitively muted, PulseAudio still reports
#   Corked no and Mute no, and zoom_bound_count still returns 1. The toolbar is
#   the only thing that knows. So the guard raises this flag, reads, and lowers
#   it — and this loop cooperates instead of yanking the toolbar back mid-capture.
#
# The env var is still honoured, so nothing that already sets it breaks.
set -uo pipefail
export DISPLAY="${DISPLAY:-:99}"
INTERVAL="${OVERLAY_REPARK_SEC:-2}"
PEEK_FLAG="${OVERLAY_PEEK_FLAG:-/tmp/overlay_mute_peek}"
OFF_X=-3000
OFF_Y=-3000

park_once() {
  for n in annotate_toolbar zoom_linux_float_video_window; do
    for w in $(xdotool search --onlyvisible --name "^${n}$" 2>/dev/null); do
      xdotool windowmove "$w" "$OFF_X" "$OFF_Y" 2>/dev/null || true
    done
  done

  # as_toolbar is the exception, and the exception is the mute guard.
  for w in $(xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null); do
    if [ "${OVERLAY_MUTE_PEEK:-0}" = "1" ] || [ -e "$PEEK_FLAG" ]; then
      xdotool windowmove "$w" 0 0 2>/dev/null || true
    else
      xdotool windowmove "$w" "$OFF_X" "$OFF_Y" 2>/dev/null || true
    fi
  done

  for w in $(xdotool search --onlyvisible --name 'Zoom Workplace' 2>/dev/null); do
    xdotool windowminimize "$w" 2>/dev/null || true
  done
}

if [ "${1:-}" = "once" ]; then
  park_once
  exit 0
fi

# Report where the toolbar is parked, so a checker reads the coordinate from the
# parker rather than hardcoding it. grok-bot's ruling: single source of truth.
if [ "${1:-}" = "where" ]; then
  echo "off_x=$OFF_X off_y=$OFF_Y peek_x=0 peek_y=0 peek_flag=$PEEK_FLAG"
  exit 0
fi

echo "overlay_repark loop DISPLAY=$DISPLAY every ${INTERVAL}s (peek flag $PEEK_FLAG)"
while true; do
  if xdotool search --onlyvisible --name '^as_toolbar$' >/dev/null 2>&1; then
    park_once
  fi
  sleep "$INTERVAL"
done
