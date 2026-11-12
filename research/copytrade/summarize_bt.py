import json, os, numpy as np
D = os.path.dirname(os.path.abspath(__file__))
r = json.load(open(os.path.join(D, "cache", "bt_results.json")))
btc = json.load(open(os.path.join(D, "cache", "candles", "BTC.json")))
from copysim import T0, T1
b0 = next(c[1] for c in btc if c[0] >= T0); b1 = [c[2] for c in btc if c[0] <= T1][-1]
print(f"Backtest of the live copier, 2026-09-22 .. 2026-11-12 (51 days), $10k per wallet, 30-min checks.")
print(f"BTC over the same window: {b1 / b0 - 1:+.1%}\n")
print(f"{'group':12s} {'n':>3s} {'excl':>4s} {'mean':>8s} {'median':>8s} {'profit%':>7s} {'blown':>5s} {'med maxDD':>9s}")
for g, xs in r.items():
    ok = [x for x in xs if x["status"] == "ok" and x["pos_mismatch"] <= 0.25 * max(x["pos_checks"], 1)]
    roi = np.array([x["roi"] for x in ok])
    print(f"{g:12s} {len(ok):3d} {len(xs) - len(ok):4d} {roi.mean():+8.1%} {np.median(roi):+8.1%} {np.mean(roi > 0):7.0%} "
          f"{sum(x['blown'] for x in ok):5d} {np.median([x['max_dd'] for x in ok]):9.0%}")
    globals()[g] = roi
print("\n'mean' = one portfolio splitting money equally across the group's wallets.")
rng = np.random.default_rng(0)
for a, b in [("wf_picks", "wf_control"), ("now_picks", "now_control")]:
    x, y = globals()[a], globals()[b]
    diffs = [rng.choice(x, len(x)).mean() - rng.choice(y, len(y)).mean() for _ in range(5000)]
    lo, hi = np.percentile(diffs, [5, 95])
    print(f"{a} minus {b}: {x.mean() - y.mean():+.1%}  (90% bootstrap range {lo:+.1%} .. {hi:+.1%})")
