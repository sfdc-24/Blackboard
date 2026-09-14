#!/bin/bash
# Runs the suites and writes a COMPLETE result block to a file. The display
# panel only ever reads a finished file, so a viewer never sees a half-drawn
# frame or an empty one while a suite is mid-run.
cd "$HOME/ordertest" || exit 1
OUT=/tmp/suite_results.txt
while true; do
  tmp="$OUT.tmp"
  : > "$tmp"
  for t in test_order_group_drain test_order_linux_containment test_order_linux_host; do
    line=$(pwsh -NoProfile -File "tests/$t.ps1" 2>&1 | grep -E "^RESULT|passed," | tail -1)
    [ -z "$line" ] && line="(no verdict)"
    printf "   %-34s %s\n" "$t" "$line" >> "$tmp"
  done
  printf "\n   last completed run  %s\n" "$(date -u +%H:%M:%SZ)" >> "$tmp"
  mv -f "$tmp" "$OUT"
  sleep 20
done
