"""Backtest the live paper copier (bot/copytrade/core.py) on Sep 22 - Nov 12 2026, 30-min steps.

Wallet positions at each step are rebuilt backwards from the wallet's CURRENT positions minus every
fill after that time (exact as long as the fill history isn't truncated). Prices = 15m candle opens.
Wallet equity = interpolated total account value from its public PnL history.

usage: backtest_live.py <groups.json> <out.json>     groups.json = {"group": [addr, ...], ...}
"""
import bisect, glob, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from bot.copytrade import core
import copysim
from copysim import post, T0, T1, C

STEP = 30 * 60_000
NOW_DIR = os.path.join(C, "now_state"); os.makedirs(NOW_DIR, exist_ok=True)

def main_dex(coin):
    return not coin.startswith("@") and "/" not in coin and ":" not in coin

def now_state(addr):
    """Current positions + all fills after T1 (cached once; 'asof' is when it was taken)."""
    f = os.path.join(NOW_DIR, addr + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    asof = int(time.time() * 1000)
    st = post({"type": "clearinghouseState", "user": addr})
    tail = post({"type": "userFillsByTime", "user": addr, "startTime": T1 + 1, "endTime": asof, "aggregateByTime": True}) or []
    time.sleep(1)
    d = {"asof": asof, "ok": st is not None,
         "pos": {p["position"]["coin"]: float(p["position"]["szi"]) for p in (st or {}).get("assetPositions", [])},
         "tail": tail, "tail_full": len(tail) < 2000}
    json.dump(d, open(f, "w")); return d

def twap_fills(addr, until):
    """TWAP order slices are NOT included in userFills; they come from a separate endpoint."""
    f = os.path.join(C, "twap", addr + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    os.makedirs(os.path.dirname(f), exist_ok=True)
    out, start, full = [], T0, True
    while True:
        r = post({"type": "userTwapSliceFillsByTime", "user": addr, "startTime": start, "endTime": until})
        time.sleep(1)
        if r is None:
            full = False; break
        out += [x["fill"] for x in r]
        if len(r) < 2000: break
        start = max(x["fill"]["time"] for x in r) + 1
        if len(out) >= 10000: full = False; break
    d = {"fills": out, "full": full}
    json.dump(d, open(f, "w")); return d

def equity_series(addr):
    d = dict(json.load(open(os.path.join(C, "portfolio", addr + ".json"))))
    av = np.array([[t, float(v)] for t, v in d["allTime"]["accountValueHistory"]])
    return lambda t: float(np.interp(t, av[:, 0], av[:, 1]))

_candles = {}
def prices(coin):
    if coin not in _candles:
        cs = copysim.candles(coin)
        _candles[coin] = ([c[0] for c in cs], cs)
    return _candles[coin]

def px(coin, t):
    ts, cs = prices(coin)
    if not cs: return None
    i = bisect.bisect_left(ts, t)
    return cs[i][1] if i < len(cs) else cs[-1][2]

def run_wallet(addr):
    fr, ns = copysim.fills(addr), now_state(addr)
    tw = twap_fills(addr, ns["asof"])
    if fr["truncated"] or not ns["ok"] or not ns["tail_full"] or not tw["full"]:
        return {"addr": addr, "status": "excluded (fill history incomplete)"}
    uniq = {}
    for x in fr["fills"] + ns["tail"] + tw["fills"]:
        if main_dex(x["coin"]) and x["time"] > T0 - 1:
            uniq[(x["tid"], x["coin"], x["time"])] = x
    fills = sorted(uniq.values(), key=lambda x: x["time"])
    sign = lambda x: (1 if x["side"] == "B" else -1) * float(x["sz"])
    # position at T0 = current - every fill after T0
    pos = {c: s for c, s in ns["pos"].items() if main_dex(c)}
    for x in fills:
        if x["time"] > T0:
            pos[x["coin"]] = pos.get(x["coin"], 0.0) - sign(x)
    # sanity check: rebuilt position just before each coin's first fill should equal that fill's startPosition
    seen, checks, bad = set(), 0, 0
    run = dict(pos)
    for x in fills:
        if x["time"] <= T0: continue
        if x["coin"] not in seen:
            seen.add(x["coin"]); checks += 1
            sp = float(x["startPosition"]); rb = run.get(x["coin"], 0.0)
            bad += abs(sp - rb) > 1e-6 + 0.01 * abs(sp)
        run[x["coin"]] = run.get(x["coin"], 0.0) + sign(x)
    eq = equity_series(addr)
    acct, i, n_trades, curve = core.new_account(), 0, 0, []
    fills = [x for x in fills if x["time"] > T0]
    for t in range(T0, T1 + 1, STEP):
        while i < len(fills) and fills[i]["time"] <= t:
            x = fills[i]; pos[x["coin"]] = pos.get(x["coin"], 0.0) + sign(x); i += 1
        live = {c: s for c, s in pos.items() if abs(s) > 1e-9}
        mids = {}
        for c in set(live) | set(acct["pos"]):
            p = px(c, t)
            if p: mids[c] = p
        n_trades += len(core.step(acct, live, eq(t), mids))
        curve.append(acct["equity"])
    curve = np.array(curve)
    dd = float(np.max(1 - curve / np.maximum.accumulate(np.maximum(curve, 1e-9))))
    return {"addr": addr, "status": "ok", "roi": curve[-1] / core.START_EQUITY - 1, "max_dd": dd,
            "trades": n_trades, "blown": acct["blown"], "pos_checks": checks, "pos_mismatch": bad,
            "curve_daily": curve[::48].round(2).tolist()}

if __name__ == "__main__":
    groups = json.load(open(sys.argv[1])); out = {}
    for g, addrs in groups.items():
        out[g] = []
        for a in addrs:
            r = run_wallet(a); out[g].append(r)
            print(g, a[:10], r["status"], *(f"roi={r['roi']:+.1%} dd={r['max_dd']:.0%} trades={r['trades']} mism={r['pos_mismatch']}/{r['pos_checks']}",) if r["status"] == "ok" else (), flush=True)
        json.dump(out, open(sys.argv[2], "w"))
