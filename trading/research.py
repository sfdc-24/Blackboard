#!/usr/bin/env python3
"""
SFDC24 — pre-registered strategy screen
claude-code-cli, 2026-09-03.

THE PROBLEM THIS FILE EXISTS TO AVOID
    "Find strategies that work" is, done naively, a machine for generating
    false discoveries. Test 100 rules on one history and the best few will look
    excellent purely by chance. Publish those and you have laundered noise into
    a trading plan.

    Three guards, all of which have to be in place BEFORE the first result is
    seen or they are worthless:

    1. PRE-REGISTRATION. The hypotheses below were written down before any
       result was looked at, and each one has a documented economic rationale
       from published literature. None was chosen because it scored well here.
       Adding a rule after seeing results and reporting it as if pre-registered
       is the exact fraud this guards against.

    2. HOLDOUT. Data splits at SPLIT_DATE. Everything before is in-sample.
       Everything after is untouched until the final scoring pass. A strategy
       that only works in-sample has told you it is curve-fitted.

    3. MULTIPLE-TESTING CORRECTION. Six strategies against one dataset means
       the naive 5% threshold is wrong. Bonferroni is applied and reported.
       It is conservative, and conservative is the correct bias here.

    Also: RANDOM SAMPLING of the universe, with a fixed seed. Picking names by
    hand is how a backtest gets pre-loaded with winners you already know about.

WHAT THIS CANNOT FIX
    Survivorship. The data source has no delisted names, so every ticker in the
    universe below is a company that still exists in 2026. Real results would
    be worse. This biases every number here upward and there is no correction
    for it available at this budget.

NOT ADVICE
    This is a research screen. It produces evidence about historical
    regularities, not a recommendation to trade anything. I am not licensed to
    advise and this is not advice.
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fetch import fetch, cache_path
import backtest as bt

# Everything before this is search space; everything after is untouched holdout.
SPLIT_DATE = "2021-01-01"

# A deliberately broad, mechanically-chosen liquid universe. Hand-picking names
# is a bias vector, so the run samples from this at random with a fixed seed.
UNIVERSE_US = [
    "AAPL", "MSFT", "JNJ", "XOM", "PG", "KO", "PEP", "WMT", "HD", "MCD",
    "CVX", "MRK", "PFE", "CSCO", "INTC", "VZ", "T", "IBM", "GE", "CAT",
    "MMM", "BA", "DIS", "NKE", "UNH", "AXP", "GS", "JPM", "BAC", "WFC",
    "TGT", "LOW", "COST", "ADBE", "ORCL", "TXN", "QCOM", "AMGN", "GILD", "BMY",
]
UNIVERSE_TSX = [
    "RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO", "ENB.TO", "TRP.TO", "CNR.TO",
    "CP.TO", "SU.TO", "CNQ.TO", "BCE.TO", "T.TO", "FTS.TO", "EMA.TO", "MFC.TO",
    "SLF.TO", "POW.TO", "NA.TO", "IMO.TO", "TECK-B.TO", "ABX.TO", "AEM.TO", "WCN.TO",
]


# ----------------------------------------------------------------------------
# PRE-REGISTERED HYPOTHESES — fixed before any result was inspected.
# ----------------------------------------------------------------------------

def h_momentum_12_1(px: pd.DataFrame) -> pd.Series:
    """H1 Time-series momentum. Long when the trailing 12-month return excluding
    the most recent month is positive. Rationale: Moskowitz, Ooi & Pedersen
    (2012) document this across asset classes over ~25 years."""
    c = px["Close"].astype(float)
    r = c.shift(21) / c.shift(252) - 1
    return (r > 0).astype(float)


def h_short_reversal(px: pd.DataFrame) -> pd.Series:
    """H2 Short-term reversal. Long after a negative 5-day return. Rationale:
    Jegadeesh (1990) — weekly reversal from liquidity provision."""
    c = px["Close"].astype(float)
    return (c.pct_change(5) < 0).astype(float)


def h_52w_high(px: pd.DataFrame) -> pd.Series:
    """H3 Proximity to the 52-week high. Long within 5% of it. Rationale:
    George & Hwang (2004) — the high acts as an anchor and breakouts persist."""
    c = px["Close"].astype(float)
    return (c >= 0.95 * c.rolling(252).max()).astype(float)


def h_vol_target(px: pd.DataFrame) -> pd.Series:
    """H4 Volatility avoidance. Long only when 20-day realised vol sits below
    its own 1-year median. Rationale: the low-volatility anomaly — Baker,
    Bradley & Wurgler (2011)."""
    c = px["Close"].astype(float)
    vol = c.pct_change().rolling(20).std()
    return (vol < vol.rolling(252).median()).astype(float)


def h_turn_of_month(px: pd.DataFrame) -> pd.Series:
    """H5 Turn-of-month seasonality. Long the last 1 and first 3 trading days of
    each month. Rationale: Lakonishok & Smidt (1988); persistent calendar
    effect often attributed to fund flows."""
    c = px["Close"].astype(float)
    idx = pd.DatetimeIndex(c.index)
    dom = pd.Series(idx.day, index=c.index)
    is_start = dom <= 3
    is_end = dom >= 28
    return (is_start | is_end).astype(float)


def h_gap_fade(px: pd.DataFrame) -> pd.Series:
    """H6 Overnight gap fade. Long after the open gaps down more than 1% from
    the prior close. Rationale: overnight overreaction reverting intraday."""
    o, c = px["Open"].astype(float), px["Close"].astype(float)
    gap = o / c.shift(1) - 1
    return (gap < -0.01).astype(float)


HYPOTHESES = {
    "H1_momentum_12_1": h_momentum_12_1,
    "H2_short_reversal": h_short_reversal,
    "H3_52w_high": h_52w_high,
    "H4_low_vol": h_vol_target,
    "H5_turn_of_month": h_turn_of_month,
    "H6_gap_fade": h_gap_fade,
}
N_TESTS = len(HYPOTHESES)


@dataclass
class Score:
    name: str
    oos_excess: list[float] = field(default_factory=list)
    oos_sharpe: list[float] = field(default_factory=list)
    is_excess: list[float] = field(default_factory=list)
    beat_bh: int = 0
    n: int = 0


def evaluate(px: pd.DataFrame, sig: pd.Series, commission_bps: float,
             slippage_bps: float) -> dict:
    res = bt.run(px, sig, commission_bps=commission_bps, slippage_bps=slippage_bps)
    m = res.metrics(benchmark=px["Close"].astype(float))
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-registered strategy screen.")
    ap.add_argument("--n-sample", type=int, default=16, help="random names to draw")
    ap.add_argument("--seed", type=int, default=20260903, help="fixed for reproducibility")
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--no-fetch", action="store_true", help="use cache only")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pool = UNIVERSE_US + UNIVERSE_TSX
    sample = rng.sample(pool, min(args.n_sample, len(pool)))

    print("=" * 78)
    print("PRE-REGISTERED SCREEN")
    print("=" * 78)
    print(f"  hypotheses fixed in advance : {N_TESTS}")
    print(f"  random sample (seed={args.seed}) : {args.n_sample} names")
    print(f"  holdout begins              : {SPLIT_DATE} (untouched during design)")
    print(f"  costs                       : {args.commission_bps}bps + {args.slippage_bps}bps")
    print(f"  Bonferroni threshold        : p < {0.05 / N_TESTS:.4f}  (0.05 / {N_TESTS})")
    print(f"  sample: {', '.join(sample)}\n")

    if not args.no_fetch:
        missing = [s for s in sample if not cache_path(s, "1d").exists()]
        if missing:
            print(f"fetching {len(missing)} uncached names...")
            fetch(missing, interval="1d")
            print()

    scores = {name: Score(name) for name in HYPOTHESES}
    loaded = 0

    for sym in sample:
        p = cache_path(sym, "1d")
        if not p.exists():
            continue
        px = pd.read_parquet(p)
        if len(px) < 800:
            continue
        px.index = pd.DatetimeIndex(px.index).tz_localize(None)
        is_df = px[px.index < SPLIT_DATE]
        oos_df = px[px.index >= SPLIT_DATE]
        if len(is_df) < 500 or len(oos_df) < 200:
            continue
        loaded += 1

        for name, fn in HYPOTHESES.items():
            try:
                # Signal computed on the full series so rolling windows are warm
                # at the holdout boundary, then sliced. The one-bar shift that
                # prevents look-ahead lives in bt.run().
                full_sig = fn(px)
                for df, bucket in ((is_df, "is"), (oos_df, "oos")):
                    m = evaluate(df, full_sig.reindex(df.index),
                                 args.commission_bps, args.slippage_bps)
                    if "error" in m:
                        continue
                    ex = m.get("excess_cagr")
                    if ex is None or not np.isfinite(ex):
                        continue
                    if bucket == "is":
                        scores[name].is_excess.append(ex)
                    else:
                        scores[name].oos_excess.append(ex)
                        sh = m.get("sharpe")
                        if sh is not None and np.isfinite(sh):
                            scores[name].oos_sharpe.append(sh)
                        scores[name].beat_bh += int(ex > 0)
                        scores[name].n += 1
            except Exception:
                continue

    print(f"evaluated {loaded} names with sufficient history\n")
    print("=" * 78)
    print("RESULTS — excess CAGR vs buy-and-hold on the same name and window")
    print("=" * 78)
    print(f"  {'hypothesis':<20} {'IS med':>8} {'OOS med':>9} {'OOS Sh':>7} {'beat B&H':>10} {'p(binom)':>9}")
    print("  " + "-" * 68)

    from math import comb
    rows = []
    for name, s in scores.items():
        if s.n == 0:
            continue
        is_med = float(np.median(s.is_excess)) if s.is_excess else float("nan")
        oos_med = float(np.median(s.oos_excess))
        oos_sh = float(np.median(s.oos_sharpe)) if s.oos_sharpe else float("nan")
        # one-sided binomial: could this hit rate arise from a coin flip?
        k, n = s.beat_bh, s.n
        p = sum(comb(n, i) for i in range(k, n + 1)) / (2 ** n)
        rows.append((name, is_med, oos_med, oos_sh, k, n, p))

    for name, is_med, oos_med, oos_sh, k, n, p in sorted(rows, key=lambda r: -r[2]):
        flag = "  <-- survives Bonferroni" if p < 0.05 / N_TESTS else ""
        print(f"  {name:<20} {is_med:>+8.2%} {oos_med:>+9.2%} {oos_sh:>7.2f} "
              f"{k:>5}/{n:<4} {p:>9.4f}{flag}")

    survivors = [r for r in rows if r[6] < 0.05 / N_TESTS and r[2] > 0]
    print()
    print("=" * 78)
    if survivors:
        print(f"{len(survivors)} hypothesis(es) survived correction with positive OOS excess.")
        print("That is a reason to investigate further, not a reason to trade. Next:")
        print("  - confirm it holds on names NOT in this sample")
        print("  - confirm it is not one sector or one regime carrying the result")
        print("  - paper trade it before any capital is involved")
    else:
        print("NO hypothesis survived multiple-testing correction with positive OOS excess.")
        print("This is the most common outcome of an honest screen and it is a")
        print("real result, not a failed run. Buy-and-hold is hard to beat after")
        print("costs, which is why most published edges do not replicate.")
    print("=" * 78)
    print("\nSurvivorship bias is uncorrected — every name here still exists in 2026,")
    print("so all figures skew optimistic. Not advice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
