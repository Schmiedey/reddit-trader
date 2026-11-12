"""Apply the live pick rule (select_live.py) as of an earlier cutoff, using only data before it.
usage: select_asof.py <cutoff YYYY-MM-DD> <out.json>"""
import glob, json, os, random, sys
import numpy as np
from persistence import metrics, ts, WEEK
from copysim import post
import datetime as dt
D = os.path.dirname(os.path.abspath(__file__)); C = os.path.join(D, "cache")
cut = dt.date.fromisoformat(sys.argv[1])
P = [(cut - dt.timedelta(days=91 * k), cut - dt.timedelta(days=91 * (k - 1))) for k in (3, 2, 1)]
P = [(a.isoformat(), b.isoformat()) for a, b in P]
MIN_CAP, N_CONTROL, MIN_PER_DAY, MAX_PER_DAY = 50_000, 30, 0.2, 200
print("periods:", P)
series = lambda h: np.array([[t, float(v)] for t, v in h])
eligible, strong = [], []
for f in glob.glob(os.path.join(C, "portfolio", "*.json")):
    d = dict(json.load(open(f)))
    if len(d["perpAllTime"]["pnlHistory"]) < 3 or len(d["allTime"]["accountValueHistory"]) < 3: continue
    p, av = series(d["perpAllTime"]["pnlHistory"]), series(d["allTime"]["accountValueHistory"])
    if p[0, 0] > ts(P[0][0]) + WEEK: continue
    ms = [metrics(p, av, ts(a), ts(b)) for a, b in P]
    if ms[-1]["cap"] < MIN_CAP or not ms[-1]["active"]: continue
    a = os.path.basename(f)[:-5]; eligible.append(a)
    if all(m["active"] and m["pnl"] > 0 and m["dd"] < m["cap"] for m in ms):
        strong.append((a, min(m["roi"] for m in ms)))
print(f"eligible {len(eligible)}  pass performance filter {len(strong)}")
# trading-pace filter on the 30 days BEFORE the cutoff (fills cached separately)
os.makedirs(os.path.join(C, "fills_pre"), exist_ok=True)
c1 = int(ts(cut.isoformat())); c0 = c1 - 30 * 86400_000
picks = []
for a, _ in strong:
    f = os.path.join(C, "fills_pre", f"{a}_{cut}.json")
    if not os.path.exists(f):
        got, start = [], c0
        while True:
            r = post({"type": "userFillsByTime", "user": a, "startTime": start, "endTime": c1, "aggregateByTime": True}) or []
            got += r
            if len(r) < 2000 or len(got) >= 10000: break
            start = max(x["time"] for x in r) + 1
        json.dump(got, open(f, "w"))
    n = len([x for x in json.load(open(f)) if not x["coin"].startswith("@")])
    if MIN_PER_DAY <= n / 30 <= MAX_PER_DAY: picks.append(a)
random.seed(11)
pool = sorted(set(eligible) - {a for a, _ in strong})
control = random.sample(pool, min(N_CONTROL, len(pool)))
json.dump({"picks": picks, "control": control}, open(sys.argv[2], "w"), indent=1)
print(f"picks {len(picks)}  control {len(control)}")
