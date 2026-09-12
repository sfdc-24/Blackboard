#!/bin/bash
# Exercise the two meeting panels for real. They are printf loops, so they need
# no X server - only a terminal to write to. Everything that can be wrong with
# them at 14:00 can be found here at 03:00.
#
# WHAT IS ACTUALLY BEING TESTED, as opposed to "it ran":
#   1. with NO file, the panel shows the fallback and NOT an empty box
#   2. with a file, the panel shows the FILE'S content and not the fallback
#   3. the notes panel prints both clocks, and they are real times not literals
#   4. a frame is written as ONE printf - a viewer never sees a half-drawn panel
#   5. neither script exits on its own; they are meant to loop forever
#
# Each check RETURNS a value that is compared. Nothing here trusts an exit code.
set -uo pipefail

RIG="${1:?usage: test_meeting_panels.sh <path to tools/walkthrough>}"
NOTES=/tmp/meeting_notes.txt
WORK=/tmp/meeting_work.txt
pass=0
fail=0

ok()   { pass=$((pass+1)); echo "  PASS  $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL  $1"; }

# capture N seconds of a panel's output, strip the escape codes, keep the text
capture() {
  local script="$1" out
  out=$(timeout 4 bash "$script" 2>&1 || true)
  printf '%s' "$out" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g'
}

echo "=== meeting panels, executed ==="
echo

# ---- 1. fallback when the file is absent ------------------------------------
rm -f "$NOTES" "$WORK"

n_fallback=$(capture "$RIG/meeting_notes_panel.sh")
if printf '%s' "$n_fallback" | grep -q 'Nothing captured yet'; then
  ok "notes panel shows its fallback when $NOTES is absent"
else
  bad "notes panel did NOT show the fallback with no file"
fi

# The failure this guards against is a panel that renders an EMPTY body - which
# looks to a guest exactly like a crashed rig. Demand real content, not just
# "did not crash".
n_lines=$(printf '%s' "$n_fallback" | grep -c '[^[:space:]]')
if [ "$n_lines" -ge 6 ]; then
  ok "notes fallback carries $n_lines non-blank lines, so the panel is not blank"
else
  bad "notes fallback rendered only $n_lines non-blank lines"
fi

w_fallback=$(capture "$RIG/meeting_work_panel.sh")
if printf '%s' "$w_fallback" | grep -q 'Idle - waiting'; then
  ok "work panel shows its fallback when $WORK is absent"
else
  bad "work panel did NOT show the fallback with no file"
fi

# ---- 2. the file wins over the fallback -------------------------------------
# A unique string, so a pass cannot be an accident of matching prepared text.
marker="MARKER-$$-$(date +%s)"
printf '   %s\n   second line\n' "$marker" > "$NOTES"
printf '   %s-work\n' "$marker" > "$WORK"

n_live=$(capture "$RIG/meeting_notes_panel.sh")
if printf '%s' "$n_live" | grep -q "$marker"; then
  ok "notes panel renders the shipped file"
else
  bad "notes panel did not render the shipped file"
fi
if printf '%s' "$n_live" | grep -q 'Nothing captured yet'; then
  bad "notes panel showed the fallback WHILE a file existed - the file is being ignored"
else
  ok "notes panel drops the fallback once a file exists"
fi

w_live=$(capture "$RIG/meeting_work_panel.sh")
if printf '%s' "$w_live" | grep -q "${marker}-work"; then
  ok "work panel renders the shipped file"
else
  bad "work panel did not render the shipped file"
fi

# ---- 2b. an EMPTY file must not blank the panel -----------------------------
# `ssh HOST 'cat > /tmp/meeting_notes.txt' < empty` is a real thing that happens
# when a push goes wrong mid-call. -s rather than -f is what makes this pass.
: > "$NOTES"
n_empty=$(capture "$RIG/meeting_notes_panel.sh")
if printf '%s' "$n_empty" | grep -q 'Nothing captured yet'; then
  ok "an empty notes file falls back instead of blanking the panel"
else
  bad "an EMPTY notes file blanked the panel - a failed push would clear the screen"
fi

# ---- 3. the clocks are real -------------------------------------------------
printf '   x\n' > "$NOTES"
clock_line=$(capture "$RIG/meeting_notes_panel.sh" | grep -m1 'Toronto')
if printf '%s' "$clock_line" | grep -qE 'Toronto [0-9]{2}:[0-9]{2}.*Buenos Aires [0-9]{2}:[0-9]{2}'; then
  ok "both clocks render as real HH:MM  ->  $(printf '%s' "$clock_line" | tr -s ' ')"
