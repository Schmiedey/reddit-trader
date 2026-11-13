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

## Round 7: risk-adjusted check and a random Reddit-name control (2026-11-12)

`uv run python -m bot.backtest.beta_check` regresses each account's daily returns (cloud replay,
Aug 2024 – Nov 2026) on SPY, SPHB, IWM and ARKK. "@beta" = the benchmark held at the account's beta.
| Account | Beta vs SPY | Alpha/yr vs SPY, 27 mo (t) | Alpha/yr, later 15 mo | Account vs SPY @beta, 27 mo |
|---|---|---|---|---|
| Early build-up | 1.19 | −2.6% (−0.2) | −16.5% | +35% vs +49% |
| Most bought | 0.91 | +18.1% (1.2) | −4.2% | +95% vs +37% |
| Both | 1.18 | 0.0% (0.0) | −15.0% | +41% vs +49% |

`uv run python -m bot.backtest.random_control --seeds 10` replays the cloud engine with each day's
real hits moved to the same number of random eligible names (4+ buyers in 5 days, tradable).
| Account | Real, 27 mo | Random avg (range) | Beats random: unseen yr / later / total |
|---|---|---|---|
| Early build-up | +33.4% | +43.2% (+6.5% .. +118.4%) | 5/10, 2/10, 5/10 |
| Most bought | +95.2% | +44.1% (+7.0% .. +100.4%) | 9/10, 8/10, 9/10 |
| Both | +39.6% | +33.2% (+4.5% .. +51.8%) | 8/10, 4/10, 6/10 |

- Early build-up and Both are explained by market beta and by picking Reddit names at all; the rules add nothing.
- Most bought is the only rule that beats random Reddit names in both periods and keeps positive alpha
  over 27 months, but t ≈ 1.2–1.5 and its alpha was negative in the later 15 months. A lead, not proof.
- Random runs alone span +6% to +118%: single-account totals in this setup are mostly luck.
- **Change:** the Both account is replaced by **Random Reddit names**, a live control that buys, each day, as
  many random eligible 4+ buyer names as Most bought fires on (seeded by the date). A rule counts as working
  only if it beats this account. Replay with the new desk: Early build-up +28.5%, Most bought +100.9%,
  Random Reddit names +73.8% (one seed; the 10-seed average was +44%), SPY +40.5%. The existing two
  accounts moved a few points from Round 6 after the price cache refreshed, more evidence of noise.

## Round 8: a stricter buy classifier (2026-11-12)

Reading the posts behind the live signals showed that about two thirds of "buys" were not buys: news
headlines, other people's trades ("Senator bought NVDA"), questions ("Buying opportunity?"), a $115K
puts YOLO, posts about a different ticker, and index-fund advice. `stance.py` now needs the author's own
buy (first person or a short title-style line), skips questions and plans, reads puts anywhere in a
YOLO post, drops a ticker when the title names a different one, and treats "bought and sold" as no
position. On 66 hand-read posts from this week: 16 of 18 real buys kept, false buys 36 → 9. Index and
sector funds (SPY, VOO, QQQ, leveraged ETFs...) no longer generate signals. `STANCE_VERSION = 2` makes every
database, including the live desk, reclassify once.

History: BUY stances 33,324 → 17,135, DD 8,368 → 4,939, SELL 26,295 → 30,411.
Cloud replay, Aug 2024 – Nov 2026 (SPY +40.5%), random account is one seed:
| Account | Old classifier | New classifier | Unseen year | Later 15 mo | Worst drop | Trades |
|---|---|---|---|---|---|---|
| Early build-up | +28.5% | **+46.7%** | +45.7% | +0.7% | −26.8% | 274 |
| Most bought | +100.9% | **+17.6%** | +30.8% | −10.1% | −25.5% | 229 |
| Random Reddit names | +73.8% | **+58.9%** | +66.6% | −4.6% | −23.9% | 288 |

