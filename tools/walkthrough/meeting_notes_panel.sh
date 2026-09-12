#!/bin/bash
# TOP panel for a PROSPECT meeting, as opposed to compose2.sh's migration panels.
#
# WHY A SECOND SET OF PANELS RATHER THAN EDITING THE FIRST
#   presenter_loop.sh shows our own migration internals: suite counts, portal
#   process counts, what was proven about screen capture. That is the right
#   content for a working session with Mr. Salam and exactly the wrong content
#   for someone who runs a tax practice. Rather than gut a panel that works and
#   is wanted, this is a parallel pair. compose2.sh and compose_meeting.sh are
#   alternatives; running both would stack four xterms on one 1080p display.
#
# THE POINT OF THIS PANEL
#   It renders a file. During the meeting that file is overwritten from the
#   laptop every time the guest says something worth keeping, so he watches his
#   own business being written down while he is still describing it. That is the
#   demonstration. A panel that only shows what we prepared in advance is a
#   slide, and he explicitly asked not to be shown slides.
#
#   ship it with:  ssh HOST 'cat > /tmp/meeting_notes.txt' < local_notes.txt
#
# NEVER BLANK. If the file is absent the panel shows the agenda instead of an
# empty box - a blank panel at the moment a share starts reads as a broken rig.
set -uo pipefail

NOTES=/tmp/meeting_notes.txt

while true; do
  if [ -s "$NOTES" ]; then
    body=$(cat "$NOTES")
  else
    body=$(printf '%s\n' \
      "   Nothing captured yet - this fills in as we talk." \
      "" \
      "   What I would like to understand:" \
      "     - where the minutes go on a single return, end to end" \
      "     - what makes a client late: forgetting, or not knowing what you need" \
      "     - what still gets done by hand that you wish did not" \
      "     - where being wrong is expensive, and who catches it today")
  fi

  # Two clocks, because he is in Toronto and Mr. Salam is in Buenos Aires, and
  # a single UTC stamp makes both of them do arithmetic.
  tor=$(TZ=America/Toronto date +'%H:%M')
  bue=$(TZ=America/Argentina/Buenos_Aires date +'%H:%M')

  frame=$(printf '%s\n' \
    "" \
    "   SFDC 24  x  TAX EVERYDAY        Toronto ${tor}   Buenos Aires ${bue}" \
    "   ================================================================" \
    "" \
    "$body")
  printf '\033[H\033[2J%s' "$frame"
  sleep 3
done
