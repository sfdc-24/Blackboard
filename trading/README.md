# Trading research rig — TSX / NYSE / NASDAQ / DJIA

claude-code-cli, 2026-09-03. Everything below was measured on this machine, not
recalled from memory.

## The boundary, stated once

**I cannot place trades.** Not stocks, not FX, not crypto, not one share, not
with your explicit instruction. That is a fixed limit, not a permission
setting. I also do not give investment advice — I am not licensed and the
distinction matters.

**Everything else is available**, and it is most of what a trading operation
actually is: data, research, backtesting, execution *simulation*, and
paper-trading integration. When something is worth trading, you place the order
— or you point it at a broker's paper account, which is where an untested
algorithm belongs anyway.

## What is here

```
trading/fetch.py      cache market data to local parquet, merging on repeat runs
trading/backtest.py   backtest harness with look-ahead and costs closed by construction
.venv-quant/          isolated env; your global Python was not touched
data/market/          the cache (gitignored — market data is not repo content)
```

```bash
.venv-quant/Scripts/python.exe trading/fetch.py --indexes --fx --symbols RY.TO AAPL
.venv-quant/Scripts/python.exe trading/backtest.py --symbol RY.TO --commission-bps 5 --slippage-bps 5
```

## Verified on 2026-09-03

**Environment.** Python 3.14.7, 4 cores / 8 threads, 15.7 GB RAM, 167 GB free.
`pandas 3.0.5`, `yfinance 1.7.0`, `pyarrow 25.0.1` all installed from native
`cp314` wheels — nothing compiled from source, which was the real risk on a
Python this new.

**All four markets return data, TSX included.** This was the open question and
the answer is yes:

| Market | Symbol | History cached |
|---|---|---|
| TSX Composite | `^GSPTSE` | 1979-06-29 → today (11,845 bars) |
| Dow Jones | `^DJI` | 1992-01-02 → today (8,729) |
| Nasdaq Composite | `^IXIC` | 1971-02-05 → today (14,011) |
| NYSE Composite | `^NYA` | 1965-12-31 → today (15,267) |
| S&P 500 | `^GSPC` | 1927-12-30 → today (24,784) |
| TSX equity | `RY.TO` | 1995-01-12 → today (7,951) — currency CAD |
| US equity | `AAPL` | 1980-12-12 → today (11,523) — currency USD |
| FX | `USDCAD=X` | 2003-09-17 → today (5,973) |

**Intraday depth, measured rather than assumed.** TSX gets identical treatment
to NASDAQ:

| Interval | History available |
|---|---|
| 1m | ~8 days |
| 2m | ~45 days |
| 5m / 15m | ~85 days |
| 1h | ~1,060 days |
| 1d | 10y+ (to inception above) |

**A 1-minute strategy cannot be backtested here.** Eight days is not a sample.
The fix is mechanical: schedule `fetch.py --interval 1m` daily and the cache
merges rather than replaces, so depth accrues from the day you start.

## What I could not determine

**The delay.** Yahoo omits the delay field entirely, and the markets were
closed when this was written — the most recent tick was the previous session's
close. **Assume delayed until measured during a live session.** Nothing
latency-sensitive should be built on it before then.

## What was rejected, and why it matters

**Stooq returned HTTP 200 and was still useless.** The body was an anti-bot
JavaScript challenge, not the CSV it appeared to be. A probe that only checked
status codes would have recommended it. Check the payload, never the status.

## Sources reachable, all needing a key you create

I cannot create accounts or sign up for services — that is yours to do. All of
these answered with 401/403, meaning the host is alive and only wants a key:
**Alpaca**, **Finnhub**, **Polygon**, **Tiingo**, **Twelve Data**. Alpha
Vantage's `demo` key returned real daily equity data, so its free tier works;
the demo key does not cover FX.

## Streaming — the honest position

What is built here **polls** delayed data. That is right for research and wrong
for live trading. On real-time streaming:

- **US real-time, free** is genuinely available — Alpaca's IEX feed and
  Finnhub's free tier both offer websockets. US only.
- **TSX real-time is the hard one.** TMX licenses that data and it is neither
  free nor unlicensed. The practical route is a broker feed — Interactive
  Brokers covers TSX and US and has an API plus a paper account. **I have not
  verified IBKR's current market-data pricing** and will not quote a number I
  have not checked.
- **The Dow is an index, not an exchange**, licensed by S&P Dow Jones Indices.
  You can read `^DJI` for the level; the tradeable proxy is the DIA ETF, which
  streams on ordinary US feeds.

## Why the harness is built the way it is

Homemade backtesters produce beautiful false results in three specific ways, so
all three are closed by construction rather than by discipline:

1. **Look-ahead** — every signal is shifted one bar before it becomes a
   position. There is no flag to disable it.
2. **Free trading** — `commission_bps` and `slippage_bps` are required
   arguments with no defaults, because a zero-cost backtest makes noise look
   like skill.
3. **Survivorship** — this cannot be fixed here; the source has no delisted
   names. So it prints on every run instead.

**The evidence it works:** the bundled MA(50/200) crossover *loses* to
buy-and-hold on both RY.TO (−6.7% CAGR) and AAPL (−7.6%) after 10bps of costs.
That is the correct result for one of the most published rules in existence. A
harness showing it beating the market would have meant a bug.

The crossover is a **test fixture, not a recommendation.**

## Sensible next steps

1. Run `fetch.py --interval 1m` on a daily schedule if intraday is the goal —
   depth only accrues if collection starts.
2. Measure the actual delay during market hours, then decide if the free feed
   is adequate.
3. If it is not: create an Alpaca account (free, includes paper trading and a
   real-time US websocket) and I will wire it in.
4. TSX real-time means a broker relationship. Worth pricing before designing
   anything that assumes it.
