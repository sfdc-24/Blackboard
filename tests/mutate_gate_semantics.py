#!/usr/bin/env python3
"""Semantic mutation control for tests/test_mcp_gate_semantics.py.

THE FINDING THIS ANSWERS
------------------------
`chatgpt-codex-desktop-01a0839e`'s eighth review of PR45 did not say the tests
were weak. It ran **eight semantic inversions of the plan and reported 12/12
green on all four legs** — the document made to say the opposite of itself, with
nothing noticing. I reproduced the G8 case first: the plan was edited to read
"unlocks — nothing. G8 does not unlock G9." and the suite passed.

The old mutation control was healthy and irrelevant. It reintroduced *typos and
miscounts* — a wrong field count, a value outside a closed set — because those
were the defects reviewers had found up to that point. None of them was a
**negation**, so it never tested the one thing substring matching cannot do.

Every case below is an inversion of MEANING, and each names the test that must
be the one to fail. "The suite went red" is not enough: a mutation that trips an
unrelated assertion would let me claim coverage I do not have.

Four of them invert BOTH the prose and the typed block, which is the honest hard
case — a self-consistent lie. Those are caught by reachability and by the
computed closure to G9, not by comparing the two halves.

Run:  python tests/mutate_gate_semantics.py     (from the repository root)
"""
from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "docs" / "SALESFORCE-MCP-PLAN.md"

