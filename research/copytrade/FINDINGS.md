# Can you copy the best public wallets? (Hyperliquid test, 2026-11-12)

Data: 100% public, no API key. Hyperliquid leaderboard (48k wallets), `portfolio` PnL histories for
1,673 wallets (random sample, mostly >$20M lifetime volume), `userFillsByTime` fills and 15m candles.
Why Hyperliquid: every perp trade is on-chain, liquid majors (BTC/ETH/SOL) so a copier can actually get
filled, and there is no MEV front-running like on DEX memecoins.

## Test 1: do past winners keep winning? (`persistence.py`)
Rank wallets on 6 months, check the next 3 months. Wallets with >= $1k capital that were active.

| next period | top 50 by past PnL: profitable next | all wallets: profitable next | quality filter* |
|---|---|---|---|
| Feb–May 2026 | 30% | 33% | 28% |
| May–Aug 2026 | 58% | 35% | 61% |
| Aug–Nov 2026 | 58% | 34% | 86% (n=14) |

*quality = profitable, >= 60% winning 2-week periods, drawdown < 50% of profit.

Rank correlation between past and next performance: -0.01 to +0.20. That's weak, and in one of three
periods the past winners did *worse* than the average wallet.

## Test 2: actually copy them with a delay (`copysim.py`)
Pick 25 wallets per group using only data before 2026-09-22, then replay their real fills from
2026-09-22 to 2026-11-12 at the price 0 min / 15 min / 1 h / 4 h later, with 0.045% fee + 0.05% slippage per side.

| group | n | their ROI | copy, no delay | copy, 1h late | copy, 4h late | share profitable (1h) |
|---|---|---|---|---|---|---|
| top past PnL | 18 | +39% | +33% | +29% | +22% | 33% |
| quality filter | 24 | +47% | +41% | +38% | +34% | 42% |
| random wallets | 18 | +42% | +39% | +41% | +40% | 56% |

(Mean ROI with each wallet floored at -100%. The means are driven by 3–4 wallets that made +200% to +500%.)

## Conclusions
1. **Picking the "best" wallets did not beat picking random wallets.** In the copy window, randomly
   chosen wallets were profitable more often (56%) than the top-PnL picks (33%).
2. **The returns are lottery-shaped.** The same "quality" group held both +537% and −332% wallets.
   Nothing in their earlier history told them apart.
3. **The delay is not the main problem for slower traders.** Copying 1–4 h late cost roughly 5–13
   points of ROI over 51 days. The problem is *which* wallets to copy, not copying them fast enough.
4. **Results depend on the market.** Aug–Nov was a falling market, and wallets already holding shorts made most of the money.
   That's market direction, not a repeatable skill.

## Caveats
- PnL histories are sampled about every 1–2 weeks, so drawdown and Sharpe numbers are rough.
- One 51-day copy window. Funding and liquidations are not modelled. ROI uses the wallet's typical account value.
- The leaderboard snapshot may under-include wallets that blew up long ago, which would make results look better, not worse.

## Rerun
```
.venv/bin/python research/copytrade/fetch_portfolios.py 1500 2e7   # cached, resumable
.venv/bin/python research/copytrade/persistence.py
.venv/bin/python research/copytrade/select_groups.py
.venv/bin/python research/copytrade/copysim.py research/copytrade/cache/groups.json research/copytrade/cache/copy_results.json
.venv/bin/python research/copytrade/summarize_copy.py
```

## Live forward test (started 2026-11-12)
`bot/copytrade` now paper-copies 52 "picks" and 30 random "control" wallets every 30 minutes
(launchd job `com.redditbot.copytrade`). Picks come from 3,440 wallet histories: perp-profitable in
each of the last three 3-month periods, drawdown never above typical account value, 0.2–200 fills/day.
Each paper account starts at $10k, mirrors the wallet's leverage (capped at 5x total), and pays
0.095% per side. Read it after 4–8 weeks with `python -m bot.copytrade.run --report`: picks must beat control.

Limits: only runs while the Mac is awake (positions are held unchanged through gaps); funding payments,
spot holdings and third-party perp markets (e.g. `xyz:` stocks/metals) are not copied.

## Backtest of the live copier (2026-11-12)
`backtest_live.py` replays `bot/copytrade/core.py` exactly (30-min checks, leverage mirrored and capped
at 5x, 0.095% per side) over 2026-09-22 .. 2026-11-12. Wallet positions are rebuilt from public
fills plus TWAP slice fills (the normal fills endpoint leaves TWAP slices out); 93% of coins matched
the exchange's own recorded position. BTC fell 15.7% in the window.

| group | n | mean | median | profitable | worst / best |
|---|---|---|---|---|---|
| **picks chosen as of Sep 22** (same rule, no lookahead) | 33 | **−10.1%** | −1.6% | 33% | −87% / +42% |
| random control as of Sep 22 | 28 | −2.5% | 0.0% | 32% | |
| current picks (chosen using this same period) | 48 | −0.5% | 0.0% | 42% | −62% / +95% |
| current random control | 26 | −11.3% | −3.3% | 31% | |

- **The honest test is the first pair:** the rule applied on Sep 22 picked wallets that did 7.5 points
  *worse* than random (90% bootstrap range −17.8% .. +2.2%). The picks' equal-weight portfolio fell
  steadily (−1, −2, −6, −7, −8% by week). The control ended near −1%.
- The current picks only beat their control because the rule saw Aug–Nov results when choosing
  them. That is lookahead, and even with it they made −0.5% after costs.
- So the live forward test is expected to fail. It's still worth running, because 51 days in one
  falling market is a small sample.
