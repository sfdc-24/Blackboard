#!/usr/bin/env python3
"""
SFDC24 Blackboard — PUDDING harness v1 ("the proof is in the pudding")
claude-code-cli, 2026-09-01. Paired, blinded, pre-registered A/B trial:
  Arm A = one premium model, direct API call (claude-opus-5)
  Arm B = the Blackboard gateway (webhook -> router -> routed lane -> board row)

PRE-REGISTERED HYPOTHESES (margins fixed BEFORE data; see protocol doc
"SFDC24 — PUDDING Protocol v1 (Blackboard vs Direct)"):
  H1 cost, tier S+M : H0 median(costB - costA) >= 0     H1 < 0        (one-sided Wilcoxon signed-rank, alpha=.05)
  H2 quality, S+M   : H0 mean(Qb - Qa) <= -0.25         H1 > -0.25    (non-inferiority, bootstrap 95% CI lower bound)
  H3 cost, tier C   : H0 median(1.25*costA - costB) <= 0  H1 > 0      (B within +25% of A; one-sided Wilcoxon)
  H4 quality, C     : same non-inferiority as H2
Quality Q in 0..3, scored by BLINDED judges (arm labels hidden, answer order
randomized) that did not author either answer (L-38). Verdicts are printed
whatever they are — an inconclusive run is a result, not a failure (HONEST LIMIT culture).

PREREQUISITES (the "architectural finishing" this harness forces):
  P1  local .env gains ANTHROPIC_API_KEY (Arm A + judge 1); optional GEMINI_API_KEY (judge 2).
  P2  gateway implements the WAR reply contract WITH cost fields
      (WAR|re=<wamid>|to=..|by=..|model=..|tok_in=..|tok_out=..|text=..)  — without
      tok_* fields Arm B cost is unmeasurable and this harness marks the run INVALID for H1/H3.
  P3  gateway TEST-SENDER GUARD: from numbers starting 1555000 are answered on the
      board only, never sent to Graph API (no WhatsApp spam, no per-message billing).
  P4  wamid dedup restored (ISSUE 028) so retries do not double-count trials.
USAGE
  python scripts/pudding_harness.py --bank data/pudding_bank_v1.tsv --tier S --n 10 --block 1
  python scripts/pudding_harness.py --bank data/pudding_bank_v1.tsv --all --block 1 --post-board
  python scripts/pudding_harness.py --stats-only results/pudding_trials.jsonl
"""
import argparse, csv, json, math, os, random, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUS_PS1 = REPO / "scripts" / "bus.ps1"
ALPHA_PS1 = REPO / "scripts" / "alpha.ps1"
RESULTS_DIR = REPO / "results"; RESULTS_DIR.mkdir(exist_ok=True)

# $/MTok input, output. Arm B rows must name their model; unknown model -> cost NaN.
PRICES = {
    "claude-opus-5": (5.00, 25.00), "claude-sonnet-5": (2.00, 10.00), "claude-haiku-4-5": (1.00, 5.00),
    "gemini-3.6-flash": (0.15, 0.60),   # confirm against current Google rate card before a scored run
    "gpt-4o-mini": (0.15, 0.60),        # confirm against current OpenAI rate card before a scored run
    "llama-3.3-70b-versatile": (0.59, 0.79),
}
ARM_A_MODEL = "claude-opus-5"
TEST_SENDER = "15550009999"  # P3 guard prefix 1555000
SYSTEM_PROMPT = ("You are a helpful assistant answering one question sent over WhatsApp. "
                 "Answer correctly and concisely, under 900 characters, plain text.")

def load_env():
    env = {}
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m: env[m.group(1)] = m.group(2).strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or k == "WEBHOOK_URL"})
    return env

ENV = load_env()

def cost_usd(model, tok_in, tok_out):
    if model not in PRICES or tok_in is None or tok_out is None: return float("nan")
    i, o = PRICES[model]
    return tok_in / 1e6 * i + tok_out / 1e6 * o

# ---------- Arm A: direct premium call (official SDK; pip install anthropic) ----------
def arm_a(question):
    import anthropic
    client = anthropic.Anthropic(api_key=ENV.get("ANTHROPIC_API_KEY") or None)
    t0 = time.time()
    r = client.messages.create(model=ARM_A_MODEL, max_tokens=2048, system=SYSTEM_PROMPT,
                               messages=[{"role": "user", "content": question}])
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    return {"text": text, "model": ARM_A_MODEL, "tok_in": r.usage.input_tokens,
            "tok_out": r.usage.output_tokens, "t_s": round(time.time() - t0, 2),
            "cost": cost_usd(ARM_A_MODEL, r.usage.input_tokens, r.usage.output_tokens)}

# ---------- Arm B: the blackboard gateway ----------
def read_board():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f: tmp = f.name
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUS_PS1),
                    "-Action", "read", "-Title", "Blackboard - Alpha DB", "-OutFile", tmp],
                   capture_output=True, timeout=180)
    raw = Path(tmp).read_text(encoding="utf-8"); Path(tmp).unlink(missing_ok=True)
    data = json.loads(raw)
    return data.get("rows") or data.get("data") or []

