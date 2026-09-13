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


class TurnError(Exception):
    """One turn is unplayable. Raised rather than exiting, so the caller can
    decide between refusing the file and stopping at this turn."""


def main(argv):
    # --allow-partial exists because of a real cost, not a preference.
    #
    # All-or-nothing is the right DEFAULT: a dialogue is played into a live
    # meeting, and discovering at turn 5 that turn 6 is malformed leaves five
    # turns already spoken. But the cost of that default is a typo in the last
    # turn refusing a rehearsal that is 95% fine, possibly at T-2 with a guest
    # already on the call. Refusing to speak at all is not obviously better
    # than speaking the part that is known good.
    #
    # So the operator gets the choice, and the default stays safe. Partial
    # plays the valid LEADING turns and stops at the first bad one - it never
    # SKIPS a turn and carries on, because a dialogue missing its middle puts
    # an answer with no question in front of the guest, which is the failure
    # this tool was built around.
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = [a for a in argv[1:] if a.startswith("--")]
    allow_partial = "--allow-partial" in flags

    for f in flags:
        if f != "--allow-partial":
            sys.stderr.write("unknown option: " + f + "\n")
            raise SystemExit(2)
    if len(args) != 1:
        sys.stderr.write("usage: dialogue_parse.py <dialogue.json> [--allow-partial]\n")
        raise SystemExit(2)

    path = args[0]
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

    def check(turn, index):
        """Return (persona, text) or raise TurnError. Raising rather than
        exiting is what lets --allow-partial stop here instead of dying."""
        where = "turn " + str(index)
        if not isinstance(turn, dict):
            raise TurnError(where + " is " + type(turn).__name__ + ", expected an object")

        persona = turn.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            raise TurnError(where + " has no 'persona'")
        persona = persona.strip().lower()
        if persona not in KNOWN_PERSONAS:
            raise TurnError(
                where + " has persona '" + persona + "', which has no voice on "
                "this rig. Known: " + ", ".join(KNOWN_PERSONAS) + ". Refusing "
                "rather than defaulting, so two speakers cannot silently "
                "become one voice.")

        text = turn.get("text")
        if not isinstance(text, str):
            raise TurnError(where + " has no 'text' string")
        # Collapse whitespace: espeak-ng reads a newline as a pause long enough
        # to sound like the speaker stopped, and JSON prose is often wrapped.
        text = " ".join(text.split())
        if not text:
            raise TurnError(where + " has empty text - refusing to speak a silent turn")
        if len(text) > MAX_TURN_CHARS:
            raise TurnError(
                where + " is " + str(len(text)) + " chars, over the "
                + str(MAX_TURN_CHARS) + " limit. Split it: a single "
                "uninterruptible block that long cannot be talked over by a "
                "human who wants to interject.")
        return persona, text

    records = []
    seen_personas = set()
    stopped_at = None
    for index, turn in enumerate(turns, start=1):
        try:
            persona, text = check(turn, index)
        except TurnError as exc:
            if not allow_partial:
                fail(str(exc))
            # Stop here. Do NOT skip this turn and keep going: a dialogue
            # missing its middle puts an answer with no question in front of
            # the guest, which is worse than a short rehearsal.
            stopped_at = (index, str(exc))
            break

        seen_personas.add(persona)
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        records.append(str(index) + " " + persona + " " + encoded)

    if not records:
        fail("no playable turns" + (": " + stopped_at[1] if stopped_at else ""))

    # A "dialogue" with one speaker is a monologue, and playing it through this
    # tool would present one voice as though the cross-examination happened.
    # This applies to the PLAYED set, so a partial run that trims back to one
    # voice is refused for the same reason a one-persona file is.
    if len(seen_personas) < 2:
        fail("the playable turns are all persona '" + list(seen_personas)[0]
             + "'. That is a monologue, not a dialogue - use presenter_say.sh "
             "for a single voice, or fix the turn that stopped it"
             + (" (" + stopped_at[1] + ")" if stopped_at else ""))

    if stopped_at:
        # stderr, so it cannot be mistaken for a playable record on stdout,
        # and loud, because a shortened rehearsal that nobody noticed was
        # shortened is its own kind of false green.
        sys.stderr.write(
            "PARTIAL: playing " + str(len(records)) + " of " + str(len(turns))
            + " turns; stopped at " + stopped_at[1] + "\n")

    sys.stdout.write("\n".join(records) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
