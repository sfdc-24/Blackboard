#!/bin/bash
set -uo pipefail
export DISPLAY="${DISPLAY:-:99}"
INTERVAL="${OVERLAY_REPARK_SEC:-2}"
park_once() {
  for n in annotate_toolbar zoom_linux_float_video_window; do
    for w in $(xdotool search --onlyvisible --name "^${n}$" 2>/dev/null); do
      xdotool windowmove "$w" -3000 -3000 2>/dev/null || true
    done
  done
  for w in $(xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null); do
    if [ "${OVERLAY_MUTE_PEEK:-0}" = "1" ]; then
      xdotool windowmove "$w" 0 0 2>/dev/null || true
    else
      xdotool windowmove "$w" -3000 -3000 2>/dev/null || true
    fi
  done
  for w in $(xdotool search --onlyvisible --name 'Zoom Workplace' 2>/dev/null); do
    xdotool windowminimize "$w" 2>/dev/null || true
  done
}
if [ "${1:-}" = "once" ]; then park_once; exit 0; fi
while true; do
  if xdotool search --onlyvisible --name '^as_toolbar$' >/dev/null 2>&1; then park_once; fi
  sleep "$INTERVAL"
done
