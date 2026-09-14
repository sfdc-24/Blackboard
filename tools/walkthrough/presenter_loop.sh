#!/bin/bash
# Top panel. Assembled into one string and written with a single printf, so the
# screen is never caught mid-redraw. Clearing and then echoing line by line
# gave a viewer a blank window every refresh.
#
# The board digest is rendered ON THE LAPTOP and shipped here as plain text.
# The bus secret never comes to this disposable box.
while true; do
  if [ -f /tmp/board_digest.txt ]; then
    digest=$(cat /tmp/board_digest.txt)
  else
    digest="   (board digest not yet shipped from the laptop)"
  fi
  frame=$(printf '%s\n' \
    "" \
    "   SFDC24 BLACKBOARD  -  GCloud migration, live" \
    "   ============================================================" \
    "" \
    "   presenter rig   $(hostname -s)   GCE us-east1-b   $(uptime -p)" \
    "   display :99     $(xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}')   Xvfb + fluxbox, no monitor attached" \
    "   capture stack   xdg-desktop-portal $(pgrep -f 'libexec/xdg-desktop-portal$' | wc -l)   pipewire $(pgrep -c -x pipewire)   zoom $(pgrep -c -x zoom)" \
    "   now             $(date -u +%FT%TZ)" \
    "" \
    "$digest" \
    "" \
    "   PROVEN TODAY   A: this client PRESENTS into a live Zoom meeting." \
    "                  B: it RECEIVES and renders another participant's share.")
  printf '\033[H\033[2J%s' "$frame"
  sleep 5
done