else
  bad "clock line is not two real times: '$clock_line'"
fi

# The two zones are one hour apart in September. If they print the SAME time the
# TZ lookup silently failed and both fell back to UTC - which is exactly the kind
# of thing that looks fine on screen and is wrong.
tor=$(printf '%s' "$clock_line" | sed -n 's/.*Toronto \([0-9][0-9]:[0-9][0-9]\).*/\1/p')
bue=$(printf '%s' "$clock_line" | sed -n 's/.*Buenos Aires \([0-9][0-9]:[0-9][0-9]\).*/\1/p')
if [ -n "$tor" ] && [ -n "$bue" ] && [ "$tor" != "$bue" ]; then
  ok "the two zones differ ($tor vs $bue), so TZ is being honoured"
else
  bad "Toronto and Buenos Aires printed the same time ('$tor' / '$bue') - TZ lookup failed"
fi

# ---- 3b. a long file keeps its NEWEST lines ---------------------------------
# The panels tail rather than cat. On the box the notes panel fits fourteen body
# lines, and during a live call the notes only grow - so a panel that cat'd would
# silently drop the line the guest had just watched being written, while keeping
# the header he read ten minutes ago. Clipping has to land on the old end.
#
# Off a tty `tput lines` gives nothing, the panels fall back to 24, and the
# budget is 24-5 = 19 body lines. With 100 lines in, the last must survive and
# the first must not.
: > "$NOTES"
i=1
while [ "$i" -le 100 ]; do printf '   LINE-%03d\n' "$i" >> "$NOTES"; i=$((i+1)); done

n_tail=$(capture "$RIG/meeting_notes_panel.sh")
if printf '%s' "$n_tail" | grep -q 'LINE-100'; then
  ok "a 100-line notes file still shows its LAST line"
else
  bad "the newest line was clipped - the panel is cat'ing, not tailing"
fi
if printf '%s' "$n_tail" | grep -q 'LINE-001'; then
  bad "line 1 of 100 is on screen, so nothing is being tailed at all"
else
  ok "the oldest lines are the ones dropped"
fi
# And the body must not exceed the budget, or the terminal scrolls and the
# single-printf redraw stops meaning anything.
# DISTINCT lines, not a raw count: capture() runs the panel for four seconds and
# the loop sleeps three, so the output holds about two identical frames and a
# plain grep -c would report double the budget and fail a correct panel.
kept=$(printf '%s' "$n_tail" | grep -o 'LINE-[0-9]\{3\}' | sort -u | wc -l | tr -d ' ')
if [ "$kept" -le 19 ]; then
  ok "kept $kept body lines, inside the 19-line budget"
else
  bad "kept $kept body lines, over the 19-line budget - the panel will scroll"
fi

# ---- 4. one printf per frame ------------------------------------------------
# Every frame must begin with the clear sequence. If a frame were echoed line by
# line the viewer would catch a blank window on every refresh - the bug the
# original panels carry a comment about.
raw=$(timeout 4 bash "$RIG/meeting_notes_panel.sh" 2>&1 || true)
clears=$(printf '%s' "$raw" | grep -o $'\033\[H\033\[2J' | wc -l)
if [ "$clears" -ge 1 ]; then
  ok "each frame is preceded by a single clear ($clears frames in 4s)"
else
  bad "no clear-and-home sequence found; the panel will scroll rather than redraw"
fi

# ---- 5. the loops do not exit on their own ----------------------------------
# timeout returns 124 when it had to kill the process, which is the PASS here:
# a panel that returns by itself leaves a dead black window mid-meeting.
timeout 3 bash "$RIG/meeting_notes_panel.sh" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 124 ]; then
  ok "notes panel was still running when the timer killed it (rc=124)"
else
  bad "notes panel EXITED on its own with rc=$rc"
fi
timeout 3 bash "$RIG/meeting_work_panel.sh" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 124 ]; then
  ok "work panel was still running when the timer killed it (rc=124)"
else
  bad "work panel EXITED on its own with rc=$rc"
fi

# ---- 6. the composer at least parses ----------------------------------------
# It needs an X display to do anything real, so this is honestly only a syntax
# check and is labelled as one. The real run happens on the box at T-15.
if bash -n "$RIG/compose_meeting.sh" 2>/tmp/compose_syntax.err; then
  ok "compose_meeting.sh parses (SYNTAX ONLY - it is unrun until the box is up)"
else
  bad "compose_meeting.sh has a syntax error: $(cat /tmp/compose_syntax.err)"
fi

rm -f "$NOTES" "$WORK"
echo
echo "RESULT passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
exit 0
