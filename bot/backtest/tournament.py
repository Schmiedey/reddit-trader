"""Tournament: which strategies on Reddit-talked-about stocks actually work?

One table of every (stock, day) the crowd is talking about, with what was known before the buy and what
happened after (bought at the next open, costs included). Three parts:

1. NAMED strategies (the live rules, crowd ideas, plain price ideas), each scored on the older 60% of days
   and again on the newer 40%.
2. MARKET regime: does it matter whether the market is in an uptrend?
3. MASS SEARCH: every combination of up to 3 conditions (about 20,000) x 7 ways to exit. Picked on the
   older days, checked on the newer days, and compared with the same search on shuffled outcomes, which
   shows how many "winners" luck alone produces.

A strategy is judged by "vs others": its return minus the average talked-about stock bought the same day.
That is the bar nothing has cleared yet (random Reddit names). "vs SPY" is shown too.

    uv run python -m bot.backtest.tournament [--rebuild] [--shuffles 3]
"""
import argparse
import itertools
import json
import math
import pickle
from collections import defaultdict

import numpy as np

from ..config import DATA
from .explore import atoms as base_atoms
from .explore import fresh_idx, run

ROWS = DATA / "backtest" / "tournament_rows.pkl"
# outcome column -> (SPY column for the same holding period, cooldown days, bar a mined rule must clear vs others)
COLS = {"day": ("spy1", 1, 0.004), "r3": ("spy3", 3, 0.008), "r5": ("spy5", 5, 0.010), "r10": ("spy10", 10, 0.015),
        "r20": ("spy20", 20, 0.020), "x15_7_5": ("spy5", 5, 0.010), "x30_10_10": ("spy10", 10, 0.015)}
LABEL = {"day": "day trade (open to close)", "r3": "hold 3 days", "r5": "hold 5 days", "r10": "hold 10 days",
         "r20": "hold 20 days", "x15_7_5": "+15% target / -7% stop / 5 days", "x30_10_10": "+30% target / -10% stop / 10 days"}


def more_atoms(A):
    v = A["b5"] + A["s5"]
    return {
        "5+ buyers (5d)": A["b5"] >= 5, "buyers doubled (5d)": (A["b5"] >= 4) & (A["b5"] >= 2 * np.maximum(A["bp5"], 1)),
        "buyers lead (5d)": A["b5"] > A["s5"], "sellers lead (5d)": A["s5"] > A["b5"],
        "bullish majority (65%+ of 5+)": (v >= 5) & (A["b5"] >= 0.65 * v),
        "bearish majority (65%+ of 5+)": (v >= 5) & (A["s5"] >= 0.65 * v),
        "holders 2+": A["h5"] >= 2, "loss posts 3+": A["L5"] >= 3, "gain posts 4+": A["W5"] >= 4,
        "down 20%+ in 20d": A["ret20"] <= -0.20, "up 5%+ in 5d": A["ret5"] >= 0.05,
        "market uptrend": A["spy_tr"] > 0, "market downtrend": A["spy_tr"] <= 0,
        "market fell 2%+ in 5d": A["spy_r5"] <= -0.02, "market rose 2%+ in 5d": A["spy_r5"] >= 0.02,
    }


def named(A, at):
    v = A["b5"] + A["s5"]
    return {
        "Any talked-about stock (control)": np.ones(len(A["i"]), bool),
        "Early build-up (live rule)": at["buyers doubled (5d)"] & (A["ret5"] < 0.10),
        "Most bought (live rule)": at["5+ buyers (5d)"] & at["buyers lead (5d)"],
        "Bullish majority, 65%+ buyers": at["bullish majority (65%+ of 5+)"],
        "Fade the crowd: bearish majority": at["bearish majority (65%+ of 5+)"],
        "Sellers outnumber buyers": at["sellers lead (5d)"] & (v >= 3),
        "Attention spike: 4x, new name": at["attention 4x"] & at["new name (quiet 20d)"],
        "Attention doubled": at["attention 2x"],
        "Crowded: 12+ people talking": at["12+ people talking"],
        "Loss posts, 2+": at["loss posts 2+"],
        "Loss posts, 3+": at["loss posts 3+"],
        "Capitulation: loss posts + down 10%": at["loss posts 2+"] & at["down 10%+ in 5d"],
        "Follow the winners: 2+ gain posts": at["gain posts 2+"],
        "DD post with 50+ upvotes": at["DD 50+ upvotes"],
        "YOLO cluster, 2+": at["YOLO 2+"],
        "Momentum: near its 60-day high": at["near 60d high"],
        "Momentum: up 30%+ in 20 days": at["up 30%+ in 20d"],
        "Momentum: up 20%+ in 5 days": at["up 20%+ in 5d"],
        "Dip: down 10%+ in 5 days": at["down 10%+ in 5d"],
        "Dip: down 20%+ in 20 days": at["down 20%+ in 20d"],
        "Dip: 40%+ below its 60-day high": at["40%+ off 60d high"],
        "Jumped 15%+ yesterday": at["up 15%+ yesterday"],
        "Fell 5%+ yesterday": at["down 5%+ yesterday"],
        "Volume 5x normal": at["volume 5x"],
        "Gaps up 3%+ at the open": at["gaps up 3%+"],
        "Gaps down 3%+ at the open": at["gaps down 3%+"],
        "Calm stocks (daily range under 3%)": at["calm (ATR <3%)"],
        "Very volatile stocks (range 8%+)": at["very volatile (ATR 8%+)"],
        "Large stocks ($200M+ traded a day)": at["big volume ($200M+)"],
        "Small stocks ($2-20M a day)": at["small volume ($2-20M)"],
    }


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0


