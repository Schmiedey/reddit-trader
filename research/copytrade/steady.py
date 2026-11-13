"""Find wallets that would have been STEADY to copy, and test whether steadiness persists.

For every pool wallet (fetch_long.py) the live copier maths (bot/copytrade/core.py) is replayed hour
by hour from 2026-04-21 to 2026-11-12. Positions come from the exchange-recorded position on each
fill (startPosition + size), so missing events self-correct at the next fill.

Rule applied to a ~103-day formation window of the copy curve:
  profit > 0, daily Sharpe >= 2, max drawdown <= 20%, >= 20 paper trades, |corr with BTC| <= 0.5
"""
import glob, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from bot.copytrade import core
from persistence import ts
from copysim import post
D = os.path.dirname(os.path.abspath(__file__)); C = os.path.join(D, "cache")
H = 3600_000
G0, G1 = int(ts("2026-04-21")), int(ts("2026-11-12")) + 23 * H
GRID = np.arange(G0, G1 + 1, H); N = len(GRID); Q = N // 4
WIN = {"Q1": (0, Q), "Q2": (Q, 2 * Q), "Q3": (2 * Q, 3 * Q), "Q4": (3 * Q, N),
       "Q1Q2": (0, 2 * Q), "Q2Q3": (Q, 3 * Q), "Q3Q4": (2 * Q, N), "ALL": (0, N)}
os.makedirs(os.path.join(C, "candles_1h"), exist_ok=True)
main_dex = lambda c: not c.startswith(("@", "#")) and "/" not in c and ":" not in c   # perps on the main exchange only

