#!/bin/bash
# Count Zoom capture streams bound to a given PulseAudio source.
#
#   usage:  zoom_bound_count.sh <source-name> [pactl-output-file]
#           (with no file, it asks pactl itself)
#
# WHY THIS IS ITS OWN FILE
#   Because the last two versions of this check were wrong in opposite directions and
#   NEITHER WAS EVER EXECUTED against real pactl output.
#
#   v1 counted ALL source-outputs. prove_voice.sh's own parec creates one, so the
#   check passed with Zoom absent - the harness satisfied it by measuring itself.
#
#   v2 (mine, "fixed") matched the source NAME against each record. But pactl prints
#   `Source: 94` - a NUMERIC INDEX, never the name. So it matched nothing and reported
#   ZOOM_BOUND=0 for a correctly bound Zoom, refusing to speak. codex reproduced that
#   with a native-shaped fixture.
#
#   I "verified" v2 by grepping the script for the new string. That tests the TEXT of
#   a fix, not its behaviour, which is how a fix ships broken while its test is green.
#   Splitting the parser out means a fixture can drive it with no live box, no Zoom and
#   no audio hardware - so it is checked on every run of the regression test instead of
#   only during a walkthrough, when being wrong is expensive.
#
# Prints a single integer. Exits 0 if it could look, 2 if it could not.
set -uo pipefail

SOURCE_NAME="${1:-}"
FIXTURE="${2:-}"

if [ -z "${SOURCE_NAME}" ]; then
    echo "usage: zoom_bound_count.sh <source-name> [pactl-output-file]" >&2
    exit 2
fi

if [ -n "${FIXTURE}" ]; then
    [ -r "${FIXTURE}" ] || { echo "cannot read fixture ${FIXTURE}" >&2; exit 2; }
    SHORT_SOURCES=$(sed -n '/^### short sources$/,/^### source-outputs$/p' "${FIXTURE}" | sed '1d;$d')
    OUTPUTS=$(sed -n '/^### source-outputs$/,$p' "${FIXTURE}" | sed '1d')
else
    command -v pactl >/dev/null 2>&1 || { echo "pactl missing" >&2; exit 2; }
    # A FAILED QUERY IS NOT AN ANSWER OF ZERO.
    #
    # These used to end in 2>/dev/null with no status check, so a dead PulseAudio
    # printed nothing, the parser counted nothing, and the caller read "Zoom is not
    # listening" - a measurement error wearing the costume of a measurement. Exit 2
    # means "could not look", which is a different thing from "looked and saw none",
    # and callers must be able to tell them apart.
    if ! SHORT_SOURCES=$(pactl list short sources 2>/dev/null); then
        echo "pactl could not list sources - PulseAudio/PipeWire is not answering" >&2
        exit 2
    fi
    if ! OUTPUTS=$(pactl list source-outputs 2>/dev/null); then
        echo "pactl could not list source-outputs" >&2
        exit 2
    fi
    if [ -z "${SHORT_SOURCES}" ]; then
        echo "pactl returned no sources at all - the audio stack is not up" >&2
        exit 2
    fi
fi

# Name -> numeric index. This is the step v2 skipped, and the whole defect.
INDEX=$(printf '%s\n' "${SHORT_SOURCES}" | awk -v n="${SOURCE_NAME}" '$2 == n { print $1; exit }')
if [ -z "${INDEX}" ]; then
    echo 0
    exit 0
fi

# Walk the source-output records. A record starts at "Source Output #N" and ends at
# the next one; within it we need BOTH the numeric Source and a Zoom-looking client.
# IDENTITY COMES FROM AN ALLOWLIST OF FIELDS, NOT FROM THE WORD APPEARING ANYWHERE.
#
# The previous version set is_zoom on any record line matching /zoom/. On THIS rig
# that is catastrophic: the instance is called zoom-presenter-tmp, so pactl's
# `application.process.host = "zoom-presenter-tmp"` appears on EVERY record - including
# the parec stream that prove_voice.sh creates. The count would go back to matching the
# test harness measuring itself, which is the exact bug the previous commit fixed.
#
# The word "zoom" in a HOSTNAME is not evidence that Zoom is listening. Only the fields
# that name the application are, so only those are consulted.
printf '%s\n' "${OUTPUTS}" | awk -v idx="${INDEX}" '
    function flush() { if (on_src && is_zoom) count++ ; on_src=0; is_zoom=0 }
    function value(line,   v) {
        v = line
        sub(/^[^=]*=[[:space:]]*/, "", v)
        gsub(/^"|"$/, "", v)
        return tolower(v)
    }
    /^Source Output #/            { flush() }
    /^[[:space:]]*Source:[[:space:]]/ {
                                    v=$0; sub(/^[[:space:]]*Source:[[:space:]]*/, "", v)
                                    if (v+0 == idx+0) on_src=1
                                  }
    # Allowlisted identity fields only. application.process.host and node.name are
    # deliberately NOT here: the first carries the machine name, the second is set by
    # the audio server rather than the application.
    /^[[:space:]]*application\.name[[:space:]]*=/            { if (value($0) ~ /zoom/) is_zoom=1 }
    /^[[:space:]]*application\.process\.binary[[:space:]]*=/ { if (value($0) ~ /zoom/) is_zoom=1 }
    /^[[:space:]]*application\.id[[:space:]]*=/              { if (value($0) ~ /zoom/) is_zoom=1 }
    END                           { flush(); print count+0 }
'
exit 0
