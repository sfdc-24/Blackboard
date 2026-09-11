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
if [ "$(pgrep -c -x fluxbox || echo 0)" -lt 1 ]; then
  nohup env DISPLAY=:99 fluxbox >/tmp/fluxbox99.log 2>&1 & disown
  sleep 2
fi
echo "  fluxbox instances: $(pgrep -c -x fluxbox || echo 0)"

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
echo "  xvfb        $(pgrep -c -x Xvfb || echo 0)"
echo "  fluxbox     $(pgrep -c -x fluxbox || echo 0)"
echo "  pulseaudio  $(pgrep -c -x pulseaudio || echo 0)"
echo "  pipewire    $(pgrep -c -x pipewire || echo 0)"
echo "  portal      $(pgrep -f '[l]ibexec/xdg-desktop-portal$' | wc -l)"
echo "  portal-gtk  $(pgrep -f '[x]dg-desktop-portal-gtk' | wc -l)"
echo "  xterms      $(pgrep -c -x xterm || echo 0)"
echo "  zoom        $(pgrep -c -x zoom || echo 0)  (0 is correct here - joining is a separate step)"
echo
echo "READY when xvfb>=1, fluxbox>=1, pipewire=1 and portal=1."
echo "Joining is deliberately NOT done here: it needs the meeting passcode,"
echo "which is shipped over stdin at join time and never stored on this box."
