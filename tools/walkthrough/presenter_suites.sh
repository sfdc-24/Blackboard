#!/bin/bash
# Display only. The whole frame is assembled in memory and written in ONE
# printf, so the terminal is never caught between a clear and a redraw. The
# suite results come from a file that the runner replaces atomically.
while true; do
  if [ -f /tmp/suite_results.txt ]; then
    results=$(cat /tmp/suite_results.txt)
  else
    results="   (first run still in progress)"
  fi
  frame=$(printf '%s\n' \
    "" \
    "   ORDER MIGRATION SUITES  -  running live on Linux / pwsh 7.5.4" \
    "   ===============================================================" \
    "" \
    "$results" \
    "" \
    "   The group-drain suite's load-bearing test is a NEGATIVE CONTROL:" \
    "   it points the kill binary at /bin/true, which signals nothing, and" \
    "   REQUIRES a non-zero return with the process group still alive." \
    "   A containment check that cannot fail is not a containment check.")
  printf '\033[H\033[2J%s' "$frame"
  sleep 3
done
