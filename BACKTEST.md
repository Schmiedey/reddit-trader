# Backtest results (2026-11-12)

## What was tested
- **Data:** archived Reddit posts (titles + bodies, not comments) from r/wallstreetbets, stocks, investing,
  StockMarket, options and pennystocks (pump-watch only), Jul 1 – Nov 8 2026: 30 sub-months, ~20k ticker
  mentions. Source: the keyless Arctic Shift archive (`scripts/backfill_reddit.py`).
- **Not in history:** X, StockTwits, news. The backtest validates the Reddit part only.
- **Replay:** each trading day at 16:30 ET, the live `compute_scores()` runs on posts made before that
  moment, with author credibility built only from calls whose 10-day outcome was already known.
  Trades use the live rules and fill rules (next open + 0.05% slippage, stops on the daily high/low).
- **No peeking:** the search picks settings on the older 60% of days and reports the newer 40%.
  Controls: SPY, the same rules buying *random* names, and the inverted (contrarian) score.

## Findings
1. **Bullish Reddit sentiment predicted underperformance.** In the test period the most-bullish fifth of
   tickers trailed SPY by 2.6% over the next 5 days. The 10-day rank correlation was −0.16 (t ≈ −2).
2. Strategy families over a grid of 36 settings each, whole period (SPY −2.4%):

   | Strategy | Avg return | Settings positive in both halves |
   |---|---|---|
   | Momentum: buy what the crowd likes (the original bot) | −11% | 0 / 36 |
   | Random tradable names, same rules | −15% | 0 / 36 |
   | Attention: buy sudden mention spikes | lost out of sample | — |
   | **Contrarian: buy what the crowd is bearish on** | **≈ 0% (beats SPY by ~2.5 pts)** | 3–6 / 36 |

3. Author credibility made little difference (4 months is too short to build track records).
4. **Chosen config (now live):** contrarian, score = −(0.7·crowd − 0.3·buzz), buy at ≥ 0.10 with
   ≥ 5 voices, no trend/chase vetoes, pump guard on, hold up to 10 days, 2×ATR stop, 2:1 target,
   2% risk per trade, max 10% per position.
   Replay: **+2.2%** (credibility on) / **+3.7%** (off) vs SPY −2.4%, max drawdown −2.6%, 13–15 trades.
   The same rules following momentum: −7.9%.

## How much to trust it
Not much yet. Four months, ~15 trades, mostly a falling market. The edge is small, and the second
half was flat (−0.2% vs SPY −3.3%). That justifies **paper** trading the contrarian setup and
watching the live scorecard. It does not prove a profitable strategy. Known gaps: posts only (no
comments), today's ticker list (delisted names missing), no news/X/StockTwits history.
The previous config is saved at `data/config.before-backtest.yaml`.

## Rerun / extend
```bash
uv run python scripts/backfill_reddit.py --start 2025-01-01 --end 2026-11-09   # resumable, slow archive
uv run python -m bot.backtest.build --variant base --since 2025-01           # replay daily signals
uv run python -m bot.backtest.build --variant nocred --no-cred --since 2025-01
uv run python -m bot.backtest.sweep --variant base --n 400                   # search + out-of-sample check
```
Results land in `data/backtest/sweep_<variant>.json`. Switch strategy in `config.yaml`
(`scoring.strategy: momentum | contrarian | attention`).

---

# Round 2: WSB-style swing, day-trade and penny-stock research (2026-11-12)

Data: ~15 months of posts (Aug 2025 – Nov 2026; r/wallstreetbets missing Oct 2025), 1,800 tickers,
including stocks under $5 and r/pennystocks. Costs: 0.1% per side, 0.5% per side under $5.
New code: `bot/signals/stance.py` (who bought / held / sold / won / lost), `bot/backtest/research.py`
(9 crowd methods), `bot/backtest/explore.py` (base rates + rule mining with a holdout and a luck
check), `bot/backtest/candidates.py` (fixed rules re-tested on months never mined).