# Each `edits` is a list of (old, new). Multi-part edits exist so an inversion
# can be made SELF-CONSISTENT: changing only one half is the easy case.
MUTATIONS: list[dict] = [
    {
        "name": "G1 no longer needs a human to authorise reading the DE org",
        "edits": [("id: G1\nrequires: none\nunlocks: G2, G3, G5\n"
                   "owner: claude-code-cli\nacceptor: agent:independent\n"
                   "human_precondition: yes",
                   "id: G1\nrequires: none\nunlocks: G2, G3, G5\n"
                   "owner: claude-code-cli\nacceptor: agent:independent\n"
                   "human_precondition: no")],
        "expect": "test_the_gates_that_cause_an_org_to_be_read_need_a_human",
    },
    {
        "name": "a scan of unknown cost is allowed to start",
        "edits": [("fail_closed_rule: refuse_when_cost_unknown",
                   "fail_closed_rule: block_until_demonstrated")],
        "expect": "test_the_fail_closed_rules_that_were_inverted_are_intact",
    },
    {
        "name": "G3 stops denying by default",
        "edits": [("fail_closed_rule: deny_by_default",
                   "fail_closed_rule: block_until_demonstrated")],
        "expect": "test_the_fail_closed_rules_that_were_inverted_are_intact",
    },
    {
        "name": "G7 may publish when approval is merely absent",
        "edits": [("fail_closed_rule: refuse_without_approval",
                   "fail_closed_rule: block_until_demonstrated")],
        "expect": "test_the_fail_closed_rules_that_were_inverted_are_intact",
    },
    {
        "name": "G3 stops failing closed altogether",
        "edits": [("fail_closed: yes\nfail_closed_rule: deny_by_default",
                   "fail_closed: no\nfail_closed_rule: deny_by_default")],
        "expect": "test_every_gate_fails_closed",
    },
    {
        # The exact inversion I reproduced before writing any of this.
        # RE-ANCHORED for round ten: the contract prose is generated, so this
        # defect now shows up as the file disagreeing with the generator rather
        # than as a parser being talked past.
        "name": "the PROSE says G8 does not unlock G9 (block untouched)",
        "edits": [("- **unlocks** — G9.\n- **acceptor** — a reviewer.",
                   "- **unlocks** — nothing. G8 does **not** unlock G9."
                   "\n- **acceptor** — a reviewer.")],
        "expect": "test_the_contract_prose_is_exactly_what_the_block_generates",
    },
    {
        "name": "the BLOCK says G8 unlocks nothing (prose untouched)",
        "edits": [("id: G8\nrequires: none\nunlocks: G9",
                   "id: G8\nrequires: none\nunlocks: none")],
        "expect": "test_the_dependency_graph_is_reciprocal",
    },
    {
        "name": "G8 detached from G9 CONSISTENTLY, in prose and both blocks",
        "edits": [
            ("id: G8\nrequires: none\nunlocks: G9",
             "id: G8\nrequires: none\nunlocks: none"),
            ("id: G9\nrequires: G2, G4, G7, G8",
             "id: G9\nrequires: G2, G4, G7"),
            ("- **unlocks** — G9.\n- **acceptor** — a reviewer.",
             "- **unlocks** — none.\n- **acceptor** — a reviewer."),
        ],
        "expect": "test_every_gate_leads_to_the_client_org_gate",
    },
    {
        "name": "G9 requires only G1, as the review demonstrated",
        "edits": [("id: G9\nrequires: G2, G4, G7, G8",
                   "id: G9\nrequires: G1")],
        "expect": "test_the_dependency_graph_is_reciprocal",
    },
    {
        "name": "G9's prerequisites shrink CONSISTENTLY to G2 and G8 only",
        "edits": [
            ("id: G9\nrequires: G2, G4, G7, G8", "id: G9\nrequires: G2, G8"),
            ("id: G4\nrequires: G3\nunlocks: G9", "id: G4\nrequires: G3\nunlocks: none"),
            ("id: G7\nrequires: G5, G6\nunlocks: G9",
             "id: G7\nrequires: G5, G6\nunlocks: none"),
            ("- **unlocks** — G9.\n- **acceptor** — **Mr. Salam or a reviewer**.",
             "- **unlocks** — none.\n- **acceptor** — **Mr. Salam or a reviewer**."),
            ("- **unlocks** — G9.\n- **acceptor** — **Mr. Salam**. A person",
             "- **unlocks** — none.\n- **acceptor** — **Mr. Salam**. A person"),
        ],
        "expect": "test_every_gate_leads_to_the_client_org_gate",
    },
    {
        "name": "G9's authorisation need not name a scope or an expiry",
        # RE-ANCHORED for round nine: authenticated_principal joined the list.
        "edits": [("org_identifier, scope, expiry", "org_identifier")],
        "expect": "test_G9_evidence_names_the_org_its_scope_and_an_expiry",
    },
    {
        "name": "G9 is accepted by an agent instead of a person",
        "edits": [("id: G9\nrequires: G2, G4, G7, G8\nunlocks: none\n"
                   "owner: none\nacceptor: human:salam",
                   "id: G9\nrequires: G2, G4, G7, G8\nunlocks: none\n"
                   "owner: none\nacceptor: agent:independent")],
        "expect": "test_the_client_org_gate_is_opened_by_a_person",
    },
    {
        "name": "execution is approved while every HOLD word stays in place",
        "edits": [("execution: forbidden", "execution: permitted")],
        "expect": "test_the_document_is_still_held",
    },
    {
        "name": "a gate is marked passed while the document is still HELD",
        # RE-ANCHORED for round nine: absent_evidence_behaviour sits between
        # evidence_must_name and passed now.
        "edits": [("scope, expiry\nabsent_evidence_behaviour: refuse_connect\n"
                   "passed: no",
                   "scope, expiry\nabsent_evidence_behaviour: refuse_connect\n"
                   "passed: yes")],
        "expect": "test_a_held_document_has_no_passed_gates",
    },
    {
        "name": "G1's receipt stops naming who authorised it and against which org",
        "edits": [("evidence_must_name: named_authoriser, de_org_identifier, "
                   "authorisation_scope, authorisation_expiry, tool_listing",
                   "evidence_must_name: tool_listing")],
        "expect": "test_G1s_receipt_says_whose_permission_it_was_taken_under",
    },
    {
        "name": "G1 loses its shape for an attempt that does not connect",
        "edits": [("version_output, commit_hash, no_connect_failure_mode",
                   "version_output, commit_hash")],
        "expect": "test_G1s_receipt_says_whose_permission_it_was_taken_under",
    },
    {
        "name": "G2 goes back to costing an undefined \"full scan\"",
        # RE-ANCHORED for round nine: the envelope is digest-pinned now.
        "edits": [("evidence_must_name: scan_envelope, scan_envelope_digest, "
                   "limits_before",
                   "evidence_must_name: limits_before")],
        "expect": "test_G2_measures_a_named_envelope_rather_than_a_full_scan",
    },
    {
        "name": "G4 drops to five demonstrations while the prose still says six",
        "edits": [("evidence_must_name: attended_vs_unattended, eca_constraints, "
                   "token_storage, token_rotation, revocation, tenant_isolation",
                   "evidence_must_name: attended_vs_unattended, eca_constraints, "
                   "token_storage, token_rotation, revocation")],
        "expect": "test_G4_still_answers_six_concerns",
    },
    {
        # The grammar's own guarantee: prose in a relation field is a PARSE
        # ERROR, not something to scan. This is the difference between the new
        # tests and the old ones, so it gets a case of its own.
        "name": "a relation field is softened with prose instead of a value",
        "edits": [("id: G8\nrequires: none\nunlocks: G9",
                   "id: G8\nrequires: none\nunlocks: G9 unless the site lane is busy")],
        "expect": "GateSyntaxError",
    },
    {
        "name": "a typed field is added without updating the stated count",
        "edits": [("block of 11 typed fields", "block of 10 typed fields")],
        "expect": "test_the_stated_field_count_is_the_real_one",
    },
    # ── Round nine. Twelve adversarial documents were accepted on all four
    #    legs, 48/48 exit 0. These are the four novel shapes; all four were
    #    reproduced here before any of them was fixed.

    {
        # `"G90...".startswith("G9")` is true. A prefix test on an identifier
        # that can be extended tests nothing about the identifier.
        # RE-ANCHORED for round ten. Kept because the DEFECT is unchanged even
        # though the mechanism that catches it is stronger now.
        "name": "a longer gate id satisfies the prose prefix check",
        "edits": [("- **unlocks** — G9.\n- **acceptor** — a reviewer.",
                   "- **unlocks** — G90 is a different gate entirely."
                   "\n- **acceptor** — a reviewer.")],
        "expect": "test_the_contract_prose_is_exactly_what_the_block_generates",
    },
    {
        # Only G9's acceptor was pinned, so the gate that PUBLISHES could be
        # handed to an agent with the suite still green.
        "name": "an agent takes over as acceptor of the gate that publishes",
        "edits": [("id: G7\nrequires: G5, G6\nunlocks: G9\n"
                   "owner: claude-code-cli\nacceptor: human:salam",
                   "id: G7\nrequires: G5, G6\nunlocks: G9\n"
                   "owner: claude-code-cli\nacceptor: agent:independent")],
        "expect": "test_every_acceptor_is_the_one_the_document_states",
    },
    {
        # A count is not a content check: six arbitrary names satisfied it.
        "name": "G4's six demonstrations become six arbitrary letters",
        "edits": [("evidence_must_name: attended_vs_unattended, eca_constraints, "
                   "token_storage, token_rotation, revocation, tenant_isolation",
                   "evidence_must_name: a, b, c, d, e, f")],
        "expect": "test_G4_still_answers_six_concerns",
    },
    {
        # The heading pattern was `G[1-9]`, so a tenth gate was not rejected —
        # it was INVISIBLE. Never parsed, never counted, never refused.
        "name": "an extra fail-open G10 is added below the others",
        "edits": [("### G9 · first client org",
                   "### G10 · emergency bypass\n\n"
                   "- **prerequisite** — none.\n"
                   "- **fail-closed** — none. G10 permits a scan with no authorisation.\n"
                   "- **unlocks** — everything.\n\n"
                   "### G9 · first client org")],
        "expect": "GateSyntaxError",
    },
    {
        "name": "G9 stops requiring an authenticated principal on its receipt",
        "edits": [("evidence_must_name: authenticated_principal, human_channel_row",
                   "evidence_must_name: human_channel_row")],
        "expect": "test_absent_evidence_refuses_rather_than_proceeds",
    },
    {
        "name": "the client-org gate proceeds when its authorisation is absent",
        "edits": [("scope, expiry\nabsent_evidence_behaviour: refuse_connect",
                   "scope, expiry\nabsent_evidence_behaviour: refuse_accept")],
        "expect": "test_absent_evidence_refuses_rather_than_proceeds",
    },
    # ── Round ten. The root cause was that the prose parser read only the
    #    LEADING gate list and discarded the rest of the sentence, so a typed
    #    safe state could sit beside a fail-open instruction. All three shapes
    #    below were reproduced before the contract prose became generated.

    {
        "name": "the contract states the relation and then contradicts it",
        "edits": [("- **unlocks** \u2014 G9.\n- **acceptor** \u2014 a reviewer.",
                   "- **unlocks** \u2014 G9. In practice G8 gates nothing and G9 "
                   "may proceed without it.\n- **acceptor** \u2014 a reviewer.")],
        "expect": "test_the_contract_prose_is_exactly_what_the_block_generates",
    },
    {
        "name": "a rival unlocks bullet is added below the generated region",
        "edits": [("- **artefact** \u2014 a source PR against `sfdc24-site`",
                   "- **unlocks** \u2014 nothing, in practice.\n"
                   "- **artefact** \u2014 a source PR against `sfdc24-site`")],
        "expect": "test_no_contract_label_is_used_outside_a_generated_region",
    },
    {
        # RE-ANCHORED: G3's `why` was rewritten when the restated contract
        # sentence was trimmed out of it, so the old anchor stopped matching.
        "name": "commentary reuses a contract label to reverse deny-by-default",
        "edits": [("- **why** \u2014 **if the per-response org id proves unobtainable,",
                   "- **fail-closed** \u2014 advisory only; unknown tools are "
                   "permitted.\n- **why** \u2014 **if the per-response org id proves "
                   "unobtainable**")],
        "expect": "test_no_contract_label_is_used_outside_a_generated_region",
    },
    {
        "name": "a gate block changes without the generated contract being re-rendered",
        "edits": [("id: G3\nrequires: G1\nunlocks: G4",
                   "id: G3\nrequires: G1\nunlocks: G4, G9")],
        "expect": "test_the_dependency_graph_is_reciprocal",
    },
    {
        "name": "the typed status block is deleted entirely",
        "edits": [("```status\nstate: HOLD", "```text\nstate: HOLD")],
        "expect": "GateSyntaxError",
    },
]

