#!/bin/bash
# ORDER 045 direction A: present from the headless GCE client into the LIVE
# meeting 861 9538 6120, with the portal/pipewire stack now installed.
#
# Coordinates read from 19_live.png at 1920x1080:
#   ( 958, 492) Join with Computer Audio
#   (1506, 221) OK on the "content is being shared with app(s)" banner
#   (1103, 900) Share on the in-meeting toolbar
#
# A sibling session logged that Zoom's two-window z-order steals clicks, so the
# home window is lowered before the meeting window is driven.
#
# Last time this exact commit dropped the client out of the meeting. The single
# question here is whether it still does. Captures at every step.
set -uo pipefail
export DISPLAY=:99
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus
export XDG_RUNTIME_DIR=/run/user/1001
SHOTS=/tmp/shots

shot() {
  import -window root "$SHOTS/$1" 2>/dev/null
  echo "  $1 colours=$(convert "$SHOTS/$1" -format %k info: 2>/dev/null || echo '?')"
}
titles() {
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    t=$(xdotool getwindowname "$w" 2>/dev/null); [ -n "$t" ] && echo "      '$t'"
  done
}

echo "=== ORDER 045-A live share $(date -u +%FT%TZ) ==="

HOME_W=$(xdotool search --onlyvisible --name '^Zoom Workplace$' 2>/dev/null | head -1)
[ -n "$HOME_W" ] && { xdotool windowminimize "$HOME_W" 2>/dev/null; echo "home window minimised: $HOME_W"; }
MEET=$(xdotool search --onlyvisible --name '^Meeting$' 2>/dev/null | head -1)
[ -n "$MEET" ] && { xdotool windowactivate "$MEET" 2>/dev/null; xdotool windowraise "$MEET" 2>/dev/null; }
sleep 2

echo "--- join with computer audio ---"
xdotool mousemove 958 492 click 1
sleep 4
shot 20_audio.png

echo "--- dismiss the app-content banner ---"
xdotool mousemove 1506 221 click 1
sleep 2
shot 21_banner.png

echo "--- click Share ---"
xdotool mousemove 1103 900 click 1
sleep 6
shot 22_picker.png
echo "  windows:"; titles

echo "=== end - if a picker is open, phase 6 commits it ==="