class Table:
    def __init__(self, A):
        self.A = A
        self.days = np.unique(A["i"])
        self.cut = self.days[int(len(self.days) * 0.6)]
        self.train, self.test = A["i"] < self.cut, A["i"] >= self.cut
        self.ex = {}  # column -> return minus the same day's average talked-about stock
        for col in COLS:
            y = A[col]
            ok = ~np.isnan(y)
            s, c = defaultdict(float), defaultdict(int)
            for i, v in zip(A["i"][ok], y[ok]):
                s[i] += v
                c[i] += 1
            self.ex[col] = y - np.array([s[i] / c[i] if c[i] else np.nan for i in A["i"]])

    def score(self, mask, col, part=None, ex=None):
        """Stats for a rule, one entry per stock per cooldown."""
        A, (spy, cd, _) = self.A, COLS[col]
        idx = fresh_idx(A, mask if part is None else mask & part, cd)
        e = (self.ex[col] if ex is None else ex)[idx]
        ok = ~np.isnan(e)
        idx, e = idx[ok], e[ok]
        if len(e) < 5:
            return {"n": int(len(e))}
        y = A[col][idx]
        by = defaultdict(list)
        for d, v in zip(A["i"][idx], e):
            by[d].append(v)
        bm = defaultdict(list)
        for m, v in zip(A["month"][idx], e):
            bm[m].append(v)
        mm = [np.mean(v) for v in bm.values() if len(v) >= 3]
        return {"n": int(len(e)), "ret": float(y.mean()), "vs_spy": float(np.nanmean(y - A[spy][idx])), "vs_others": float(e.mean()),
                "win": float((y > 0).mean()), "t": tstat([np.mean(v) for v in by.values()]),
                "months": f"{sum(x > 0 for x in mm)}/{len(mm)}", "per_month": round(len(e) / max(len(bm), 1), 1)}


