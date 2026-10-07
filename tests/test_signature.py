"""A board row must say WHICH instance wrote it, and how it got here.

Mr Salam, 2026-10-06: "signs should reflect who its from; claude mobile or claude code cli makes a
difference, also good to know who relayed the message, direct or by aya".

TWO THINGS ARE UNDER TEST, and they fail for different reasons.

    THE GUARD. The cases below are the shapes the ruling is about, including the real one that
    prompted it: four governance rulings for this fleet signed "claude-mobile (Claude, governance +
    data security gate)" and relayed by aya. Read as "Claude" that looks like the instance holding
    governance on its own lane; claude-mobile is a Drive-only phone surface that cannot reach the
    repository, the gateway or any cloud runtime, and two of its premises were false against the
    live Redis instance precisely because it could not measure them.

    THE ROSTER the guard reads. scripts/signature.py is only as good as scripts/agent_roster.json:
    a duplicate id, a family with no instances or an instance with no wake path all turn the guard
    into something that passes rows it should refuse. Those checks live here so they run on every
    branch, including ones with no Redis tooling in them at all.

WHY A NEGATIVE CONTROL IS THE POINT. The first version of this guard also demanded an explicit
`via=` marker on every row. Measured against 200 live board rows it would have refused 198 - every
active writer on the fleet. The same sample held zero family-name signatures, zero unknown tags and
zero malformed relays: the shape the owner asked for was already the habit, and only the marker was
missing. A guard is not proven by the traffic it passes; it is proven by what it refuses.

No network. Reads one file in the repository and nothing else.
"""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import signature  # noqa: E402

ROSTER = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "agent_roster.json"


class SignatureGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.known = signature.roster()
        if not cls.known["instances"]:
            raise AssertionError("the roster could not be read, so none of this tested anything")

    def refuses(self, source_tag, payload, because):
        problems = signature.check(source_tag, payload, self.known)
        self.assertTrue(problems, "should have been refused (%s) but was allowed: %r"
                        % (because, payload))
        return problems

    def allows(self, source_tag, payload):
        problems = signature.check(source_tag, payload, self.known)
        self.assertEqual([], problems, "should have been allowed but was refused: %s" % problems)

    # ---- the shapes the ruling is about ----------------------------------------------------

    def test_family_signature_is_refused_naming_the_candidates(self):
        """from=claude is the case in the ruling. The refusal must say which instances it could be,
        because "be more specific" is not actionable and a list of seven is."""
        problems = self.refuses(
            "claude", "BCB|v=1|id=X|from=claude|to=ALL|text=governance rulings",
            "claude is a family, not an instance")
        joined = " ".join(problems)
        self.assertIn("FAMILY", joined)
        self.assertIn("claude-code-cli", joined)
        self.assertIn("claude-mobile", joined)

    def test_a_relay_signed_as_if_direct_is_refused(self):
        """claude-mobile's words arriving under aya's Source_Tag with from=claude-mobile and no
        relayer: the reader cannot tell which of the two fields is the lie."""
        self.refuses("aya", "BCB|v=1|id=X|from=claude-mobile|to=ALL|text=rulings",
                     "from and Source_Tag disagree with no relay declared")

    def test_a_relay_must_say_who_carried_it(self):
        self.refuses("aya", "BCB|v=1|id=X|claimed_author=claude-mobile|to=ALL",
                     "claimed_author without relayer")

    def test_a_relay_must_say_whose_words_these_are(self):
        self.refuses("aya", "BCB|v=1|id=X|relayer=aya|to=ALL",
                     "relayer without claimed_author")

    def test_the_relayer_is_the_sender(self):
        """A row whose relayer is not its Source_Tag claims to have come from somewhere it did not."""
        self.refuses("grok", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|to=ALL",
                     "relayer and Source_Tag differ")

    def test_a_relay_of_oneself_is_not_a_relay(self):
        self.refuses("aya", "BCB|v=1|id=X|relayer=aya|claimed_author=aya|to=ALL",
                     "relayer equals claimed_author")

    def test_an_unenrolled_tag_is_refused_with_the_fix(self):
        """Nobody downstream can check a tag the roster does not hold, so the refusal carries the
        one-line remedy rather than just the complaint."""
        problems = self.refuses("claude-watch", "BCB|v=1|id=X|from=claude-watch|to=ALL",
                                "not in the roster")
        self.assertIn("agent_roster.json", " ".join(problems))

    def test_via_without_a_relayer_is_refused(self):
        self.refuses("claude-code-cli",
                     "BCB|v=1|id=X|from=claude-code-cli|via=aya|to=ALL",
                     "via names a path but no relayer is given")

    # ---- the shapes that must keep working -------------------------------------------------

    def test_the_relay_shape_aya_actually_wrote_is_allowed(self):
        """Aya invented relayer/claimed_author/relay_only/source_claims unprompted. The rule makes
        that non-optional; it must not punish the agent that got there first."""
        self.allows("aya", "BCB|v=1|id=CM-20261006-002|relayer=aya|claimed_author=claude-mobile"
                           "|to=ALL|relay_only=true|source_claims=unverified")

    def test_an_ordinary_direct_row_is_allowed(self):
        self.allows("claude-code-cli", "BCB|v=1|id=X|from=claude-code-cli|to=aya|text=hello")

    def test_via_direct_is_allowed(self):
        self.allows("claude-code-cli", "BCB|v=1|id=X|from=claude-code-cli|via=direct|to=ALL")

    def test_a_family_name_that_is_also_an_instance_id_is_not_ambiguous(self):
        """grok, gemini and cursor are both a family and an instance. Calling those ambiguous would
        refuse the two most prolific writers on the board for no reason."""
        for tag in ("grok", "gemini", "cursor"):
            if tag in self.known["instances"]:
                self.allows(tag, "BCB|v=1|id=X|from=%s|to=ALL" % tag)

    def test_an_inbound_owner_channel_row_needs_no_signature(self):
        """A WhatsApp message he sends arrives with no agent author because it HAS none. The
        exemption is read off the roster's owner family, not hardcoded by tag."""
        for tag in self.known["families"].get("owner", []):
            self.allows(tag, "BCB|v=1|id=WRK-1|to=ALL|text=from the owner")

    # ---- the guard cannot quietly not run --------------------------------------------------

    def test_a_missing_roster_is_reported_not_passed(self):
        """A guard that cannot run has not passed. With no roster, check() must say so rather than
        return clean - otherwise deleting one file silently disables the rule."""
        problems = signature.check("claude-code-cli", "BCB|v=1|from=claude|to=ALL",
                                   {"instances": {}, "families": {}})
        self.assertTrue(problems)
        self.assertIn("SIGNATURE_UNCHECKED", problems[0])


