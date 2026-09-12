#!/bin/bash
# Play a two-persona dialogue into the live meeting, in two DISTINCT voices.
#
# WHY THIS EXISTS
#   presenter_say.sh speaks one line in one voice. The intake rehearsal is a
#   conversation between a business analyst and a solution architect, and read
#   in a single voice it is indistinguishable from one agent talking to itself.
#   That is the same failure the cross-check beat argues against: the value is
#   in two sides being separable, and if the guest cannot HEAR which side is
#   speaking, the demonstration has not been made.
#
# WHAT IT DELIBERATELY DOES NOT DO
#   It does not re-implement the Zoom binding check. presenter_say.sh carries
#   three paragraphs about getting that check wrong twice in opposite
#   directions; copying it here would be a fourth. Every turn goes through
#   presenter_say.sh, so its refusals - synthesis too small, counter
#   unreadable, no Zoom capture stream bound - are inherited exactly.
#
# IT STOPS ON THE FIRST FAILED TURN
#   A dialogue that loses turn 3 and plays 4 through 6 puts an answer with no
#   question in front of a guest, which is worse than stopping. The whole file
#   is validated before a single word is spoken, and playback halts the moment
#   a turn does not land.
#
# WHAT IT STILL DOES NOT PROVE
#   The same limit prove_voice.sh states: that a HUMAN HEARD IT. This proves
#   each turn reached the source Zoom captures. It says nothing about encoding,
#   transmission, or the far end.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SAY="${HERE}/presenter_say.sh"
PARSER="${HERE}/dialogue_parse.py"

DRY_RUN=0
FILE=""

# GAP BETWEEN TURNS. Not cosmetic: back-to-back synthesis runs together into
# one block of speech and the two personas stop sounding like a exchange.
GAP="${PRESENTER_DIALOGUE_GAP:-0.7}"

usage() {
    echo "usage: presenter_dialogue.sh <dialogue.json> [--dry-run]" >&2
    echo "" >&2
    echo "  --dry-run  validate and print the running order WITHOUT speaking." >&2
    echo "             Runs the identical parse and voice-resolution path, so" >&2
    echo "             a clean dry run means the file is playable. Use it to" >&2
    echo "             rehearse before the box is even up." >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage ;;
        -*) echo "unknown option: $1" >&2; usage ;;
        *)
            if [ -n "${FILE}" ]; then
                echo "give exactly one dialogue file" >&2
                usage
            fi
            FILE="$1"
            ;;
    esac
    shift
done

[ -n "${FILE}" ] || usage

# VOICE PER PERSONA.
#
# Chosen to differ on TWO axes, accent and pitch, not one. A guest on a laptop
# speaker through Zoom's codec loses fine pitch distinctions, and two voices
# that differ only by a few pitch points arrive as the same speaker. The
# parser refuses any persona not listed here, so this table and KNOWN_PERSONAS
# have to stay in step - test_presenter_dialogue.sh asserts that they do.
voice_for() {
    case "$1" in
        ba) echo "en-gb 140 45" ;;
        sa) echo "en-us 132 30" ;;
        *)  return 1 ;;
    esac
}

label_for() {
    case "$1" in
        ba) echo "BA (business analyst)" ;;
        sa) echo "SA (solution architect)" ;;
        *)  echo "$1" ;;
    esac
}

[ -r "${PARSER}" ] || { echo "cannot read ${PARSER}" >&2; exit 1; }
if [ "${DRY_RUN}" -eq 0 ] && [ ! -r "${SAY}" ]; then
    echo "cannot read ${SAY} - refusing to guess how to speak" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is missing" >&2; exit 1; }

# VALIDATE THE WHOLE FILE FIRST. Nothing below runs unless every turn parsed.
RECORDS="$(python3 "${PARSER}" "${FILE}")"
PARSE_RC=$?
if [ "${PARSE_RC}" -ne 0 ]; then
    echo "refusing to play ${FILE} - it did not validate" >&2
    exit 1
fi
if [ -z "${RECORDS}" ]; then
    echo "the parser produced no turns and still exited 0 - refusing" >&2
    exit 1
fi

TOTAL=$(printf '%s\n' "${RECORDS}" | grep -c '[^[:space:]]')
echo "=== dialogue: ${FILE} - ${TOTAL} turns ==="

spoken=0
while read -r index persona encoded; do
    [ -n "${index}" ] || continue

    # Positional split rather than a nested heredoc: this loop is already
    # being fed by one, and a heredoc inside a `read` inside that loop is a
    # construct nobody should have to reason about at 14:00.
    voice_spec="$(voice_for "${persona}")" || voice_spec=""
    set -- ${voice_spec}
    voice="${1:-}"
    speed="${2:-}"
    pitch="${3:-}"
    if [ -z "${voice}" ]; then
        echo "no voice mapped for persona '${persona}' at turn ${index} - the" >&2
        echo "parser accepted a persona this script cannot speak. They are out" >&2
        echo "of step; fix the table rather than defaulting." >&2
        exit 1
    fi

    text="$(printf '%s' "${encoded}" | base64 -d)"
    if [ -z "${text}" ]; then
        echo "turn ${index} decoded to nothing - refusing" >&2
        exit 1
    fi

    printf '\n[%s/%s] %s  (%s pitch=%s speed=%s)\n' \
        "${index}" "${TOTAL}" "$(label_for "${persona}")" "${voice}" "${pitch}" "${speed}"
    printf '    %s\n' "${text}"

    if [ "${DRY_RUN}" -eq 1 ]; then
        spoken=$((spoken + 1))
        continue
    fi

    # stdin closed for the child: this loop is reading from a heredoc, and a
    # child that consumes it would silently swallow the remaining turns. The
    # dialogue would end early and still exit 0.
    PRESENTER_VOICE="${voice}" \
    PRESENTER_SPEED="${speed}" \
    PRESENTER_PITCH="${pitch}" \
        bash "${SAY}" "${text}" </dev/null
    SAY_RC=$?
    if [ "${SAY_RC}" -ne 0 ]; then
        echo "" >&2
        echo "turn ${index} of ${TOTAL} did not land (presenter_say.sh exit ${SAY_RC})." >&2
        echo "STOPPING with ${spoken} turns already spoken. Playing the rest would" >&2
        echo "put an answer with no question in front of the guest." >&2
        exit 1
    fi
    spoken=$((spoken + 1))
    sleep "${GAP}"
done <<EOF_RECORDS
${RECORDS}
EOF_RECORDS

echo ""
if [ "${DRY_RUN}" -eq 1 ]; then
    echo "dry_run_ok turns=${spoken} file=${FILE}"
    echo "Validated and voiced-resolved only. NOTHING was spoken."
else
    echo "dialogue_ok turns=${spoken} file=${FILE}"
fi

# The count is returned rather than implied by exit 0, so a caller can assert
# on it. A run that speaks 4 of 6 turns and exits 0 is the failure this line
# exists to make visible.
if [ "${spoken}" -ne "${TOTAL}" ]; then
    echo "spoke ${spoken} of ${TOTAL} turns" >&2
    exit 1
fi
