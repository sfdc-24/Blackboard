#!/bin/bash
# Put the PROSPECT-FACING page on :99, in place of the xterm panels.
#
# WHY THIS REPLACES compose_meeting.sh FOR A CLIENT AUDIENCE
#   Mr Salam watched the xterm panels live on 2026-09-12 and said they looked
#   "dull and hard to understand". He was right, and it is not a matter of
#   colours: a terminal says "we are debugging" to someone who came to find out
#   whether we can help their tax practice. The panels stay in the repo for
#   working sessions with the fleet; this is what a guest sees.
#
# THE MECHANISM IS UNCHANGED ON PURPOSE
#   The page polls state.json. The laptop ships state.json exactly the way it
#   shipped meeting_notes.txt. One way of doing things, and a screen that holds
#   its last good frame if the laptop goes quiet instead of going blank.
#
# WHY A LOCAL HTTP SERVER RATHER THAN file://
#   Chrome refuses fetch() against file:// origins, so a file:// page could not
#   poll at all. A loopback server on 127.0.0.1 is the smallest thing that makes
#   the page work, and it is not reachable from outside the box.
set -uo pipefail
export DISPLAY=:99
export XDG_RUNTIME_DIR=/run/user/$(id -u)

DIR="$HOME/present"
PORT="${PRESENT_PORT:-8777}"
PROFILE="$HOME/.chrome-present"

[ -f "$DIR/index.html" ] || { echo "no $DIR/index.html - ship the page first"; exit 1; }
[ -f "$DIR/state.json" ] || echo '{"mode":"notes","notes":["# waiting","the laptop has not sent a state yet"]}' > "$DIR/state.json"

# --- take the display ---------------------------------------------------------
pkill -f meeting_notes_panel  2>/dev/null
pkill -f meeting_work_panel   2>/dev/null
pkill -f presenter_loop       2>/dev/null
pkill -f presenter_suites     2>/dev/null
pkill -x xterm                2>/dev/null
pkill -f "http.server $PORT"  2>/dev/null
pkill -f "chrome-present"     2>/dev/null
sleep 1
xsetroot -solid '#F6F7F4' 2>/dev/null || true

# --- the server ---------------------------------------------------------------
nohup python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$DIR" >/tmp/present_http.log 2>&1 &
disown

# WAIT FOR THE PORT, do not sleep at it.
served=0
for i in $(seq 1 25); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/index.html"; then served=1; break; fi
  sleep 1
done
if [ "$served" -ne 1 ]; then
  echo "the page never became reachable on 127.0.0.1:$PORT - see /tmp/present_http.log"
  tail -5 /tmp/present_http.log 2>/dev/null
  exit 1
fi
echo "  http server serving $DIR on 127.0.0.1:$PORT  (after ${i}s)"

# state.json must be served too, or the page polls a 404 forever and silently
# keeps its opening frame - which looks like a working screen that never updates.
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/state.json"; then
  echo "  state.json reachable"
else
  echo "  state.json is NOT reachable - the page would never update"; exit 1
fi

# --- the browser --------------------------------------------------------------
# --no-sandbox: this is a disposable box rendering one page it serves to itself.
# Chrome's sandbox needs user namespaces that are not reliably present here, and
# a browser that will not start is worse than one without a sandbox on a machine
# that is destroyed after the meeting.
nohup google-chrome \
  --kiosk --app="http://127.0.0.1:$PORT/index.html" \
  --user-data-dir="$PROFILE" --class=chrome-present \
  --no-sandbox --no-first-run --no-default-browser-check \
  --disable-infobars --disable-session-crashed-bubble --disable-translate \
  --disable-features=TranslateUI,ChromeWhatsNewUI \
  --window-position=0,0 --window-size=1920,1080 \
  >/tmp/present_chrome.log 2>&1 &
disown

# WAIT FOR THE WINDOW. Chrome's first start on a fresh profile is slow.
win=""
for i in $(seq 1 40); do
  sleep 2
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    t=$(xdotool getwindowname "$w" 2>/dev/null)
    case "$t" in *"SFDC 24"*|*"Tax Everyday"*) win="$w"; break 2 ;; esac
  done
done

if [ -z "$win" ]; then
  echo "  NO BROWSER WINDOW after 80s. Windows on :99:"
  for w in $(xdotool search --onlyvisible --name '.*' 2>/dev/null); do
    t=$(xdotool getwindowname "$w" 2>/dev/null); [ -n "$t" ] && echo "    '$t'"
  done
  tail -8 /tmp/present_chrome.log 2>/dev/null
  exit 1
fi

# --- assert it actually fills the screen --------------------------------------
# A kiosk window that opened at 800x600 is a window, not a presentation, and
# every check above would still have passed.
eval "$(xdotool getwindowgeometry --shell "$win" | grep -E '^(X|Y|WIDTH|HEIGHT)=')"
screen=$(xdpyinfo | awk '/dimensions/{print $2}')
sw=${screen%x*}; sh=${screen#*x}
echo "  window '$(xdotool getwindowname "$win")'  ${WIDTH}x${HEIGHT} at ${X},${Y}  screen ${screen}"
if [ "$WIDTH" -lt $((sw - 40)) ] || [ "$HEIGHT" -lt $((sh - 120)) ]; then
  echo "  FAILED - the window does not fill the display"
  exit 1
fi

echo "  the guest-facing page is up and full screen"
echo
echo "  drive it by shipping state.json:"
echo "    scp state.json user@BOX:$DIR/state.json"
exit 0
