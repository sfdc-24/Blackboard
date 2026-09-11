#!/bin/bash
# Two non-overlapping panels on :99, plus the suite runner that feeds the lower
# one. Does NOT touch the Zoom client - it can be re-run while a share is live.
set -uo pipefail
export DISPLAY=:99

pkill -f presenter_loop 2>/dev/null
pkill -f presenter_suites 2>/dev/null
pkill -x xterm 2>/dev/null
sleep 1

chmod +x "$HOME/presenter_loop.sh" "$HOME/presenter_suites.sh" "$HOME/presenter_suites_runner.sh"

# The runner is separate from the display so the panel never shows a partial
# result set while a suite is mid-run.
if ! pgrep -f presenter_suites_runner >/dev/null 2>&1; then
  nohup "$HOME/presenter_suites_runner.sh" >/tmp/suite_runner.log 2>&1 &
  disown
fi

# Geometry chosen so the two panels do NOT overlap. The first attempt put the
# bottom window at y=560 while the top one was 679 tall from y=30, so the top
# panel was clipped by 150px and its header was never visible.
# At font size 15 a cell is about 13x24, so 132x24 is roughly 1720x600.
nohup xterm -geometry 132x24+40+24 -fa 'DejaVu Sans Mono' -fs 15 \
      -bg '#06131b' -fg '#5ff2a8' -title 'SFDC24 board' \
      -e "$HOME/presenter_loop.sh" >/tmp/xterm_top.log 2>&1 &
disown
sleep 2
nohup xterm -geometry 132x16+40+672 -fa 'DejaVu Sans Mono' -fs 15 \
      -bg '#1b1206' -fg '#ffc857' -title 'SFDC24 suites' \
      -e "$HOME/presenter_suites.sh" >/tmp/xterm_bot.log 2>&1 &
disown
sleep 5

echo "xterms: $(pgrep -c -x xterm || echo 0)  runner: $(pgrep -f presenter_suites_runner | wc -l)"
for w in $(xdotool search --onlyvisible --name 'SFDC24' 2>/dev/null); do
  echo "  '$(xdotool getwindowname "$w")'  $(xdotool getwindowgeometry --shell "$w" | grep -E '^Y=|HEIGHT' | tr '\n' ' ')"
done
