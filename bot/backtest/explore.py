"""Explorer: where do the big moves come from, and can a rule catch them before they happen?

1. Builds one row per (stock, day) that the crowd is talking about, with ~30 facts known BEFORE the
   buy (who is buying, how fast attention is growing, price/volume action) and what happened AFTER
   (day trade, 3/5/10-day holds, target/stop exits), net of trading costs.
2. Base rates: how often do +10% / +25% moves really happen, by price range?
3. Rule mining: tries every combination of up to 3 conditions on the OLDER 60% of days, then checks
   the winners on the NEWER 40% they never saw. A shuffled-outcome run shows how many "winners"
   pure luck would produce.

    uv run python -m bot.backtest.explore [--evening] [--since 2025-08]
"""
import argparse
import bisect
import itertools
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from ..db import DATA, connect
from ..signals.stance import stance_for
from ..sources.prices import load_valid_symbols
from .build import BARS, bt_connect, load_bars, load_posts
from .sim import Bars

ET = ZoneInfo("America/New_York")
JUNK = {None, "", "[deleted]", "AutoModerator"}
CORE = "wallstreetbets"
PENNY_SUBS = {"pennystocks", "shortsqueeze", "smallstreetbets", "robinhoodpennystocks"}
EXITS = {"x10_5_3": (0.10, 0.05, 3), "x15_7_5": (0.15, 0.07, 5), "x25_10_5": (0.25, 0.10, 5),
         "x30_10_10": (0.30, 0.10, 10), "x50_15_10": (0.50, 0.15, 10)}
TARGETS = ["day", "r3", "r5", "r10", *EXITS]


def _ts(d, h, m):
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET).timestamp()


def exit_rule(o, path, target, stop, hold, slip):
    """Buy at the open `o`; each day check stop first, then target; else sell at the close of day `hold`."""
    entry = o * (1 + slip)
    for k, (bo, bh, bl, bc) in enumerate(path[:hold]):
        if bl <= entry * (1 - stop):
            return min(bo, entry * (1 - stop)) * (1 - slip) / entry - 1 if k else (1 - stop) * (1 - slip) - 1
        if bh >= entry * (1 + target):
            return max(bo, entry * (1 + target)) * (1 - slip) / entry - 1 if k else (1 + target) * (1 - slip) - 1
    last = path[min(hold, len(path)) - 1][3]
    return last * (1 - slip) / entry - 1