## What did not work
| Idea | Result |
|---|---|
| Most bought, early build-up, YOLO clusters, fresh DD, proven buyers, buying + breakout | +0.5% to −1.2% per 5-day trade vs SPY; none significant. Early build-up looked good at first (+2.1%) and fell to +0.7% with more months (negative Jan–Apr 2026) |
| Follow people who posted gains before | Their next buys lost 1.8% in 5 days (42% winners) |
| Day trades (buy the open, sell the close) | Negative in every group (−0.2% to −1.9%): crowd names open high and fade |
| Overnight holds | About zero after costs |
| Rule mining, ~10,000 combinations × 5 exit styles | Survivors on new data barely above the shuffled-luck count; the best ("bounce after a 10% drop", +2.5%) fell to +0.6% on never-mined months |
| Penny stocks (under $5) | 42–49% touch +10% within 5 days and 17–25% touch +25%, but the median trade ends −2% to −7% and the average is about zero. 0 of 510 mined rules held on new data |
| Crowd mood as a market timer | No effect on SPY |

## The one signal with potential: buy what the crowd is posting losses on
Loss-flair posts on a stock in the last 5 days, losses ≥ 65% of gain+loss posts, price ≥ $5:
| Loss posts | Signals | 5-day return | 10-day return | Winners (10d) |
|---|---|---|---|---|
| 2+ | 145 | +0.7% | +2.8% | 55% |
| 3+ | 44 | +2.8% | +5.6% | 61% |
| 3+ and stock down 10%+ in 5 days | 26 | +4.3% | +8.5% | 55% |
| 5+ | 12 | +5.8% | +18.6% | 67% |
More loss posts → bigger bounce, which is what a real effect looks like. But it is ~3 trades a month
and 44 signals; the t-statistic is about 1.3, so it is a lead, not proof.

## Why "10%+ a trade" looks common on Reddit
Gain posts outnumber Loss posts 3.4 to 1 (5,364 vs 1,582) even though the average crowd buy trails
SPY. People post wins and stay quiet about losses.

---

# Round 3: full account simulation, and what runs live (2026-11-12)

`uv run python -m bot.backtest.final` simulates a $100k account with the bot's own rules (buy next
open, costs, 10% per trade, max 10 positions, pump guard) and compares each method with SPY and
with 10 runs of random picks on the same days. Two windows, to see whether a ranking survives.
The loss-post table in Round 2 came from an earlier, smaller data snapshot; these numbers use all
the data downloaded by the end of the day.

**Stops matter more than the method.** With the old 2-ATR stop and 2:1 target every method lost or
badly trailed its own wide-stop version. Volatile Reddit stocks hit tight stops and then recover.
All results below use a wide stop (4 ATR, max 25%), no profit target, sell after 10 trading days.

| Method (10-day hold, wide stop) | Aug 2025 – Nov 2026 (SPY +19.1%) | beats random | Nov 2025 – Nov 2026 (SPY +14.0%) | beats random | Worst drop |
|---|---|---|---|---|---|
| **Loss bounce + early build-up (live)** | **+20.5%** | 6/10 | **+28.9%** | 9/10 | −22% |
| Loss bounce + most bought | +28.8% | 8/10 | +8.2% | 7/10 | −19% |
| Most bought | +29.8% | 10/10 | +9.0% | 7/10 | −17% |
| Early build-up | +10.2% | 6/10 | +22.2% | 7/10 | −24% |
| Loss bounce, 3+ loss posts | +9.2% | 8/10 | +6.3% | 6/10 | −7% |
| Loss bounce, 3+ and down 10% in 5 days | +11.4% | 10/10 | +10.1% | 10/10 | −5% |

- The live mix is the only method that beat SPY in both windows. Its margin in the longer window is
  small (1.4 points) and it beat only 6 of 10 random runs there, so this is not proof.
- "Loss bounce, 3+ and down 10%" is the most consistent: it beat every random run in both windows,
  averaged +4.2% to +4.5% a trade, and never dropped more than 5%. It trails SPY only because it
  trades about 1.5 times a month and sits in cash. At 20% per trade it made +29.9% / +25.9%.
- "Most bought" depends on Aug–Oct 2025; without those months it trails SPY.
- Rankings moved when more posts were downloaded. Treat every number here as ±10 points.

## Round 4: the capitulation signal, and a replay of the real bot

Loss posts work best when the stock has also just fallen hard (10-day return after the signal):
| Setup | Signals | Average | Winners | Trades of +10% or more | 1st half / 2nd half |
|---|---|---|---|---|---|
| 3+ loss posts, not down 10% | 41 | +1.2% | 50% | 13% | +3.7% / −1.3% |
| 3+ loss posts, down 10%+ in 5 days | 26 | +3.8% | 57% | 22% | +3.5% / +4.0% |
| 2+ loss posts, down 15%+ | 47 | +4.1% | 56% | 27% | +5.2% / +3.0% |
| 2+ loss posts, down 20%+ | 32 | +6.0% | 59% | 33% | +4.8% / +7.1% |
| 3+ loss posts, down 20%+ | 11 | +8.0% | 67% | 33% | +2.7% / +12.2% |