class RosterIsUsable(unittest.TestCase):
    """The roster is the guard's only authority, so its shape is part of the guard."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(ROSTER.read_text(encoding="utf-8"))

    def test_every_instance_id_is_unique_case_insensitively(self):
        """Codex has written under seven sender tags and a case-sensitive watcher missed
        CODEX-DESKTOP rows once. Two ids differing only in case would resolve to one canon entry."""
        seen = {}
        for fam in self.data["families"]:
            for inst in fam["instances"]:
                key = inst["id"].lower()
                self.assertNotIn(key, seen,
                                 "%r appears in both %s and %s" % (inst["id"], seen.get(key),
                                                                   fam["family"]))
                seen[key] = fam["family"]

    def test_every_instance_names_its_wake_path(self):
        """"wake Aya" cost an hour on 2026-10-06 because the roster listed surfaces and not routes.
        NONE is a legitimate answer; silence is not."""
        for fam in self.data["families"]:
            for inst in fam["instances"]:
                self.assertTrue(str(inst.get("wake", "")).strip(),
                                "%s names no wake path" % inst["id"])

    def test_access_claims_cannot_contradict_reachability(self):
        """An instance outside the VPC cannot read redis-central, and write without read is not a
        thing. A roster that asserts either would be read as an access grant."""
        for fam in self.data["families"]:
            for inst in fam["instances"]:
                if inst.get("redis_read"):
                    self.assertTrue(inst.get("in_vpc"),
                                    "%s claims redis_read from outside the VPC" % inst["id"])
                if inst.get("redis_write"):
                    self.assertTrue(inst.get("redis_read"),
                                    "%s claims write without read" % inst["id"])

    def test_every_family_has_at_least_one_instance(self):
        """signature.py resolves a family name to its instance list. An empty family would report
        that a tag "could mean" nothing at all."""
        for fam in self.data["families"]:
            self.assertTrue(fam["instances"], "family %s has no instances" % fam["family"])


class ARelayCannotNameThreeAuthors(unittest.TestCase):
    """Codex, reviewing 314bead: Source_Tag=aya, relayer=aya, claimed_author=claude-mobile and
    from=claude-code-cli passed with NO problems - three different authors in one row, waved through
    by the rule that exists to remove exactly that ambiguity.

    The cause was narrow and worth naming: the DIRECT branch compared from= against Source_Tag, and
    the relay branch compared nothing at all. So the field a reader is most likely to trust went
    unchecked precisely where it is most likely to lie."""

    def setUp(self):
        self.known = signature.roster()
        if not self.known["instances"]:
            raise AssertionError("no roster, so this tested nothing")

    def test_a_relay_whose_from_contradicts_its_author_is_refused(self):
        problems = signature.check(
            "aya",
            "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|from=claude-code-cli|to=ALL",
            self.known)
        self.assertTrue(problems)
        self.assertIn("may only restate its author", " ".join(problems))

    def test_a_relay_whose_from_names_the_relayer_is_refused(self):
        """Not allowed either: the relayer is already in relayer= and in Source_Tag, so letting
        from= name it too gives a reader two plausible authors and nothing to choose between."""
        problems = signature.check(
            "aya", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|from=aya|to=ALL",
            self.known)
        self.assertTrue(problems)

    def test_a_relay_may_restate_its_author_in_from(self):
        """Allowed, because it adds no second answer: from= says what claimed_author says."""
        self.assertEqual([], signature.check(
            "aya", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|from=claude-mobile|to=ALL",
            self.known))

    def test_a_relay_with_no_from_is_still_fine(self):
        """The shape Aya actually writes. The fix must not punish the agent that got there first."""
        self.assertEqual([], signature.check(
            "aya", "BCB|v=1|id=CM-1|relayer=aya|claimed_author=claude-mobile|to=ALL"
                   "|relay_only=true|source_claims=unverified",
            self.known))

    def test_via_cannot_name_a_different_carrier(self):
        problems = signature.check(
            "aya", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|via=grok|to=ALL",
            self.known)
        self.assertTrue(any("two different carriers" in p for p in problems))

    def test_via_direct_on_a_relay_is_refused(self):
        problems = signature.check(
            "aya", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|via=direct|to=ALL",
            self.known)
        self.assertTrue(any("both cannot be on the record" in p for p in problems))


class ARowCannotStateAFieldTwice(unittest.TestCase):
    """Codex, reviewing PR 336: a payload repeating claimed_author with claude-mobile AND
    claude-code-cli passed check() and append.py's conflict check both - a row naming two authors,
    through the one guard written to stop exactly that.

    Two causes, both worth keeping a test on: field() returns the FIRST match, so a second value was
    invisible here; and append.py's AUTHORITY_KEYS list predated the relay fields, so the newest
    authority keys on the fleet were the only ones a duplicate could slip past."""

    def setUp(self):
        self.known = signature.roster()
        if not self.known["instances"]:
            raise AssertionError("no roster, so this tested nothing")

    def test_a_repeated_claimed_author_with_two_values_is_refused(self):
        problems = signature.check(
            "aya",
            "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile"
            "|claimed_author=claude-code-cli|to=ALL", self.known)
        self.assertTrue(any("stated 2 times" in p for p in problems), problems)

    def test_a_repeated_from_is_refused(self):
        problems = signature.check(
            "claude-code-cli", "BCB|v=1|id=X|from=claude-code-cli|from=pi1-cli|to=ALL", self.known)
        self.assertTrue(any("stated 2 times" in p for p in problems), problems)

    def test_the_same_value_twice_is_not_a_contradiction(self):
        """Redundant, not ambiguous. Refusing it would fail rows that say one thing clearly."""
        self.assertEqual([], signature.check(
            "aya", "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile"
                   "|claimed_author=claude-mobile|to=ALL", self.known))

    def test_a_blank_repeated_value_is_a_contradiction_too(self):
        """Codex, third pass: `via=|via=direct` passed here while append.py refused it, because the
        blank was filtered out and one distinct value remained. "Said nothing" and "said direct" are
        two different answers, and a reader scanning forwards gets the first."""
        problems = signature.check(
            "claude-code-cli", "BCB|v=1|id=X|from=claude-code-cli|via=|via=direct|to=ALL",
            self.known)
        self.assertTrue(any("stated 2 times" in p for p in problems), problems)

    def test_the_relay_fields_are_authority_keys_in_append(self):
        """The other half of the fix. append.py refuses a duplicated authority key outright, and
        these three were not on its list."""
        import append
        for key in ("relayer", "claimed_author", "via"):
            self.assertIn(key, append.AUTHORITY_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
