#!/bin/bash
# Bring the presenter rig back up after the instance has been stopped.
#
# WHY THIS EXISTS
#   The GCE schedule powers the instance ON. It does not start anything inside
#   it. Every piece of the display stack was started interactively and none of
#   it survives a stop/start: no Xvfb, no window manager, no audio, no portal,
#   no pipewire, no panels. A freshly started box looks healthy and can do
#   nothing.
#
#   The packages and the scripts DO survive, because the disk is preserved.
#   Only the running processes are gone. This restarts them.
#
# IDEMPOTENT. Safe to run twice; each piece is started only if absent.
# NO CREDENTIAL. This does not join a meeting and does not want the join URL.
set -uo pipefail

export DISPLAY=:99
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
export XDG_CURRENT_DESKTOP=GNOME
export XDG_SESSION_TYPE=x11

start_if_absent() {           # name, then the command
  local name="$1"; shift
  if pgrep -x "$name" >/dev/null 2>&1; then
    echo "  $name already running"
  else
    nohup "$@" >"/tmp/${name}.log" 2>&1 &
    disown
    echo "  $name started"
  fi
}

count_proc() {                # exactly one line, always, whatever pgrep does
  # THE BUG THIS REPLACES, and it cost a live walkthrough its window manager.
  #
  # Every counter here was written as "$(pgrep -c -x NAME || echo 0)". When pgrep
  # matches nothing it PRINTS "0" and ALSO exits non-zero, so the || fires and
  # appends a SECOND "0". The substitution becomes the two-line string "0\n0".
  #
  # Printed, that looks like a harmless stray zero. Fed to [ ... -lt 1 ] it is
  # "integer expression expected", the test errors, and - the part that actually
  # hurt - the if body NEVER RUNS. So fluxbox was never started on 2026-09-11,
  # while the readiness line below demanded fluxbox>=1. A guard that fails open
  # and a status line that reads almost-right is a bad combination.
  local n
  n=$(pgrep -c -x "$1" 2>/dev/null)
  printf '%s' "${n:-0}"
}

count_match() {               # same, for pgrep -f patterns
  local n
  n=$(pgrep -f -c "$1" 2>/dev/null)
  printf '%s' "${n:-0}"
}

echo "=== presenter rig up $(date -u +%FT%TZ) ==="

# 1. The displays. :99 is the one that gets shared; :98 only matters for the
#    two-client direction-B demonstration and is cheap to have.
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  nohup Xvfb :99 -screen 0 1920x1080x24 >/tmp/xvfb99.log 2>&1 & disown
  sleep 3
fi
if ! xdpyinfo -display :98 >/dev/null 2>&1; then
  nohup Xvfb :98 -screen 0 1280x800x24 >/tmp/xvfb98.log 2>&1 & disown
  sleep 2
fi
echo "  :99 $(xdpyinfo -display :99 2>/dev/null | awk '/dimensions/{print $2}' || echo DEAD)"
echo "  :98 $(xdpyinfo -display :98 2>/dev/null | awk '/dimensions/{print $2}' || echo DEAD)"

# 2. Window manager. Zoom's dialogs are unmanageable without one.
# PER DISPLAY, because a global count cannot answer a per-display question.
#
# My first repair of this loop gated each iteration on the TOTAL fluxbox count, which
# codex knocked over with one counterexample: start with a window manager on :99 and
# none on :98, and the :99 iteration starts a SECOND :99, the total reaches 2, and the
# :98 iteration then skips. Result :99=2, :98=0 - while the status line says "2" and
# the commit message claimed both displays were covered.
#
# That is the same failure as the bug it replaced, one level up: a number that is
# almost right standing in for the condition actually being asked about. Ask each
# display whether IT has a window manager, by talking to that display.
wm_present() {                # a window manager is actually managing this display
  # THE EXIT CODE OF xprop IS NOT THE ANSWER.
  #
  # `xprop -root _NET_SUPPORTING_WM_CHECK` prints "_NET_SUPPORTING_WM_CHECK:  not
  # found." and EXITS 0 when no window manager is running. So the obvious check
  # succeeds on a bare display, reports "already running", and never starts fluxbox -
  # which is exactly what happened on a cold box: "fluxbox on :99 already running"
  # printed directly above "fluxbox instances: 0".
  #
  # That is the third guard in this file to be satisfied for the wrong reason, after
  # the pgrep counter and the global-count loop. The shape is always the same: a check
  # whose success path can be reached without the condition being true. Look at the
  # VALUE - a real window manager publishes a window id.
  DISPLAY="$1" xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -qi 'window id'
}

for wm_display in :99 :98; do
  wm_log="/tmp/fluxbox${wm_display#:}.log"
  if wm_present "$wm_display"; then
    echo "  fluxbox on $wm_display already running"
  else
    nohup env DISPLAY="$wm_display" fluxbox >"$wm_log" 2>&1 & disown
    sleep 3
    if wm_present "$wm_display"; then
      echo "  fluxbox on $wm_display started"
    else
      echo "  fluxbox on $wm_display FAILED to take the display - see $wm_log"
    fi
  fi
done
echo "  fluxbox instances: $(count_proc fluxbox)  (expect one per display)"

# 3. Audio, then THE PIECE THAT ACTUALLY MATTERS.
#    Zoom refuses to run its share manager unless xdg-desktop-portal and
#    pipewire are present - even on X11, where it never uses the ScreenCast
#    interface. Without them it destroys SharePresenterModeMgr and LEAVES the
#    meeting the instant a share is committed. That was the whole bug.
start_if_absent pulseaudio pulseaudio --start --exit-idle-time=-1
start_if_absent pipewire /usr/bin/pipewire
start_if_absent wireplumber /usr/bin/wireplumber
if ! pgrep -f '[l]ibexec/xdg-desktop-portal$' >/dev/null 2>&1; then
  nohup /usr/libexec/xdg-desktop-portal >/tmp/portal.log 2>&1 & disown
fi
if ! pgrep -f '[x]dg-desktop-portal-gtk' >/dev/null 2>&1; then
  nohup /usr/libexec/xdg-desktop-portal-gtk >/tmp/portal-gtk.log 2>&1 & disown
fi
sleep 3

# 4. The panels, so a share is not a blank desktop.
if [ -x "$HOME/compose2.sh" ]; then
  "$HOME/compose2.sh" >/tmp/compose2.log 2>&1 || echo "  compose2.sh reported a problem, see /tmp/compose2.log"
fi

echo
echo "--- state, as returned values rather than assurances ---"
echo "  xvfb        $(count_proc Xvfb)"
echo "  fluxbox     $(count_proc fluxbox)"
echo "  pulseaudio  $(count_proc pulseaudio)"
echo "  pipewire    $(count_proc pipewire)"
echo "  portal      $(count_match '[l]ibexec/xdg-desktop-portal$')"
echo "  portal-gtk  $(count_match '[x]dg-desktop-portal-gtk')"
echo "  xterms      $(count_proc xterm)"
echo "  zoom        $(count_proc zoom)  (0 is correct here - joining is a separate step)"
echo
echo "READY when xvfb>=1, fluxbox>=1, pipewire=1 and portal=1."
echo
echo "The rig is MUTE until the virtual microphone exists. Run presenter_voice_up.sh"
echo "if this box needs to speak; it is separate because a silent share still works."
echo "Joining is deliberately NOT done here: it needs the meeting passcode,"
echo "which is shipped over stdin at join time and never stored on this box."
