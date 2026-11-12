"""Pick wallets to copy using ONLY portfolio data before 2026-09-22 (the copy test window starts then)."""
import json, random, os
import numpy as np
from persistence import load, metrics, ts, WEEK
D = os.path.dirname(os.path.abspath(__file__))
F0, F1 = ts("2026-03-22"), ts("2026-09-22")
N = 25
rows = []
for a, (p, av) in load().items():
    if p[0, 0] > F0 + WEEK:
        continue
    m = metrics(p, av, F0, F1)
    if m["active"] and m["cap"] >= 50_000:
        rows.append((a, m))
print("eligible (cap>=50k, active in formation):", len(rows))
top_pnl = sorted(rows, key=lambda r: -r[1]["pnl"])[:N]
qual = sorted([r for r in rows if r[1]["pnl"] > 0 and r[1]["winwk"] >= 0.6 and r[1]["dd"] < 0.5 * r[1]["pnl"]],
              key=lambda r: -r[1]["sharpe"])[:N]
random.seed(7); ctrl = random.sample(rows, min(N, len(rows)))
g = {k: [[a, m["cap"]] for a, m in v] for k, v in
     {"top_pnl": top_pnl, "quality": qual, "random_control": ctrl}.items()}
for k, v in g.items():
    print(k, len(v))
json.dump(g, open(os.path.join(D, "cache", "groups.json"), "w"))
