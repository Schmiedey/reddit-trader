# Reddit paper-trading bot

Reads Reddit, X, StockTwits and news; trusts people by how often they've actually beaten
the S&P 500; paper trades swing positions with fake money in a local account.
No API keys. Not investment advice.

## Run it
```bash
uv sync
uv run python -m bot.collect                 # gather posts and news (every 15 min via launchd)
uv run python -m bot.trader.run_daily        # after the US close: fill orders, score, trade
open data/report.html                        # the report, rebuilt after every daily run
bash scripts/install_launchd.sh              # run both automatically in the background
```
`--dry-run` on the daily job scores and prints without queuing orders.

## Data (all keyless)
Reddit, X and StockTwits (see `bot/sources/`), Yahoo Finance + Google News RSS
(feedparser), prices from yfinance, ticker list from nasdaqtrader.com.

## How it decides
> **Current strategy: crowd swing** (`scoring.strategy: crowd_swing`). Backtests on 27 months of Reddit
> history (see [BACKTEST.md](BACKTEST.md)) found that post wording does not predict returns. The live bot
> buys (1) stocks with a cluster of Loss posts and (2) stocks where the number of buyers is doubling
> before the price has moved, then holds 10 trading days with a wide stop. A replay of this exact bot
> made +58.8% vs SPY +40.5% (Aug 2024 – Nov 2026), including +33.5% vs +18.1% on a year the rules had
> never seen; over the most recent 15 months it only tied SPY. Expect drops of 25%+.
> The thresholds below describe the original momentum mode; live values are in config.yaml.
>
> Replay the real bot over history: `uv run python -m bot.backtest.replay_live`.

**Who to trust.** Every bullish/bearish post is a "call", graded 10 trading days later
against SPY. Usually-right authors get up to ~2x weight, usually-wrong ones much less.
Accounts under 30 days old or with near-zero karma are ignored.

**Per-ticker sentiment.** "Dumping AMD, loading NVDA" is bearish AMD and bullish NVDA:
each ticker is scored on the sentences that mention it.

**Score per ticker** = 50% crowd + 30% news + 20% hype, where:
- *Crowd*: one opinion per author (spam adds nothing), recent posts count more (24h half-life),
  long DD posts count up to 2x and meme/YOLO flairs 0.3x. A split crowd is discounted and a
  handful of voices is shrunk toward neutral.
- *News*: recency-weighted headline sentiment, duplicates across feeds removed.
- *Hype*: a mention spike from trusted authors helps; a spike from untested or low-credibility
  accounts **hurts**, because retail attention spikes tend to reverse. Off for the first week.

**Before buying** (score ≥ 0.25, at least 5 voices, 2+ sources agree), skip if:
- it looks like a pump: under $5, under 1M shares/day, or pushed in r/pennystocks
- it's already up 25%+ in 5 days (chasing)
- it's below its 20-day average (downtrend)
- it moves 8%+ a day on average (too volatile)
- the market is weak (SPY under its 50-day average): then the bar rises to 0.35 and only 1 buy a day

**Risk.** Stop 2 average daily ranges below entry (4–12%), target at 2x that distance.
Each trade is sized so hitting the stop loses about 1% of the account (max 5% per position,
10 positions, 3 new a day). Once a trade is up by its risk, the stop moves to breakeven.
Sell after 10 trading days or if sentiment turns (score ≤ -0.15).

**Paper fills.** Orders fill at the next day's real opening price (+0.05% slippage);
stops and targets trigger on each day's high/low, and gaps fill at the open.

## Is it working?
The report's **signal scorecard** grades every past signal against SPY over the following
10 trading days, by score group. After 3–4 weeks, the "Buy zone" group should beat
"Neutral". If it doesn't, the logic isn't adding anything. All thresholds live in `config.yaml`.

## Side test: copying public crypto wallets (paper only)
`bot/copytrade` mirrors the perp positions of public Hyperliquid wallets in $10k paper accounts,
every 30 minutes, with fees and slippage. It places no orders. "picks" are wallets that passed a
strict track-record filter; "control" are random wallets. The picks have to beat the control.

    .venv/bin/python -m bot.copytrade.run --report

Background and method: `research/copytrade/FINDINGS.md`. Wallet list: `research/copytrade/wallets.json`
(rebuild with `research/copytrade/select_live.py`). State: `data/copytrade.db`.
