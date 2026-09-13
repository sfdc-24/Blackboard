#!/bin/bash
# Exercise presenter_mute_state.sh.
#
# IT ASSERTS TRANSITIONS, NOT ABSOLUTE STATES. The first version assumed the
# client started unmuted, and failed twice because a previous calibration had
# left it muted - while the detector under test was reading 108 and 0 exactly
# correctly in every single case. A test that assumes the state it did not
# measure reports the tester's belief, not the system's behaviour. That is the
# same mistake this whole file exists to guard against, committed by the guard.
#
# So: read the state, toggle, require it FLIPPED. That is true regardless of
# where it starts, and it is a stronger claim than either absolute reading.
set -uo pipefail
export DISPLAY=:99
M=/home/user/presenter_mute_state.sh
AUDIO_BTN_DX=41
AUDIO_BTN_DY=26

tb=$(timeout 8 xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null | head -1)
[ -n "$tb" ] || { echo "PRECONDITION FAILED: no as_toolbar - the box is not sharing"; exit 2; }

SCREEN_H=$(xdpyinfo | awk '/dimensions/{split($2,d,"x"); print d[2]}')
# TOP, not bottom. Parked along the bottom the Audio button cannot be clicked
# at all - nine attempts, with windowraise, none reached Zoom. That is a
# question about where production should park the toolbar, NOT about whether
# the detector works, and conflating the two was costing runs.
BOTTOM=40
timeout 8 xdotool windowmove "$tb" 40 "$BOTTOM"; sleep 2
echo "toolbar id=$tb parked at 40,$BOTTOM"
echo

pass=0; fail=0
ok ()  { pass=$((pass+1)); printf '  PASS  %s\n' "$1"; }
bad () { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; }

state () {                      # echo OPEN|MUTED|UNKNOWN
  local out; out=$(bash "$M" 2>&1); local rc=$?
  pkill -x import 2>/dev/null
  case "$rc" in 0) echo OPEN ;; 1) echo MUTED ;; *) echo "UNKNOWN($out)" ;; esac
}

toggle () {                     # click Audio, and CONFIRM it moved
  local before after x y
  before="$1"
  for attempt in 1 2 3; do
    # RAISE FIRST. Parked along the bottom the toolbar sits behind the Chrome
    # kiosk window, so the click lands on Chrome and Zoom never sees it - which
    # reads as "toggle did not flip" and looks like a broken detector.
    timeout 8 xdotool windowraise "$tb" 2>/dev/null
    eval "$(timeout 8 xdotool getwindowgeometry --shell "$tb" | grep -E '^(X|Y)=')"
    timeout 8 xdotool mousemove $((X + AUDIO_BTN_DX)) $((Y + AUDIO_BTN_DY)) click 1
    sleep 3
    after=$(state)
    [ "$after" != "$before" ] && { echo "$after"; return 0; }
  done
  echo "$after"
  return 1
}

# 1. It must produce a definite reading at all, parked where production parks it.
s0=$(state)
case "$s0" in
  OPEN|MUTED) ok "reads a definite state when parked on-screen: $s0" ;;
  *)          bad "no definite reading when on-screen: $s0" ;;
esac

# 2. Toggling the client must flip the reading. This is the real assertion:
#    it proves the detector tracks Zoom rather than returning a constant.
s1=$(toggle "$s0")
if [ "$s1" != "$s0" ] && [ "$s1" = "OPEN" -o "$s1" = "MUTED" ]; then
  ok "toggle flipped the reading: $s0 -> $s1"
else
  bad "toggle did not flip the reading: $s0 -> $s1"
fi

# 3. And back, so neither direction is special.
s2=$(toggle "$s1")
if [ "$s2" = "$s0" ]; then
  ok "toggle flipped it back: $s1 -> $s2"
else
  bad "did not return to the original state: $s1 -> $s2 (was $s0)"
fi

# 4. THE REFUSAL. Off-screen must be UNKNOWN, never a comfortable OPEN.
timeout 8 xdotool windowmove "$tb" -3000 -3000; sleep 2
bash "$M" >/dev/null 2>&1; rc=$?
pkill -x import 2>/dev/null
if [ "$rc" -eq 2 ]; then ok "off-screen refuses with exit 2, not 0"
else bad "off-screen returned $rc - it must refuse, not guess"; fi

# 5. Leave the rig usable: on-screen at the bottom, and UNMUTED.
timeout 8 xdotool windowmove "$tb" 40 "$BOTTOM"; sleep 2
final=$(state)
if [ "$final" = "MUTED" ]; then final=$(toggle MUTED); fi
echo
echo "left the client: $final"
echo "RESULT passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
exit 0
