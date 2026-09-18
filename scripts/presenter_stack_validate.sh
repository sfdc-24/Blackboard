#!/bin/bash
# Assert that the presenter box is genuinely ready. Item 1 of grok-bot's
# permanent-fix plan, and the half that actually earns trust.
#
# WHY THIS IS SEPARATE FROM THE RESET
#   On 2026-09-18 this rig passed a self test, then a script was run out of
#   order, and the state that had passed no longer existed. Nothing noticed. The
#   reset can only claim it ran; this can claim the box is ready, and it says so
#   by failing loudly rather than by printing a reassuring line.
#
# EVERY CHECK IS A MEASUREMENT. No check here infers a fact from a related one:
# a bound capture stream is not proof of a meeting, and a running unit is not
# proof of a device. Each thing is read where it is actually true.
#
# Exit 0 only when every assertion passes. Exit 1 otherwise, with the failures
# listed. Safe to run at any time, changes nothing.

set -u
export DISPLAY=:99
FAIL=0
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*" >&2; FAIL=$((FAIL+1)); }
info() { echo "  ..    $*"; }

echo "=== presenter_stack_validate $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ---- 1. pipewire is the sound server here, and pulseaudio must be gone -------
for unit in pipewire pipewire-pulse; do
  if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
    ok "$unit active"
  else
    bad "$unit is NOT active - pactl will talk to nothing"
  fi
done

PACOUNT=$(pgrep -x -c pulseaudio 2>/dev/null || echo 0)
if [ "$PACOUNT" = "0" ]; then
  ok "no pulseaudio process (this is the R1 killer; it must stay absent)"
else
  bad "pulseaudio is running ($PACOUNT proc) - it will tear the modules down"
fi

# ---- 2. the virtual devices themselves, read from pactl not assumed ---------
SINKS=$(pactl list short sinks 2>/dev/null)
SOURCES=$(pactl list short sources 2>/dev/null)

for s in vmic zspk; do
  if printf '%s\n' "$SINKS" | grep -qE "[[:space:]]$s[[:space:]]|^[0-9]+[[:space:]]+$s\b"; then
    ok "sink $s present"
  else
    bad "sink $s MISSING"
  fi
done

if printf '%s\n' "$SOURCES" | grep -q '\bvmic_src\b'; then
  ok "source vmic_src present (this is what Zoom uses as its microphone)"
else
  bad "source vmic_src MISSING - Zoom would fall back to a dead default"
fi

if printf '%s\n' "$SOURCES" | grep -q '\bzspk\.monitor\b'; then
  ok "source zspk.monitor present (this is the ear)"
else
  bad "source zspk.monitor MISSING - nothing can hear the room"
fi

# auto_null alone is the exact fingerprint of the R1 collapse. Name it.
if printf '%s\n' "$SINKS" | grep -q 'auto_null' && ! printf '%s\n' "$SINKS" | grep -q '\bvmic\b'; then
  bad "only auto_null remains - this is the R1 signature, re-run presenter_voice_up.sh"
fi

# ---- 3. the displays -------------------------------------------------------
for d in 99 98; do
  if pgrep -f "Xvfb :$d" >/dev/null 2>&1; then
    ok "Xvfb :$d running"
  else
    bad "Xvfb :$d NOT running"
  fi
done

if command -v xdotool >/dev/null 2>&1 && xdotool getdisplaygeometry >/dev/null 2>&1; then
  ok "display :99 answers xdotool ($(xdotool getdisplaygeometry 2>/dev/null | tr '\n' 'x' | sed 's/x$//'))"
else
  bad "display :99 does not answer xdotool"
fi

# ---- 4. nothing should be holding the mic before a join ---------------------
# grok-bot asked for zero bound streams pre-join. That is right, and it is only
# right pre-join: during a meeting the correct answer is 1. Read which case we
# are in rather than asserting the wrong one.
BOUND=$(bash "$(dirname "$0")/zoom_bound_count.sh" vmic_src 2>/dev/null || echo "?")
if xdotool search --onlyvisible --name 'Meeting' >/dev/null 2>&1; then
  info "a meeting window is open, so a bound stream is expected here"
  if [ "$BOUND" = "1" ]; then
    ok "zoom capture streams bound to vmic_src = 1 (in a meeting)"
  else
    bad "in a meeting but bound streams = $BOUND (expected 1)"
  fi
else
  if [ "$BOUND" = "0" ]; then
    ok "zoom capture streams bound to vmic_src = 0 (clean, pre-join)"
  else
    bad "no meeting window but bound streams = $BOUND (expected 0) - stale client?"
  fi
fi

# ---- 5. the robotic voice must not be reachable ------------------------------
# R4. presenter_say.sh is espeak-ng, which he heard live and called "robotic and
# very unpleasant". grok-bot ruled it must not exist on disk, because a shim is
# still a regression vector under operator error.
if [ -e "/home/user/presenter_say.sh" ]; then
  bad "presenter_say.sh still on disk - the espeak path is reachable (R4)"
else
  ok "presenter_say.sh absent - nova via say_in_zoom.ps1 is the only synthesis path"
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "READY - every assertion passed."
  exit 0
fi
echo "NOT READY - $FAIL assertion(s) failed. Do not join a meeting on this state." >&2
exit 1
