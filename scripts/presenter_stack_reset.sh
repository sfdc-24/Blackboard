#!/bin/bash
# The single bring-up contract for the presenter box. Item 1 of grok-bot's
# permanent-fix plan (logs/_grok_drive_plan.txt), which closes regression R1.
#
# R1, THE REGRESSION THIS EXISTS TO MAKE IMPOSSIBLE
#   presenter_up.sh and presenter_voice_up.sh are order dependent and nothing
#   said so. Run presenter_up.sh AFTER presenter_voice_up.sh and it starts
#   pulseaudio alongside the already running pipewire; every loaded module goes
#   with it and only auto_null remains. That happened on 2026-09-18 and the
#   symptom was a rig that had passed a self test minutes earlier. There is now
#   one entry point and the order lives here rather than in someone's memory.
#
# TWO DELIBERATE DEVIATIONS FROM THE PLAN AS WRITTEN, BOTH STATED NOT HIDDEN
#   1. The plan says this "always runs first" and includes
#      pkill -9 -f 'Xvfb|fluxbox|zoom|pipewire'. Run during a live meeting that
#      drops the presenter out of the room mid conversation. This refuses when a
#      meeting window exists unless --force is passed.
#   2. The plan kills pipewire with -9 and then asserts systemctl is-active
#      pipewire a few lines later. Those contradict. pulseaudio is the
#      interloper on this box and pulseaudio is what gets stopped; pipewire is
#      left alone and is only restarted if it is genuinely not running.
#
# Safe to run twice. Fails loud rather than degrading.

set -u
FORCE=0
COLD=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --cold)  COLD=1 ;;
    *) echo "unknown argument: $a"; echo "usage: $0 [--force] [--cold]"; exit 2 ;;
  esac
done

export DISPLAY=:99
say() { echo "  $*"; }
echo "=== presenter_stack_reset $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ---- refuse to destroy a live meeting ---------------------------------------
# A meeting window is the honest signal. pactl cannot tell us this; a bound
# capture stream survives states where the meeting has already ended.
INMEETING=0
if command -v xdotool >/dev/null 2>&1; then
  if xdotool search --onlyvisible --name 'Meeting' >/dev/null 2>&1; then INMEETING=1; fi
fi
if [ "$INMEETING" = "1" ] && [ "$FORCE" = "0" ]; then
  echo "REFUSING: a Zoom meeting window is open on :99."
  echo "This reset restarts the audio stack and would drop the presenter out of"
  echo "the room. Re-run with --force only if dropping the call is intended."
  exit 3
fi
[ "$INMEETING" = "1" ] && say "--force given, proceeding despite an open meeting window"

# ---- pulseaudio is the interloper, not pipewire ------------------------------
# pactl on this box talks to pipewire-pulse. A running pulseaudio is what tore
# the modules down in R1, so it is stopped and disabled, never merely killed.
if systemctl --user is-active --quiet pulseaudio 2>/dev/null; then
  say "stopping pulseaudio (it is what destroys the virtual devices)"
  systemctl --user stop pulseaudio.socket pulseaudio.service 2>/dev/null
fi
if pgrep -x pulseaudio >/dev/null 2>&1; then
  say "pulseaudio still running, terminating it"
  pkill -x pulseaudio 2>/dev/null
  sleep 2
fi

# ---- pipewire must be up, but is never -9'd ----------------------------------
for unit in pipewire pipewire-pulse; do
  if ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
    say "$unit not active, starting it"
    systemctl --user start "$unit" 2>/dev/null
    sleep 2
  fi
done

# ---- cold mode tears down the display stack only -----------------------------
if [ "$COLD" = "1" ]; then
  say "--cold: tearing down zoom, fluxbox and Xvfb (audio units left alone)"
  pkill -x zoom 2>/dev/null
  pkill -x fluxbox 2>/dev/null
  pkill -x Xvfb 2>/dev/null
  sleep 3
  rm -f /tmp/.X99-lock /tmp/.X98-lock 2>/dev/null
fi

# ---- the order, finally written down ----------------------------------------
say "presenter_up.sh   (display, window manager, portal)"
bash "$(dirname "$0")/presenter_up.sh" >/tmp/stack_up.log 2>&1
say "  exit=$? (log /tmp/stack_up.log)"

say "presenter_voice_up.sh   (virtual audio devices)"
bash "$(dirname "$0")/presenter_voice_up.sh" >/tmp/stack_voice.log 2>&1
say "  exit=$? (log /tmp/stack_voice.log)"

# ---- the reset is not the verdict; the validator is -------------------------
echo
bash "$(dirname "$0")/presenter_stack_validate.sh"
exit $?