def build_rows(con, B, morning=True):
    dates = B.dates["SPY"]
    n = len(dates)
    if morning:  # posts up to 9:00 ET count for that morning's open
        cut = [_ts(dates[i + 1], 9, 0) for i in range(n - 1)] + [_ts(dates[-1], 9, 0) + 86400]
    else:        # posts up to 16:30 ET count for the next morning's open
        cut = [_ts(d, 16, 30) for d in dates]
    rows = con.execute(
        """SELECT m.post_id, m.ticker, m.author, m.created, m.sentiment, p.community, p.title, p.body,
                  p.score, p.flair FROM mentions m JOIN posts p ON p.id = m.post_id""").fetchall()
    n_t = defaultdict(int)
    for r in rows:
        n_t[r["post_id"]] += 1
    from ..signals.crowd import ensure_stances
    ensure_stances(con)  # the same classification the live desk uses
    stmap = {(r[0], r[1]): r[2] for r in con.execute("SELECT post_id, ticker, stance FROM stances")}
    F = defaultdict(lambda: defaultdict(lambda: {"M": set(), "B": set(), "S": set(), "H": set(), "W": 0, "L": 0, "dd": 0,
                                                 "yolo": 0, "pen": 0, "n": 0}))
    core = defaultdict(int)
    for r in rows:
        com = (r["community"] or "").lower()
        if r["author"] in JUNK or n_t[r["post_id"]] > 5:
            continue
        i = bisect.bisect_right(cut, r["created"])
        if i >= n:
            continue
        st = stmap.get((r["post_id"], r["ticker"]))
        f = F[r["ticker"]][i]
        f["M"].add(r["author"])
        f["n"] += 1
        f["pen"] += com in PENNY_SUBS
        core[i] += com == CORE
        if st in ("BUY", "DD"):
            f["B"].add(r["author"])
        elif st == "SELL":
            f["S"].add(r["author"])
        elif st == "HOLD":
            f["H"].add(r["author"])
        elif st == "WON":
            f["W"] += 1
        elif st == "LOST":
            f["L"] += 1
        if st == "DD":
            f["dd"] = max(f["dd"], r["score"] or 0)
        f["yolo"] += "yolo" in (r["flair"] or "").lower()
    counts = sorted(v for v in core.values() if v)
    med = counts[len(counts) // 2] if counts else 1
    valid = [all(core.get(j, 0) > 0 for j in range(i - 9, i + 1))
             and sum(core.get(j, 0) for j in range(i - 9, i + 1)) >= 2.0 * med for i in range(n)]
    spy = B.b["SPY"]
    out = []
    for t, days in F.items():
        tb = B.b.get(t)
        if not tb or t == "SPY":
            continue
        pos_of = {b["d"]: k for k, b in enumerate(tb)}
        cand = sorted({a + k for a in days for k in (0, 1, 2)})

        def uni(i, lo, hi, key):
            acc = set()
            for j in range(i - hi, i - lo + 1):
                if j in days:
                    acc |= days[j][key]
            return acc

        def tot(i, lo, hi, key, fn=sum):
            return fn([days[j][key] for j in range(i - hi, i - lo + 1) if j in days] or [0])

        for i in cand:
            if i + 1 >= n or not valid[i]:
                continue
            p = pos_of.get(dates[i])
            if p is None or p < 25 or p + 1 >= len(tb) or tb[p + 1]["d"] != dates[i + 1]:
                continue
            m3 = len(uni(i, 0, 2, "M"))
            if m3 < 2:
                continue
            w = tb[p - 25:p + 11]
            if any((b["c"] >= 3 * q["c"] and b["v"] <= 0.3 * q["v"]) or b["c"] >= 8 * q["c"] or b["c"] * 8 <= q["c"]
                   for q, b in zip(w, w[1:]) if q["c"] > 0):
                continue  # unadjusted reverse split in the price data (price jumps, volume collapses): not a real move
            c = [b["c"] for b in tb[p - 25:p + 1]]
            price = c[-1]
            dv20 = sum(b["c"] * b["v"] for b in tb[p - 19:p + 1]) / 20
            if price <= 0 or dv20 < 3e5:
                continue
            v20 = sum(b["v"] for b in tb[p - 19:p + 1]) / 20
            hi60 = max(b["h"] for b in tb[max(0, p - 59):p + 1])
            lo60 = min(b["l"] for b in tb[max(0, p - 59):p + 1])
            atr = sum(max(b["h"] - b["l"], abs(b["h"] - q["c"]), abs(b["l"] - q["c"]))
                      for q, b in zip(tb[p - 14:p], tb[p - 13:p + 1])) / 14 / price
            prev = len(uni(i, 3, 12, "M"))
            nposts = tot(i, 0, 2, "n")
            path = [(b["o"], b["h"], b["l"], b["c"]) for b in tb[p + 1:p + 22]]
            o = path[0][0]
            if o <= 0:
                continue
            slip = 0.005 if price < 5 else 0.001
            entry = o * (1 + slip)

            def hold(h):
                return path[h - 1][3] * (1 - slip) / entry - 1 if len(path) >= h else float("nan")

            row = {
                "t": t, "i": i, "day": dates[i + 1], "price": price, "dv20": dv20,
                "m1": len(days[i]["M"]) if i in days else 0, "m3": m3, "prev10": prev,
                "accel": m3 / max(1.0, prev * 0.3), "new20": len(uni(i, 3, 22, "M")) == 0,
                "b3": len(uni(i, 0, 2, "B")), "s3": len(uni(i, 0, 2, "S")),
                "W5": tot(i, 0, 4, "W"), "L5": tot(i, 0, 4, "L"), "dd3": tot(i, 0, 2, "dd", max),
                "yolo3": tot(i, 0, 2, "yolo"), "pen": tot(i, 0, 2, "pen") / max(nposts, 1),
                "ret1": c[-1] / c[-2] - 1, "ret5": c[-1] / c[-6] - 1, "ret20": c[-1] / c[-21] - 1,
                "volx": tb[p]["v"] / v20 if v20 else 0, "atr": atr, "off60": price / hi60 - 1,
                "up60": price / lo60 - 1 if lo60 > 0 else 0, "gap": o / price - 1,
                "o_day": path[0][3] * (1 - slip) / entry - 1, "r3": hold(3), "r5": hold(5), "r10": hold(10), "r20": hold(20),
                "b5": len(uni(i, 0, 4, "B")), "bp5": len(uni(i, 5, 9, "B")), "s5": len(uni(i, 0, 4, "S")),
                "h5": len(uni(i, 0, 4, "H")),
                "mfe5": max(b[1] for b in path[:5]) / entry - 1, "mae5": min(b[2] for b in path[:5]) / entry - 1,
            }
            for name, (tg, sp, h) in EXITS.items():
                row[name] = exit_rule(o, path[:10], tg, sp, h, slip) if len(path) >= min(h, 3) else float("nan")
            sp1 = bisect.bisect_left(B.dates["SPY"], dates[i + 1])
            row["spy5"] = spy[min(sp1 + 4, len(spy) - 1)]["c"] / spy[sp1]["o"] - 1
            for h in (1, 3, 10, 20):  # SPY over the same holding period, for "did it beat the market?"
                row[f"spy{h}"] = spy[sp1 + h - 1]["c"] / spy[sp1]["o"] - 1 if sp1 + h - 1 < len(spy) else float("nan")
            row["spy_tr"] = spy[i]["c"] / (sum(b["c"] for b in spy[max(0, i - 49):i + 1]) / len(spy[max(0, i - 49):i + 1])) - 1
            row["spy_r5"] = spy[i]["c"] / spy[i - 5]["c"] - 1 if i >= 5 else 0.0
            out.append(row)
    return out


def atoms(A):
    """Named yes/no conditions, all knowable before the buy (gap is known at the open)."""
    a = {
        "price<1": A["price"] < 1, "price 1-5": (A["price"] >= 1) & (A["price"] < 5),
        "price 5-20": (A["price"] >= 5) & (A["price"] < 20), "price 20+": A["price"] >= 20,
        "tiny volume (<$2M/day)": A["dv20"] < 2e6, "small volume ($2-20M)": (A["dv20"] >= 2e6) & (A["dv20"] < 2e7),
        "big volume ($200M+)": A["dv20"] >= 2e8,
        "3+ people talking": A["m3"] >= 3, "6+ people talking": A["m3"] >= 6, "12+ people talking": A["m3"] >= 12,
        "attention 2x": A["accel"] >= 2, "attention 4x": A["accel"] >= 4, "new name (quiet 20d)": A["new20"] > 0.5,
        "2+ buyers": A["b3"] >= 2, "4+ buyers": A["b3"] >= 4, "more buyers than sellers": A["b3"] > A["s3"],
        "sellers lead": A["s3"] > A["b3"], "gain posts 2+": A["W5"] >= 2, "loss posts 2+": A["L5"] >= 2,
        "DD 50+ upvotes": A["dd3"] >= 50, "YOLO 2+": A["yolo3"] >= 2,
        "penny-sub driven": A["pen"] >= 0.5, "no penny-sub posts": A["pen"] == 0,
        "up 5%+ yesterday": A["ret1"] >= 0.05, "up 15%+ yesterday": A["ret1"] >= 0.15,
        "down 5%+ yesterday": A["ret1"] <= -0.05, "up 20%+ in 5d": A["ret5"] >= 0.20,
        "down 10%+ in 5d": A["ret5"] <= -0.10, "flat 5d": np.abs(A["ret5"]) < 0.05,
        "up 30%+ in 20d": A["ret20"] >= 0.30, "volume 2x": A["volx"] >= 2, "volume 5x": A["volx"] >= 5,
        "near 60d high": A["off60"] >= -0.05, "40%+ off 60d high": A["off60"] <= -0.40,
        "doubled off 60d low": A["up60"] >= 1.0, "very volatile (ATR 8%+)": A["atr"] >= 0.08,
        "calm (ATR <3%)": A["atr"] < 0.03, "gaps up 3%+": A["gap"] >= 0.03, "gaps down 3%+": A["gap"] <= -0.03,
        "opens flat": np.abs(A["gap"]) < 0.01,
    }
    return a


def fresh_idx(A, mask, cooldown):
    """Row numbers where the rule fires, keeping one per stock per `cooldown` days."""
    idx = np.flatnonzero(mask)
    idx = idx[np.lexsort((A["i"][idx], A["tcode"][idx]))]
    keep, last_t, last_i = [], -1, -99
    for k in idx:
        t, i = A["tcode"][k], A["i"][k]
        if t != last_t or i - last_i >= cooldown:
            keep.append(k)
            last_t, last_i = t, i
    return np.array(keep, dtype=int)


def stats(A, idx, col):
    y = A[col][idx]
    ok = ~np.isnan(y)
    y, days_i, months = y[ok], A["i"][idx][ok], A["month"][idx][ok]
    if len(y) < 5:
        return {"n": int(len(y))}
    by_day = defaultdict(list)
    for d, v in zip(days_i, y):
        by_day[d].append(v)
    dm = np.array([np.mean(v) for v in by_day.values()])
    by_m = defaultdict(list)
    for m, v in zip(months, y):
        by_m[m].append(v)
    mm = [float(np.mean(v)) for v in by_m.values() if len(v) >= 3]
    return {"n": int(len(y)), "mean": round(float(y.mean()), 4), "median": round(float(np.median(y)), 4),
            "win": round(float((y > 0).mean()), 3), "p10": round(float((y >= 0.10).mean()), 3),
            "m10": round(float((y <= -0.10).mean()), 3),
            "t": round(float(dm.mean() / dm.std(ddof=1) * math.sqrt(len(dm))), 2) if len(dm) > 2 and dm.std(ddof=1) > 0 else 0.0,
            "months": f"{sum(x > 0 for x in mm)}/{len(mm)}", "trades_per_month": round(len(y) / max(len(by_m), 1), 1)}


def mine(A, col, train, test, at, max_k=3, min_n=40, min_mean=0.02, y_override=None):
    """Every combination of up to max_k conditions; pick on train, score on test."""
    y = y_override if y_override is not None else A[col]
    names = list(at)
    cd = {"day": 1, "r3": 3, "r5": 5, "r10": 10}.get(col, EXITS.get(col, (0, 0, 5))[2])
    ytr = np.where(train & ~np.isnan(y), y, 0.0)
    ntr_ok = (train & ~np.isnan(y)).astype(float)
    found = []
    for k in range(1, max_k + 1):
        for combo in itertools.combinations(names, k):
            m = at[combo[0]]
            for c in combo[1:]:
                m = m & at[c]
            cnt = float((ntr_ok * m).sum())
            if cnt < min_n or (ytr * m).sum() / cnt < min_mean:
                continue
            tr_idx = fresh_idx(A, m & train, cd)
            ytr_f = y[tr_idx]
            ytr_f = ytr_f[~np.isnan(ytr_f)]
            if len(ytr_f) < min_n * 0.6 or ytr_f.mean() < min_mean or np.median(ytr_f) <= 0:
                continue
            te_idx = fresh_idx(A, m & test, cd)
            yte = y[te_idx]
            yte = yte[~np.isnan(yte)]
            found.append((combo, len(ytr_f), float(ytr_f.mean()), len(yte), float(yte.mean()) if len(yte) else float("nan"), m))
    return found


def run(since=None, morning=True, min_mentions=6):
    con = bt_connect()
    load_posts(con, load_valid_symbols(connect()), since or "0000")
    first = con.execute("SELECT MIN(created) FROM mentions").fetchone()[0]
    if BARS.exists():
        import gzip
        spy0 = json.load(gzip.open(BARS)).get("SPY", [{}])[0].get("d", "9")
        if spy0 > (datetime.fromtimestamp(first) - timedelta(days=120)).date().isoformat():
            BARS.unlink()
    B = Bars(load_bars(con, min_mentions))
    rows = build_rows(con, B, morning)
    cols = [k for k in rows[0] if k not in ("t", "day")]
    A = {k: np.array([r[k] for r in rows], dtype=float) for k in cols}
    A["o_day"], A["day"] = None, A["o_day"]  # "day" = the day-trade result (open to close)
    del A["o_day"]
    A["i"] = A["i"].astype(int)
    tick = {t: k for k, t in enumerate(sorted({r["t"] for r in rows}))}
    A["tcode"] = np.array([tick[r["t"]] for r in rows])
    A["month"] = np.array([r["day"][:7] for r in rows])
    A["t"] = np.array([r["t"] for r in rows])
    A["date"] = np.array([r["day"] for r in rows])
    return A


def report(A, max_k=3, seed=0, penny=False):
    if penny:  # only stocks under $5
        keep = A["price"] < 5
        A = {k: v[keep] for k, v in A.items()}
    out = {"rows": int(len(A["i"])), "span": [min(A["date"].tolist()), max(A["date"].tolist())]}
    at = atoms(A)
    all_rows = np.ones(len(A["i"]), dtype=bool)
    print(f"{out['rows']} stock-days, {out['span'][0]}..{out['span'][1]}")
    print("\nBASE RATES (every talked-about stock, bought at the open, one entry per stock per 5 days)")
    print(f"{'group':<26}{'n':>6}{'day':>8}{'5d mean':>9}{'5d median':>10}{'win':>6}{'+10% in 5d':>11}{'-10% in 5d':>11}{'ever +10%':>10}{'ever +25%':>10}")
    out["base"] = {}
    for name in ("price<1", "price 1-5", "price 5-20", "price 20+", "penny-sub driven", "no penny-sub posts"):
        idx = fresh_idx(A, at[name], 5)
        s5, sd = stats(A, idx, "r5"), stats(A, idx, "day")
        mfe = A["mfe5"][idx]
        out["base"][name] = {"r5": s5, "day": sd, "mfe10": float((mfe >= 0.10).mean()), "mfe25": float((mfe >= 0.25).mean())}
        print(f"{name:<26}{s5['n']:>6}{sd.get('mean', 0):>8.2%}{s5.get('mean', 0):>9.2%}{s5.get('median', 0):>10.2%}{s5.get('win', 0):>6.0%}"
              f"{s5.get('p10', 0):>11.0%}{s5.get('m10', 0):>11.0%}{(mfe >= 0.10).mean():>10.0%}{(mfe >= 0.25).mean():>10.0%}")
    days = np.unique(A["i"])
    cut = days[int(len(days) * 0.6)]
    train, test = A["i"] < cut, A["i"] >= cut
    out["split"] = [max(A["date"][train].tolist()), min(A["date"][test].tolist())]
    print(f"\nRULE MINING: picked on days up to {out['split'][0]}, checked on {out['split'][1]} onward")
    rng = np.random.default_rng(seed)
    out["rules"] = {}
    for col in ("day", "r3", "r5", "x15_7_5", "x30_10_10"):
        min_mean = (0.01 if col == "day" else 0.02) * (2 if penny else 1)
        found = mine(A, col, train, test, at, max_k, min_n=25 if penny else 40, min_mean=min_mean)
        held = [f for f in found if f[3] >= (12 if penny else 20) and f[4] >= min_mean / 2]
        # luck check: shuffle outcomes among the stocks of the same day, then repeat the whole search
        y = A[col].copy()
        for d in days:
            k = np.flatnonzero(A["i"] == d)
            y[k] = y[rng.permutation(k)]
        fake = mine(A, col, train, test, at, max_k, min_n=25 if penny else 40, min_mean=min_mean, y_override=y)
        fake_held = [f for f in fake if f[3] >= (12 if penny else 20) and f[4] >= min_mean / 2]
        print(f"\n[{col}] {len(found)} rules looked good on old data, {len(held)} still worked on new data "
              f"(shuffled-luck run: {len(fake)} found, {len(fake_held)} held)")
        held.sort(key=lambda f: -min(f[2], f[4]))
        top, seen = [], []
        for f in held:  # drop near-duplicates (same rows)
            if any((f[5] & g).sum() > 0.7 * min(f[5].sum(), g.sum()) for g in seen):
                continue
            seen.append(f[5])
            top.append(f)
            if len(top) >= 8:
                break
        res = []
        for combo, ntr, mtr, nte, mte, m in top:
            cd = {"day": 1, "r3": 3, "r5": 5}.get(col, EXITS.get(col, (0, 0, 5))[2])
            s_te = stats(A, fresh_idx(A, m & test, cd), col)
            s_all = stats(A, fresh_idx(A, m, cd), col)
            res.append({"rule": list(combo), "train": {"n": ntr, "mean": round(mtr, 4)}, "test": s_te, "all": s_all})
            print(f"  {' + '.join(combo):<70} old {mtr:+.1%} (n={ntr})  new {s_te.get('mean', 0):+.1%} med {s_te.get('median', 0):+.1%} "
                  f"win {s_te.get('win', 0):.0%} n={s_te['n']} t={s_te.get('t')} months {s_te.get('months')} ~{s_te.get('trades_per_month')}/mo")
        out["rules"][col] = {"found": len(found), "held": len(held), "luck_found": len(fake), "luck_held": len(fake_held), "top": res}
    (DATA / "backtest").mkdir(exist_ok=True)
    (DATA / "backtest" / ("explore_penny.json" if penny else "explore.json")).write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--evening", action="store_true", help="use only posts up to 16:30 ET the day before")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--penny", action="store_true", help="only stocks under $5")
    a = ap.parse_args()
    report(run(a.since, morning=not a.evening), a.k, penny=a.penny)
