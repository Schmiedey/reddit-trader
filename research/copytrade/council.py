"""Council experiments: can public wallet data make money some other way than copying stars?

Every choice is made on the FORMATION window (Apr 21 - Aug 15 2026) and judged once on the TEST
window (Aug 16 - Nov 12 2026). Hourly bars, trades execute at the next hour's open, 0.095% per side,
hourly funding paid/received. Markets: BTC, ETH, SOL, HYPE (25% each, 1x gross).

  1. consensus  net positioning of wallets with timing skill (measured in formation), followed
  2. fade       bet against coins where high-win-rate wallets are crowded AND losing
  3. filter     copy formation-chosen daily traders, but stand aside while the copy is below its 30-day average

Pass rule (fixed before results): test return > 0, beats buy-and-hold and the plain baseline, beats
95% of fake versions, both halves of the test window > 0, positive alpha after removing market moves.

    python council.py prep      # once: positions, trips, funding  (cache/council_*.npz)
    python council.py run
"""
import glob, json, os, sys, time
import numpy as np
import steady
from profiles import trips
from copysim import post
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from bot.copytrade import core

C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
COINS = ["BTC", "ETH", "SOL", "HYPE"]
GRID, N = steady.GRID, steady.N
MID = int(np.searchsorted(GRID, steady.ts("2026-08-16")))
COST = 0.00045 + 0.0005
rng = np.random.default_rng(7)


# ---------------------------------------------------------------- data prep
def prices():
    P = np.array([steady.price_grid(c) for c in COINS])
    for i in range(len(COINS)):                       # forward-fill rare gaps
        for t in range(1, N):
            if np.isnan(P[i, t]): P[i, t] = P[i, t - 1]
    return P


def funding_grid():
    out = np.zeros((len(COINS), N))
    for i, c in enumerate(COINS):
        f = os.path.join(C, f"funding_{c}.json")
        if not os.path.exists(f):
            rows, start = [], int(GRID[0])
            while start < GRID[-1] + 3600_000:
                r = post({"type": "fundingHistory", "coin": c, "startTime": start, "endTime": int(GRID[-1]) + 3600_000}) or []
                if not r: break
                rows += [(x["time"], float(x["fundingRate"])) for x in r]
                start = max(x["time"] for x in r) + 1
                time.sleep(0.5)
                if len(r) < 500: break
            json.dump(rows, open(f, "w"))
        for t, rate in json.load(open(f)):
            k = int(np.searchsorted(GRID, t)) - 1          # paid on the position held during hour k
            if 0 <= k < N: out[i, k] += rate
    return out


def prep():
    files = sorted(glob.glob(os.path.join(C, "long", "*.json")))
    addrs, POS, stats = [], [], []
    for n, f in enumerate(files):
        d = json.load(open(f))
        if not d.get("complete"): continue
        a = os.path.basename(f)[:-5]
        g = steady.position_grids(d)
        POS.append(np.array([g.get(c, np.zeros(N)) for c in COINS], dtype=np.float32))
        # formation-only trade stats (for the high-win-rate set and the daily-trader picks)
        seen, fl = set(), []
        for x in sorted(d["fills"] + d["twap"], key=lambda x: x["time"]):
            if steady.main_dex(x["coin"]) and (x["tid"], x["time"]) not in seen and GRID[0] <= x["time"] < GRID[MID]:
                seen.add((x["tid"], x["time"])); fl.append(x)
        done, still = trips(fl)
        pnl = np.array([t["pnl"] for t in done]) if done else np.zeros(0)
        open_pnl = 0.0
        for t in still:
            p = steady.price_grid(t["coin"])[:MID]; p = p[~np.isnan(p)]
            if len(p) and t["pos"]: open_pnl += t["pos"] * (p[-1] - t["avg"])
        hold = np.median([(t["end"] - t["start"]) / 3600_000 for t in done]) if done else 1e9
        w = pnl > 0
        stats.append({"n": len(done), "win": float(w.mean()) if len(pnl) else 0.0, "pnl": float(pnl.sum()), "open": float(open_pnl),
                      "pf": float(pnl[w].sum() / -pnl[~w].sum()) if (~w).any() and pnl[~w].sum() < 0 else 99.0, "hold": float(hold)})
        addrs.append(a)
        if n % 100 == 0: print("prep", n, len(files), flush=True)
    np.savez_compressed(os.path.join(C, "council_pos.npz"), pos=np.array(POS), addrs=np.array(addrs))
    json.dump(stats, open(os.path.join(C, "council_stats.json"), "w"))
    print("wallets", len(addrs))


