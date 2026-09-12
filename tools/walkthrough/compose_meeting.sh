#!/bin/bash
# Put the PROSPECT-MEETING panels on :99, in place of the migration panels.
#
# compose2.sh and this script are ALTERNATIVES, never both. Each puts two xterms
# on a 1920x1080 display at fixed geometry; running both stacks four windows in
# two positions and the guest sees whichever won the race.
#
# Like compose2.sh this does NOT touch the Zoom client, so it can be re-run
# while a share is already live. That matters: if the panels need changing at
# 14:20 with someone watching, the share must not drop to do it.
set -uo pipefail
export DISPLAY=:99

count_proc() {            # exactly one line, always, whatever pgrep does.
  # `pgrep -c NAME` prints 0 AND exits non-zero when it matches nothing, so the
  # familiar "$(pgrep -c -x x || echo 0)" yields the two-line string "0\n0".
  # It cost this rig its window manager once already, in presenter_up.sh.
  local n
  n=$(pgrep -c -x "$1" 2>/dev/null)
  printf '%s' "${n:-0}"
}

# --- take the display cleanly -------------------------------------------------
pkill -f presenter_loop          2>/dev/null
pkill -f presenter_suites        2>/dev/null
pkill -f meeting_notes_panel     2>/dev/null
pkill -f meeting_work_panel      2>/dev/null
pkill -x xterm                   2>/dev/null
sleep 1

chmod +x "$HOME/meeting_notes_panel.sh" "$HOME/meeting_work_panel.sh"

# --- the panels ---------------------------------------------------------------
# Geometry is compose2.sh's, which is already known not to overlap: at font size
# 15 a cell is about 13x24, so 132 columns is roughly 1720px and the bottom
# window starts below the top one's 600px.
#
# Colours are deliberately NOT the green-on-black of the migration panels. The
# audience for this one runs a tax practice; near-white on deep slate reads as a
# document, and reads on a phone, which is how he may well be joining.
nohup xterm -geometry 132x24+40+24 -fa 'DejaVu Sans Mono' -fs 15 \
      -bg '#0d1b24' -fg '#e6eef3' -title 'SFDC24 meeting notes' \
      -e "$HOME/meeting_notes_panel.sh" >/tmp/xterm_notes.log 2>&1 &
disown
sleep 2
nohup xterm -geometry 132x16+40+672 -fa 'DejaVu Sans Mono' -fs 15 \
      -bg '#1a1410' -fg '#f0d9a8' -title 'SFDC24 meeting work' \
      -e "$HOME/meeting_work_panel.sh" >/tmp/xterm_work.log 2>&1 &
disown
sleep 4

# --- assert, do not announce --------------------------------------------------
# An exit code of 0 here would only mean nohup accepted the command. What matters
# is whether two windows are actually ON the display, so ask the display.
xterms=$(count_proc xterm)
wins=()   # declared before mapfile: under `set -u` an undeclared array is an
          # error on reference, and mapfile finding nothing is a normal outcome
          # here - it is the case this check exists to catch.
mapfile -t wins < <(xdotool search --onlyvisible --name 'SFDC24' 2>/dev/null)
echo "  xterms running : $xterms"
echo "  windows on :99 : ${#wins[@]}"
for w in "${wins[@]}"; do
  echo "    '$(xdotool getwindowname "$w")'  $(xdotool getwindowgeometry --shell "$w" | grep -E '^Y=|HEIGHT=' | tr '\n' ' ')"
done

if [ "$xterms" -lt 2 ] || [ "${#wins[@]}" -lt 2 ]; then
  echo "  FAILED - expected two panels on the display. See /tmp/xterm_notes.log and /tmp/xterm_work.log"
  exit 1
fi

echo
echo "  Both panels are up. They render these two files, which are shipped from"
echo "  the laptop and may be overwritten as often as you like while a share is live:"
echo "    /tmp/meeting_notes.txt   what we are learning about the guest's business"
echo "    /tmp/meeting_work.txt    what is being worked on right now"
echo
echo "  ssh HOST 'cat > /tmp/meeting_notes.txt' < local_notes.txt"
