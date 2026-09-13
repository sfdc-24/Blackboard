#!/bin/bash
# Exercise the dialogue player WITHOUT a sound card, an X server or a meeting.
#
# WHAT IS ACTUALLY BEING TESTED, as opposed to "it ran":
#   1. --dry-run speaks NOTHING - proved by a stub say that would leave a mark
#   2. the two personas get DIFFERENT voices, which is the tool's whole promise
#   3. an unknown persona is REFUSED, not quietly given the default voice
#   4. a one-persona file is refused as a monologue
#   5. a file with ANY bad turn speaks ZERO turns, not the good ones first
#   6. a turn that fails to speak STOPS the dialogue, and the count says so
#   7. the parser's persona list and the script's voice table are in step
#
# THE STUB IS THE POINT. Every check that cares about what was spoken replaces
# presenter_say.sh with a script that appends to a log. Asserting on that log
# is the only way to tell "spoke nothing" from "did not run", and those two are
# the pair this tool must never confuse.
set -uo pipefail

RIG="${1:?usage: test_presenter_dialogue.sh <path to tools/walkthrough>}"
RIG="$(cd "${RIG}" && pwd)"
pass=0
fail=0

ok()  { pass=$((pass+1)); echo "  PASS  $1"; }
bad() { fail=$((fail+1)); echo "  FAIL  $1"; }

WORK="$(mktemp -d /tmp/test-dialogue-XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT

# A sandbox copy of the rig, so a stubbed presenter_say.sh cannot touch the
# real one and a failed test cannot leave the checkout modified.
SAND="${WORK}/rig"
mkdir -p "${SAND}"
cp "${RIG}/presenter_dialogue.sh" "${RIG}/dialogue_parse.py" "${SAND}/"

SPOKEN="${WORK}/spoken.log"

# Stub say: records persona-relevant env and the text, then succeeds.
install_stub() {
    local mode="$1" failing_turn="${2:-0}"
    cat > "${SAND}/presenter_say.sh" <<STUB
#!/bin/bash
echo "\${PRESENTER_VOICE:-?} \${PRESENTER_PITCH:-?} \${PRESENTER_SPEED:-?} \$*" >> "${SPOKEN}"
if [ "${mode}" = "failafter" ]; then
    n=\$(wc -l < "${SPOKEN}")
    if [ "\$n" -ge "${failing_turn}" ]; then exit 1; fi
fi
exit 0
STUB
    : > "${SPOKEN}"
}

spoken_count() {
    if [ -s "${SPOKEN}" ]; then wc -l < "${SPOKEN}" | tr -d ' '; else echo 0; fi
}

write_json() { printf '%s\n' "$2" > "${WORK}/$1"; }

REAL="${RIG}/dialogue/ba-sa-rehearsal-20260912.json"

echo "=== presenter dialogue, executed ==="
echo

# ---- 1. the committed rehearsal validates and dry-runs ----------------------
install_stub ok
out=$(bash "${SAND}/presenter_dialogue.sh" "${REAL}" --dry-run 2>&1)
rc=$?
if [ "${rc}" -eq 0 ] && printf '%s' "${out}" | grep -q 'dry_run_ok turns=6'; then
  ok "the committed rehearsal dry-runs clean with 6 turns"
else
  bad "committed rehearsal dry-run rc=${rc}: $(printf '%s' "${out}" | tail -3)"
fi

# ---- 2. --dry-run speaks NOTHING --------------------------------------------
# The check that matters most: a presenter rehearsing during a live call must
# not put audio into the meeting. "It printed the text" is not evidence of that.
n=$(spoken_count)
if [ "${n}" -eq 0 ]; then
  ok "--dry-run invoked presenter_say.sh ZERO times"
else
  bad "--dry-run spoke ${n} turns - it must speak none"
fi

# ---- 3. the two personas get DIFFERENT voices -------------------------------
install_stub ok
bash "${SAND}/presenter_dialogue.sh" "${REAL}" >/dev/null 2>&1
n=$(spoken_count)
if [ "${n}" -eq 6 ]; then
  ok "a real run spoke all 6 turns"
else
  bad "a real run spoke ${n} of 6 turns"
fi

distinct=$(awk '{print $1, $2}' "${SPOKEN}" | sort -u | wc -l | tr -d ' ')
if [ "${distinct}" -eq 2 ]; then
  ok "the 6 turns used exactly 2 distinct voice+pitch pairs"
else
  bad "expected 2 distinct voice+pitch pairs across the turns, got ${distinct}"
