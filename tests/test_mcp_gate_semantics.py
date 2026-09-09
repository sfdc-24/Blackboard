"""What the gates MEAN, not which words they contain.

WHY THIS FILE EXISTS
--------------------
`chatgpt-codex-desktop-01a0839e`'s eighth review of PR45 did not assert that my
tests were weak — it showed it, with **eight semantic inversions of the plan
that all left 12/12 green on all four legs**. I reproduced one before writing
anything here: the document was edited to say

    - **unlocks** — nothing. G8 does **not** unlock G9.

and `test_mcp_plan_consistency.py` still passed. That is not a gap in coverage.
It is the wrong kind of test: `assertIn("G9", line)` is satisfied by a line
saying G9 is *not* unlocked, because a substring cannot carry a negation.

Twice in one day the same mechanism: the site's honesty guard was walked past
by a claim phrased in verbs its blocklist had never been taught, and this file's
predecessor was walked past by a document that said the opposite of itself.

So the relations are typed now (`tests/gate_graph.py`), and everything below
asserts a **positive property of a parsed value**. Where a test pins a specific
value rather than deriving it, the failure message says why it is pinned and
what changing it legitimately would look like — because a guard that silently
enforces the author's side of an open question is a thing I have shipped before
and do not intend to ship again.

Run:  python -B -m unittest discover -s tests -p 'test_mcp_gate_semantics.py'
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import gate_graph as gg

PLAN = Path(__file__).resolve().parents[1] / "docs" / "SALESFORCE-MCP-PLAN.md"

ALL_GATES = frozenset(f"G{n}" for n in range(1, 10))


def _render(ids: frozenset[str]) -> str:
    """The single canonical spelling of a gate set — `none` or `G2, G4`."""
    return ", ".join(sorted(ids)) if ids else "none"


def _bullet(prose: str, label: str) -> str | None:
    """The text of `- **label** — …`, with wrapped continuation lines joined."""
    m = re.search(rf"^- \*\*{label}\*\* — (.*?)(?=^- \*\*|\Z)", prose,
                  re.MULTILINE | re.DOTALL)
    if not m:
        return None
    return " ".join(m.group(1).split())


class GateSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Bytes, then normalise: the suite must behave the same on an LF
        # checkout and a CRLF one, and reading as text made that depend on git.
        cls.text = PLAN.read_bytes().decode("utf-8").replace("\r\n", "\n")
        cls.gates = gg.parse(cls.text)
        cls.status = gg.parse_status(cls.text)

    # ── The graph ───────────────────────────────────────────────────────────

    def test_every_gate_carries_a_typed_block(self) -> None:
        self.assertEqual(
            set(self.gates), set(ALL_GATES),
            "§8 names nine gates; the typed blocks must cover exactly those. A "
            "gate without a block is a gate no test can read.",
        )

    def test_the_dependency_graph_is_reciprocal(self) -> None:
        """`A unlocks B` exactly when `B requires A`.

        This is the assertion the G8 inversion fails. Saying "G8 does not unlock
        G9" while G9 still requires G8 is a contradiction the document cannot
        state once both halves are typed.
        """
        errors = gg.reciprocity_errors(self.gates)
        self.assertEqual(
            errors, [],
            "the gate graph disagrees with itself:\n  " + "\n  ".join(errors),
        )

    def test_no_gate_is_its_own_prerequisite(self) -> None:
        on_cycle = gg.cycles(self.gates)
        self.assertEqual(
            on_cycle, [],
            f"these gates lie on a cycle and can never be passed: {on_cycle}",
        )

    def test_every_gate_leads_to_the_client_org_gate(self) -> None:
        """A gate that cannot reach G9 restrains nothing.

        Structural, not editorial: it does not say which gates should exist,
        only that a gate which no longer leads anywhere has stopped being a
        precondition for the one thing this plan is about.
        """
        stranded = sorted(
            g for g in self.gates
            if g != "G9" and not gg.reaches(self.gates, g, "G9")
        )
        self.assertEqual(
            stranded, [],
            f"{stranded} no longer lead to G9, so nothing they check stands "
            "between us and a client's org.",
        )

    def test_the_client_org_gate_is_terminal(self) -> None:
        self.assertEqual(
            self.gates["G9"].unlocks, frozenset(),
            "G9 now unlocks a further gate. G9 is the authorisation to read an "
            "org we do not own; there is nothing after it in this document.",
        )

    def test_reaching_G9_requires_the_whole_of_G1_to_G8(self) -> None:
        """Computed closure, not the sentence that used to assert it.

        G9 lists four direct prerequisites. The other four reach it through
        those. Earlier versions wrote the full list in prose and asked the
        reader to trust it — which is exactly what "only G1 prerequisite"
        inverted without any test noticing.
        """
        ancestors: set[str] = set()
        stack = list(self.gates["G9"].requires)
        while stack:
            node = stack.pop()
            if node in ancestors:
                continue
            ancestors.add(node)
            stack.extend(self.gates[node].requires)
        self.assertEqual(
            ancestors, ALL_GATES - {"G9"},
            "the gates that must be accepted before G9 are "
            f"{sorted(ancestors)}. Every one of G1–G8 has to be reachable "
            "backwards from G9, or a gate has been quietly detached from the "
            "thing it was protecting.",
        )

    # ── Fail-closed behaviour ───────────────────────────────────────────────

    def test_every_gate_fails_closed(self) -> None:
        open_gates = sorted(g for g, v in self.gates.items() if not v.fail_closed)
        self.assertEqual(
            open_gates, [],
            f"{open_gates} no longer fail closed. A gate whose failure has no "
            "consequence is not a gate — §8 says so in its own opening lines.",
        )

    # These four rules are PINNED, and each is pinned because inverting it was
    # one of the eight demonstrations. Changing one is a real decision: change
    # the value here and in the block, and say in the PR which review finding
    # is being reopened. The point is that it cannot change silently.
    PINNED_RULES = {
        "G2": ("refuse_when_cost_unknown",
               "an unmeasured scan ran under the previous wording, which "
               "blocked only the measured excess"),
        "G3": ("deny_by_default",
               "an allowlist that permits what it has not considered is not an "
               "allowlist"),
        "G7": ("refuse_without_approval",
               "absence of approval is not permission, and this gate publishes"),
        "G9": ("refuse_without_authorisation",
               "silence is not a yes, and this gate reads a client's org"),
    }

    def test_the_fail_closed_rules_that_were_inverted_are_intact(self) -> None:
        for gid, (rule, why) in self.PINNED_RULES.items():
            self.assertEqual(
                self.gates[gid].fail_closed_rule, rule,
                f"{gid}.fail_closed_rule is "
                f"{self.gates[gid].fail_closed_rule!r}, not {rule!r}. Pinned "
                f"because {why}.",
            )

    # ── Who may accept, and what they must be shown ─────────────────────────

    def test_the_client_org_gate_is_opened_by_a_person(self) -> None:
        g9 = self.gates["G9"]
        self.assertEqual(
            g9.acceptor, "human:salam",
            f"G9 is accepted by {g9.acceptor!r}. The gate to a client's org is "
            "opened by a person, in the open. An agent that can write its own "
            "authorisation has no gate.",
        )
        self.assertTrue(
            g9.human_precondition,
            "G9 no longer requires a human precondition.",
        )

    def test_no_gate_is_accepted_by_the_party_that_owns_it(self) -> None:
        """Self-acceptance is unspellable, and this is where that is stated.

        The acceptor vocabulary contains no agent name, so `owner: X,
        acceptor: X` cannot be written. This test fails loudly if the
        vocabulary is ever widened to make it writable again.
        """
        for name in gg.ACCEPTORS:
            self.assertNotIn(
                name, gg.OWNERS,
                f"{name!r} is now both an owner and an acceptor, which makes "
                "self-acceptance expressible.",
            )

    def test_the_gates_that_cause_an_org_to_be_read_need_a_human(self) -> None:
        """G1 reads a Developer Edition org; G9 reads a client's.

        PINNED with its reason: those are the only two gates in this document
        that cause a Salesforce org to be read at all, so they are the only two
        that need a person to have said yes first. If a future gate reads an
        org, add it here deliberately — do not delete the test.
        """
        needing = sorted(g for g, v in self.gates.items() if v.human_precondition)
        self.assertEqual(
            needing, ["G1", "G9"],
            f"gates requiring a human precondition are {needing}. G1 reads a "
            "Developer Edition org and G9 reads a client's; both need a person "
            "to have authorised it, and nothing else here reads an org.",
        )

    # Round nine, "agent replaces G7 human": only G9's acceptor was pinned, so
    # G7 — the gate that PUBLISHES — was quietly handed to an agent and the
    # suite stayed green. Every acceptor is pinned now, with the reason, because
    # "who may accept this" is the whole content of a gate. Changing one is a
    # real decision: change it here and in the block, and say in the PR which
    # accountability is being moved and to whom.
    PINNED_ACCEPTORS = {
        "G1": ("agent:independent", "a capability matrix is checkable by anyone who did not write it"),
        "G2": ("agent:independent", "a cost measurement is checkable the same way"),
        "G3": ("agent:independent", "an allowlist is checkable by reading it against the tool listing"),
        "G4": ("human:salam-or-reviewer", "it decides where a client's credentials live, so it does not pass on my say-so"),
        "G5": ("agent:independent", "a schema and its validator are mechanically checkable"),
        "G6": ("human:salam-or-reviewer", "I should not be the one who decides my own statistical model is sound"),
        "G7": ("human:salam", "it publishes, and a person approves the exact bytes that become public"),
        "G8": ("human:reviewer", "Mr. Salam chooses the wording; a reviewer accepts the change — the typed field said human:salam and contradicted its own prose"),
        "G9": ("human:salam", "it opens a client's org; an agent that can write its own authorisation has no gate"),
    }

    def test_every_acceptor_is_the_one_the_document_states(self) -> None:
        for gid, (acceptor, why) in self.PINNED_ACCEPTORS.items():
            self.assertEqual(
                self.gates[gid].acceptor, acceptor,
                f"{gid} is accepted by {self.gates[gid].acceptor!r}, not "
                f"{acceptor!r}. Pinned because {why}.",
            )

    def test_a_gate_that_needs_a_person_did_not_get_an_agent(self) -> None:
        """The structural half of the same property, independent of the pins.

        Publishing and reading someone else's org are the two things this plan
        does that cannot be taken back. Whatever else changes, those acceptors
        stay human — this fails even if someone edits the pinned table above.
        """
        for gid in ("G7", "G9"):
            self.assertTrue(
                self.gates[gid].acceptor.startswith("human:"),
                f"{gid} is accepted by {self.gates[gid].acceptor!r}. This gate's "
                "outcome leaves our hands — published bytes, or a scan of an org "
                "we do not own — and an agent cannot be accountable for it.",
            )

    def test_absent_evidence_refuses_rather_than_proceeds(self) -> None:
        """Round nine, blocker 2: what happens when the receipt is NOT there.

        Every gate said what evidence it needs. None said what it does when
        that evidence is missing, so "no valid receipt, no connect" was an
        inference a reader made, not a property anything checked.
        """
        for gid in ("G1", "G9"):
            self.assertEqual(
                self.gates[gid].absent_evidence_behaviour, "refuse_connect",
                f"{gid} does not refuse to CONNECT when its authorisation "
                f"evidence is absent — it says "
                f"{self.gates[gid].absent_evidence_behaviour!r}. These are the "
                "two gates that cause an org to be read.",
            )
        self.assertIn(
            "authenticated_principal", self.gates["G9"].evidence_must_name,
            "G9's receipt no longer has to identify an AUTHENTICATED principal. "
            "A board row naming Mr. Salam is a claim about who wrote it; the "
            "gate needs the claim verified, or anyone who can write a row can "
            "authorise a client scan.",
        )

    def test_G9_evidence_names_the_org_its_scope_and_an_expiry(self) -> None:
        required = {"org_identifier", "scope", "expiry", "human_channel_row"}
        missing = sorted(required - set(self.gates["G9"].evidence_must_name))
        self.assertEqual(
            missing, [],
            f"G9's evidence no longer has to name {missing}. An authorisation "
            "that does not say which org, how far it reaches, and when it "
            "lapses is not an authorisation — it is a standing permission.",
        )

    def test_G1s_receipt_says_whose_permission_it_was_taken_under(self) -> None:
        """Round eight, blocker 2: the receipt lacked provenance.

        A committed tool listing proves a connection happened. It does not say
        who authorised it, to which org, how far, or until when — and it has no
        shape at all for the case that matters most in practice, which is the
        attempt that does not connect.
        """
        required = {"named_authoriser", "de_org_identifier", "authorisation_scope",
                    "authorisation_expiry", "no_connect_failure_mode"}
        missing = sorted(required - set(self.gates["G1"].evidence_must_name))
        self.assertEqual(
            missing, [],
            f"G1's evidence no longer has to name {missing}. A receipt that "
            "proves a connection happened but not whose permission it happened "
            "under is a receipt for the wrong thing.",
        )

    def test_G2_measures_a_named_envelope_rather_than_a_full_scan(self) -> None:
        """Round eight, blocker 3: "a full scan" is undefined this early.

        What a scan covers is not settled until G5's schemas and G6's
        catalogue, both of which come after G2 in the order. A cost measured
        against an undefined envelope is a number with no unit.
        """
        self.assertIn(
            "scan_envelope", self.gates["G2"].evidence_must_name,
            "G2 no longer names the object set it measured. Without it the "
            "figure is a number with no unit, and G5 or G6 can change what a "
            "scan covers without anyone re-taking the measurement.",
        )

    # Round nine, "arbitrary six G4 evidence names": the old test asserted only
    # that there were SIX, so `a, b, c, d, e, f` satisfied it. A count is not a
    # content check. These are the six concerns G4's prose names, and each is
    # required to appear in that prose too, so the pair cannot drift.
    G4_CONCERNS = {
        "attended_vs_unattended": "attended",
        "eca_constraints": "ECA",
        "token_storage": "token storage",
        "token_rotation": "rotation",
        "revocation": "revocation",
        "tenant_isolation": "isolation",
    }

    def test_G4_still_answers_six_concerns(self) -> None:
        """The count that was wrong four times — and now the names as well."""
        tokens = self.gates["G4"].evidence_must_name
        self.assertEqual(
            len(tokens), 6,
            f"G4 lists {len(tokens)} demonstrations: {list(tokens)}. The prose "
            "says six concerns; the two have to be the same number, and "
            "counting from memory is how this document went wrong four times.",
        )
        self.assertEqual(
            set(tokens), set(self.G4_CONCERNS),
            f"G4's demonstrations are {sorted(tokens)}. Six arbitrary names "
            "satisfy a count and answer nothing — round nine passed the whole "
            "suite with `a, b, c, d, e, f`.",
        )
        prose = self.gates["G4"].prose
        for token, phrase in self.G4_CONCERNS.items():
            self.assertIn(
                phrase, prose,
                f"G4's typed evidence names {token} but its prose no longer "
                f"mentions {phrase!r}, so the demonstration and the concern it "
                "answers have come apart.",
            )
        prose = _bullet(self.gates["G4"].prose, "artefact") or ""
        self.assertIn(
            "**six**", prose,
            "G4's prose no longer says six while its typed block lists "
            f"{len(tokens)}.",
        )

    def test_the_stated_field_count_is_the_real_one(self) -> None:
        """§8 says how many typed fields a gate carries. Derive it, don't trust it.

        This document has stated a count from memory and got it wrong four
        times — seven versus nine, sixteen versus seventeen, five versus six.
        The count in the prose is now checked against `REQUIRED_FIELDS` itself,
        so adding a field and forgetting the sentence is a failure here rather
        than a correction from a reviewer.
        """
        n = len(gg.REQUIRED_FIELDS)
        self.assertIn(
            f"block of {n} typed fields", self.text,
            f"§8 no longer says the gate block carries {n} typed fields, and "
            f"{n} is what tests/gate_graph.py actually requires: "
            f"{', '.join(gg.REQUIRED_FIELDS)}.",
        )

    # ── The hold ────────────────────────────────────────────────────────────

    def test_the_document_is_still_held(self) -> None:
        self.assertEqual(self.status.state, "HOLD",
                         "the status block no longer reads HOLD.")
        self.assertEqual(self.status.authorises, "nothing")
        self.assertEqual(self.status.execution, "forbidden")
        self.assertEqual(
            self.status.lifted_by, "human:salam",
            "a hold is lifted by the person who can lift it, not by this file.",
        )

    def test_a_held_document_has_no_passed_gates(self) -> None:
        """The sentence "no gate in §8 is passed", made checkable.

        This is the one that catches "execution approved with HOLD words": the
        prose can keep every reassuring phrase it likes, but a gate marked
        passed under a HOLD is a contradiction in typed fields.
        """
        if self.status.state != "HOLD":
            self.skipTest("the hold has been lifted; this invariant does not apply")
        passed = sorted(g for g, v in self.gates.items() if v.passed)
        self.assertEqual(
            passed, [],
            f"{passed} are marked passed while the document is HELD. A gate is "
            "passed when its acceptor accepts it, which is recorded outside "
            "this file — not by editing this file.",
        )

    # ── Prose and typed block must say the same thing ───────────────────────

    def test_the_prose_states_the_same_relations_as_the_typed_blocks(self) -> None:
        """Positive agreement, in both fields, for every gate.

        Not "the prose mentions G9" — the bullet must OPEN with the canonical
        rendering of the typed value. `- **unlocks** — nothing. G8 does not
        unlock G9.` opens with "nothing" while the block says `G9`, and fails
        here even before reciprocity gets to it.
        """
        problems: list[str] = []
        for gid, gate in sorted(self.gates.items()):
            for label, value in (("prerequisite", gate.requires),
                                 ("unlocks", gate.unlocks)):
                text = _bullet(gate.prose, label)
                if text is None:
                    problems.append(f"{gid}: no `{label}` bullet")
                    continue
                # PARSED, not prefix-matched. Round nine walked through
                # `startswith` with "G90 is a different gate entirely" — because
                # "G90..." starts with "G9". A prefix test on an identifier that
                # can be extended tests nothing about the identifier. This runs
                # the prose through the same grammar as the block and compares
                # sets, so G90 is refused as a gate id rather than accepted as a
                # prefix.
                try:
                    said = gg.prose_relation(text, f"{gid}.{label} prose")
                except gg.GateSyntaxError as exc:
                    problems.append(str(exc))
                    continue
                if said != value:
                    problems.append(
                        f"{gid}.{label}: block says {_render(value)}; prose says "
                        f"{_render(said)}"
                    )
        self.assertEqual(
            problems, [],
            "the prose and the typed blocks disagree:\n  " + "\n  ".join(problems),
        )

    def test_the_prose_repeats_each_gates_fail_closed_rule(self) -> None:
        """Every gate's fail-closed bullet must exist and be non-empty.

        Deliberately weaker than the relation check above: a fail-closed rule
        is an English sentence, and pretending a token match proves the
        sentence means it would be the same mistake in a new costume. The
        typed field is what the tests assert on; this only ensures the reader
        is told something.
        """
        empty = sorted(
            gid for gid, gate in self.gates.items()
            if not (_bullet(gate.prose, "fail-closed") or "").strip()
        )
        self.assertEqual(empty, [], f"{empty} state no fail-closed behaviour in prose")


if __name__ == "__main__":
    unittest.main()
