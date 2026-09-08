#!/usr/bin/env python3
"""
SFDC24 — market data fetcher (TSX / NYSE / NASDAQ / DJIA + FX)
claude-code-cli, 2026-09-03.

WHAT THIS IS
    A cache-to-disk fetcher so backtests read local parquet instead of hammering
    a public endpoint. Nothing here places an order or connects to a broker.

SOURCE AND ITS LIMITS — read before trusting a result
    Data comes from Yahoo Finance via yfinance. It is FREE and it is NOT an
    official API. Three consequences, all verified on this machine 2026-09-03:

    1. INTRADAY HISTORY IS SHALLOW. Measured, not assumed:
           1m  -> ~8 days      2m -> ~45 days     5m/15m -> ~85 days
           1h  -> ~1060 days   1d -> 10y+
       So a 1-minute strategy CANNOT be backtested here beyond about a week.
       Fix: run `collect` daily on a schedule and archive; depth accrues.

    2. DELAY IS UNDETERMINED. Yahoo omits the delay field entirely. It was not
       measurable when this was written because the market was closed. Assume
       delayed until measured during a live session. Do NOT build anything
       latency-sensitive on it.

    3. SURVIVORSHIP BIAS. Delisted tickers are absent. Any universe-level
       backtest run on a list of today's members is biased upward, sometimes
       enormously. Single-name research is unaffected.

    Terms of use: fine for private research. Do not redistribute the data or
    build a commercial product on this endpoint.

CURRENCY — the trap for a Canadian running both markets
    TSX quotes in CAD, US markets in USD. Summing them in one portfolio without
    conversion produces a number that means nothing. fetch() records the
    currency per symbol so downstream code can refuse to mix silently.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "market"

# Index symbols for the four markets Mr. Salam named. Note that "the Dow" is an
# index, not an exchange, and it is licensed by S&P Dow Jones Indices — you can
# read its level here, but the tradeable proxy is the DIA ETF.
INDEXES = {
    "tsx_composite": "^GSPTSE",
    "djia": "^DJI",
    "nasdaq_composite": "^IXIC",
    "nyse_composite": "^NYA",
    "sp500": "^GSPC",
}

FX = {
    "usdcad": "USDCAD=X",
    "cadusd": "CADUSD=X",
    "eurusd": "EURUSD=X",
}

# Empirically measured ceilings. Asking for more silently returns less, which is
# how a backtest ends up quietly running on a fraction of the window you think.
MAX_RANGE = {
    "1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d",
    "30m": "60d", "1h": "730d", "1d": "max", "1wk": "max", "1mo": "max",
}


def cache_path(symbol: str, interval: str) -> Path:
    safe = symbol.replace("^", "_idx_").replace("=", "_").replace(".", "_")
    return CACHE / interval / f"{safe}.parquet"


def fetch(symbols: list[str], interval: str = "1d", period: str | None = None,
          use_cache: bool = True) -> dict[str, pd.DataFrame]:
    """Download and cache. Returns {symbol: DataFrame} with a .attrs['currency'].

    Merges with any existing cache rather than replacing it — that is what makes
    the daily `collect` job accumulate intraday depth the source will not give
    you in one request.
    """
    if interval not in MAX_RANGE:
        raise ValueError(f"interval must be one of {sorted(MAX_RANGE)}")
    period = period or MAX_RANGE[interval]

    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        path = cache_path(sym, interval)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            t = yf.Ticker(sym)
            df = t.history(period=period, interval=interval, auto_adjust=True)
        except Exception as e:
            print(f"  {sym:<16} FETCH FAILED: {str(e)[:70]}", file=sys.stderr)
            continue

        if df is None or df.empty:
            print(f"  {sym:<16} no data returned", file=sys.stderr)
            continue

        df = df[~df.index.duplicated(keep="last")].sort_index()

        # Merge with cache so repeated runs deepen history instead of truncating.
        if use_cache and path.exists():
            try:
                old = pd.read_parquet(path)
                before = len(old)
                df = (pd.concat([old, df])
                        .pipe(lambda d: d[~d.index.duplicated(keep="last")])
                        .sort_index())
                grew = len(df) - before
                note = f"cache {before} -> {len(df)} (+{grew})"
            except Exception:
                note = f"{len(df)} bars (cache unreadable, replaced)"
        else:
            note = f"{len(df)} bars"

        currency = "?"
        try:
            currency = (t.fast_info.get("currency") or "?").upper()
        except Exception:
            pass
        df.attrs["currency"] = currency
        df.attrs["symbol"] = sym

        df.to_parquet(path)
        span = f"{df.index[0].date()} -> {df.index[-1].date()}"
        print(f"  {sym:<16} {currency:<4} {note:<34} {span}")
        out[sym] = df

    return out


def load(symbol: str, interval: str = "1d") -> pd.DataFrame:
    """Read a cached symbol. Raises if it was never fetched — better than
    silently backtesting on an empty frame."""
    path = cache_path(symbol, interval)
    if not path.exists():
        raise FileNotFoundError(f"{symbol} @ {interval} not cached. Run: fetch.py --symbols {symbol} --interval {interval}")
    return pd.read_parquet(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch and cache market data.")
    ap.add_argument("--symbols", nargs="*", default=[],
                    help="Yahoo symbols. TSX uses a .TO suffix, e.g. RY.TO SHOP.TO")
    ap.add_argument("--indexes", action="store_true", help="fetch the four market indexes")
    ap.add_argument("--fx", action="store_true", help="fetch FX pairs")
    ap.add_argument("--interval", default="1d", choices=sorted(MAX_RANGE))
    ap.add_argument("--period", default=None, help="default is the measured maximum for the interval")
    args = ap.parse_args()

    syms = list(args.symbols)
    if args.indexes:
        syms += list(INDEXES.values())
    if args.fx:
        syms += list(FX.values())
    if not syms:
        ap.error("give --symbols, --indexes or --fx")

    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"interval={args.interval} period={args.period or MAX_RANGE[args.interval]} -> {CACHE}")
    got = fetch(syms, interval=args.interval, period=args.period)
    print(f"\n{len(got)}/{len(syms)} symbols cached.")
    return 0 if got else 1


if __name__ == "__main__":
    raise SystemExit(main())
