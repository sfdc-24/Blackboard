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

Run:  python -m unittest tests.test_mcp_plan_consistency
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
        cls.text = PLAN.read_text(encoding="utf-8")

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

    def test_every_gate_names_an_artefact_an_owner_and_an_acceptance(self) -> None:
        """A gate nobody can fail is not a gate — the reviewer's structural
        finding. Each row of the gate table must fill all four columns."""
        rows = [
            line for line in self.text.splitlines()
            if re.match(r"^\| \*\*G\d+\*\*", line)
        ]
        self.assertGreaterEqual(len(rows), 8, "the gate table is missing or short")
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertEqual(len(cells), 4, f"gate row has {len(cells)} cells: {row[:60]}")
            gate, artefact, owner, done = cells
            self.assertTrue(artefact, f"{gate} names no artefact")
            self.assertTrue(owner, f"{gate} names no owner")
            self.assertTrue(done, f"{gate} has no acceptance condition")
            self.assertGreater(
                len(done), 20, f"{gate}'s acceptance is too vague to fail: {done!r}"
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
