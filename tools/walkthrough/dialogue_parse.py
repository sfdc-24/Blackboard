#!/usr/bin/env python3
"""Validate a persona dialogue file and emit one playable record per turn.

WHY THIS IS A SEPARATE, TESTED FILE
  presenter_say.sh carries three comment paragraphs about a listener check that
  was written inline twice and was wrong both times, in opposite directions,
  because neither version was ever run against real input. The fix there was to
  move the count into zoom_bound_count.sh and drive it from committed fixtures.
  This parser is that lesson applied before the fact rather than after it.

WHY IT VALIDATES EVERYTHING BEFORE ANYTHING IS SPOKEN
  A dialogue is played into a LIVE meeting. Discovering at turn 5 that turn 6 is
  malformed leaves five turns already spoken and no way to take them back. So
  the contract is all-or-nothing: this exits non-zero having emitted nothing
  unless every turn is playable.

OUTPUT
  One line per turn:  <index> <persona> <base64 of the text>
  Base64 because the text is free prose that may contain quotes, tabs and
  newlines, and a delimiter chosen to be "probably not in the text" is a bug
  that waits for the one client note that contains it.
"""
import base64
import json
import sys

# The personas this rig can actually give DISTINCT voices to. An unknown
# persona is refused rather than defaulted, because the whole promise of a
# two-voice rehearsal is that the guest can hear which side is speaking. A
# silent fallback to the default voice keeps the transcript correct and makes
# the audio a monologue - the exact shape of lie this walkthrough argues
# against elsewhere (one model sampled twice presented as two opinions).
KNOWN_PERSONAS = ("ba", "sa")

MAX_TURN_CHARS = 1200


def fail(msg):
    sys.stderr.write("dialogue_parse: " + msg + "\n")
    raise SystemExit(1)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: dialogue_parse.py <dialogue.json>\n")
        raise SystemExit(2)

    path = argv[1]
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        fail("cannot read " + path + ": " + str(exc))

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        fail(path + " is not valid UTF-8 JSON: " + str(exc))

    # Accept both the bare list Codex sent on PR83 and a wrapped object, so a
    # contribution does not have to be reshaped by hand before it can be heard.
    if isinstance(doc, dict):
        turns = doc.get("turns")
        if turns is None:
            fail("object form needs a 'turns' array")
    else:
        turns = doc

    if not isinstance(turns, list):
        fail("expected a JSON array of turns, got " + type(turns).__name__)
    if not turns:
        fail("the dialogue is empty - refusing to claim a rehearsal happened")

    records = []
    seen_personas = set()
    for index, turn in enumerate(turns, start=1):
        where = "turn " + str(index)
        if not isinstance(turn, dict):
            fail(where + " is " + type(turn).__name__ + ", expected an object")

        persona = turn.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            fail(where + " has no 'persona'")
        persona = persona.strip().lower()
        if persona not in KNOWN_PERSONAS:
            fail(where + " has persona '" + persona + "', which has no voice on "
                 "this rig. Known: " + ", ".join(KNOWN_PERSONAS) + ". Refusing "
                 "rather than defaulting, so two speakers cannot silently "
                 "become one voice.")

        text = turn.get("text")
        if not isinstance(text, str):
            fail(where + " has no 'text' string")
        # Collapse whitespace: espeak-ng reads a newline as a pause long enough
        # to sound like the speaker stopped, and JSON prose is often wrapped.
        text = " ".join(text.split())
        if not text:
            fail(where + " has empty text - refusing to speak a silent turn")
        if len(text) > MAX_TURN_CHARS:
            fail(where + " is " + str(len(text)) + " chars, over the "
                 + str(MAX_TURN_CHARS) + " limit. Split it: a single "
                 "uninterruptible block that long cannot be talked over by a "
                 "human who wants to interject.")

        seen_personas.add(persona)
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        records.append(str(index) + " " + persona + " " + encoded)

    # A "dialogue" with one speaker is a monologue, and playing it through this
    # tool would present one voice as though the cross-examination happened.
    if len(seen_personas) < 2:
        fail("every turn is persona '" + list(seen_personas)[0] + "'. That is a "
             "monologue, not a dialogue - use presenter_say.sh for a single "
             "voice, or add the other side.")

    sys.stdout.write("\n".join(records) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
