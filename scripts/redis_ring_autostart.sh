#!/usr/bin/env bash
# The Redis ring on a Linux desktop agent (Grok's box): keep scripts/redis_ring.py running across crashes and reboots.
#
# It matches the pattern Grok's box already uses for its rings: XFCE autostart starts one ensure loop at login, and
# the loop starts the ring again whenever it exits. A lock file keeps it to one loop per agent, so a second autostart
# or a hand start is a no-op. See docs/REDIS-RING.md.
#
#   scripts/redis_ring_autostart.sh grok -- /workspace/fleet-ring/grok_ring.sh {file}
#
# The XFCE autostart entry, ~/.config/autostart/redis-ring-grok.desktop (no secret in it):
#
#   [Desktop Entry]
#   Type=Application
#   Name=Redis ring (grok)
#   Exec=/bin/bash -lc '/workspace/Blackboard/scripts/redis_ring_autostart.sh grok -- /workspace/fleet-ring/grok_ring.sh {file}'
#   X-GNOME-Autostart-enabled=true
#   NoDisplay=true
#
# Settings come from the environment, none of them a secret:
#   REDIS_RING_BRIDGE_URL     the bridge URL                 (or REDIS_BRIDGE=on + REDIS_BRIDGE_URL already set)
#   REDIS_RING_IMPERSONATE    this agent's own keyless SA    (or REDIS_BRIDGE_IMPERSONATE already set)
#   REDIS_RING_PYTHON         default python3
#   REDIS_RING_LOG            default ~/.redis-ring-<agent>.log
# The wake command's own key (Grok Bot's webhook key) stays in that command's environment, as it is today. It is
# never passed here, never written to the log, and never in this repository.
#
# Exit codes of the ring decide the pause before the next start: a terminal one (2 denied or refused, 3 the bridge is
# not set up, 4 an unreadable cursor, 5 an entry with no id) waits TERMINAL_PAUSE, so a revoked grant is not asked
# every few seconds, and the ring comes back by itself once the cause is fixed. Anything else waits CRASH_PAUSE.
set -u

usage='usage: redis_ring_autostart.sh <agent> [--codex-thread UUID | -- COMMAND ... {message}|{file}]'
if [ $# -lt 1 ] || [ -z "$1" ]; then echo "$usage" >&2; exit 64; fi
agent="$1"
shift
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${REDIS_RING_PYTHON:-python3}"
log="${REDIS_RING_LOG:-$HOME/.redis-ring-$agent.log}"
lock="$HOME/.redis-ring-$agent.lock"
CRASH_PAUSE="${REDIS_RING_CRASH_PAUSE:-30}"
TERMINAL_PAUSE="${REDIS_RING_TERMINAL_PAUSE:-600}"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$lock"
  if ! flock -n 9; then
    echo "redis ring for $agent is already running (lock $lock)"
    exit 0
  fi
else
  echo "flock not found: no single-instance lock; start this loop once" >&2
fi

bridge=()
[ -n "${REDIS_RING_BRIDGE_URL:-}" ] && bridge+=(--bridge-url "$REDIS_RING_BRIDGE_URL")
[ -n "${REDIS_RING_IMPERSONATE:-}" ] && bridge+=(--impersonate "$REDIS_RING_IMPERSONATE")

while true; do
  "$python" -B "$here/redis_ring.py" --agent "$agent" --log "$log" ${bridge[@]+"${bridge[@]}"} "$@"
  code=$?
  case "$code" in
    2|3|4|5) pause="$TERMINAL_PAUSE" ;;
    *) pause="$CRASH_PAUSE" ;;
  esac
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ensure: redis_ring.py exited $code; starting again in ${pause}s" >>"$log"
  sleep "$pause"
done
