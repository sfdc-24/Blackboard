#!/usr/bin/env python3
"""
SFDC24 — job estimator and quote builder
claude-code-cli, 2026-09-03.

WHAT THIS IS FOR
    Someone asks "what would it cost to build a simple site and host it" or
    "change this thing in Salesforce". This turns that into a defensible number:
    effort, contingency, margin, and a client-facing quote.

CALIBRATED WHERE IT CAN BE, HONEST WHERE IT CANNOT
    Agent throughput is measured, not guessed — it comes from logs/prompt-log.jsonl,
    which reads real usage out of the session transcript. As of the first
    calibration: 99 prompts, 703 tool calls, median prompt 170s, longest 1608s.

    What is NOT measured, and must be set from real invoices before quoting:
      - dollars per token for the main model. The harness recorded cost for only
        one minor model; 1704 records of the primary model carry no cost. Any
        rate here is therefore CONFIG, not evidence.
      - Mr. Salam's hourly rate and pass-through costs.
    Running with defaults prints a warning. Quote from warned numbers at your
    own risk.

WHY PERCENTILES AND NOT A FLAT UPLIFT
    Task durations are viciously right-skewed — the measured median was 170s
    against a 1608s worst case, roughly 9x. Averaging that is how estimates get
    destroyed. Contingency here is the measured p80-minus-p50 gap, so it grows
    for job types that actually vary and stays small for ones that do not.

A WARNING WORTH MORE THAN THE TOOL
    Cost-plus pricing is the wrong model for this business. When agents do the
    legwork, cost approaches zero, and cost-plus then caps earnings at
    (almost nothing) x margin. That is backwards. Use this to know your floor
    and never to set your price. Price on what the fix is worth to the client:
    the reporting they can finally trust, the migration that does not duplicate
    40,000 accounts. See VALUE_ANCHOR below.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "prompt-log.jsonl"

# ---------------------------------------------------------------------------
# CONFIG — set these from real invoices. Defaults are placeholders and say so.
# ---------------------------------------------------------------------------
RATES = {
    "human_hourly_cad": 150.0,      # SET ME. Gemini observed $125-200/hr for this market.
    "usd_per_1k_tokens": 0.02437,   # SET ME. Derived from the ONE model the harness costed.
    "usd_to_cad": 1.39,             # SET ME. Refresh from fetch.py USDCAD=X.
    "margin_pct": 0.35,             # your profit on top of loaded cost
    "contingency_floor_pct": 0.15,  # minimum, even for jobs with no measured spread

    # THE NUMBER THAT ACTUALLY SETS YOUR FLOOR.
    # Stated by Mr. Salam 2026-09-03: tools, licences, systems, accounting, his
    # own time for approvals and reviews, relationship maintenance and
    # follow-ups, plus R&D and product management. None of that is billable to
    # any single client, so every job must carry a share of it or the business
    # loses money while looking busy.
    "fixed_monthly_cad": 500.0,

    # Utilisation. This is the most sensitive input in the whole model: at one
    # job a month each job absorbs the entire $500; at four it absorbs $125.
    # Estimating this optimistically is the classic way a solo practice
    # under-quotes itself into insolvency. Be pessimistic.
    "jobs_per_month": 2.0,
}
RATES_ARE_DEFAULT = True            # flipped False when a rates file is loaded

VALUE_ANCHOR = """  PRICE CHECK — do not quote the floor
    This number is your COST plus a margin. It is not a price.
    Ask instead: what is this worth to them? A migration that does not
    duplicate 40,000 accounts is worth more than the hours it takes. If the
    value number is far above the cost number, quote nearer the value and keep
    the difference. If it is below, decline the job."""


@dataclass
class Job:
    """agent_prompts / human_hours are (p50, p80) pairs — the spread IS the estimate."""
    key: str
    label: str
    agent_prompts: tuple[int, int]
    human_hours: tuple[float, float]
    passthrough_cad: float = 0.0
    passthrough_note: str = ""
    notes: str = ""
    recurring_cad_month: float = 0.0


CATALOGUE: dict[str, Job] = {
    j.key: j for j in [
        Job("site-simple", "Simple website, built and hosted",
            agent_prompts=(25, 60), human_hours=(2.0, 5.0),
            passthrough_cad=20.0, passthrough_note="domain ~$15-20/yr",
            recurring_cad_month=0.0,
            notes="Static site on free hosting. Hosting only costs money above trivial traffic."),

        Job("site-cms", "Website with a CMS the client edits themselves",
            agent_prompts=(45, 110), human_hours=(4.0, 10.0),
            passthrough_cad=20.0, passthrough_note="domain; CMS host billed to client",
            recurring_cad_month=25.0,
            notes="The recurring line is the client's, not yours. Quote it separately or they will think it is your fee."),

        Job("sf-small", "Small Salesforce change (field, flow, report, permission)",
            agent_prompts=(10, 30), human_hours=(1.0, 3.0),
            notes="Sandbox first, always. The variance here is discovery, not build."),

        Job("sf-medium", "Salesforce build (object model, automation, integration point)",
            agent_prompts=(60, 160), human_hours=(8.0, 20.0),
            notes="Where scope creep lives. Quote against explicit_out_of_scope from the intake record."),

        Job("sf-migration", "Data migration with de-duplication",
            agent_prompts=(80, 220), human_hours=(12.0, 35.0),
            notes="Cost is driven by whether a stable external ID exists. No shared ID means fuzzy matching plus human review - price that as a separate phase."),

        Job("sf-rescue", "Untangle automation that fights itself",
            agent_prompts=(70, 200), human_hours=(10.0, 30.0),
            notes="Genuinely unpredictable. Sell the diagnostic first, then quote the repair once you know."),

        Job("app-small", "Small internal app or tool",
            agent_prompts=(60, 150), human_hours=(8.0, 20.0),
            passthrough_cad=20.0, passthrough_note="domain",
            recurring_cad_month=10.0,
            notes="Hosting is cheap until it is not. Scale-to-zero unless the client needs it warm."),

        Job("app-full", "Customer-facing application",
            agent_prompts=(150, 400), human_hours=(25.0, 70.0),
            passthrough_cad=40.0, passthrough_note="domain + certs",
            recurring_cad_month=35.0,
            notes="Auth, payments and support turn a build into an obligation. Quote maintenance from day one or you have sold yourself a job."),

        Job("diagnostic", "Fixed-fee diagnostic and written recommendation",
            agent_prompts=(20, 45), human_hours=(2.0, 4.0),
            notes="The wedge. Sell a decision, not hours. Designed to earn the implementation SOW that follows."),
    ]
}


def measured_throughput() -> dict | None:
    """Real per-prompt cost from the log. Returns None if not calibrated yet."""
    if not LOG.exists():
        return None
    rows = []
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows = [r for r in rows if r.get("billable_tokens", 0) > 0]
    if len(rows) < 10:
        return None
    toks = sorted(r["billable_tokens"] for r in rows)
    secs = sorted(r["elapsed_s"] for r in rows if r.get("elapsed_s"))

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    return {
        "n": len(rows),
        "tokens_p50": pct(toks, 0.50), "tokens_p80": pct(toks, 0.80),
        "secs_p50": pct(secs, 0.50) if secs else 0,
        "secs_p80": pct(secs, 0.80) if secs else 0,
        "tokens_mean": statistics.mean(toks),
    }


def estimate(job: Job, complexity: float = 1.0) -> dict:
    tp = measured_throughput()
    tok_p50 = tp["tokens_p50"] if tp else 35_000
    tok_p80 = tp["tokens_p80"] if tp else 90_000
    sec_p50 = tp["secs_p50"] if tp else 170
    sec_p80 = tp["secs_p80"] if tp else 600

    p50_prompts = job.agent_prompts[0] * complexity
    p80_prompts = job.agent_prompts[1] * complexity
    h50 = job.human_hours[0] * complexity
    h80 = job.human_hours[1] * complexity

    usd_per_tok = RATES["usd_per_1k_tokens"] / 1000.0
    fx = RATES["usd_to_cad"]

    agent_cost_50 = p50_prompts * tok_p50 * usd_per_tok * fx
    agent_cost_80 = p80_prompts * tok_p80 * usd_per_tok * fx
    human_cost_50 = h50 * RATES["human_hourly_cad"]
    human_cost_80 = h80 * RATES["human_hourly_cad"]

    # Overhead absorption. Fixed monthly costs do not care how cheap the tokens
    # are -- they arrive every month regardless. Each job carries a share, and
    # the share is set by utilisation, not by effort.
    overhead = RATES["fixed_monthly_cad"] / max(RATES["jobs_per_month"], 0.1)

    marginal = agent_cost_50 + human_cost_50 + job.passthrough_cad
    base = marginal + overhead
    p80 = agent_cost_80 + human_cost_80 + job.passthrough_cad + overhead

    contingency = max(p80 - base, base * RATES["contingency_floor_pct"])
    loaded = base + contingency
    price = loaded * (1 + RATES["margin_pct"])

    agent_hours_50 = p50_prompts * sec_p50 / 3600.0
    agent_hours_80 = p80_prompts * sec_p80 / 3600.0

    return {
        "job": job, "complexity": complexity, "calibrated_on": tp,
        "agent_cost_50": agent_cost_50, "agent_cost_80": agent_cost_80,
        "human_cost_50": human_cost_50, "human_cost_80": human_cost_80,
        "human_hours_50": h50, "human_hours_80": h80,
        "agent_hours_50": agent_hours_50, "agent_hours_80": agent_hours_80,
        "elapsed_days_50": (agent_hours_50 + h50) / 6.0,
        "elapsed_days_80": (agent_hours_80 + h80) / 6.0,
        "marginal": marginal, "overhead": overhead,
        "base": base, "contingency": contingency, "loaded": loaded, "price": price,
    }


def render(e: dict, client_facing: bool) -> str:
    j: Job = e["job"]
    L = []
    if client_facing:
        L.append(f"\n{j.label}")
        L.append("=" * len(j.label))
        L.append(f"\n  Estimated fee     CAD ${e['price']:,.0f}")
        lo, hi = e["elapsed_days_50"], e["elapsed_days_80"]
        L.append(f"  Timeline          {lo:.0f}-{hi:.0f} working days")
        if j.recurring_cad_month:
            L.append(f"  Ongoing           ~CAD ${j.recurring_cad_month:,.0f}/month (billed to you, not by us)")
        if j.passthrough_note:
            L.append(f"  Includes          {j.passthrough_note}")
        L.append("\n  Fixed fee. If scope changes we requote before doing the work,")
        L.append("  never after. Anything explicitly out of scope stays out of scope.")
        return "\n".join(L)

    tp = e["calibrated_on"]
    L.append(f"\n{'=' * 66}")
    L.append(f"INTERNAL ESTIMATE — {j.label}")
    L.append(f"{'=' * 66}")
    if tp:
        L.append(f"  calibrated on {tp['n']} measured prompts "
                 f"(p50 {tp['tokens_p50']:,} tok / {tp['secs_p50']:.0f}s, "
                 f"p80 {tp['tokens_p80']:,} tok / {tp['secs_p80']:.0f}s)")
    else:
        L.append("  NOT CALIBRATED — run scripts/prompt_log.py --backfill first")
    if e["complexity"] != 1.0:
        L.append(f"  complexity multiplier: {e['complexity']}x")
    L.append("")
    L.append(f"  {'':22} {'p50':>12} {'p80':>12}")
    L.append(f"  {'agent (tokens)':22} {e['agent_cost_50']:>11,.2f} {e['agent_cost_80']:>11,.2f}")
    L.append(f"  {'human review':22} {e['human_cost_50']:>11,.2f} {e['human_cost_80']:>11,.2f}"
             f"   ({e['human_hours_50']:.1f}-{e['human_hours_80']:.1f} hrs)")
    if j.passthrough_cad:
        L.append(f"  {'pass-through':22} {j.passthrough_cad:>11,.2f} {j.passthrough_cad:>11,.2f}   ({j.passthrough_note})")
    L.append("  " + "-" * 48)
    L.append(f"  {'marginal cost':22} {e['marginal']:>11,.2f}")
    L.append(f"  {'overhead share':22} {e['overhead']:>11,.2f}   "
             f"(${RATES['fixed_monthly_cad']:,.0f}/mo over {RATES['jobs_per_month']:g} jobs)")
    L.append("  " + "-" * 48)
    L.append(f"  {'base cost (p50)':22} {e['base']:>11,.2f}")
    L.append(f"  {'contingency':22} {e['contingency']:>11,.2f}   (p80 gap, floor "
             f"{RATES['contingency_floor_pct']:.0%})")
    L.append(f"  {'loaded cost':22} {e['loaded']:>11,.2f}")
    L.append(f"  {'margin':22} {e['price'] - e['loaded']:>11,.2f}   ({RATES['margin_pct']:.0%})")
    L.append(f"  {'QUOTE':22} {e['price']:>11,.2f}   CAD")
    L.append("")
    L.append(f"  agent time  {e['agent_hours_50']:.1f}-{e['agent_hours_80']:.1f} hrs   "
             f"human time {e['human_hours_50']:.1f}-{e['human_hours_80']:.1f} hrs   "
             f"elapsed {e['elapsed_days_50']:.0f}-{e['elapsed_days_80']:.0f} working days")
    # Utilisation sensitivity. The single biggest lever on the number above,
    # and the one most likely to be guessed optimistically.
    L.append("\n  IF THE PIPELINE IS THINNER THAN ASSUMED")
    for n in (1, 2, 4, 8):
        oh = RATES["fixed_monthly_cad"] / n
        b = e["marginal"] + oh
        c = max((e["base"] + e["contingency"]) - e["base"], b * RATES["contingency_floor_pct"])
        p = (b + c) * (1 + RATES["margin_pct"])
        mark = "  <- assumed" if abs(n - RATES["jobs_per_month"]) < 0.01 else ""
        L.append(f"    {n} job/mo: overhead ${oh:>6,.0f}  ->  quote ${p:>9,.0f}{mark}")
    share = e["overhead"] / e["base"] if e["base"] else 0
    L.append(f"\n  Overhead is {share:.0%} of base cost. Agent tokens are "
             f"{e['agent_cost_50'] / e['base']:.0%}.")
    if share > 0.5:
        L.append("  Your economics are set by UTILISATION, not by cost of delivery.")
        L.append("  Winning one more job a month moves the number far more than any")
        L.append("  saving on tooling ever will.")

    if j.notes:
        L.append(f"\n  NOTE: {j.notes}")
    if RATES_ARE_DEFAULT:
        L.append("\n  ** RATES ARE UNVERIFIED DEFAULTS. The token price is derived from the")
        L.append("     one minor model the harness costed; the primary model's 1704 records")
        L.append("     carry no cost at all. Set real numbers before sending this to anyone. **")
    L.append("\n" + VALUE_ANCHOR)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Estimate and quote a job.")
    ap.add_argument("job", nargs="?", choices=sorted(CATALOGUE), help="job type")
    ap.add_argument("--complexity", type=float, default=1.0,
                    help="0.5 trivial, 1.0 typical, 2.0 gnarly")
    ap.add_argument("--client", action="store_true", help="client-facing quote only")
    ap.add_argument("--margin", type=float, default=None, help="override margin, e.g. 0.4")
    ap.add_argument("--list", action="store_true", help="list job types")
    args = ap.parse_args()

    if args.list or not args.job:
        print("\nJob types:\n")
        for k, j in CATALOGUE.items():
            print(f"  {k:16} {j.label}")
        tp = measured_throughput()
        print(f"\ncalibration: {tp['n']} measured prompts" if tp else
              "\ncalibration: NONE — run scripts/prompt_log.py --backfill")
        return 0

    if args.margin is not None:
        RATES["margin_pct"] = args.margin

    print(render(estimate(CATALOGUE[args.job], args.complexity), args.client))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
