import json, os, numpy as np
D = os.path.dirname(os.path.abspath(__file__))
r = json.load(open(os.path.join(D, "cache", "copy_results.json")))
keys = ["their_roi", "copy_roi_0m", "copy_roi_15m", "copy_roi_60m", "copy_roi_240m"]
print("Copy window 2026-09-22 .. 2026-11-12 (51 days). ROI on the wallet's formation-period capital. Fees 0.045%+0.05% slippage/side.\n")
print(f"{'group':16s} {'n':>3s} {'fills/day med':>13s}  " + "  ".join(f"{k.replace('copy_roi_','copy@'):>14s}" for k in keys))
for g, xs in r.items():
    xs = [x for x in xs if x.get("nfills", 0) > 0]
    if not xs: continue
    line = f"{g:16s} {len(xs):3d} {np.median([x['fills_per_day'] for x in xs]):13.1f}  "
    for k in keys:
        v = np.clip([x[k] for x in xs], -1, None)  # can't lose more than allocated
        line += f"  {np.mean(v):+6.1%}/{np.mean(v > 0):4.0%}  "
    print(line)
print("\n(cells: equal-weight mean ROI / share of wallets profitable)\n")
print("Per-wallet, quality + top_pnl groups:")
seen = set()
for g in ("quality", "top_pnl"):
    for x in sorted(r.get(g, []), key=lambda x: -x.get("copy_roi_60m", -9)):
        if x.get("nfills", 0) == 0 or x["addr"] in seen: continue
        seen.add(x["addr"])
        print(f"  {g:8s} {x['addr']}  cap=${x['cap']/1e3:8.0f}k fills/d={x['fills_per_day']:7.1f} trunc={str(x['truncated'])[0]} "
              + " ".join(f"{k.replace('copy_roi_','@').replace('their_roi','them')}={x[k]:+.0%}" for k in keys))