def mass_search(T, at, shuffles, seed=0):
    """Every 1-3 condition rule x every exit, best and worst. Returns per-column results and luck counts."""
    A = T.A
    names = list(at)
    M = np.array([at[n] for n in names])                      # atoms x rows
    rng = np.random.default_rng(seed)
    cols = list(COLS)
    variants = []                                             # (column, shuffle number, excess array)
    for col in cols:
        variants.append((col, 0, T.ex[col]))
        for k in range(1, shuffles + 1):
            y = T.ex[col].copy()
            for d in T.days:                                  # shuffle outcomes among the stocks of the same day
                j = np.flatnonzero(A["i"] == d)
                y[j] = y[rng.permutation(j)]
            variants.append((col, k, y))
    tr = T.train
    Y = np.array([np.where(tr & ~np.isnan(y), y, 0.0) for _, _, y in variants])      # train sums
    N = np.array([(tr & ~np.isnan(T.ex[c])).astype(float) for c in cols])             # train counts per column
    thr = np.array([COLS[c][2] for c, _, _ in variants])
    col_of = np.array([cols.index(c) for c, _, _ in variants])
    found = defaultdict(list)                                 # (column, shuffle, side) -> [(combo, train mean)]
    combos = [c for k in (1, 2, 3) for c in itertools.combinations(range(len(names)), k)]
    for combo in combos:
        m = M[combo[0]]
        for c in combo[1:]:
            m = m & M[c]
        mf = m.astype(float)
        cnt = N @ mf
        if cnt.max() < 40:
            continue
        mean = (Y @ mf) / np.maximum(cnt[col_of], 1)
        for v in np.flatnonzero((cnt[col_of] >= 40) & (np.abs(mean) >= thr)):
            found[(variants[v][0], variants[v][1], "best" if mean[v] > 0 else "worst")].append((combo, float(mean[v]), v))
    out = {}
    for (col, k, side), lst in found.items():
        sign = 1 if side == "best" else -1
        bar = COLS[col][2]
        held, strict, keep = 0, 0, []
        for combo, _, v in lst:
            m = M[combo[0]]
            for c in combo[1:]:
                m = m & M[c]
            y = variants[v][2]
            s_tr = T.score(m, col, T.train, ex=y)
            if s_tr["n"] < 25 or sign * s_tr["vs_others"] < bar:
                continue
            s_te = T.score(m, col, T.test, ex=y)
            ok = s_te["n"] >= 20 and sign * s_te["vs_others"] >= bar / 2
            held += ok
            strict += ok and sign * s_te["t"] >= 2
            if k == 0 and ok:
                keep.append((combo, s_tr, s_te, m))
        out[(col, k, side)] = {"candidates": len(lst), "held": int(held), "strict": int(strict), "keep": keep}
    return out, names, len(combos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--shuffles", type=int, default=3)
    ap.add_argument("--max-atr", type=float, default=0, help="drop stocks whose average daily range is at or above this (0.08 = 8%%)")
    a = ap.parse_args()
    if a.rebuild or not ROWS.exists():
        A = run(None, morning=False)  # evening signals, bought at the next open, as the live desk does
        ROWS.write_bytes(pickle.dumps(A))
    A = pickle.loads(ROWS.read_bytes())
    keep = (A["price"] >= 5) & (A["dv20"] >= 5e6)  # what the live desk may trade
    if a.max_atr:
        # Prices are split-adjusted, so a penny stock that later did a reverse split looks like a $20+ stock.
        # Those pump-and-dump names are the extremely volatile ones; dropping them leaves ordinary stocks.
        keep &= A["atr"] < a.max_atr
    A = {k: v[keep] for k, v in A.items()}
    T = Table(A)
    at_all = {**base_atoms(A), **more_atoms(A)}
    at = {k: v for k, v in at_all.items() if v.sum() >= 60 and not k.startswith("price<") and k != "price 1-5"}
    d_tr, d_te = A["date"][T.train], A["date"][T.test]
    print(f"{len(A['i']):,} stock-days, {len(set(A['t']))} stocks, {min(A['date'])} .. {max(A['date'])}")
    print(f"picked on {min(d_tr)} .. {max(d_tr)} (OLD), checked on {min(d_te)} .. {max(d_te)} (NEW)")
    res = {"rows": int(len(A["i"])), "old": [min(d_tr), max(d_tr)], "new": [min(d_te), max(d_te)], "named": {}}

    # 1. named strategies
    strategies = named(A, at_all)
    for col in ("r5", "r10", "day"):
        print(f"\nNAMED STRATEGIES, {LABEL[col]}. Each side: trades, return, vs SPY, vs others (t by day)")
        print(f"{'':<38}{'OLD':>6}{'ret':>8}{'vs SPY':>8}{'vs oth':>8}{'t':>6}   |{'NEW':>6}{'ret':>8}{'vs SPY':>8}{'vs oth':>8}{'t':>6}  works?")
        res["named"][col] = {}
        for name, mask in strategies.items():
            o, n = T.score(mask, col, T.train), T.score(mask, col, T.test)
            res["named"][col][name] = {"old": o, "new": n}
            if o["n"] < 5 or n["n"] < 5:
                print(f"{name:<38}{o['n']:>6}{'too few trades':>30}   |{n['n']:>6}")
                continue
            both = o["vs_others"] > 0 and n["vs_others"] > 0
            verdict = ("YES" if o["t"] >= 2 and n["t"] >= 2 else "maybe" if both and max(o["t"], n["t"]) >= 2 else
                       "weak" if both else "AVOID" if o["vs_others"] < 0 and n["vs_others"] < 0 and min(o["t"], n["t"]) <= -2 else "no")
            res["named"][col][name]["verdict"] = verdict
            print(f"{name:<38}{o['n']:>6}{o['ret']:>+8.1%}{o['vs_spy']:>+8.1%}{o['vs_others']:>+8.1%}{o['t']:>6.1f}   |"
                  f"{n['n']:>6}{n['ret']:>+8.1%}{n['vs_spy']:>+8.1%}{n['vs_others']:>+8.1%}{n['t']:>6.1f}  {verdict}")

    # 2. market regime: every talked-about stock, by what the market was doing
    print("\nMARKET REGIME: every talked-about stock, hold 10 days (return / vs SPY)")
    res["regime"] = {}
    for name in ("market uptrend", "market downtrend", "market fell 2%+ in 5d", "market rose 2%+ in 5d"):
        o, n = T.score(at_all[name], "r10", T.train), T.score(at_all[name], "r10", T.test)
        res["regime"][name] = {"old": o, "new": n}
        f = lambda s: f"{s['n']:>6}{s['ret']:>+8.1%}{s['vs_spy']:>+8.1%}" if s["n"] >= 5 else f"{s['n']:>6}{'':>16}"
        print(f"  {name:<28} OLD{f(o)}   | NEW{f(n)}")

    # 3. mass search
    out, names, n_combos = mass_search(T, at, a.shuffles)
    print(f"\nMASS SEARCH: {len(names)} conditions, {n_combos:,} rules x {len(COLS)} exits = {n_combos * len(COLS):,} strategies each way")
    print("A rule 'holds' if it beat the other talked-about stocks on OLD days and again on NEW days (at half the size).")
    print("'strict' also needs t >= 2 on NEW days. The luck column is the same search on shuffled outcomes.")
    print(f"{'exit':<36}{'side':<7}{'looked good':>12}{'held':>6}{'strict':>7}   |  luck: {'held':>10}{'strict':>10}")
    res["mass"] = {"conditions": len(names), "rules": n_combos, "by_exit": {}}
    tot = defaultdict(float)
    for col in COLS:
        for side in ("best", "worst"):
            r = out.get((col, 0, side), {"candidates": 0, "held": 0, "strict": 0, "keep": []})
            lk = [out.get((col, k, side), {"held": 0, "strict": 0}) for k in range(1, a.shuffles + 1)]
            lh, ls = np.mean([x["held"] for x in lk]), np.mean([x["strict"] for x in lk])
            for key, val in (("held", r["held"]), ("strict", r["strict"]), ("luck_held", lh), ("luck_strict", ls)):
                tot[(side, key)] += val
            res["mass"]["by_exit"][f"{col}|{side}"] = {"candidates": r["candidates"], "held": r["held"], "strict": r["strict"],
                                                        "luck_held": float(lh), "luck_strict": float(ls)}
            print(f"{LABEL[col]:<36}{side:<7}{r['candidates']:>12,}{r['held']:>6}{r['strict']:>7}   |  {lh:>16.1f}{ls:>10.1f}")
    for side in ("best", "worst"):
        print(f"TOTAL {side}: held {tot[(side, 'held')]:.0f} vs luck {tot[(side, 'luck_held')]:.1f};  "
              f"strict {tot[(side, 'strict')]:.0f} vs luck {tot[(side, 'luck_strict')]:.1f}")
        res["mass"][side] = {k: float(tot[(side, k)]) for k in ("held", "strict", "luck_held", "luck_strict")}

    res["top"] = {}
    for side in ("best", "worst"):
        sign = 1 if side == "best" else -1
        cand = []
        for col in COLS:
            for combo, s_tr, s_te, m in out.get((col, 0, side), {"keep": []})["keep"]:
                cand.append((sign * min(s_tr["t"], s_te["t"]), col, combo, s_tr, s_te, m))
        cand.sort(key=lambda c: -c[0])
        top, seen = [], []
        for c in cand:
            if any((c[5] & g).sum() > 0.6 * min(c[5].sum(), g.sum()) for g in seen):
                continue  # nearly the same trades as a rule already listed
            seen.append(c[5])
            top.append(c)
            if len(top) >= 10:
                break
        print(f"\nTOP SURVIVORS, {side.upper()} (ranked by the weaker of the two t values; near-duplicates dropped)")
        res["top"][side] = []
        for _, col, combo, s_tr, s_te, _ in top:
            rule = " + ".join(names[c] for c in combo)
            res["top"][side].append({"rule": rule, "exit": col, "old": s_tr, "new": s_te})
            print(f"  {rule:<66} {LABEL[col][:22]:<22} OLD n={s_tr['n']:<4} {s_tr['vs_others']:+.1%} t={s_tr['t']:.1f} | "
                  f"NEW n={s_te['n']:<4} {s_te['vs_others']:+.1%} vsSPY {s_te['vs_spy']:+.1%} t={s_te['t']:.1f} months {s_te['months']}")
    (DATA / "backtest" / ("tournament_calm.json" if a.max_atr else "tournament.json")).write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