_px = {}
def price_grid(coin):
    if coin in _px: return _px[coin]
    f = os.path.join(C, "candles_1h", coin + ".json")
    if os.path.exists(f):
        cs = json.load(open(f))
    else:
        r = None
        for _ in range(2):   # delisted coins error out: fail fast and treat as "no price"
            try:
                import urllib.request
                req = urllib.request.Request("https://api.hyperliquid.xyz/info", headers={"Content-Type": "application/json"},
                    data=json.dumps({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": G0 - H, "endTime": G1 + H}}).encode())
                with urllib.request.urlopen(req, timeout=20) as resp:
                    r = json.load(resp); break
            except Exception:
                time.sleep(3)
        r = r or []
        time.sleep(1.2)
        cs = [[c["t"], float(c["o"])] for c in r]
        json.dump(cs, open(f, "w"))
    g = np.full(N, np.nan)
    if cs:
        t = np.array([c[0] for c in cs]); o = np.array([c[1] for c in cs])
        idx = np.searchsorted(t, GRID, side="right") - 1          # last candle opening at/before grid time
        ok = (idx >= 0) & (GRID - t[np.clip(idx, 0, None)] <= 6 * H)  # don't carry stale prices > 6h
        g[ok] = o[idx[ok]]
    _px[coin] = g
    return g

def position_grids(d):
    ev = {}
    seen = set()
    for x in d["fills"] + d["twap"]:
        if not main_dex(x["coin"]) or (x["tid"], x["time"]) in seen: continue
        seen.add((x["tid"], x["time"]))
        sp = float(x["startPosition"])
        ev.setdefault(x["coin"], []).append((x["time"], sp, sp + (1 if x["side"] == "B" else -1) * float(x["sz"])))
    grids = {}
    for coin, e in ev.items():
        e.sort()
        t = np.array([x[0] for x in e]); after = np.array([x[2] for x in e])
        idx = np.searchsorted(t, GRID, side="right") - 1
        grids[coin] = np.where(idx >= 0, after[np.clip(idx, 0, None)], e[0][1])
    for coin, s in d["pos"].items():
        if main_dex(coin) and coin not in grids:
            grids[coin] = np.full(N, s)                           # held untouched the whole time
    return {c: np.where(np.abs(g) < 1e-9, 0.0, g) for c, g in grids.items()}

def simulate(pos, px, eq, a, b, btc):
    acct, curve, trades = core.new_account(), np.empty(b - a), 0
    coins = list(pos)
    for k in range(a, b):
        live = {c: pos[c][k] for c in coins if pos[c][k] != 0.0}
        mids = {c: px[c][k] for c in set(live) | set(acct["pos"]) if px[c][k] == px[c][k]}
        trades += len(core.step(acct, live, eq[k], mids))
        curve[k - a] = acct["equity"]
    daily = curve[::24]
    r = np.diff(daily) / np.maximum(daily[:-1], 1e-9)
    rb = np.diff(btc[a:b:24]) / btc[a:b:24][:-1]
    sd = r.std()
    return {"roi": float(curve[-1] / core.START_EQUITY - 1), "sharpe": float(r.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
            "dd": float(np.max(1 - curve / np.maximum.accumulate(np.maximum(curve, 1e-9)))), "trades": trades,
            "corr": float(np.corrcoef(r, rb)[0, 1]) if sd > 0 else 0.0, "blown": acct["blown"]}

def run_all():
    out_f = os.path.join(C, "steady_results.json")
    out = json.load(open(out_f)) if os.path.exists(out_f) else {}
    btc = price_grid("BTC")
    files = sorted(glob.glob(os.path.join(C, "long", "*.json")))
    for i, f in enumerate(files):
        a = os.path.basename(f)[:-5]
        if a in out: continue
        d = json.load(open(f))
        if not d["complete"]:
            out[a] = {"status": "incomplete"}; continue
        pos = position_grids(d)
        px = {c: price_grid(c) for c in pos}
        p = dict(json.load(open(os.path.join(C, "portfolio", a + ".json"))))["allTime"]["accountValueHistory"]
        av = np.array([[t, float(v)] for t, v in p])
        eq = np.interp(GRID, av[:, 0], av[:, 1])
        out[a] = {"status": "ok", "n_fills": len(d["fills"]) + len(d["twap"]),
                  **{w: simulate(pos, px, eq, lo, hi, btc) for w, (lo, hi) in WIN.items()}}
        if i % 25 == 0:
            print(i, len(files), a[:10], f"all={out[a]['ALL']['roi']:+.0%}", flush=True)
            json.dump(out, open(out_f, "w"))
    json.dump(out, open(out_f, "w"))
    return out

def passes(m):
    return m["roi"] > 0 and m["sharpe"] >= 2 and m["dd"] <= 0.20 and m["trades"] >= 20 and abs(m["corr"]) <= 0.5

def spearman(x, y):
    return float(np.corrcoef(np.argsort(np.argsort(x)), np.argsort(np.argsort(y)))[0, 1])

def report(out):
    ok = {a: r for a, r in out.items() if r["status"] == "ok"}
    print(f"\nwallets simulated: {len(ok)}  (skipped, history too long for the API: {len(out) - len(ok)})")
    print("BTC by quarter:", "  ".join(f"{w} {price_grid('BTC')[hi - 1] / price_grid('BTC')[lo] - 1:+.0%}" for w, (lo, hi) in list(WIN.items())[:4]))
    rng = np.random.default_rng(0)
    for form, test in [("Q1Q2", "Q3"), ("Q2Q3", "Q4")]:
        sel = [r for r in ok.values() if passes(r[form])]
        rest = [r for r in ok.values() if not passes(r[form])]
        act = [r for r in ok.values() if r[form]["trades"] >= 20]
        print(f"\n=== pick on {form} -> copy during {test}")
        for name, g in (("steady picks", sel), ("rest of pool", rest)):
            x = np.array([r[test]["roi"] for r in g])
            print(f"  {name:13s} n={len(g):3d}  mean {x.mean():+6.1%}  median {np.median(x):+6.1%}  profitable {np.mean(x > 0):4.0%}  "
                  f"lost>20% {np.mean(x < -0.2):4.0%}  median maxDD {np.median([r[test]['dd'] for r in g]):4.0%}")
        x, y = np.array([r[test]["roi"] for r in sel]), np.array([r[test]["roi"] for r in rest])
        dif = [rng.choice(x, len(x)).mean() - rng.choice(y, len(y)).mean() for _ in range(5000)]
        print(f"  picks minus rest: {x.mean() - y.mean():+.1%}  (90% range {np.percentile(dif, 5):+.1%} .. {np.percentile(dif, 95):+.1%})")
        print(f"  rank correlation, formation Sharpe vs next ROI (active wallets, n={len(act)}): "
              f"{spearman([r[form]['sharpe'] for r in act], [r[test]['roi'] for r in act]):+.2f}")
    final = sorted([(a, r) for a, r in ok.items() if passes(r["Q3Q4"])], key=lambda x: -x[1]["ALL"]["sharpe"])
    both = [(a, r) for a, r in final if passes(r["Q1Q2"])]
    print(f"\n=== steady over the latest ~103 days (Q3Q4): {len(final)}   also steady in the first ~103 days (Q1Q2): {len(both)}")
    print("  wallet                                      7mo ROI Sharpe maxDD trades BTCcorr   Q1    Q2    Q3    Q4  both")
    for a, r in final:
        m = r["ALL"]
        print(f"  {a}  {m['roi']:+7.0%} {m['sharpe']:6.1f} {m['dd']:5.0%} {m['trades']:6d} {m['corr']:+7.2f} "
              + " ".join(f"{r[q]['roi']:+5.0%}" for q in ("Q1", "Q2", "Q3", "Q4")) + ("   yes" if passes(r["Q1Q2"]) else ""))
    json.dump({"final": [a for a, _ in final], "both": [a for a, _ in both]}, open(os.path.join(C, "steady_wallets.json"), "w"))

if __name__ == "__main__":
    report(run_all())
