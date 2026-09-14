"""A typed reading of §8's gates, so the tests can check meaning.

WHY THIS EXISTS
---------------
`chatgpt-codex-desktop-01a0839e`'s eighth review of PR45 did not argue that my
tests were weak. It demonstrated it: **eight semantic inversions of the plan,
all leaving 12/12 green on all four legs.** Among them —

    G8 explicitly not unlocking G9        G3's deny-by-default disabled
    an unknown-cost scan permitted        G7 publishing without approval
    G9's org scope and expiry optional    the hold lifted in the status block

I reproduced the G8 one before writing a line of this: the document was made to
say *"unlocks — nothing. G8 does not unlock G9."* and the suite still passed.

The mechanism was always the same, and it is exactly the mechanism that let a
present-tense claim through the site's honesty guard the same morning: **the
tests scanned for keywords.** `assertIn("G9", unlocks_line)` is satisfied by a
line that says G9 is *not* unlocked. A substring cannot carry a negation, so a
test built on substrings cannot see one.

WHAT REPLACES IT
----------------
Each gate carries a fenced ```gate block of typed fields with a **closed
grammar**. Every field is parsed into a real value — a set of gate ids, a member
of a fixed vocabulary, a boolean — and anything outside the grammar is a
**parse error, not a shrug**. That is the property the keyword tests lacked:

  * inverting a value changes what is parsed, and the graph assertions fail;
  * inverting it in prose instead fails to parse, or contradicts the block;
  * inverting both leaves a graph that no longer reaches G9, which also fails.

The prose bullets stay. They are what a person reads, and a separate test
requires them to agree with the typed block rather than merely mention it.

This module is deliberately dependency-free (stdlib only, no YAML) so the suite
runs anywhere the plan does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── The grammar. Everything outside these sets is a parse error. ─────────────

GATE_ID = re.compile(r"^G[1-9]$")

# A gate's acceptor. `agent:independent` is the weakest form the plan allows,
# and the gates that expose a client org are separately required to be human.
ACCEPTORS = frozenset({
    "agent:independent",
    "human:reviewer",
    "human:salam",
    "human:salam-or-reviewer",
})

OWNERS = frozenset({"claude-code-cli", "none"})

# What a gate does when its condition is not met. There is no "continue".
FAIL_CLOSED_RULES = frozenset({
    "refuse_unconfirmed_claim",
    "refuse_when_cost_unknown",
    "deny_by_default",
    "block_until_demonstrated",
    "refuse_unvalidated_emitter",
    "report_rate_not_sigma",
    "refuse_without_approval",
    "refuse_until_live_readback_matches",
    "refuse_without_authorisation",
})

BOOLS = {"yes": True, "no": False}

REQUIRED_FIELDS = (
    "id",
    "requires",
    "unlocks",
    "owner",
    "acceptor",
    "human_precondition",
    "fail_closed",
    "fail_closed_rule",
    "evidence_must_name",
    # Round nine, blocker 2: the gates said what evidence they need and nothing
    # said what happens when it is ABSENT. "No valid receipt, no connect" has to
    # be a field, not an inference from the fail-closed sentence beside it.
    "absent_evidence_behaviour",
    # "no gate in §8 is passed" was a sentence in the status block and nothing
    # else. A sentence cannot be checked against anything; this field can, and
    # the hold is enforced as `state == HOLD implies every passed == no`.
    "passed",
)

# What the gate does when its required evidence is missing or unverified. There
# is no "proceed and note it".
ABSENT_EVIDENCE = frozenset({
    "refuse_connect",
    "refuse_publish",
    "refuse_accept",
    "refuse_scan",
})

# ── The document-level hold, typed for the same reason ──────────────────────

STATUS_BLOCK = re.compile(r"^```status\n(.*?)^```$", re.MULTILINE | re.DOTALL)
STATUS_FIELDS = ("state", "authorises", "execution", "lifted_by")
STATES = frozenset({"HOLD", "LIFTED"})
AUTHORISES = frozenset({"nothing", "the-gates-named-below"})
EXECUTION = frozenset({"forbidden", "permitted"})
LIFTED_BY = frozenset({"human:salam"})

EVIDENCE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")

BLOCK = re.compile(r"^```gate\n(.*?)^```$", re.MULTILINE | re.DOTALL)

# `G\d+`, NOT `G[1-9]`. Round nine slipped an extra fail-open `### G10` past the
# whole suite: the old pattern simply did not see it, so it was neither parsed
# nor counted nor rejected — it was invisible. Match any gate-shaped heading and
# let the id grammar refuse it loudly.
HEADING = re.compile(r"^### (G\d+) [^\n]*$", re.MULTILINE)

# The leading relation value of a prose bullet, parsed with the SAME grammar as
# the typed field rather than compared as a string.
#
# The previous check was `stripped.startswith(want)`, and round nine walked
# through it with `- **unlocks** — G90 is a different gate entirely.` — because
# "G90..." starts with "G9". A prefix test on an identifier that can be extended
# is not a test of the identifier. `G\d+` is greedy, so `G90` is captured whole
# and then REFUSED by GATE_ID, which is the behaviour a grammar is for.
PROSE_LEAD = re.compile(r"^(?:\*\*)?(none(?![a-z])|G\d+(?:\s*,\s*G\d+)*)")


# ── Generated contract prose ────────────────────────────────────────────────
#
# ROUND TEN, and the root cause was exact: PROSE_LEAD parsed only the LEADING
# `none` or gate list and DISCARDED THE REST OF THE SENTENCE. So
#
#     - **unlocks** — G9. In practice G8 gates nothing and G9 may proceed
#       without it.
#
# parsed to {G9}, matched the block, and passed. I had replaced a prefix check
# with a leading-token parse, which is still only looking at the beginning. A
# typed safe state could sit beside a fail-open instruction, and eight earlier
# attacks came back this way.
#
# The answer is not a longer parser. It is ONE AUTHORITATIVE SOURCE: the
# contract bullets are GENERATED from the typed block, and a test asserts the
# file matches what the generator produces, byte for byte. There is no remainder
# to contradict because nobody writes that text by hand. Commentary keeps its
# place below the generated region, under labels the contract does not use.

CONTRACT_BEGIN = "<!-- generated from the gate block above; do not hand-edit -->"
CONTRACT_END = "<!-- end generated -->"
CONTRACT_LABELS = ("prerequisite", "unlocks", "acceptor", "fail-closed")

ACCEPTOR_SENTENCE = {
    "agent:independent": "any agent that did not write it.",
    "human:reviewer": "a reviewer. Not the author, and not an agent.",
    "human:salam": "**Mr. Salam**. A person, in the open.",
    "human:salam-or-reviewer": "**Mr. Salam or a reviewer**. Not the author.",
}

FAIL_CLOSED_SENTENCE = {
    "refuse_unconfirmed_claim":
        "a claim that cannot be confirmed with a version is **struck**, not softened.",
    "refuse_when_cost_unknown":
        "a scan does not start when its cost **is not yet known**. Unknown is not permission.",
    "deny_by_default":
        "**deny by default** — a tool not on the allowlist is refused even if the server offers it.",
    "block_until_demonstrated":
        "any concern without an executable demonstration **blocks G9**.",
    "refuse_unvalidated_emitter":
        "an emitter that cannot validate **does not publish**.",
    "report_rate_not_sigma":
        "a metric without its demonstration reports a **rate**, not a sigma.",
    "refuse_without_approval":
        "**no approval, no publication.** Absence of approval is not permission.",
    "refuse_until_live_readback_matches":
        "if the live read-back still shows the old wording the gate is **not passed**, however green CI was.",
    "refuse_without_authorisation":
        "**no authorisation, no scan.** Silence is not a yes.",
}

ABSENT_EVIDENCE_SENTENCE = {
    "refuse_connect": "no valid evidence, **no connection**.",
    "refuse_publish": "no valid evidence, **nothing is published**.",
    "refuse_accept": "no valid evidence, **the gate is not accepted**.",
    "refuse_scan": "no valid evidence, **no scan starts**.",
}


def render_contract(gate: "Gate") -> str:
    """The generated contract bullets for one gate. The single source is the block."""
    def ids(value):
        return ", ".join(sorted(value)) + "." if value else "none."
    return "\n".join([
        CONTRACT_BEGIN,
        f"- **prerequisite** — {ids(gate.requires)}",
        f"- **unlocks** — {ids(gate.unlocks)}",
        f"- **acceptor** — {ACCEPTOR_SENTENCE[gate.acceptor]}",
        f"- **fail-closed** — {FAIL_CLOSED_SENTENCE[gate.fail_closed_rule]} "
        f"And {ABSENT_EVIDENCE_SENTENCE[gate.absent_evidence_behaviour]}",
        CONTRACT_END,
    ])


class GateSyntaxError(ValueError):
    """A gate block does not parse. Never downgraded to a warning: an
    unparseable relation is the failure mode this whole module exists for."""


@dataclass(frozen=True)
class Gate:
    id: str
    requires: frozenset[str]
    unlocks: frozenset[str]
    owner: str
    acceptor: str
    human_precondition: bool
    fail_closed: bool
    fail_closed_rule: str
    evidence_must_name: tuple[str, ...]
    absent_evidence_behaviour: str
    passed: bool
    line: int = 0
    prose: str = field(default="", compare=False)


@dataclass(frozen=True)
class Status:
    state: str
    authorises: str
    execution: str
    lifted_by: str


def _gate_set(raw: str, where: str) -> frozenset[str]:
    """`none`, or gate ids ascending, comma-separated, no repeats.

    Ascending-and-unique is not tidiness. It means there is exactly ONE way to
    write any given set, so a diff of this field is always a change of meaning.
    """
    if raw == "none":
        return frozenset()
    parts = [p.strip() for p in raw.split(",")]
    if any(not p for p in parts):
        raise GateSyntaxError(f"{where}: empty entry in gate list {raw!r}")
    for p in parts:
        if not GATE_ID.match(p):
            raise GateSyntaxError(
                f"{where}: {p!r} is not a gate id. This field takes `none` or "
                "gate ids only — prose here is how a negation hides."
            )
    if len(set(parts)) != len(parts):
        raise GateSyntaxError(f"{where}: repeated gate id in {raw!r}")
    if parts != sorted(parts):
        raise GateSyntaxError(f"{where}: gate ids must ascend, got {raw!r}")
    return frozenset(parts)


def prose_relation(bullet: str, where: str) -> frozenset[str]:
    """The gate set a prose bullet OPENS with, parsed, not prefix-matched.

    Returns the same kind of value as the typed field, so the test compares
    `frozenset == frozenset` instead of asking whether one string starts with
    another. Anything that is not a relation value — "nothing", "everything",
    "G90" — raises rather than quietly matching.
    """
    m = PROSE_LEAD.match(bullet.strip())
    if not m:
        raise GateSyntaxError(
            f"{where}: the bullet does not open with a relation value. It opens "
            f"{bullet.strip()[:60]!r}. Write `none` or gate ids; the explanation "
            "goes after them."
        )
    return _gate_set(re.sub(r"\s*,\s*", ", ", m.group(1)), where)


def parse_gate_block(body: str, where: str) -> dict[str, str]:
    """`key: value` lines, every required key exactly once, in order, no others."""
    fields: dict[str, str] = {}
    order: list[str] = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            raise GateSyntaxError(f"{where}: blank line {lineno} inside the block")
        if ":" not in line:
            raise GateSyntaxError(f"{where}: line {lineno} is not `key: value`: {line!r}")
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in fields:
            raise GateSyntaxError(f"{where}: {key!r} given twice")
        if not value:
            raise GateSyntaxError(f"{where}: {key!r} has no value")
        fields[key] = value
        order.append(key)

    missing = [k for k in REQUIRED_FIELDS if k not in fields]
    if missing:
        raise GateSyntaxError(f"{where}: missing field(s) {', '.join(missing)}")
    extra = [k for k in order if k not in REQUIRED_FIELDS]
    if extra:
        raise GateSyntaxError(f"{where}: unknown field(s) {', '.join(extra)}")
    if tuple(order) != REQUIRED_FIELDS:
        raise GateSyntaxError(
            f"{where}: fields are out of order.\n  expected: {', '.join(REQUIRED_FIELDS)}"
            f"\n  found:    {', '.join(order)}"
        )
    return fields


def _build(fields: dict[str, str], where: str, line: int, prose: str) -> Gate:
    gid = fields["id"]
    if not GATE_ID.match(gid):
        raise GateSyntaxError(f"{where}: id {gid!r} is not G1..G9")

    for name, table in (("owner", OWNERS), ("acceptor", ACCEPTORS),
                        ("fail_closed_rule", FAIL_CLOSED_RULES),
                        ("absent_evidence_behaviour", ABSENT_EVIDENCE)):
        if fields[name] not in table:
            raise GateSyntaxError(
                f"{where}: {name}={fields[name]!r} is outside the closed set. "
                f"Allowed: {', '.join(sorted(table))}"
            )

    for name in ("human_precondition", "fail_closed", "passed"):
        if fields[name] not in BOOLS:
            raise GateSyntaxError(
                f"{where}: {name}={fields[name]!r} must be exactly `yes` or `no`"
            )

    evidence = tuple(t.strip() for t in fields["evidence_must_name"].split(","))
    if not evidence or any(not EVIDENCE_TOKEN.match(t) for t in evidence):
        raise GateSyntaxError(
            f"{where}: evidence_must_name must be lower_snake tokens, got "
            f"{fields['evidence_must_name']!r}"
        )
    if len(set(evidence)) != len(evidence):
        raise GateSyntaxError(f"{where}: repeated token in evidence_must_name")

    return Gate(
        id=gid,
        requires=_gate_set(fields["requires"], f"{where}.requires"),
        unlocks=_gate_set(fields["unlocks"], f"{where}.unlocks"),
        owner=fields["owner"],
        acceptor=fields["acceptor"],
        human_precondition=BOOLS[fields["human_precondition"]],
        fail_closed=BOOLS[fields["fail_closed"]],
        fail_closed_rule=fields["fail_closed_rule"],
        evidence_must_name=evidence,
        absent_evidence_behaviour=fields["absent_evidence_behaviour"],
        passed=BOOLS[fields["passed"]],
        line=line,
        prose=prose,
    )


def parse_status(text: str) -> Status:
    """The typed hold at the top of the document."""
    blocks = list(STATUS_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise GateSyntaxError(
            f"expected exactly one ```status block, found {len(blocks)}. The hold "
            "on this document is enforced from that block; without it the only "
            "thing restraining the plan is a sentence."
        )
    fields: dict[str, str] = {}
    for lineno, line in enumerate(blocks[0].group(1).splitlines(), start=1):
        if ":" not in line:
            raise GateSyntaxError(f"status block line {lineno} is not `key: value`")
        key, _, value = line.partition(":")
        if key.strip() in fields:
            raise GateSyntaxError(f"status block: {key.strip()!r} given twice")
        fields[key.strip()] = value.strip()
    if tuple(fields) != STATUS_FIELDS:
        raise GateSyntaxError(
            f"status block fields must be exactly {', '.join(STATUS_FIELDS)} in that "
            f"order; found {', '.join(fields) or '(none)'}"
        )
    for name, table in (("state", STATES), ("authorises", AUTHORISES),
                        ("execution", EXECUTION), ("lifted_by", LIFTED_BY)):
        if fields[name] not in table:
            raise GateSyntaxError(
                f"status block: {name}={fields[name]!r} is outside the closed set "
                f"{{{', '.join(sorted(table))}}}"
            )
    return Status(**fields)


def parse(text: str) -> dict[str, Gate]:
    """Every ```gate block in the document, keyed by gate id.

    `text` must already be LF-normalised; the caller reads bytes and normalises
    so that a CRLF checkout and an LF checkout parse identically.
    """
    headings = [(m.group(1), m.start()) for m in HEADING.finditer(text)]
    blocks = list(BLOCK.finditer(text))
    if not blocks:
        raise GateSyntaxError(
            "no ```gate blocks found. The typed gate records are the only thing "
            "the semantic tests can read; without them the suite is back to "
            "scanning prose for keywords."
        )
    if len(blocks) != len(headings):
        raise GateSyntaxError(
            f"{len(headings)} gate heading(s) but {len(blocks)} typed block(s): "
            "every gate carries exactly one, or one of them is unchecked."
        )

    unknown = sorted({h for h, _ in headings if not GATE_ID.match(h)})
    if unknown:
        raise GateSyntaxError(
            f"gate heading(s) {unknown} are outside G1..G9. Round nine added an "
            "extra fail-open `### G10` and the old heading pattern could not see "
            "it, so it was never parsed, counted or refused. A gate this module "
            "cannot name is a gate it cannot check."
        )

    gates: dict[str, Gate] = {}
    for (heading_id, heading_at), block in zip(headings, blocks):
        where = f"gate block after ### {heading_id}"
        line = text.count("\n", 0, block.start()) + 1
        # The prose bullets belong to this gate: heading -> next heading.
        end = text.find("\n### ", block.end())
        prose = text[heading_at: end if end != -1 else len(text)]
        gate = _build(parse_gate_block(block.group(1), where), where, line, prose)
        if gate.id != heading_id:
            raise GateSyntaxError(
                f"{where}: block says id={gate.id} under heading {heading_id}"
            )
        if gate.id in gates:
            raise GateSyntaxError(f"{where}: gate {gate.id} defined twice")
        gates[gate.id] = gate
    return gates


