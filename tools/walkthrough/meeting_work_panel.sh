#!/bin/bash
# BOTTOM panel for a prospect meeting. Shows WORK HAPPENING, not status.
#
# It renders /tmp/meeting_work.txt, shipped from the laptop the same way as the
# notes panel. During the call it carries, in order:
#
#   Beat 2  two agents answering the same question independently, and the
#           DISAGREEMENT left visible rather than averaged away. That is the
#           product: you cannot buy confidence from one model, only from two
#           that had to agree in front of you.
#   Beat 3  the thing being built, as it is built.
#
# WHY THE DISAGREEMENT STAYS ON SCREEN
#   Every demo of this kind shows agents agreeing, which proves nothing - two
#   models trained alike agree on wrong answers too. The only interesting frame
#   is the one where they split and the board refuses to pick a winner. For a
#   tax practice, where a wrong number arrives later as a CRA letter, that frame
#   IS the value. If it never appears during the call, the call did not show the
#   product.
set -uo pipefail

WORK=/tmp/meeting_work.txt

# Same row budget as the notes panel: blank, title, rule, blank, and one held
# back so nothing is pushed off the bottom.
visible_rows() {
  local rows
  rows=$(tput lines 2>/dev/null)
  case "$rows" in ''|*[!0-9]*) rows=24 ;; esac
  rows=$((rows - 5))
  [ "$rows" -lt 3 ] && rows=3
  printf '%s' "$rows"
}

while true; do
  if [ -s "$WORK" ]; then
    # TAIL, NOT CAT - and it matters more here than in the notes panel, because
    # this one carries the output of something running. Work output only ever
    # grows, and the interesting line is always the most recent.
    body=$(tail -n "$(visible_rows)" "$WORK")
  else
    body=$(printf '%s\n' \
      "   Idle - waiting for something to work on." \
      "" \
      "   This panel shows two agents taking the SAME question and answering" \
      "   it separately. Where they agree, the line goes green. Where they" \
      "   disagree, it stops and waits for a human rather than picking one." \
      "" \
      "   Nothing here is pre-recorded.")
  fi

  frame=$(printf '%s\n' \
    "" \
    "   WORKING  -  live, on this machine, right now" \
    "   ================================================================" \
    "" \
    "$body")
  printf '\033[H\033[2J%s' "$frame"
  sleep 3
done