**Capitulation** = (3+ loss posts and down 10%+) or (2+ loss posts and down 15%+), losses ≥ 65% of
gain+loss posts. Account simulation, 10-day hold, wide stop:
| | Aug 2025 – Nov 2026 (SPY +19.1%) | Nov 2025 – Nov 2026 (SPY +14.0%) | Worst drop | Beat random |
|---|---|---|---|---|
| Normal size (10% per trade) | +18.2% | +17.0% | −7% | 9/10 and 10/10 |
| **Double size (20% per trade), live** | **+33.3%** | **+30.2%** | −13% | — |
| Double size + early build-up | +30.6% | +34.2% | −24% | — |

**Replay of the real bot.** `uv run python -m bot.backtest.replay_live` runs the live daily job
(`bot.trader.run_daily`, unchanged) once per trading day on a copy of the history, with prices cut
off at each day. Result for Aug 14 2025 – Nov 12 2026: account **+36.6%** vs SPY +19.1%, worst drop
−12.6%, 46 closed trades (52% winners, +3.7% average, 12 of +10% or more, 4 of −10% or worse),
10 of 16 months positive, average month +2.1%, worst month −6.5%. This matches the simulator.

Honest limits: 46 trades. One month (Aug 2026, +20.2%) is more than half the total gain; without it
the account trails SPY. The setup and its thresholds were chosen after looking at this data, and the
average winner is +3.7%, not 10%. It needs live paper results before anyone should trust it.

## Round 5: a year the rules had never seen (Aug 2024 – Aug 2025)

After Round 4, I downloaded r/wallstreetbets for Aug 2024 – Jul 2025 (posts only; the other
subreddits were not downloaded for this year) and ran the rules exactly as they were.
SPY +18.1% over this year.

| Method (10-day hold, wide stop) | Unseen year | Beat random | Aug 2025 – Nov 2026 | Nov 2025 – Nov 2026 |
|---|---|---|---|---|
| Capitulation, double size (live in Round 4) | **−20.8%** | 1/10 | +33.3% | +30.2% |
| Capitulation, normal size | −10.6% | 1/10 | +18.2% | +17.0% |
| **Loss bounce 3+ and early build-up** | **+68.9%** | 10/10 | +20.5% | +28.9% |
| Early build-up alone | +68.3% | 10/10 | +10.2% | +22.2% |
| Most bought | +32.2% | 9/10 | +29.8% | +9.0% |
| Loss bounce 3+ alone | −6.7% | 4/10 | +9.2% | +6.3% |

**Capitulation was overfit.** It was tuned on Aug 2025 – Nov 2026 and lost money on the unseen year.
**Loss bounce + early build-up** had its thresholds fixed in Round 2, before this year was
downloaded, and beat SPY in all three windows. It is now the live rule.

**Replay of the real bot, Aug 15 2024 – Nov 12 2026** (`replay_live`, new config):
| | Account | SPY |
|---|---|---|
| Unseen year (to Aug 13 2025) | $100k → $150.3k, **+50.3%** | +18.1% |
| Aug 2025 – Nov 2026 | **+25.3%** | +19.0% |
| **Whole 27 months** | **+88.3%** | +40.5% |
Worst drop −26.2%. 433 closed trades, 52% winners, +1.8% average; 79 trades made 10%+ and 63 lost
10%+. 18 of 28 months positive, average month +2.5%, best +18.2%, worst −8.3%.
Early build-up did the work (360 trades, +2.1% average); loss bounce added little (73, +0.4%).

Limits: the unseen year had a strong rally in volatile stocks after April 2025, which suits buying
names the crowd piles into. That year was scored on WSB posts only. Expect drops of 25%+ and losing
streaks of several months. It still needs live paper results.

## Round 5b: the unseen year with all subreddits (2026-11-12)

Round 5 scored Aug 2024 – Jul 2025 on r/wallstreetbets posts only. I then downloaded stocks,
StockMarket, investing, options and pennystocks for the same months (all 60 sub-months, 78k extra
mentions) and reran everything unchanged. SPY +18.1% over the year.

