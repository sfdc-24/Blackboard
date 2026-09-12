#!/bin/bash
# Join the meeting in ~/join.url and start sharing :99. One command, because the
# alternative is doing six things by hand while a prospect watches.
#
# THE WINDOW IS CALLED "Meeting", NOT "Zoom Meeting".
#   The first version of this waited for a title matching *Zoom*Meeting* and gave
#   up after 90 seconds reporting NO MEETING WINDOW - while sitting in the
#   meeting. The client was in, the check was wrong, and a screenshot was the
#   only thing that revealed it. A join detector that reports failure on success
#   is worse than none: it invites you to "fix" a thing that is working.
#
# KEYBOARD, NOT COORDINATES.
#   Alt+S is Zoom's start-share shortcut. The first share was driven by clicking
#   absolute pixel positions read off a screenshot, which works until the window
#   moves. The picker's own Share button is still clicked, but its position is
#   computed FROM THE PICKER WINDOW rather than remembered.
#
# The URL carries the passcode and is never echoed here. Zoom logs its own launch
# URL, which is why the sweep at the end of a session looks for the class of
# file, not a filename.
set -uo pipefail
export DISPLAY=:99
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
export XDG_CURRENT_DESKTOP=GNOME
export XDG_SESSION_TYPE=x11

[ -s "$HOME/join.url" ] || { echo "no join.url on the box"; exit 1; }

win_titled() {                      # echo the id of the first visible window whose title matches
  local pat="$1" w t
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    t=$(xdotool getwindowname "$w" 2>/dev/null)
    case "$t" in $pat) printf '%s' "$w"; return 0 ;; esac
  done
  return 1
}

echo "=== join $(date -u +%FT%TZ) ==="

# --- a clean client -----------------------------------------------------------
# Zoom left in an ended meeting will not join a new one cleanly.
if pgrep -x zoom >/dev/null 2>&1; then
  echo "  stopping the previous zoom"
  pkill -x zoom; sleep 4
fi

URL=$(head -1 "$HOME/join.url")
nohup zoom --url="$URL" >/tmp/zoom_join.log 2>&1 &
disown
unset URL

# --- wait for the MEETING window ----------------------------------------------
mw=""
for i in $(seq 1 40); do
  sleep 3
  mw=$(win_titled 'Meeting' || win_titled '*Zoom*Meeting*' || true)
  [ -n "$mw" ] && break
done
if [ -z "$mw" ]; then
  echo "  NOT IN THE MEETING after 120s. Windows on :99:"
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    t=$(xdotool getwindowname "$w" 2>/dev/null); [ -n "$t" ] && echo "    '$t'"
  done
  exit 1
fi
echo "  in the meeting after $((i*3))s  (window '$(xdotool getwindowname "$mw")')"

# --- start the share ----------------------------------------------------------
if win_titled 'as_toolbar' >/dev/null 2>&1; then
  echo "  already sharing"
else
  # LET ZOOM SETTLE, THEN RETRY THE SHORTCUT.
  #
  # alt+s is not reliable immediately after joining. On a run where the join took
  # 15s it opened the picker first time; on one that took 9s it did nothing at
  # all, and the script correctly reported SHARE NOT LIVE. The difference is not
  # the keystroke, it is whether the client has finished setting itself up.
  # So: wait, then ask up to three times, checking for the picker between each
  # rather than assuming the first press landed.
  sleep 8
  pick=""
  for attempt in 1 2 3; do
    xdotool windowactivate "$mw" 2>/dev/null; sleep 2
    xdotool key --clearmodifiers alt+s
    for w in 1 2 3 4 5; do
      sleep 2
      pick=$(win_titled '*want to share*' || true)
      [ -n "$pick" ] && break
    done
    [ -n "$pick" ] && { echo "  picker opened on attempt $attempt"; break; }
    echo "  alt+s attempt $attempt did not open the picker"
  done
  if [ -n "$pick" ]; then
    eval "$(xdotool getwindowgeometry --shell "$pick" | grep -E '^(X|Y|WIDTH|HEIGHT)=')"
    # The picker's Share button sits bottom-centre, 53px above the reported
    # bottom edge.
    #
    # WHY 53 AND NOT SOMETHING ROUNDER: xdotool's geometry does not agree with
    # what is drawn. It reports Y=231 and HEIGHT=640, implying a bottom at 871,
    # while the picker visibly ends at ~845 and the button centre sits at 818.
    # A first attempt used HEIGHT-30, landed just below the button, and left the
    # picker open with the script reporting "SHARE NOT LIVE" - correctly, which
    # is the only reason it was caught in rehearsal rather than in front of Nav.
    # 53 is measured from a screenshot of this client at this size, and the
    # assertion below is what actually protects us if it ever drifts.
    #
    # Return does NOT commit the picker - tried, it does nothing.
    bx=$(( X + WIDTH / 2 ))
    by=$(( Y + HEIGHT - 53 ))
    echo "  picker at ${X},${Y} ${WIDTH}x${HEIGHT} - clicking Share at ${bx},${by}"
    xdotool mousemove "$bx" "$by" click 1
  else
    echo "  no share picker appeared after alt+s"
  fi
fi

# --- assert the share is really live ------------------------------------------
# as_toolbar and as_preview are Zoom's own share windows. They exist only while a
# share is committed, so they are the condition - not the click having happened.
live=0
for i in $(seq 1 15); do
  sleep 2
  if win_titled 'as_toolbar' >/dev/null 2>&1; then live=1; break; fi
done

echo
echo "--- state ---"
echo "  zoom processes : $(pgrep -c -x zoom 2>/dev/null)"
echo "  sharing        : $([ "$live" -eq 1 ] && echo YES || echo NO)"
echo "  bound to mic   : $(bash "$(dirname "$0")/zoom_bound_count.sh" vmic_src 2>/dev/null || echo '?')"
for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
  t=$(xdotool getwindowname "$w" 2>/dev/null); [ -n "$t" ] && echo "    '$t'"
done

[ "$live" -eq 1 ] || { echo "  SHARE NOT LIVE - start it by hand before anyone joins"; exit 1; }
echo "  the box is in the meeting and sharing"
exit 0
