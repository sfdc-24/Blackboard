#!/bin/bash
# Does presenter_say.sh actually REFUSE when the client is muted?
#
# THE MUTATION IS THE TEST. Adding a guard and watching the happy path still pass
# proves nothing - that is what happened on 2026-09-12, when every check passed
# against a muted client. So: mute the real client, try to speak, and require a
# refusal. Then unmute and require it to speak.
set -uo pipefail
export DISPLAY=:99
SAY=/home/user/presenter_say.sh
M=/home/user/presenter_mute_state.sh
AUDIO_BTN_DX=41; AUDIO_BTN_DY=26

tb=$(timeout 8 xdotool search --onlyvisible --name '^as_toolbar$' 2>/dev/null | head -1)
[ -n "$tb" ] || { echo "PRECONDITION FAILED: not sharing"; exit 2; }
timeout 8 xdotool windowmove "$tb" 40 40; sleep 2

state () { bash "$M" >/dev/null 2>&1; local rc=$?; pkill -x import 2>/dev/null
           case "$rc" in 0) echo OPEN;; 1) echo MUTED;; *) echo UNKNOWN;; esac; }
toggle () { local b="$1" a
            for i in 1 2 3; do
              timeout 8 xdotool windowraise "$tb" 2>/dev/null
              eval "$(timeout 8 xdotool getwindowgeometry --shell "$tb" | grep -E '^(X|Y)=')"
              timeout 8 xdotool mousemove $((X+AUDIO_BTN_DX)) $((Y+AUDIO_BTN_DY)) click 1; sleep 3
              a=$(state); [ "$a" != "$b" ] && { echo "$a"; return 0; }
            done; echo "$a"; return 1; }

pass=0; fail=0
ok () { pass=$((pass+1)); printf '  PASS  %s\n' "$1"; }
bad () { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; }

s=$(state); echo "starting state: $s"
[ "$s" = "MUTED" ] || s=$(toggle "$s")
if [ "$s" != "MUTED" ]; then echo "could not mute the client; cannot run the mutation"; exit 2; fi

echo
echo "--- with the client MUTED, presenter_say.sh must REFUSE ---"
out=$(bash "$SAY" "this must never be heard" 2>&1); rc=$?
echo "$out" | sed 's/^/    /'
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi 'MUTED'; then
  ok "refused, and said why (exit $rc)"
else
  bad "did NOT refuse while muted (exit $rc) - the guard is decorative"
fi
if printf '%s' "$out" | grep -q 'spoke_bytes='; then
  bad "printed spoke_bytes while muted - it played anyway"
else
  ok "printed no spoke_bytes - it did not play"
fi

echo
echo "--- unmuted, it must speak again ---"
s=$(toggle MUTED)
if [ "$s" != "OPEN" ]; then echo "could not unmute"; exit 2; fi
out=$(bash "$SAY" "mute guard check, speaking normally" 2>&1); rc=$?
echo "$out" | sed 's/^/    /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'spoke_bytes='; then
  ok "speaks normally when open (exit 0)"
else
  bad "refused or errored while open (exit $rc) - the guard is too aggressive"
fi

echo
echo "left the client: $(state)"
echo "RESULT passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
exit 0
