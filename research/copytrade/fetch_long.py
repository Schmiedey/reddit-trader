"""Download ~7 months of fills (+TWAP slices, + current positions) for a pool of candidate wallets.

Pool (uses only Apr 21 - Jul 31 info, so later test windows stay clean): wallet existed by Apr 21,
median total equity >= $50k, traded perps and made money on them in that span.
"""
import glob, json, os, sys, time
import numpy as np
from persistence import metrics, ts, WEEK
from copysim import post
D = os.path.dirname(os.path.abspath(__file__)); C = os.path.join(D, "cache")
START = int(ts("2026-04-21"))
OUT = os.path.join(C, "long"); os.makedirs(OUT, exist_ok=True)
KEEP = ("coin", "time", "side", "sz", "px", "startPosition", "tid")

def pool():
    out = []
    series = lambda h: np.array([[t, float(v)] for t, v in h])
    for f in glob.glob(os.path.join(C, "portfolio", "*.json")):
        d = dict(json.load(open(f)))
        if len(d["perpAllTime"]["pnlHistory"]) < 3 or len(d["allTime"]["accountValueHistory"]) < 3: continue
        p, av = series(d["perpAllTime"]["pnlHistory"]), series(d["allTime"]["accountValueHistory"])
        if p[0, 0] > START + WEEK: continue
        m = metrics(p, av, START, ts("2026-07-31"))
        if m["active"] and m["cap"] >= 50_000 and m["pnl"] > 0:
            out.append(os.path.basename(f)[:-5])
    return sorted(out)

def paged(kind, addr, until, unwrap):
    out, start = [], START
    while True:
        r = post({"type": kind, "user": addr, "startTime": start, "endTime": until, **({"aggregateByTime": True} if kind == "userFillsByTime" else {})})
        time.sleep(1.0)
        if r is None: return out, False
        r = [unwrap(x) for x in r]
        out += [{k: x[k] for k in KEEP} for x in r]
        if len(r) < 2000: return out, True
        if len(out) >= 10000: return out, False   # API only serves the latest 10k
        start = max(x["time"] for x in r) + 1

if __name__ == "__main__":
    addrs = json.load(open(sys.argv[sys.argv.index("--list") + 1])) if "--list" in sys.argv else pool()
    print("pool", len(addrs), flush=True)
    if "--count" in sys.argv: sys.exit()
    for i, a in enumerate(addrs):
        f = os.path.join(OUT, a + ".json")
        if os.path.exists(f): continue
        asof = int(time.time() * 1000)
        pf = os.path.join(C, "portfolio", a + ".json")
        if not os.path.exists(pf):
            pr = post({"type": "portfolio", "user": a})
            if pr is None: continue
            json.dump(pr, open(pf, "w"))
        st = post({"type": "clearinghouseState", "user": a})
        fills, ok1 = paged("userFillsByTime", a, asof, lambda x: x)
        twap, ok2 = paged("userTwapSliceFillsByTime", a, asof, lambda x: x["fill"])
        json.dump({"asof": asof, "complete": bool(ok1 and ok2 and st is not None),
                   "pos": {p["position"]["coin"]: float(p["position"]["szi"]) for p in (st or {}).get("assetPositions", [])},
                   "fills": fills, "twap": twap}, open(f, "w"))
        if i % 20 == 0: print(i, a[:10], len(fills), len(twap), flush=True)
    print("finished", flush=True)