def arm_b(question, qid, block):
    url = ENV.get("WEBHOOK_URL", "https://eoykh6zqr7ibsuw.m.pipedream.net/")
    wamid = f"wamid.PUD-{qid}-B{block}-{int(time.time())}"
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "TEST", "changes": [{"field": "messages",
        "value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "TEST"},
                  "contacts": [{"profile": {"name": "PUDDING harness"}, "wa_id": TEST_SENDER}],
                  "messages": [{"from": TEST_SENDER, "id": wamid, "timestamp": str(int(time.time())),
                                "type": "text", "text": {"body": question}}]}}]}]}
    t0 = time.time()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()
    # poll the board for the WAR row answering this wamid (P2 contract)
    for _ in range(30):
        time.sleep(3)
        for row in reversed(read_board()):
            p = str(row[5]) if len(row) > 5 else ""
            if f"re={wamid}" in p or f"re_wamid={wamid}" in p:
                f = dict(kv.split("=", 1) for kv in p.split("|")[1:] if "=" in kv)
                tok_in = int(f["tok_in"]) if f.get("tok_in", "").isdigit() else None
                tok_out = int(f["tok_out"]) if f.get("tok_out", "").isdigit() else None
                model = f.get("model", "unknown")
                return {"text": f.get("text", ""), "model": model, "tok_in": tok_in, "tok_out": tok_out,
                        "t_s": round(time.time() - t0, 2), "cost": cost_usd(model, tok_in, tok_out),
                        "lane": f.get("by", "?"), "wamid": wamid}
    return {"text": "", "model": "TIMEOUT", "tok_in": None, "tok_out": None,
            "t_s": round(time.time() - t0, 2), "cost": float("nan"), "lane": "none", "wamid": wamid}

# ---------- Blinded judging ----------
JUDGE_RUBRIC = ("Score each answer 0-3 for the question, using the reference points as ground truth: "
                "0=wrong or harmful; 1=vague or partly wrong; 2=correct and useful; "
                "3=correct, complete vs reference points, and well-sized for a phone message. "
                "Also name the trap if an answer fell into it. Reply ONLY with JSON: "
                '{"score_X": n, "score_Y": n, "better": "X"|"Y"|"tie", "note": "<20 words"}')

