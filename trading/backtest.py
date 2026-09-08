#!/usr/bin/env python3
"""
SFDC24 — backtest harness
claude-code-cli, 2026-09-03.

WHY THIS EXISTS IN THIS SHAPE
    Most homemade backtesters produce beautiful, false results. They do it in
    three specific ways, so this harness closes all three by construction rather
    than by remembering:

    1. LOOK-AHEAD. If a signal computed from today's close is executed at
       today's close, you traded on information you did not have. `run()`
       shifts every signal forward one bar. There is no flag to turn this off.

    2. FREE TRADING. Zero-cost backtests make noise look like alpha. Commission
       and slippage are REQUIRED arguments with no defaults you can forget.

    3. SURVIVORSHIP. This harness cannot fix it — the data source has no
       delisted names. It is printed on every run so the number is read with
       the caveat attached.

    Nothing here connects to a broker or places an order.

WHAT THE EXAMPLE STRATEGY IS AND IS NOT
    A moving-average crossover is included so the harness can be tested against
    something. It is a TEST FIXTURE, not a recommendation, and it is not
    investment advice. It is one of the most widely published rules in
    existence, which is a good reason to assume it is priced in.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fetch import load

TRADING_DAYS = 252


@dataclass
class Result:
    equity: pd.Series
    returns: pd.Series
    position: pd.Series
    trades: int
    currency: str

    def metrics(self, benchmark: pd.Series | None = None) -> dict:
        r = self.returns.dropna()
        if r.empty:
            return {"error": "no returns"}

        years = len(r) / TRADING_DAYS
        total = float(self.equity.iloc[-1] / self.equity.iloc[0])
        cagr = total ** (1 / years) - 1 if years > 0 else float("nan")
        vol = float(r.std() * np.sqrt(TRADING_DAYS))
        sharpe = float(r.mean() / r.std() * np.sqrt(TRADING_DAYS)) if r.std() > 0 else float("nan")

        peak = self.equity.cummax()
        dd = self.equity / peak - 1
        max_dd = float(dd.min())

        downside = r[r < 0].std()
        sortino = float(r.mean() / downside * np.sqrt(TRADING_DAYS)) if downside and downside > 0 else float("nan")

        m = {
            "years": round(years, 2),
            "total_return": round(total - 1, 4),
            "cagr": round(cagr, 4),
            "vol_annual": round(vol, 4),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "max_drawdown": round(max_dd, 4),
            "trades": self.trades,
            "time_in_market": round(float((self.position != 0).mean()), 3),
            "currency": self.currency,
        }
        if benchmark is not None:
            b = benchmark.reindex(self.equity.index).ffill().dropna()
            if len(b) > 1:
                b_total = float(b.iloc[-1] / b.iloc[0])
                b_cagr = b_total ** (1 / years) - 1 if years > 0 else float("nan")
                b_dd = float((b / b.cummax() - 1).min())
                m["benchmark_cagr"] = round(b_cagr, 4)
                m["benchmark_max_dd"] = round(b_dd, 4)
                m["excess_cagr"] = round(cagr - b_cagr, 4)
        return m


def run(prices: pd.DataFrame, signal: pd.Series, *,
        commission_bps: float, slippage_bps: float,
        currency: str = "?") -> Result:
    """Backtest a long/flat or long/short signal.

    signal: -1, 0 or 1, indexed like prices. It is SHIFTED BY ONE BAR here —
    pass the signal as you computed it from that bar's data and the harness
    handles the timing. This is the single most important line in the file.
    """
    px = prices["Close"].astype(float)
    sig = signal.reindex(px.index).fillna(0.0)

    position = sig.shift(1).fillna(0.0)        # <- no look-ahead, not optional

    asset_ret = px.pct_change().fillna(0.0)
    gross = position * asset_ret

    turnover = position.diff().abs().fillna(position.abs())
    cost_rate = (commission_bps + slippage_bps) / 10_000.0
    costs = turnover * cost_rate

    net = gross - costs
    equity = (1 + net).cumprod()
    trades = int((position.diff().fillna(position) != 0).sum())

    return Result(equity=equity, returns=net, position=position,
                  trades=trades, currency=currency)


def ma_crossover(prices: pd.DataFrame, fast: int = 50, slow: int = 200,
                 allow_short: bool = False) -> pd.Series:
    """TEST FIXTURE ONLY. Not a recommendation and not investment advice."""
    c = prices["Close"].astype(float)
    f, s = c.rolling(fast).mean(), c.rolling(slow).mean()
    sig = pd.Series(0.0, index=c.index)
    sig[f > s] = 1.0
    if allow_short:
        sig[f < s] = -1.0
    sig[s.isna()] = 0.0          # no position before the slow MA exists
    return sig


CAVEATS = """
  READ THE NUMBER WITH THESE ATTACHED
  - Survivorship bias: the source has no delisted names. Universe results skew high.
  - Single price series: no dividends beyond auto_adjust, no borrow cost for shorts.
  - Fills assumed at the close with fixed slippage. Real fills are worse in size.
  - One instrument, one currency. Never sum a .TO result with a US result.
  - Past behaviour is not predictive. A good backtest is a reason to keep looking,
    not a reason to trade."""


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest a cached symbol.")
    ap.add_argument("--symbol", required=True, help="e.g. RY.TO or AAPL")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--fast", type=int, default=50)
    ap.add_argument("--slow", type=int, default=200)
    ap.add_argument("--short", action="store_true", help="allow short side")
    ap.add_argument("--commission-bps", type=float, required=True,
                    help="round-trip commission in basis points, e.g. 5")
    ap.add_argument("--slippage-bps", type=float, required=True,
                    help="assumed slippage in basis points, e.g. 5")
    args = ap.parse_args()

    prices = load(args.symbol, args.interval)
    currency = prices.attrs.get("currency", "?")

    sig = ma_crossover(prices, args.fast, args.slow, allow_short=args.short)
    res = run(prices, sig, commission_bps=args.commission_bps,
              slippage_bps=args.slippage_bps, currency=currency)

    bh = prices["Close"].astype(float)
    m = res.metrics(benchmark=bh)

    print(f"\n{args.symbol}  {args.interval}  MA({args.fast}/{args.slow})"
          f"{' long/short' if args.short else ' long/flat'}")
    print(f"{prices.index[0].date()} -> {prices.index[-1].date()}   "
          f"costs: {args.commission_bps}bps commission + {args.slippage_bps}bps slippage")
    print("-" * 62)
    for k, v in m.items():
        print(f"  {k:<18} {v}")
    print(CAVEATS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
