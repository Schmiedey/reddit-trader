"""Simulate copying wallets' perp trades with a delay, using public Hyperliquid fills + 15m candles.
usage: copysim.py <addresses.json> <out.json>   (addresses.json = {"group": [addr,...], ...})"""
import json, os, sys, time, bisect, datetime as dt, urllib.request
import numpy as np
D = os.path.dirname(os.path.abspath(__file__)); C = os.path.join(D, "cache")
os.makedirs(os.path.join(C, "fills"), exist_ok=True); os.makedirs(os.path.join(C, "candles"), exist_ok=True)
T0 = int(dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc).timestamp() * 1000)
T1 = int(dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
DELAYS_MIN = [0, 15, 60, 240]
FEE = 0.00045      # taker fee per side
SLIP = 0.0005      # extra slippage per side on top of the delayed candle price

def post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for i in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(4 * (i + 1))
    return None

def fills(addr):
    f = os.path.join(C, "fills", addr + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    out, start, truncated = [], T0, False
    while True:
        d = post({"type": "userFillsByTime", "user": addr, "startTime": start, "endTime": T1, "aggregateByTime": True})
        time.sleep(2)
        if not d:
            break
        out += d
        if len(d) < 2000:
            break
        start = max(x["time"] for x in d) + 1
        if len(out) >= 10000:
            truncated = True; break
    # API only serves the most recent 10k fills: if earliest fill is far after T0 and we got many, history is cut
    res = {"fills": out, "truncated": truncated}
    json.dump(res, open(f, "w")); return res

def candles(coin):
    f = os.path.join(C, "candles", coin.replace(":", "_").replace("/", "_") + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    d = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": T0 - 3600_000, "endTime": T1 + 86400_000}}) or []
    time.sleep(1.5)
    d = [[c["t"], float(c["o"]), float(c["c"])] for c in d]
    json.dump(d, open(f, "w")); return d

def price_at(cs, t):
    """open of first 15m candle starting at/after t (fallback: last close)"""
    ts = [c[0] for c in cs]
    i = bisect.bisect_left(ts, t)
    return cs[i][1] if i < len(cs) else cs[-1][2]

def sim(addr, cap):
    fr = fills(addr)
    fl = [x for x in fr["fills"] if not x["coin"].startswith("@") and "/" not in x["coin"] and T0 <= x["time"] <= T1]
    if not fl:
        return {"addr": addr, "nfills": 0}
    fl.sort(key=lambda x: x["time"])
    bycoin = {}
    for x in fl:
        bycoin.setdefault(x["coin"], []).append(x)
    their = 0.0; copy = {d: 0.0 for d in DELAYS_MIN}; their_fees = 0.0; vol = 0.0; skipped = []
    for coin, xs in bycoin.items():
        cs = candles(coin)
        if len(cs) < 100:
            skipped.append(coin); continue
        q0 = float(xs[0]["startPosition"])
        p_start, p_end = price_at(cs, T0), cs[-1][2] if cs[-1][0] <= T1 else price_at(cs, T1)
        dq = [(x["time"], (1 if x["side"] == "B" else -1) * float(x["sz"]), float(x["px"])) for x in xs]
        q_end = q0 + sum(q for _, q, _ in dq)
        # their realised + unrealised price PnL over window (pre-existing position marked at window start)
        their += -q0 * p_start - sum(q * px for _, q, px in dq) + q_end * p_end
        their_fees += sum(float(x["fee"]) for x in xs)
        vol += sum(abs(q) * px for _, q, px in dq)
        for d in DELAYS_MIN:
            pnl = -q0 * p_start
            for t, q, px in dq:
                p = px if d == 0 else price_at(cs, t + d * 60_000)
                if t + d * 60_000 > T1:
                    p = p_end
                pnl -= q * p + abs(q) * p * (FEE + SLIP)
            pnl += q_end * p_end
            copy[d] += pnl
    days = (T1 - T0) / 86400_000
    return {"addr": addr, "cap": cap, "nfills": len(fl), "fills_per_day": len(fl) / days, "truncated": fr["truncated"],
            "coins": len(bycoin), "skipped": skipped, "turnover_x": vol / cap,
            "their_roi": (their - their_fees) / cap, **{f"copy_roi_{d}m": copy[d] / cap for d in DELAYS_MIN}}

if __name__ == "__main__":
    groups = json.load(open(sys.argv[1]))
    out = {}
    for g, items in groups.items():
        out[g] = []
        for addr, cap in items:
            r = sim(addr, cap); out[g].append(r)
            print(g, addr[:10], {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k not in ("addr", "skipped")}, flush=True)
        json.dump(out, open(sys.argv[2], "w"), indent=1)