def judge_claude(question, refs, trap, ans_x, ans_y):
    import anthropic
    client = anthropic.Anthropic(api_key=ENV.get("ANTHROPIC_API_KEY") or None)
    r = client.messages.create(model="claude-opus-5", max_tokens=300,
        system="You are a strict, fair grader. You never know which system produced which answer.",
        messages=[{"role": "user", "content":
            f"QUESTION: {question}\nREFERENCE POINTS: {refs}\nKNOWN TRAP: {trap}\n\n"
            f"ANSWER X:\n{ans_x}\n\nANSWER Y:\n{ans_y}\n\n{JUDGE_RUBRIC}"}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None

# ---------- Statistics (stdlib-only implementations, scipy used when present) ----------
def wilcoxon_one_sided(diffs):
    """One-sided Wilcoxon signed-rank: H1 median(diff) < 0. Returns (W+, n_eff, p)."""
    d = [x for x in diffs if x != 0 and not math.isnan(x)]
    n = len(d)
    if n < 6: return (None, n, None)  # too small for a meaningful p
    try:
        from scipy.stats import wilcoxon
        stat, p = wilcoxon(d, alternative="less")
        return (float(stat), n, float(p))
    except Exception:
        ranks = sorted((abs(x), i) for i, x in enumerate(d))
        rank_of = {}
        k = 0
        while k < n:  # average ties
            j = k
            while j + 1 < n and ranks[j + 1][0] == ranks[k][0]: j += 1
            avg = (k + j) / 2 + 1
            for t in range(k, j + 1): rank_of[ranks[t][1]] = avg
            k = j + 1
        w_plus = sum(rank_of[i] for i, x in enumerate(d) if x > 0)
        mu = n * (n + 1) / 4; sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        z = (w_plus - mu) / sigma
        p = 0.5 * (1 + math.erf(z / math.sqrt(2)))  # P(W+ <= observed) ~ H1: diffs negative
        return (w_plus, n, p)

def bootstrap_ci_mean(diffs, iters=10000, alpha=0.05, seed=24):
    d = [x for x in diffs if not math.isnan(x)]
    if len(d) < 5: return (None, None)
    rng = random.Random(seed); n = len(d); means = []
    for _ in range(iters):
        means.append(sum(rng.choice(d) for _ in range(n)) / n)
    means.sort()
    return (means[int(alpha / 2 * iters)], means[int((1 - alpha / 2) * iters) - 1])

def run_stats(trials):
    out = {}
    for tier_group, name in ((("S", "M"), "simple+moderate"), (("C",), "complex")):
        t = [x for x in trials if x["tier"] in tier_group and x["b"]["model"] != "TIMEOUT"]
        cost_d = [x["b"]["cost"] - x["a"]["cost"] for x in t]
        q_d = [x["q_b"] - x["q_a"] for x in t if x.get("q_a") is not None and x.get("q_b") is not None]
        if name == "complex":  # H3: 1.25*A - B > 0  ->  test diffs (B - 1.25A) < 0
            cost_d = [x["b"]["cost"] - 1.25 * x["a"]["cost"] for x in t]
        w, n, p = wilcoxon_one_sided(cost_d)
        lo, hi = bootstrap_ci_mean(q_d)
        out[name] = {
            "n": len(t), "n_cost_valid": sum(1 for x in cost_d if not math.isnan(x)),
            "mean_cost_A": round(sum(x["a"]["cost"] for x in t) / len(t), 5) if t else None,
            "mean_cost_B": round(sum(x["b"]["cost"] for x in t if not math.isnan(x["b"]["cost"])) / max(1, sum(1 for x in t if not math.isnan(x["b"]["cost"]))), 5) if t else None,
            "cost_hypothesis": "H1: B < A" if name != "complex" else "H3: B <= 1.25*A",
            "wilcoxon_p_one_sided": round(p, 4) if p is not None else "n<6",
            "cost_verdict": ("REJECT H0 (blackboard cheaper within margin)" if p is not None and p < 0.05 else "FAIL TO REJECT H0 (not proven)"),
            "quality_mean_diff_B_minus_A": round(sum(q_d) / len(q_d), 3) if q_d else None,
            "quality_CI95": (round(lo, 3), round(hi, 3)) if lo is not None else "n<5",
            "quality_verdict": ("NON-INFERIOR (CI lower bound > -0.25)" if lo is not None and lo > -0.25 else "NOT ESTABLISHED"),
        }
    return out

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", default="data/pudding_bank_v1.tsv")
    ap.add_argument("--tier", choices=["S", "M", "C"]); ap.add_argument("--all", action="store_true")
    ap.add_argument("--n", type=int, default=10); ap.add_argument("--block", type=int, default=1)
    ap.add_argument("--post-board", action="store_true", help="append one BCB v=3 summary row (never per-trial spam)")
    ap.add_argument("--stats-only", help="recompute stats from an existing trials JSONL")
    args = ap.parse_args()

    if args.stats_only:
        trials = [json.loads(l) for l in Path(args.stats_only).read_text(encoding="utf-8").splitlines()]
        print(json.dumps(run_stats(trials), indent=2)); return

    bank = list(csv.DictReader(Path(REPO / args.bank).open(encoding="utf-8"), delimiter="\t"))
    pick = [q for q in bank if args.all or q["tier"] == args.tier]
    rng = random.Random(args.block)  # deterministic per block, different across blocks
    pick = rng.sample(pick, min(args.n if not args.all else len(pick), len(pick)))

    trials, out_path = [], RESULTS_DIR / f"pudding_trials_block{args.block}.jsonl"
    for q in pick:
        print(f"[{q['id']}] {q['question'][:70]}...")
        a = arm_a(q["question"]); b = arm_b(q["question"], q["id"], args.block)
        flip = rng.random() < 0.5  # blind: randomize order, judge never sees arm labels
        jx, jy = (a["text"], b["text"]) if flip else (b["text"], a["text"])
        j = judge_claude(q["question"], q["reference_points"], q["trap"], jx, jy) or {}
        q_a = j.get("score_X") if flip else j.get("score_Y")
        q_b = j.get("score_Y") if flip else j.get("score_X")
        trial = {"id": q["id"], "tier": q["tier"], "domain": q["domain"], "block": args.block,
                 "a": a, "b": b, "q_a": q_a, "q_b": q_b, "judge_note": j.get("note", "")}
        trials.append(trial)
        with out_path.open("a", encoding="utf-8") as f: f.write(json.dumps(trial) + "\n")
    stats = run_stats(trials)
    print(json.dumps(stats, indent=2))
    (RESULTS_DIR / f"pudding_summary_block{args.block}.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    if args.post_board:
        summary = (f"BCB|v=3|id=PUD-B{args.block}|phase=VERIFY|class=EXPERIMENT|from=claude-code-cli"
                   f"|task=PUDDING block {args.block} n={len(trials)} - " +
                   " - ".join(f"{k}: cost {v['cost_verdict']} / quality {v['quality_verdict']}" for k, v in stats.items()))
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ALPHA_PS1),
                        "-Action", "append", "-SourceTag", "claude-code-cli",
                        "-TargetSurface", "Blackboard Alpha DB", "-Payload", summary], timeout=180)
        print("summary row appended - READ IT BACK before trusting (L-1)")

if __name__ == "__main__":
    main()