fi

ba_voice=$(head -1 "${SPOKEN}" | awk '{print $1, $2}')
sa_voice=$(sed -n '2p' "${SPOKEN}" | awk '{print $1, $2}')
if [ "${ba_voice}" != "${sa_voice}" ]; then
  ok "BA (${ba_voice}) and SA (${sa_voice}) differ on accent AND pitch"
else
  bad "BA and SA both spoke as ${ba_voice} - the guest cannot tell them apart"
fi

# ---- 4. an unknown persona is refused, not defaulted ------------------------
install_stub ok
write_json unknown.json '[{"persona":"ba","text":"one"},{"persona":"cfo","text":"two"}]'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/unknown.json" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "an unknown persona is refused and nothing is spoken"
else
  bad "unknown persona: rc=${rc} spoke=${n} - it must refuse having spoken none"
fi

# ---- 5. a monologue is refused ----------------------------------------------
install_stub ok
write_json mono.json '[{"persona":"ba","text":"one"},{"persona":"ba","text":"two"}]'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/mono.json" 2>&1)
rc=$?
if [ "${rc}" -ne 0 ] && printf '%s' "${out}" | grep -qi 'monologue'; then
  ok "a single-persona file is refused as a monologue"
else
  bad "a one-voice file was accepted (rc=${rc})"
fi

# ---- 6. ONE bad turn means ZERO turns spoken --------------------------------
# All-or-nothing. The good turns must not go out first and strand the guest
# halfway through a conversation.
install_stub ok
write_json late_bad.json '[{"persona":"ba","text":"one"},{"persona":"sa","text":"two"},{"persona":"ba","text":""}]'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/late_bad.json" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "a file whose LAST turn is empty speaks zero turns, not the first two"
else
  bad "bad-last-turn file: rc=${rc} spoke=${n} - validation must precede speech"
fi

# ---- 7. malformed JSON is refused -------------------------------------------
install_stub ok
write_json broken.json '[{"persona":"ba","text":"one"'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/broken.json" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "malformed JSON is refused with nothing spoken"
else
  bad "malformed JSON: rc=${rc} spoke=${n}"
fi

# ---- 8. a failed turn STOPS the dialogue ------------------------------------
# presenter_say.sh exits non-zero when Zoom is not bound. If that happens at
# turn 3 the remaining turns must not play into a meeting that cannot hear the
# question they answer.
install_stub failafter 3
out=$(bash "${SAND}/presenter_dialogue.sh" "${REAL}" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 3 ]; then
  ok "a turn-3 failure stopped playback at 3 of 6 and exited non-zero"
else
  bad "turn-3 failure: rc=${rc} spoke=${n} of 6 (expected rc!=0 and 3)"
fi
if printf '%s' "${out}" | grep -q 'turn 3 of 6 did not land'; then
  ok "the failure names which turn did not land"
else
  bad "the failure did not say which turn stopped it"
fi

# ---- 8b. DRIFT AT RUNTIME speaks zero turns ---------------------------------
# Found by chatgpt-codex-connector reviewing PR85. Check 9 below compares the
# two persona lists statically, but a static check cannot bind a runtime
# promise: before the preflight existed, a parser that accepted a persona the
# voice table lacked spoke every turn up to it and only then refused. The
# all-or-nothing guarantee held for every malformed file and broke on exactly
# the case that splitting parser and table makes possible.
#
# So this mutates ONLY the parser, in a sandbox of its own so check 9 still
# sees an unmutated pair, and demands zero turns spoken.
DRIFT="${WORK}/drift"
mkdir -p "${DRIFT}"
cp "${SAND}/presenter_dialogue.sh" "${SAND}/dialogue_parse.py" "${DRIFT}/"
sed -i 's/KNOWN_PERSONAS = ("ba", "sa")/KNOWN_PERSONAS = ("ba", "sa", "cfo")/' \
    "${DRIFT}/dialogue_parse.py"

if grep -q '"cfo"' "${DRIFT}/dialogue_parse.py"; then
  ok "drift fixture: the sandbox parser was actually mutated"
else
  bad "drift fixture did NOT apply - the check below would pass vacuously"
fi