# ---------------------------------------------------------------- strategy maths
def backtest(hold, P, F):
    """hold[c,t] in {-1,0,1} decided with info up to t, held from t+1 to t+2. Returns hourly returns (len N)."""
    r1 = np.zeros_like(P); r1[:, :-1] = P[:, 1:] / P[:, :-1] - 1
    h = np.zeros_like(hold, dtype=float); h[:, 1:] = hold[:, :-1]          # execute next hour
    w = 1.0 / len(COINS)
    ret = (w * h * r1).sum(0) - (w * h * F).sum(0)
    turn = np.abs(np.diff(h, axis=1, prepend=0)); ret -= w * COST * turn.sum(0)
    return ret


def summary(ret, lo, hi, P=None):
    r = ret[lo:hi]; eq = np.cumprod(1 + r)
    day = eq[23::24]; dr = np.diff(np.concatenate([[1.0], day])) / np.concatenate([[1.0], day[:-1]])
    out = {"ret": float(eq[-1] - 1), "sharpe": float(dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0.0,
           "dd": float(np.max(1 - eq / np.maximum.accumulate(eq)))}
    if P is not None:                                                     # alpha vs equal-weight coins
        m = np.mean(P[:, lo + 24:hi:24] / P[:, lo:hi - 24:24] - 1, axis=0)
        k = min(len(m), len(dr))
        b = np.polyfit(m[:k], dr[:k], 1) if k > 5 else (0, 0)
        out.update(beta=float(b[0]), alpha_yr=float(b[1] * 365))
    return out


def verdict(name, strat, base, fakes, P, extra_bases=()):
    half = MID + (N - MID) // 2
    s, h1, h2 = summary(strat, MID, N, P), summary(strat, MID, half), summary(strat, half, N)
    bh = summary(base, MID, N)
    fk = np.array([summary(f, MID, N)["ret"] for f in fakes]) if len(fakes) else np.array([])
    pct = float((fk < s["ret"]).mean()) if len(fk) else float("nan")
    checks = {"profitable": s["ret"] > 0, "beats buy&hold": s["ret"] > bh["ret"],
              **{f"beats {n}": s["ret"] > summary(b, MID, N)["ret"] for n, b in extra_bases},
              "beats 95% of fakes": pct >= 0.95, "both halves > 0": h1["ret"] > 0 and h2["ret"] > 0, "alpha > 0": s.get("alpha_yr", 0) > 0}
    print(f"\n  {name}: TEST {s['ret']:+.1%} (Sharpe {s['sharpe']:.2f}, max DD {s['dd']:.0%}, beta {s['beta']:+.2f}, alpha/yr {s['alpha_yr']:+.0%})"
          f" | halves {h1['ret']:+.1%} / {h2['ret']:+.1%} | buy&hold {bh['ret']:+.1%}"
          + "".join(f" | {n} {summary(b, MID, N)['ret']:+.1%}" for n, b in extra_bases)
          + (f" | fakes median {np.median(fk):+.1%}, real beats {pct:.0%}" if len(fk) else ""))
    print("  " + "  ".join(f"{'PASS' if v else 'fail'}: {k}" for k, v in checks.items()))
    print(f"  ==> {'PASSES' if all(checks.values()) else 'FAILS'}")
    return all(checks.values())


def pick_theta(make, P, F, thetas):
    best = max(thetas, key=lambda th: summary(backtest(make(th), P, F), 0, MID)["sharpe"])
    s = summary(backtest(make(best), P, F), 0, MID)
    print(f"  chosen on formation: threshold {best}  (formation return {s['ret']:+.1%}, Sharpe {s['sharpe']:.2f})")
    return best


# ---------------------------------------------------------------- 1. consensus
def timing_scores(POS, P):
    r24 = np.full_like(P, np.nan); r24[:, :-24] = P[:, 24:] / P[:, :-24] - 1
    days = np.arange(0, MID - 24, 24)
    mu = np.nanmean(r24[:, days], axis=1, keepdims=True)
    ex = r24[:, days] - mu                                                # coin's own drift removed
    sg = np.sign(POS[:, :, days])                                         # (wallets, coins, days)
    x = sg * ex[None]
    n = (sg != 0).sum((1, 2))
    m = x.sum((1, 2)) / np.maximum(n, 1)
    var = ((x - m[:, None, None]) ** 2 * (sg != 0)).sum((1, 2)) / np.maximum(n - 1, 1)
    t = m / np.sqrt(var / np.maximum(n, 1) + 1e-18)
    return np.where(n >= 30, t, 0.0), n


def consensus_hold(POS, w, th):
    s = np.einsum("w,wct->ct", w, np.sign(POS)) / max(w.sum(), 1e-9)
    return np.where(np.abs(s) >= th, np.sign(s), 0.0)


def exp_consensus(POS, P, F, base, trend):
    print("\n=== 1. Consensus of wallets with timing skill")
    t, n = timing_scores(POS, P)
    sk = t >= 2.0
    print(f"  wallets with >= 30 daily observations in formation: {(n >= 30).sum()}; timing t-stat >= 2: {sk.sum()}"
          f" (luck alone would give about {0.023 * (n >= 30).sum():.0f})")
    w = np.where(sk, t, 0.0)
    th = pick_theta(lambda th: consensus_hold(POS, w, th), P, F, [0.05, 0.1, 0.2, 0.3])
    strat = backtest(consensus_hold(POS, w, th), P, F)
    allw = backtest(consensus_hold(POS, (n >= 30).astype(float), th), P, F)
    fakes = []
    elig = np.where(n >= 30)[0]
    for _ in range(200):
        fw = np.zeros(len(w)); fw[rng.choice(elig, sk.sum(), replace=False)] = rng.permutation(w[sk])
        fakes.append(backtest(consensus_hold(POS, fw, th), P, F))
    long_share = (consensus_hold(POS, w, th)[:, MID:] > 0).mean()
    print(f"  signal is long {long_share:.0%} of coin-hours in the test window")
    return verdict("consensus", strat, base, fakes, P, (("trend rule", trend), ("all-wallet consensus", allw)))


# ---------------------------------------------------------------- 2. crowding fade
def underwater_sign(pos, p):
    """+1 long & losing, -1 short & losing, 0 otherwise, using an average-entry price rebuilt hourly."""
    out = np.zeros_like(pos); avg = np.nan; prev = 0.0
    for t in range(len(pos)):
        q = pos[t]
        if q == 0: avg = np.nan
        elif prev == 0 or np.sign(q) != np.sign(prev): avg = p[t]
        elif abs(q) > abs(prev): avg = (avg * abs(prev) + p[t] * (abs(q) - abs(prev))) / abs(q)
        if q != 0 and (p[t] - avg) * np.sign(q) < 0: out[t] = np.sign(q)
        prev = q
    return out


def fade_hold(UW, POS, idx, th, min_n=5):
    uw = UW[idx].sum(0); npos = (POS[idx] != 0).sum(0)
    score = np.where(npos >= min_n, uw / np.maximum(npos, 1), 0.0)
    return np.where(np.abs(score) >= th, -np.sign(score), 0.0)


def exp_fade(POS, P, F, base, trend, stats):
    print("\n=== 2. Fade coins where high-win-rate wallets are crowded and losing")
    hwr = np.array([s["n"] >= 20 and s["win"] >= 0.65 for s in stats])
    act = np.array([s["n"] >= 20 for s in stats])
    print(f"  high-win-rate wallets in formation (>= 20 closed trades, >= 65% wins): {hwr.sum()} of {act.sum()} active")
    UW = np.zeros_like(POS)
    for i in np.where(act)[0]:
        for c in range(len(COINS)):
            if POS[i, c].any(): UW[i, c] = underwater_sign(POS[i, c], P[c])
    idx = np.where(hwr)[0]
    th = pick_theta(lambda th: fade_hold(UW, POS, idx, th), P, F, [0.2, 0.3, 0.4, 0.5])
    strat = backtest(fade_hold(UW, POS, idx, th), P, F)
    fakes = [backtest(fade_hold(UW, POS, rng.choice(np.where(act)[0], len(idx), replace=False), th), P, F) for _ in range(200)]
    return verdict("crowding fade", strat, base, fakes, P, (("trend rule", trend),))


# ---------------------------------------------------------------- 3. copy with a loss filter
def copy_curve(addr, start):
    d = json.load(open(os.path.join(C, "long", addr + ".json")))
    pos = steady.position_grids(d); px = {c: steady.price_grid(c) for c in pos}
    av = dict(json.load(open(os.path.join(C, "portfolio", addr + ".json"))))["allTime"]["accountValueHistory"]
    av = np.array([[t, float(v)] for t, v in av]); eq = np.interp(GRID, av[:, 0], av[:, 1])
    acct, curve, lev = core.new_account(), np.empty(N - start), np.empty(N - start)
    for k in range(start, N):
        live = {c: pos[c][k] for c in pos if pos[c][k] != 0.0}
        mids = {c: px[c][k] for c in set(live) | set(acct["pos"]) if px[c][k] == px[c][k]}
        core.step(acct, live, eq[k], mids)
        curve[k - start], lev[k - start] = acct["equity"], core.gross_leverage(acct)
    return curve, lev


def filtered(curve, lev, start, ma_h=720):
    r = np.zeros(len(curve)); r[1:] = curve[1:] / np.maximum(curve[:-1], 1e-9) - 1
    ma = np.array([curve[max(0, k - ma_h):k + 1].mean() for k in range(len(curve))])
    on = np.zeros(len(curve), bool); on[1:] = curve[:-1] >= ma[:-1]
    toggles = np.abs(np.diff(on.astype(int), prepend=on[0]))
    return r * on - toggles * lev * COST


def exp_filter(stats, addrs):
    print("\n=== 3. Copy daily traders chosen in formation, standing aside while the copy is below its 30-day average")
    form_days = MID / 24
    pick = [i for i, s in enumerate(stats) if s["n"] / form_days >= 0.5 and s["hold"] <= 72 and s["win"] >= 0.65
            and s["pf"] >= 1.5 and s["pnl"] + min(s["open"], 0) >= 25_000]
    act = [i for i, s in enumerate(stats) if s["n"] >= 20 and i not in pick]
    ctrl = list(rng.choice(act, min(30, len(act)), replace=False))
    print(f"  formation picks: {len(pick)} | random control: {len(ctrl)}")
    start = MID - 720
    res = {}
    for name, ids in (("picks", pick), ("control", ctrl)):
        plain, filt = [], []
        for i in ids:
            cv, lv = copy_curve(addrs[i], start)
            r = np.zeros(len(cv)); r[1:] = cv[1:] / np.maximum(cv[:-1], 1e-9) - 1
            plain.append(r[720:]); filt.append(filtered(cv, lv, start)[720:])
        res[name] = (np.mean(np.cumprod(1 + np.array(plain), axis=1), axis=0), np.mean(np.cumprod(1 + np.array(filt), axis=1), axis=0))
    def as_ret(eq):
        full = np.zeros(N); e = np.concatenate([[1.0], eq]); full[MID:] = e[1:] / e[:-1] - 1
        return full
    P = prices()
    base = backtest(np.ones((len(COINS), N)), P, funding_grid())
    ok = verdict("picks, filtered", as_ret(res["picks"][1]), base, [], P,
                 (("picks, plain copy", as_ret(res["picks"][0])), ("control, plain copy", as_ret(res["control"][0])),
                  ("control, filtered", as_ret(res["control"][1]))))
    return ok


if __name__ == "__main__":
    if sys.argv[1:] == ["prep"]:
        prep(); sys.exit()
    z = np.load(os.path.join(C, "council_pos.npz")); POS, addrs = z["pos"], list(z["addrs"])
    stats = json.load(open(os.path.join(C, "council_stats.json")))
    if "--all" not in sys.argv:
        # Only wallets chosen with information up to Jul 31 (fetch_long.pool). The 845 leaderboard wallets
        # were picked partly on last-month profit, which is inside the test window: lookahead.
        import fetch_long
        clean = set(fetch_long.pool())
        keep = [i for i, a in enumerate(addrs) if a in clean]
        POS, addrs, stats = POS[keep], [addrs[i] for i in keep], [stats[i] for i in keep]
        print(f"clean universe (chosen on data up to Jul 31 only): {len(addrs)} wallets")
    P, F = prices(), funding_grid()
    base = backtest(np.ones((len(COINS), N)), P, F)
    r7 = np.zeros_like(P); r7[:, 168:] = P[:, 168:] / P[:, :-168] - 1
    trend = backtest(np.sign(r7), P, F)
    print(f"wallets {len(addrs)} | formation {time.strftime('%b %d', time.gmtime(GRID[0]/1000))}-{time.strftime('%b %d', time.gmtime(GRID[MID]/1000))}"
          f" | test to {time.strftime('%b %d', time.gmtime(GRID[-1]/1000))}"
          f" | equal-weight coins: formation {summary(base, 0, MID)['ret']:+.1%}, test {summary(base, MID, N)['ret']:+.1%}")
    which = [a for a in sys.argv[2:] if not a.startswith("--")] or ["1", "2", "3"]
    if "1" in which: exp_consensus(POS, P, F, base, trend)
    if "2" in which: exp_fade(POS, P, F, base, trend, stats)
    if "3" in which: exp_filter(stats, addrs)