| Method (10-day hold, wide stop) | Unseen year, WSB only | Unseen year, all subreddits | Beat random |
|---|---|---|---|
| **Loss bounce 3+ and early build-up (live)** | +68.9% | **+51.7%** | 10/10 |
| Early build-up alone | +68.3% | +56.0% | 10/10 |
| Most bought | +32.2% | +67.6% | 10/10 |
| Loss bounce 3+ alone | −6.7% | −6.7% | 1/10 |
| Capitulation (retired) | −10.6% | −10.6% | 1/10 |

**Replay of the real bot, Aug 15 2024 – Nov 12 2026, all subreddits:**
| | Account | SPY |
|---|---|---|
| Unseen year (to Aug 13 2025) | $100k → $133.5k, **+33.5%** | +18.1% |
| Aug 2025 – Nov 2026 | **+19.0%** | +19.0% |
| **Whole 27 months** | **+58.8%** | +40.5% |
Worst drop −27.8%. 450 closed trades, 51% winners, +1.2% average; 73 made 10%+, 65 lost 10%+.
18 of 28 months positive, average month +1.9%, worst −9.4%.

What this changes: the edge is real enough to keep testing but smaller than the WSB-only replay
(+88.3%) suggested, and in the most recent 15 months it only tied SPY. All the gain comes from
**early build-up** (378 trades, +1.4% average); **loss bounce lost money on its own in the unseen
year and averaged −0.1% in the replay.** I did not change the live rules after seeing this, since
retuning on the same data is what broke the capitulation rule. The live paper results decide next.

## Round 6: the cloud desk, three accounts side by side (2026-11-12)

Loss bounce is dropped (it lost on the unseen year). The Render service (`bot/cloud`) runs three
$100k paper accounts: **Early build-up**, **Most bought** (5+ different buyers in 5 trading days, more
buyers than sellers) and **Both**. A signal is bought only on the first day it fires, then ignored
for 10 trading days, which is how the Round 3-5 simulations counted signals.

Replay of the cloud engine itself (`uv run python -m bot.cloud.replay`), Aug 15 2024 – Nov 12 2026:
| Account | Aug 2024 – Aug 2025 (SPY +18.1%) | Aug 2025 – Nov 2026 (SPY +19.0%) | All 27 months (SPY +40.5%) | Worst drop | Trades | Winners | Avg trade |
|---|---|---|---|---|---|---|---|
| Early build-up | +36.3% | −2.1% | +33.4% | −31.1% | 429 | 52% | +0.8% |
| Most bought | +75.5% | +11.2% | +95.2% | −24.5% | 388 | 55% | +2.1% |
| Both | +40.2% | −0.4% | +39.6% | −30.9% | 455 | 49% | +1.0% |

- All three beat SPY in the first year. **None beat SPY in the most recent 15 months.**
- The totals move a lot with small implementation details. Buying a signal every day it stays on,
  instead of only the first day, changed Most bought from +95% to +64% and Both from +40% to +79%.
  With ~400 trades averaging +1 to +2%, differences of ±20 points are noise. The live paper test,
  not this table, decides which rule is kept.
- **Idle cash in SPY** (`cloud.park_idle_cash_in_spy`): totals +45.2% / +99.9% / +56.3%, so +5 to +17
  points, but worst drops of −35.7% / −33.3% / −32.0% and Most bought did worse in the first year.
  Left **off** so the accounts measure the Reddit signals alone; SPY is the yardstick on the dashboard.
- Live and backtest differ in one known way: moderators later remove about a third of WSB posts and
  most r/stocks posts. The history only has those posts' titles. The live desk re-reads the last
  48 hours each evening and drops removed text, which narrows but does not close that gap.

## The earlier local bot (`scoring.strategy: crowd_swing`, `bot/signals/crowd.py`), replaced by the cloud desk
1. **Loss bounce (first priority):** 3+ Loss-flair posts on a stock in 5 trading days, losses ≥ 65% of
   its gain+loss posts (`scoring.loss_rule: bounce`).
2. **Early build-up:** 4+ distinct buyers in 5 trading days, at least double the 5 days before, price
   up less than 10% in 5 days (`scoring.early_build_up: true`).
Posts only (no comments), pump-watch subreddits ignored, price ≥ $5 and ≥ 1M shares a day.
10% of the account per trade (less when the stop is wide), max 10 positions, 3 new buys a day,
stop 4 ATR (max 25%), no profit target, sell at the open after 10 trading days.
`scripts/seed_reddit_history.py` loads recent archived posts into the live database.
Replay any period with `uv run python -m bot.backtest.replay_live --start YYYY-MM-DD --end YYYY-MM-DD`.