install_stub ok
cp "${SPOKEN}" /dev/null 2>/dev/null || true
cat > "${DRIFT}/presenter_say.sh" <<STUB
#!/bin/bash
echo "\${PRESENTER_VOICE:-?} \$*" >> "${SPOKEN}"
exit 0
STUB
write_json drift.json '[{"persona":"ba","text":"one"},{"persona":"sa","text":"two"},{"persona":"cfo","text":"three"},{"persona":"ba","text":"four"}]'
out=$(bash "${DRIFT}/presenter_dialogue.sh" "${WORK}/drift.json" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "parser/table drift refuses with ZERO turns spoken, not two then a stop"
else
  bad "drift: rc=${rc} spoke=${n} - expected rc!=0 with 0 spoken"
fi

# ---- 8c. --allow-partial: the operator's escape hatch ----------------------
# All-or-nothing is the right default and it has a real cost: a typo in the
# last turn refuses a rehearsal that is 95% fine, possibly at T-2. Partial
# plays the valid LEADING turns and stops - it must never SKIP a turn and
# carry on, because a dialogue missing its middle is the "answer with no
# question" failure this tool was built around.
install_stub ok
write_json partial.json '[{"persona":"ba","text":"one"},{"persona":"sa","text":"two"},{"persona":"ba","text":"three"},{"persona":"sa","text":""}]'

out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/partial.json" 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "without --allow-partial the default still refuses the whole file"
else
  bad "default changed: rc=${rc} spoke=${n} - all-or-nothing must remain the default"
fi

install_stub ok
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/partial.json" --allow-partial 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -eq 0 ] && [ "${n}" -eq 3 ]; then
  ok "--allow-partial plays the 3 valid leading turns and stops"
else
  bad "--allow-partial: rc=${rc} spoke=${n} of 3 valid"
fi
if printf '%s' "${out}" | grep -q 'PARTIAL: playing 3 of 4'; then
  ok "it says how many of how many it played"
else
  bad "a shortened rehearsal did not announce that it was shortened"
fi
if printf '%s' "${out}" | grep -q 'SHORTENED'; then
  ok "the shortening is repeated at the END, where the operator reads"
else
  bad "the last line described a truncated rehearsal as a clean one"
fi

# It must STOP, not skip. A bad turn 2 with valid 3 and 4 must play only 1.
install_stub ok
write_json midbad.json '[{"persona":"ba","text":"one"},{"persona":"sa","text":""},{"persona":"ba","text":"three"},{"persona":"sa","text":"four"}]'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/midbad.json" --allow-partial 2>&1)
rc=$?
n=$(spoken_count)
if [ "${n}" -le 1 ]; then
  ok "a bad middle turn stops playback rather than skipping to the good ones"
else
  bad "spoke ${n} turns across a gap - that puts an answer with no question in the room"
fi

# Trimming back to one voice is still a monologue and still refused.
install_stub ok
write_json trimmono.json '[{"persona":"ba","text":"one"},{"persona":"ba","text":""},{"persona":"sa","text":"three"}]'
out=$(bash "${SAND}/presenter_dialogue.sh" "${WORK}/trimmono.json" --allow-partial 2>&1)
rc=$?
n=$(spoken_count)
if [ "${rc}" -ne 0 ] && [ "${n}" -eq 0 ]; then
  ok "a partial run that trims back to ONE voice is refused as a monologue"
else
  bad "partial trimmed to a monologue and played it: rc=${rc} spoke=${n}"
fi

# ---- 9. parser personas and voice table are in step -------------------------
# These live in two files and drift silently: the parser would accept a persona
# the player has no voice for, and the player would refuse at playback time -
# during the meeting - rather than at validation time.
parser_personas=$(grep -o 'KNOWN_PERSONAS = ([^)]*)' "${SAND}/dialogue_parse.py" \
    | grep -o '"[a-z]*"' | tr -d '"' | sort | tr '\n' ' ')
table_personas=$(sed -n '/^voice_for()/,/^}/p' "${SAND}/presenter_dialogue.sh" \
    | grep -oE '^ *(ba|sa|[a-z]+)\)' | tr -d ' )' | grep -v '^\*$' | sort | tr '\n' ' ')
if [ "${parser_personas}" = "${table_personas}" ]; then
  ok "parser personas [${parser_personas}] match the voice table"
else
  bad "parser has [${parser_personas}] but the voice table has [${table_personas}]"
fi

# ---- 10. both scripts parse -------------------------------------------------
if bash -n "${RIG}/presenter_dialogue.sh" 2>"${WORK}/syn.err"; then
  ok "presenter_dialogue.sh parses"
else
  bad "presenter_dialogue.sh syntax error: $(cat "${WORK}/syn.err")"
fi

echo
echo "RESULT passed=${pass} failed=${fail}"
[ "${fail}" -eq 0 ] || exit 1
exit 0
