"""Choose wallets for the live paper test (bot/copytrade). Writes wallets.json.

picks   = perp-profitable in each of the last three 3-month periods, never drew down more than
          its typical account value in any of them, and trades at a copyable pace.
control = random wallets from the same eligible pool (same size/age/activity rules, no performance rule).
usage: select_live.py [--dry]   (--dry skips the fills download and writes nothing)
"""
import glob, json, os, random, sys
import numpy as np
from persistence import metrics, ts, WEEK

D = os.path.dirname(os.path.abspath(__file__))
PERIODS = [("2026-02-15", "2026-05-15"), ("2026-05-15", "2026-08-15"), ("2026-08-15", "2026-11-12")]
MIN_CAP, N_CONTROL = 50_000, 30
MIN_FILLS, MIN_PER_DAY, MAX_PER_DAY = 20, 0.2, 200   # skip dormant wallets and market-maker bots

def series(h):
    return np.array([[t, float(v)] for t, v in h])

eligible, strong = [], []
for f in glob.glob(os.path.join(D, "cache", "portfolio", "*.json")):
    d = dict(json.load(open(f)))
    if len(d["perpAllTime"]["pnlHistory"]) < 3 or len(d["allTime"]["accountValueHistory"]) < 3:
        continue
    p, av = series(d["perpAllTime"]["pnlHistory"]), series(d["allTime"]["accountValueHistory"])  # total equity incl. spot
    if p[0, 0] > ts(PERIODS[0][0]) + WEEK:
        continue
    ms = [metrics(p, av, ts(a), ts(b)) for a, b in PERIODS]
    if ms[-1]["cap"] < MIN_CAP or not ms[-1]["active"]:
        continue
    addr = os.path.basename(f)[:-5]
    eligible.append(addr)
    if all(m["active"] and m["pnl"] > 0 and m["dd"] < m["cap"] for m in ms):
        strong.append((addr, min(m["roi"] for m in ms)))
print(f"eligible pool: {len(eligible)}   pass performance filter: {len(strong)}")
if "--dry" in sys.argv:
    sys.exit()

import copysim
picks = []
days = (copysim.T1 - copysim.T0) / 86400_000
for addr, worst_roi in sorted(strong, key=lambda x: -x[1]):
    n = len([x for x in copysim.fills(addr)["fills"] if not x["coin"].startswith("@")])
    ok = n >= MIN_FILLS and MIN_PER_DAY <= n / days <= MAX_PER_DAY
    print(f"  {addr}  worst 3-month ROI {worst_roi:+.0%}  fills/day {n / days:6.1f}  {'PICK' if ok else 'skip'}")
    if ok:
        picks.append(addr)
random.seed(11)
pool = sorted(set(eligible) - {a for a, _ in strong})
control = random.sample(pool, min(N_CONTROL, len(pool)))
json.dump({"picks": picks, "control": control}, open(os.path.join(D, "wallets.json"), "w"), indent=1)
print(f"picks: {len(picks)}   control: {len(control)}   -> wallets.json")