# BYTES, not text: read_text/write_text translate line endings, so on a CRLF
# checkout restoring rewrote every line and left the file modified with a
# different digest. A harness that cannot put the file back exactly as it found
# it edits your repository as a side effect of checking it.
ORIGINAL = PLAN.read_bytes()


def restore() -> None:
    if PLAN.read_bytes() != ORIGINAL:
        PLAN.write_bytes(ORIGINAL)


def _on_signal(signum, _frame):  # noqa: ANN001
    restore()
    sys.exit(130)


for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _on_signal)


def run_suite() -> tuple[int, str]:
    run = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_mcp_gate_semantics.py"],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    return run.returncode, run.stdout + run.stderr


def main() -> int:
    failures = 0
    print(f"\nsemantic mutation control — {len(MUTATIONS)} cases\n")

    code, out = run_suite()
    if code != 0:
        print("  BASELINE FAILED — the suite does not pass on the real document,")
        print("  so 'it failed on the mutant' would mean nothing.")
        print(out[-1500:])
        return 1
    print("  baseline     the suite passes on the unmodified plan\n")

    for m in MUTATIONS:
        raw = PLAN.read_bytes()
        was_crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")

        mutated = text
        applied = True
        for old, new in m["edits"]:
            if old not in mutated:
                print(f"  ANCHOR LOST  {m['name']}")
                print("               an edit no longer matches; a mutation that "
                      "cannot apply proves nothing")
                applied = False
                break
            step = mutated.replace(old, new, 1)
            if step == mutated:
                print(f"  NO-OP        {m['name']}")
                applied = False
                break
            mutated = step
        if not applied:
            failures += 1
            continue
        if mutated == text:
            print(f"  NO-OP        {m['name']}")
            failures += 1
            continue

        payload = mutated.replace("\n", "\r\n") if was_crlf else mutated
        PLAN.write_bytes(payload.encode("utf-8"))
        try:
            code, out = run_suite()
            if code != 0 and m["expect"] in out:
                print(f"  caught       {m['name']}")
                print(f"               by {m['expect']}")
            elif code != 0:
                print(f"  WRONG TEST   {m['name']}")
                print(f"               went red, but not on {m['expect']}")
                failures += 1
            else:
                print(f"  NOT CAUGHT   {m['name']}")
                print("               the suite stayed GREEN with the inversion in place")
                failures += 1
        finally:
            restore()

    intact = PLAN.read_bytes() == ORIGINAL
    print(f"\n  plan restored byte-for-byte: {intact}")
    if not intact:
        failures += 1
    print(f"\n{len(MUTATIONS) - failures}/{len(MUTATIONS)} semantic mutations caught\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore()