def reciprocity_errors(gates: dict[str, Gate]) -> list[str]:
    """`A unlocks B` must hold exactly when `B requires A`.

    Stated as a set difference in BOTH directions, because a one-way check is
    satisfied by a graph that has quietly dropped an edge.
    """
    errors: list[str] = []
    for gid, gate in gates.items():
        for target in sorted(gate.unlocks):
            if target not in gates:
                errors.append(f"{gid}.unlocks names {target}, which does not exist")
            elif gid not in gates[target].requires:
                errors.append(
                    f"{gid} unlocks {target}, but {target}.requires does not list {gid}"
                )
        for source in sorted(gate.requires):
            if source not in gates:
                errors.append(f"{gid}.requires names {source}, which does not exist")
            elif gid not in gates[source].unlocks:
                errors.append(
                    f"{gid} requires {source}, but {source}.unlocks does not list {gid}"
                )
    return errors


def reaches(gates: dict[str, Gate], start: str, target: str) -> bool:
    """Is there a path start -> … -> target along `unlocks`?"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen or node not in gates:
            continue
        seen.add(node)
        stack.extend(sorted(gates[node].unlocks))
    return False


def cycles(gates: dict[str, Gate]) -> list[str]:
    """Gate ids that lie on a cycle — a gate that is its own prerequisite."""
    on_cycle = sorted(g for g in gates if any(
        reaches(gates, nxt, g) for nxt in gates[g].unlocks
    ))
    return on_cycle
