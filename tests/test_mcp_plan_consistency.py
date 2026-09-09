"""The MCP plan has to agree with itself.

WHY THIS EXISTS
---------------
Three review rounds on docs/SALESFORCE-MCP-PLAN.md found the same class of
defect three times, and every one was a thing the document said about itself:

  round 1  "a signal is not a metric until SEVEN things are written down",
           followed by a table of NINE
  round 2  the correction said sixteen; the table had seventeen
  round 3  the worked example filled in nine of the seventeen, used an
           `unknown` value that was not in the closed set the schema defines,
           and a later section still said "all seven fields per metric"

Every one was caught by a reviewer. None was caught by me, because I kept
stating counts from memory in a document about writing things down precisely.
A rule cannot fix that — the rule was already "count carefully" and I broke it
three times. This is the mechanism instead.

Run:  python -B -m unittest discover -s tests -p 'test_mcp_plan_consistency.py'

Use the discovery form, not `python -m unittest tests.<module>`: there is no
tests/__init__.py, so the dotted form fails on Python 3.12 for every module in
this directory. Verified on 3.11, 3.12 and 3.14.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "docs" / "SALESFORCE-MCP-PLAN.md"


def _table_after(heading: str, text: str) -> list[str]:
    """Field names from the first markdown table following `heading`."""
    start = text.index(heading)
    rows = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("| `"):
            rows.append(line.split("`")[1])
        elif rows and not line.startswith("|"):
            break
    return rows


class McpPlanConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Read BYTES and normalise, so the suite behaves identically on an LF
        # checkout and a CRLF one. Reading as text made every assertion depend
        # on how git happened to check the file out.
        cls.text = PLAN.read_bytes().decode("utf-8").replace("\r\n", "\n")

    # ── The safety property, which was unguarded until 2026-09-09 ────────────
    #
    # chatgpt-codex-desktop-01a0839e demonstrated the hole rather than asserting
    # it: change the status block to "approved executable architecture, client
    # scans authorised" and change G9's authoriser to "claude-code-cli, start
    # now", and BOTH suites stayed green. I reproduced it before fixing it.
    #
    # Everything else in this file checks that the document is internally
    # consistent. None of it checked the one claim that actually restrains
    # anything — that the plan authorises nothing and that a human, not me,
    # opens the gate to a client org. Consistency is not safety.

    REQUIRED_STATUS_PHRASES = (
        "HELD",
        "authorises nothing",
        "not executable architecture",
        "does not authorise a scan",
        "no gate in §8 is passed",
    )
    # Words that would mean the hold had been lifted inside this file rather
    # than by the person who is supposed to lift it.
    FORBIDDEN_STATUS_PHRASES = (
        "approved executable",
        "scans authorised",
        "scans authorized",
        "authorised to scan",
        "gate passed",
    )

    def test_the_status_block_still_holds_the_plan(self) -> None:
        head = self.text[: self.text.index("## 0.")]
        self.assertIn("STATUS", head, "the status block is gone from the top of the file")
        for phrase in self.REQUIRED_STATUS_PHRASES:
            self.assertIn(
                phrase, head,
                f'the status block no longer says "{phrase}". This document authorises '
                "nothing, and the sentence saying so is the only thing enforcing that.",
            )
        lowered = head.lower()
        for phrase in self.FORBIDDEN_STATUS_PHRASES:
            self.assertNotIn(
                phrase, lowered,
                f'the status block now says "{phrase}". A hold is lifted by the person '
                "who imposed it, in the open — not by editing the document that records it.",
            )

    def test_the_client_org_gate_is_not_self_authorised(self) -> None:
        """G9 is the gate to a real client's Salesforce org. Its owner column
        must name a human. An agent that can write 'claude-code-cli authorises'
        into its own gate table has no gate."""
        parts = re.split(r"^### G9[^\n]*$", self.text, flags=re.MULTILINE)
        self.assertEqual(len(parts), 2, "the G9 client-org gate has been removed")
        block = parts[-1]

        acceptor = next(
            (ln for ln in block.splitlines() if "acceptor" in ln.lower()),
            None,
        )
        self.assertIsNotNone(acceptor, "G9 states no acceptor")
        self.assertIn(
            "Salam", acceptor,
            f'G9 is accepted by "{acceptor.strip()}". The gate to a client org must be '
            "opened by a person; a self-authorising gate is not a gate.",
        )
        for agent in ("claude", "codex", "vm-"):
            self.assertNotIn(
                agent, acceptor.lower(),
                f'G9 names an agent as its acceptor: "{acceptor.strip()}"',
            )
        self.assertIn(
            "no authorisation, no scan", block.lower(),
            "G9 no longer states its fail-closed behaviour. Silence must not be a yes.",
        )

    def test_plan_exists(self) -> None:
        self.assertTrue(PLAN.is_file(), f"{PLAN} is missing")
        self.assertGreater(len(self.text), 5000, "the plan is suspiciously short")

    def test_schema_and_worked_example_have_the_same_fields(self) -> None:
        """A worked example that does not satisfy its own schema teaches the
        wrong thing twice. Round three had 9 of 17."""
        schema = _table_after("| field | meaning |", self.text)
        example = _table_after("| field | value |", self.text)
        self.assertGreater(len(schema), 5, "no schema table found")
        self.assertEqual(
            schema,
            example,
            "the worked example does not supply exactly the schema's fields, in order.\n"
            f"  schema  ({len(schema)}): {schema}\n"
            f"  example ({len(example)}): {example}",
        )

    def test_every_stated_field_count_matches_the_table(self) -> None:
        """The document must not claim a number the table contradicts. This is
        the assertion that would have caught all three rounds."""
        schema = _table_after("| field | meaning |", self.text)
        n = len(schema)
        words = {
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
            "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
            "sixteen": 16, "seventeen": 17, "eighteen": 18,
        }
        pattern = re.compile(
            r"\b(\d{1,2}|" + "|".join(words) + r")\s+fields?\b", re.IGNORECASE
        )
        for match in pattern.finditer(self.text):
            raw = match.group(1).lower()
            claimed = words.get(raw, int(raw) if raw.isdigit() else None)
            self.assertEqual(
                claimed, n,
                f'the plan says "{match.group(0)}" but the schema table has {n} rows',
            )

    def test_unknown_values_stay_inside_the_closed_set(self) -> None:
        """`unknown` is declared as a CLOSED set. Round three used
        `no-interviews-in-window`, which is not in it — and a closed set with an
        exception is an open set."""
        schema_row = next(
            line for line in self.text.splitlines()
            if line.startswith("| `unknown` | the closed set")
        )
        allowed = set(re.findall(r"`([a-z-]+)`", schema_row)) - {"unknown"}
        self.assertGreaterEqual(len(allowed), 3, "the closed set looks empty")

        example_row = next(
            line for line in self.text.splitlines()
            if line.startswith("| `unknown` |") and line is not schema_row
            and "closed set" not in line
        )
        used = set(re.findall(r"`([a-z-]+)`", example_row)) - {"unknown"}
        self.assertTrue(
            used <= allowed,
            f"the worked example uses {sorted(used - allowed)}, which the closed "
            f"set {sorted(allowed)} does not contain",
        )

    # Every field a gate must state. A gate that cannot be failed, or whose
    # failure has no defined consequence, is not a gate.
    #
    # THIS LIST REPLACED AN ASSERTION THAT WAS ACTIVELY HARMFUL. The previous
    # version required `len(cells) == 4` on a markdown table row — which meant
    # my own test forbade the prerequisite, acceptor, evidence, fail-closed and
    # unlock columns that chatgpt-codex-desktop-01a0839e had asked for across
    # three review rounds. I was arguing those belonged elsewhere while a guard
    # I had written prevented them from being anywhere. A test that enforces
    # one side of an open disagreement is not a neutral check.
    GATE_FIELDS = (
        "prerequisite",
        "artefact",
        "owner",
        "acceptor",
        "evidence",
        "fail-closed",
        "unlocks",
    )

    def test_every_gate_states_all_of_its_required_fields(self) -> None:
        blocks = re.split(r"^### (G\d+)[^\n]*$", self.text, flags=re.MULTILINE)
        # re.split with one group yields [pre, name, body, name, body, ...]
        gates = dict(zip(blocks[1::2], blocks[2::2]))
        self.assertGreaterEqual(len(gates), 9, f"expected at least 9 gates, found {sorted(gates)}")

        for name, body in gates.items():
            lowered = body.lower()
            for field in self.GATE_FIELDS:
                self.assertIn(
                    field, lowered,
                    f"{name} does not state its {field!r}. "
                    "A gate whose failure has no defined consequence is not a gate.",
                )
            # An acceptance that cannot be failed is decoration.
            self.assertGreater(
                len(body.strip()), 200,
                f"{name} is too thin to fail on: {body.strip()[:80]!r}",
            )

    def test_the_client_org_gate_pins_its_prerequisites_by_name(self) -> None:
        """"mostly done" must not pass for done. G9 opens a real customer's org,
        so its dependencies are named individually rather than as a range."""
        block = re.split(r"^### G9[^\n]*$", self.text, flags=re.MULTILINE)[-1]
        for gate in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"):
            self.assertIn(
                gate, block,
                f"G9 does not name {gate} among its prerequisites. A range like "
                '"G1-G7" lets a skipped gate hide inside a dash.',
            )

    # Exactly which fields the worked example does not yet know. Pinned rather
    # than counted: the first version of this test asserted only that the word
    # UNRESOLVED appeared SOMEWHERE, so resolving `threshold` to an invented
    # number left two others in place and the suite stayed green. The mutation
    # control caught that. A pinned set makes resolving one a deliberate act.
    UNRESOLVED_FIELDS = {"threshold", "query", "cost"}

    def test_unresolved_items_are_marked_not_omitted(self) -> None:
        """Leaving a field out reads as complete; marking it UNRESOLVED does not.

        Each of these is unresolved for a stated reason — the threshold must
        come from an observed distribution, and the query and cost from the §8
        spike. Inventing any of them is exactly the over-claim this document
        exists to stop making.
        """
        example_block = self.text[self.text.index("| field | value |"):]
        example_block = example_block[: example_block.index("\n\n")]

        unresolved = {
            line.split("`")[1]
            for line in example_block.splitlines()
            if line.startswith("| `") and "UNRESOLVED" in line
        }
        self.assertEqual(
            unresolved, self.UNRESOLVED_FIELDS,
            "the set of UNRESOLVED fields changed.\n"
            f"  expected: {sorted(self.UNRESOLVED_FIELDS)}\n"
            f"  found   : {sorted(unresolved)}\n"
            "If one was genuinely resolved, say where the value came from and "
            "update UNRESOLVED_FIELDS in this test. If it was filled in with a "
            "plausible-looking number, that is the over-claim this document is "
            "about.",
        )


if __name__ == "__main__":
    unittest.main()
