"""How do the four-quarter winners trade? Round trips, holding times, leverage, scaling style, order type."""
import json, os, sys, time
import numpy as np
import steady
from copysim import post
C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
W = json.load(open(os.path.join(C, "win4.json")))
sgn = lambda x: 1 if x["side"] == "B" else -1

def trips(fills):
    """Round trips per coin with average-cost accounting. A sign flip closes one trip and opens another."""
    out, open_ = [], {}
    for x in fills:
        c, q, px, sp = x["coin"], sgn(x) * float(x["sz"]), float(x["px"]), float(x["startPosition"])
        t = open_.get(c)
        if t is None or abs(t["pos"] - sp) > 1e-6 * max(1, abs(sp)):   # (re)sync to exchange position
            t = open_[c] = {"coin": c, "start": x["time"], "pos": sp, "avg": px, "pnl": 0.0, "adds": 0, "down": 0, "up": 0,
                            "side": np.sign(sp) or np.sign(q), "peak": abs(sp) * px, "fills": 0}
        t["fills"] += 1
        if t["pos"] == 0 or np.sign(q) == np.sign(t["pos"]):          # opening / adding
            if t["pos"] != 0:
                t["adds"] += 1
                worse = (px < t["avg"]) if t["pos"] > 0 else (px > t["avg"])
                t["down" if worse else "up"] += 1
            else:
                t["side"], t["start"] = np.sign(q), x["time"]
            t["avg"] = (t["avg"] * abs(t["pos"]) + px * abs(q)) / (abs(t["pos"]) + abs(q))
            t["pos"] += q
        else:                                                            # reducing / closing / flipping
            red = min(abs(q), abs(t["pos"]))
            t["pnl"] += red * (px - t["avg"]) * np.sign(t["pos"])
            rest = abs(q) - red
            t["pos"] += np.sign(q) * red
            if abs(t["pos"]) < 1e-9:
                out.append({**t, "end": x["time"]})
                open_[c] = {"coin": c, "start": x["time"], "pos": np.sign(q) * rest, "avg": px, "pnl": 0.0, "adds": 0, "down": 0,
                            "up": 0, "side": np.sign(q), "peak": rest * px, "fills": 0}
                if rest == 0: del open_[c]
        if c in open_:
            open_[c]["peak"] = max(open_[c]["peak"], abs(open_[c]["pos"]) * px)
    return out, list(open_.values())

def fmt_h(h):
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"

for a in W:
    d = json.load(open(os.path.join(C, "long", a + ".json")))
    seen, fl = set(), []
    for x in sorted(d["fills"] + d["twap"], key=lambda x: x["time"]):
        if steady.main_dex(x["coin"]) and (x["tid"], x["time"]) not in seen:
            seen.add((x["tid"], x["time"])); fl.append(x)
    done, still = trips(fl)
    # leverage + time in market from the hourly grids
    pos = steady.position_grids(d); px = {c: steady.price_grid(c) for c in pos}
    av = dict(json.load(open(os.path.join(C, "portfolio", a + ".json"))))["allTime"]["accountValueHistory"]
    av = np.array([[t, float(v)] for t, v in av]); eq = np.interp(steady.GRID, av[:, 0], av[:, 1])
    gross = np.nansum([np.abs(pos[c] * px[c]) for c in pos], axis=0)
    net = np.nansum([pos[c] * px[c] for c in pos], axis=0)
    inm = gross > 0
    lev = gross[inm] / np.maximum(eq[inm], 1)
    # notional by coin and side
    notional = {}
    for x in fl:
        notional[x["coin"]] = notional.get(x["coin"], 0) + float(x["sz"]) * float(x["px"])
    tot = sum(notional.values())
    top = sorted(notional.items(), key=lambda kv: -kv[1])[:5]
    hold = np.array([(t["end"] - t["start"]) / 3600_000 for t in done])
    pnl = np.array([t["pnl"] for t in done])
    longs = [t for t in done if t["side"] > 0]
    adds = sum(t["adds"] for t in done); down = sum(t["down"] for t in done)
    # order style from the most recent fills (maker = resting limit order, taker = crossed the spread)
    rec = post({"type": "userFills", "user": a}) or []; time.sleep(1)
    maker = np.mean([not x["crossed"] for x in rec]) if rec else float("nan")
    twap_share = len(d["twap"]) / max(len(fl), 1)
    print(f"\n=== {a}")
    print(f"  equity now ~${eq[-1]:,.0f} | fills {len(fl)} ({twap_share:.0%} TWAP slices) | maker share of recent fills {maker:.0%}")
    print(f"  markets: " + ", ".join(f"{c} {v / tot:.0%}" for c, v in top) + f"  ({len(notional)} coins total)")
    print(f"  in a position {inm.mean():.0%} of hours | gross leverage median {np.median(lev):.1f}x, 95th pct {np.percentile(lev, 95):.1f}x"
          f" | net long share of exposure {np.mean(net[inm] > 0):.0%} of hours")
    if len(done):
        wins = pnl > 0
        print(f"  {len(done)} closed trips: {len(longs) / len(done):.0%} long | hold median {fmt_h(np.median(hold))}, "
              f"25-75% {fmt_h(np.percentile(hold, 25))}-{fmt_h(np.percentile(hold, 75))}")
        print(f"  win rate {wins.mean():.0%} | avg win ${pnl[wins].mean():,.0f} vs avg loss ${pnl[~wins].mean() if (~wins).any() else 0:,.0f} "
              f"| top 5 trips = {np.sort(pnl)[-5:].sum() / pnl[pnl > 0].sum():.0%} of gross wins")
        print(f"  adds to open trips: {adds} ({down / max(adds, 1):.0%} at a worse price = averaging down) | median fills per trip {np.median([t['fills'] for t in done]):.0f}")
    st = post({"type": "clearinghouseState", "user": a}); time.sleep(1)
    if st:
        ps = sorted(st["assetPositions"], key=lambda p: -abs(float(p["position"]["positionValue"])))
        print("  holding now: " + (", ".join(f"{p['position']['coin']} {'long' if float(p['position']['szi']) > 0 else 'short'} "
              f"${float(p['position']['positionValue']) / 1e3:,.0f}k ({p['position']['leverage']['value']}x)" for p in ps[:5]) or "nothing"))
