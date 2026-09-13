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

# --- a deliberate background --------------------------------------------------
# fluxbox's default root shows through the gap between the two panels as a strip
# of someone else's wallpaper. On a screenshot of the first attempt it was the
# only thing on the display that looked accidental. One flat colour, close to the
# notes panel, and the desktop stops competing with the content.
xsetroot -solid '#0a1218' 2>/dev/null || echo "  (xsetroot unavailable - the root window keeps its default)"

# --- the panels ---------------------------------------------------------------
# GEOMETRY IS MEASURED, NOT CALCULATED.
#
# The first attempt copied compose2.sh's numbers and put the bottom window at
# y=717 with height 404, ending at 1121 on a 1080-tall screen - the last rows
# were off the display, and under the taskbar before that. The arithmetic missed
# that fluxbox adds a title bar: a window ASKED for y=24 is PLACED at y=69, so
# every requested y is 45px optimistic, and the taskbar takes ~28px at the
# bottom. Usable height is therefore about 1050, not 1080.
#
# The work panel gets the larger share. The notes panel is the guest's own words
# and fills to maybe fourteen lines; the work panel carries agent output while
# something is being built, which is the part that runs long.
nohup xterm -geometry 132x18+40+24 -fa 'DejaVu Sans Mono' -fs 15 \
      -bg '#0d1b24' -fg '#e6eef3' -title 'SFDC24 meeting notes' \
      -e "$HOME/meeting_notes_panel.sh" >/tmp/xterm_notes.log 2>&1 &
disown
sleep 2
nohup xterm -geometry 132x20+40+500 -fa 'DejaVu Sans Mono' -fs 15 \
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

if [ "$xterms" -lt 2 ] || [ "${#wins[@]}" -lt 2 ]; then
  echo "  FAILED - expected two panels on the display. See /tmp/xterm_notes.log and /tmp/xterm_work.log"
  exit 1
fi

# --- do the panels actually FIT ------------------------------------------------
# The first run of this script placed the work panel from y=717 to y=1121 on a
# 1080-tall screen. Every check above passed: two xterms, two windows, exit 0.
# It took a screenshot to notice, which means the script was reporting success
# about a display it had never looked at.
#
# So ask the display where the windows ended up and compare against the screen.
# Off the bottom and overlapping each other are the two ways this goes wrong, and
# both are now returned values rather than things someone has to spot.
screen_h=$(xdpyinfo | awk '/dimensions/{split($2,d,"x"); print d[2]}')
TASKBAR=28          # fluxbox's toolbar sits along the bottom edge

geom_fail=0
tops=()
for w in "${wins[@]}"; do
  eval "$(xdotool getwindowgeometry --shell "$w" | grep -E '^(Y|HEIGHT)=')"
  name=$(xdotool getwindowname "$w")
  bottom=$((Y + HEIGHT))
  printf '    %-24s y=%-5s h=%-5s bottom=%s\n' "'$name'" "$Y" "$HEIGHT" "$bottom"
  if [ "$bottom" -gt $((screen_h - TASKBAR)) ]; then
    echo "      OFF THE SCREEN - bottom $bottom exceeds usable $((screen_h - TASKBAR))"
    geom_fail=1
  fi
  tops+=("$Y:$bottom")
done

# pairwise overlap, so a guest never sees one panel sitting on top of another
for a in "${tops[@]}"; do
  for b in "${tops[@]}"; do
    [ "$a" = "$b" ] && continue
    a_top=${a%%:*}; a_bot=${a##*:}
    b_top=${b%%:*}; b_bot=${b##*:}
    if [ "$a_top" -lt "$b_bot" ] && [ "$b_top" -lt "$a_bot" ]; then
      echo "      PANELS OVERLAP: ${a_top}-${a_bot} and ${b_top}-${b_bot}"
      geom_fail=1
    fi
  done
done

if [ "$geom_fail" -ne 0 ]; then
  echo "  FAILED - the panels are on the display but not laid out correctly."
  exit 1
fi
echo "  layout OK - both panels inside ${screen_h}px, no overlap"

echo
echo "  Both panels are up. They render these two files, which are shipped from"
echo "  the laptop and may be overwritten as often as you like while a share is live:"
echo "    /tmp/meeting_notes.txt   what we are learning about the guest's business"
echo "    /tmp/meeting_work.txt    what is being worked on right now"
echo
echo "  ssh HOST 'cat > /tmp/meeting_notes.txt' < local_notes.txt"