Beta check, new classifier, 27 months vs SPY: Early build-up beta 0.69, alpha +8%/yr (t 0.6); Most bought
beta 0.53, alpha +1%/yr (t 0.1); random beta 0.61, alpha +14%/yr (t 0.9).

- **Most bought's lead was an artifact of the noisy classifier.** Counting news and chatter as buys made
  it a measure of attention; with real buys only, it trails SPY and the random control.
- Neither rule beats random Reddit names. Whatever edge exists comes from holding names Reddit talks about
  in a year when they rallied, not from who is buying.
- The ranking flipped completely on a classifier change. Treat every number in this file as fragile.

## Round 9: does the majority of r/wallstreetbets know what it is doing? (2026-11-13)

The bot had only ever read posts: about 400 a day on WSB against about 30,000 comments. This round reads
the comments. `scripts/backfill_comments.py --stance-only` downloads every WSB comment containing a
buy/sell/hold word (about a quarter of all comments, 5,000–7,700 a weekday); `stance.comment_stance`
keeps only comments where the author states their OWN position (drops questions, what-ifs, price talk
like "AMD is dumping", other people's trades, "put" the verb); `bot.backtest.majority` counts each
author once per stock by their latest stance in 5 trading days and scores the next 5 and 10 days.
Hand check of the comment rules: first version 49 of 100 right; final version 43 of 64 clearly right,
8 unclear, 13 wrong. Every threshold in `majority.py` (10+ authors, majority = over half, 10-day
cooldown) was fixed before any result was seen.

Data: 55 weekdays, Aug 28 – Nov 12 2026 (weekends not downloaded), 15,473 comment stances + 1,067 post
stances from 5,061 authors, 50 days scored, 1,106 stock-days with 10+ authors (about 22 stocks a day).
(The first run used Sep 22 – Nov 12 only; adding Aug 28 – Sep 21 afterwards halved the losses but changed no sign.)

**What the majority does.** It is buying on 57% of stock-days, betting against on 20%, split on 23%. It
buys after drops (the stock's prior 5-day move averages −4.7%) and bets against stocks that have risen (+3.8%).

**Whether it is right.** Returns from the next open, after costs. "vs others" = minus the same day's
average talked-about stock; t is across days.
| Stock-days where... | n | Next 5 days | vs SPY | vs others (t) | Next 10 days | vs SPY | vs others (t) |
|---|---|---|---|---|---|---|---|
| majority buying | 563 | −2.5% | −1.9% | −0.4% (−2.4) | −4.4% | −3.7% | −0.7% (−2.6) |
| majority betting against | 191 | +0.7% | +1.0% | +1.6% (4.2) | +1.1% | +1.1% | +2.3% (4.2) |
| no majority | 226 | −1.9% | −1.5% | −0.4% | −2.2% | −1.9% | −0.1% |
| under 35% buyers | 103 | +1.5% | +1.9% | +2.6% (2.2) | +1.7% | +1.7% | +2.2% |
| 80%+ buyers | 65 | −1.1% | −0.7% | +0.3% | −3.3% | −3.1% | −1.4% |

- Daily rank correlation between how bullish the crowd is and the next return: −0.06 over 5 days (t −1.9),
  −0.14 over 10 days (t −3.7). The same sign as Round 1's post-sentiment result (−0.16).
- It is not only that they buy falling stocks. Next 5 days vs SPY, majority buying / betting against:
  stocks down 5%+ in the prior 5 days −2.0% (n 241) / +3.8% (n 16); stocks that barely moved −1.6% (n 270) /
  +1.2% (n 97); stocks up 5%+ −3.2% (n 52) / +0.2% (n 78).
- As tradable signals (once per stock, 10-day cooldown) the effect mostly disappears. 10 days vs others:
  majority starts buying −0.4% (n 98), flips to buying −0.2% (n 48), majority starts betting against +1.9%
  (n 43, t 1.4), flips to betting against +0.9% (n 27). None is significant.
- **Individuals:** 10,039 graded calls by 3,892 authors averaged −0.9% vs SPY over 5 days; 47% were right.
  Of 865 authors with 3+ calls, 39% were right more often than wrong. Past record predicts nothing: early
  winners later averaged −0.50%, early losers −0.53% (325 authors, rank correlation +0.09). Stocks that
  authors with a good record were mostly buying lost 3.5% over 5 days.

**Decision (rule set in advance):** following the majority does not beat the other talked-about stocks in
either half of the period, so it does not become a live account.

**How much to trust it:** little, in either direction. Eleven weeks, mostly one regime: talked-about stocks
fell 2.4% per 5 days on average (Nasdaq correction, the Nvidia scandal), and a crowd that buys dips loses
in a falling market. Days overlap, so the t-values overstate. The classifier is right about 70–75% of the time.
The one lead is the inverse: stocks the majority bets against held up best. That needs months of
comments including a rising market before it is worth an account. The download is resumable
(`scripts/backfill_comments.py --stance-only --weekdays --newest-first`), about 8 minutes per day.

Also found: the post history's top "tickers" include jargon that matches real symbols (RSI, ET, PR, IP,
NASA, WTF). `comments_probe.JARGON` excludes them for comments; the post pipeline still counts them.

## Round 10: a tournament of everything (2026-11-13)

`uv run python -m bot.backtest.tournament [--max-atr 0.08]`. One table of every (stock, day) with 2+ people
talking about it in 3 days, price $5+, $5M+ traded a day: 55,191 stock-days, 1,742 stocks, Aug 2024 – Nov 2026,
using the same post classification as the live desk. Evening signal, bought at the next open, costs included.
Everything is picked on the OLD days (to Dec 18 2025) and checked on the NEW days (Dec 19 2025 on). The yardstick
is "vs others": the return minus the average talked-about stock bought the same day (random Reddit names).
- 30 named strategies (the live rules, crowd ideas, plain price ideas), holds of 1, 5 and 10 days.
- A mass search: every combination of up to 3 of 52 conditions (23,478 rules) x 7 exits = 164,346 strategies,
  best and worst, repeated 3 times on outcomes shuffled within each day to count what luck alone produces.

**First run, all $5+ stocks.**
| | Real search | Shuffled (luck) |
|---|---|---|
| Buy rules that held on NEW days | 1,646 | 1,869 |
| ...with t >= 2 on NEW days | 165 | 111 |
| Losing rules that held | 22,424 | 1,626 |
| ...with t <= -2 | 9,976 | 100 |
No buy rule beat luck. Losing patterns were everywhere (next 10 days vs SPY, OLD / NEW): volume 5x normal
−9.6% / −15.0%; jumped 15%+ yesterday −7.4% / −12.4%; new name with a 4x attention spike −11.2% / −16.4%;
daily range 8%+ −5.0% / −7.5%; 40%+ below its 60-day high −5.6% / −10.1%.

**A data problem this exposed.** Prices are split-adjusted, so a penny stock that later did a reverse split
shows up as a $20–$140 stock (XTIA, RIME, WETO, LIMN, AIXI...). The losses are real but the live desk would
have rejected those names on their actual price. Every earlier backtest and replay in this file used the same
adjusted prices for its $5 filter, so they all let some of these in. By volatility (10-day hold vs SPY):
range under 3% −0.2%, 3–5% −0.1%, 5–8% +0.2%, 8%+ **−6.0%** (2,684 trades, 36% winners).

**Second run, ordinary stocks only (`--max-atr 0.08`, daily range under 8%).**
- Any talked-about stock: +0.6% / +0.4% per 10 days, **0.0% / −0.1% vs SPY**. Reddit attention itself is worth nothing.
- Named strategies: none passes. Early build-up +0.7% / +0.4% vs others (t 1.0 / 0.8), Most bought +0.6% / −0.8%,
  bullish majority +1.2% / −0.1%, fade the crowd +0.8% / −0.1%, loss posts 3+ +0.9% / +1.8% (72 and 35 trades),
  follow the winners +1.0% / +0.8% (t 1.8 / 0.9). Market uptrend or downtrend made no difference vs SPY.
- Mass search: 3,076 buy rules held vs 382 by luck (165 vs 29 strict), mostly 10- and 20-day holds. They are
  nearly all one thing: **momentum**. "Doubled off its 60-day low": 10-day hold +3.9% vs SPY OLD (345 trades,
  t 2.2), +2.5% NEW (172 trades, t 1.7). It is a price pattern, not a Reddit one, and it is fragile: 53% winners,
  median +1.0%, and without the best 5% of trades the average is +0.1%. The names are this period's AI and space
  winners (NBIS, RKLB, PLTR, SNDK, ASTS, HOOD, MU).

**What it means.** (1) Nothing built on who is buying or selling beats random Reddit names. (2) The one robust
finding is what to avoid: extremely volatile, just-pumped names. (3) Momentum is the only buy-side lead, at t
around 2 in a two-year bull market for those names. (4) The live desk has no volatility cap: its only guards are
price and share volume, so a real $5+ stock in mid-pump can still be bought.

### Round 10b: the volatility cap, checked in the engine itself (2026-11-13)

The tournament holds every trade 10 days with no stop. The live desk has a stop (4 ATR, max 25%, then
breakeven). Replaying the cloud engine with and without a cap of 8% average daily range:
| Account | No cap: total / worst drop | With cap: total / worst drop |
|---|---|---|
| Early build-up | +46.7% / −26.8% | +28.5% / −18.1% |
| Most bought | +17.6% / −25.5% | +7.6% / −21.3% |
| Random Reddit names | +58.9% / −23.9% | +35.7% / −17.6% |

Trade by trade in the uncapped replay, the 8%+ names were not the losers the tournament predicted: Early
build-up 65 trades averaging +3.0% (the rest +1.4%), Random 74 at +1.3% (rest +2.4%), Most bought 48 at 0.0%
(rest +1.5%). Pooled, +1.6% against +1.8%. The stop cuts off the −50% to −90% collapses that drive the
tournament's average, and the winners are real (SOUN +74%, POET +82%, LUNR +79%, RGTI +64%).
So for this desk the cap lowers the swings and does not raise the return. The totals fall by more than the
removed trades explain because freeing a slot changes which other trades happen: the same ±20-point noise
as Round 6. Decision: no cap. The live desk keeps its rules unchanged.

## Round 11: Most bought replaced by a Momentum account (2026-11-13)

Most bought is dropped (Round 8: its edge was a classifier artifact; it trails SPY and random names). The
third account now trades the tournament's one buy-side lead: **Momentum** = 2+ people posting about the stock
in 3 trading days, price at least double its 60-day low, average daily range under 8%. Most-discussed first,
same sizing, stop and 10-day hold as the others. It ignores who is buying. The random control still draws as
many 4+ buyer names as the old Most bought rule would have fired on.

Replay of the cloud engine, Aug 15 2024 – Nov 12 2026 (SPY +40.5%):
| Account | Total | Unseen year | Later 15 mo | Worst drop | Trades | Winners | Avg trade |
|---|---|---|---|---|---|---|---|
| Early build-up | +46.7% | +45.7% | +0.7% | −26.8% | 274 | 51% | +1.8% |
| Momentum | **+286.6%** | +126.4% | +70.8% | −14.4% | 289 | 54% | +6.0% |
| Random Reddit names | +58.9% | +66.6% | −4.6% | −23.9% | 288 | 54% | +2.1% |

**Do not read +287% as what to expect.**
- The rule was chosen from 164,346 strategies on this same data. This replay is the winner's in-sample result.
- Ten trades made 65% of the profit; three made over +100% (IXHL +225%, CAR +187%, PACS +117%). The bars are
  real, but IXHL and CAR were sold days before they collapsed, which is exit-timing luck. Median trade +1.8%;
  without the best 5% of trades the average is +1.9%, about the same as the other accounts.
- Two years of a bull market in exactly these names (NBIS, SNDK, RKLB, MU). No bear market in the sample.
The live paper account and a long price-history test decide whether any of it is real.
