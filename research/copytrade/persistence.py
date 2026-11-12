"""Out-of-sample test: do wallets that did well in a formation window keep doing well in the next window?"""
import json, os, glob, statistics as st, datetime as dt
import numpy as np

D = os.path.dirname(os.path.abspath(__file__))
ts = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000
SPLITS = [("2025-08-15", "2026-02-15", "2026-05-15"), ("2025-11-15", "2026-05-15", "2026-08-15"), ("2026-02-15", "2026-08-15", "2026-11-12")]
WEEK = 14 * 86400 * 1000  # history is sampled ~every 1-2 weeks, so use a 2-week grid

def load():
    out = {}
    for f in glob.glob(os.path.join(D, "cache", "portfolio", "*.json")):
        d = dict(json.load(open(f)))
        a = d.get("perpAllTime") or d.get("allTime")
        if not a or len(a["pnlHistory"]) < 3:
            continue
        p = np.array([[t, float(v)] for t, v in a["pnlHistory"]])
        av = np.array([[t, float(v)] for t, v in a["accountValueHistory"]])
        out[os.path.basename(f)[:-5]] = (p, av)
    return out

def at(series, t):  # cumulative value at time t (0 before first point, hold last after)
    if t <= series[0, 0]:
        return 0.0 if series is not None else 0.0
    return float(np.interp(t, series[:, 0], series[:, 1]))

def metrics(p, av, t0, t1):
    grid = np.arange(t0, t1 + 1, WEEK)
    cum = np.array([at(p, t) for t in grid])
    wk = np.diff(cum)
    acct = np.array([np.interp(t, av[:, 0], av[:, 1]) for t in grid])
    cap = max(np.median(acct), 1.0)
    pnl = at(p, t1) - at(p, t0)
    dd = float(np.max(np.maximum.accumulate(cum) - cum))
    sharpe = wk.mean() / wk.std() if wk.std() > 0 else 0.0
    return dict(pnl=pnl, cap=cap, roi=pnl / cap, sharpe=sharpe, winwk=float((wk > 0).mean()),
                dd=dd, active=bool(np.any(wk != 0)))

def spearman(x, y):
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])

def summarize(name, rows):
    if not rows:
        print(f"  {name:38s} n=0"); return
    tp = np.array([r[1]["pnl"] for r in rows]); tr = np.array([r[1]["roi"] for r in rows])
    print(f"  {name:38s} n={len(rows):4d}  active_next={np.mean([r[1]['active'] for r in rows]):4.0%}  profitable_next={np.mean(tp>0):5.1%}  "
          f"median_roi_next={np.median(tr):+7.1%}  mean_roi_next={np.mean(np.clip(tr,-1,5)):+7.1%}  "
          f"sum_pnl_next=${tp.sum()/1e6:+8.2f}M")

def main():
    data = load()
    print("wallets loaded:", len(data))
    results = {}
    for f0, f1, t1 in SPLITS:
        F0, F1, T1 = ts(f0), ts(f1), ts(t1)
        rows = []
        for a, (p, av) in data.items():
            if p[0, 0] > F0 + WEEK:  # must exist at formation start
                continue
            mf, mt = metrics(p, av, F0, F1), metrics(p, av, F1, T1)
            cap = max(mf['cap'], mt['cap'], 1000.0)  # same capital base, avoids blow-ups when account is emptied
            mt['roi'] = mt['pnl'] / cap
            if not mf["active"] or mf["cap"] < 1000:
                continue
            rows.append((mf, mt, a))
        results[(f0, f1, t1)] = rows
        print(f"\n=== formation {f0}..{f1}  ->  test {f1}..{t1}   (eligible wallets: {len(rows)})")
        fx = lambda k: np.array([r[0][k] for r in rows])
        for k in ["pnl", "roi", "sharpe", "winwk"]:
            print(f"  spearman(formation {k:6s}, next roi) = {spearman(fx(k), np.array([r[1]['roi'] for r in rows])):+.3f}"
                  f"   (vs next pnl {spearman(fx(k), np.array([r[1]['pnl'] for r in rows])):+.3f})")
        summarize("ALL eligible (baseline)", rows)
        summarize("formation profitable", [r for r in rows if r[0]["pnl"] > 0])
        summarize("formation losing", [r for r in rows if r[0]["pnl"] <= 0])
        big = [r for r in rows if r[0]["cap"] >= 50_000]
        summarize("top 50 by formation PnL ($)", sorted(rows, key=lambda r: -r[0]["pnl"])[:50])
        summarize("top 10% by formation PnL ($)", sorted(rows, key=lambda r: -r[0]["pnl"])[:len(rows)//10])
        summarize("top 50 by formation ROI (cap>=50k)", sorted(big, key=lambda r: -r[0]["roi"])[:50])
        summarize("top 50 by 2wk Sharpe (cap>=50k)", sorted(big, key=lambda r: -r[0]["sharpe"])[:50])
        q = [r for r in big if r[0]["pnl"] > 0 and r[0]["winwk"] >= 0.6 and r[0]["dd"] < 0.5 * r[0]["pnl"]]
        summarize("quality: winwk>=60%, DD<50% of pnl", q)
        summarize("bottom 50 by formation PnL ($)", sorted(rows, key=lambda r: r[0]["pnl"])[:50])
        # decile table by formation sharpe
        srt = sorted(big, key=lambda r: r[0]["sharpe"])
        n = len(srt)
        print("  deciles by formation 2-week Sharpe (cap>=50k): median next ROI / %profitable")
        print("   ", "  ".join(f"D{i+1}:{np.median([r[1]['roi'] for r in srt[i*n//10:(i+1)*n//10]]):+.1%}/"
                              f"{np.mean([r[1]['pnl']>0 for r in srt[i*n//10:(i+1)*n//10]]):.0%}" for i in range(10)))

    json.dump({f"{k[0]}_{k[1]}_{k[2]}": [[r[2], r[0], r[1]] for r in v] for k, v in results.items()},
              open(os.path.join(D, "cache", "persistence_rows.json"), "w"))


if __name__ == '__main__':
    main()
