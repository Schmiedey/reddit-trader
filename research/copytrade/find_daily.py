"""Find wallets that close trades every day or two with a high win rate and high profit.

Rules (fixed before looking): main-exchange perps, Apr 21 - Nov 12 2026, round trips from public fills.
  active   >= 0.5 closed trips per day overall, and trips in >= 60% of weeks
  pace     median hold <= 3 days
  quality  win rate >= 65%, profit factor >= 1.5, profit >= $50k after counting losses still open
  holds up first half (Apr 21-Aug 15) and second half (Aug 16-Nov 12) each: win rate >= 60% and profit > 0
"""
import glob, json, os, sys
import numpy as np
from profiles import trips
import steady
C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
T0, MID, T1 = steady.G0, int(steady.ts("2026-08-16")), steady.G1
DAYS = (T1 - T0) / 86400_000

def stats(done):
    if not done:
        return None
    p = np.array([t["pnl"] for t in done])
    w = p > 0
    return {"n": len(done), "win": float(w.mean()), "pnl": float(p.sum()),
            "pf": float(p[w].sum() / -p[~w].sum()) if (~w).any() and p[~w].sum() < 0 else 99.0,
            "worst": float(p.min()), "best": float(p.max())}

def screen(addr):
    d = json.load(open(os.path.join(C, "long", addr + ".json")))
    if not d.get("complete"):
        return None
    seen, fl = set(), []
    for x in sorted(d["fills"] + d["twap"], key=lambda x: x["time"]):
        if steady.main_dex(x["coin"]) and (x["tid"], x["time"]) not in seen and T0 <= x["time"] <= T1:
            seen.add((x["tid"], x["time"])); fl.append(x)
    done, still = trips(fl)
    done = [t for t in done if t["start"] >= T0]
    if not done:
        return None
    allst = stats(done)
    # positions still open at the end, valued at the last hourly price (losers left open would flatter the win rate)
    open_pnl = 0.0
    for t in still:
        g = steady.price_grid(t["coin"])
        last = g[~np.isnan(g)][-1] if np.any(~np.isnan(g)) else None
        if last is not None and abs(t["pos"]) > 0:
            open_pnl += t["pos"] * (last - t["avg"])
    allst["open_pnl"] = float(open_pnl)
    hold = np.median([(t["end"] - t["start"]) / 3600_000 for t in done])
    weeks = {int((t["end"] - T0) // (7 * 86400_000)) for t in done}
    h1, h2 = stats([t for t in done if t["end"] < MID]), stats([t for t in done if t["end"] >= MID])
    eq = dict(json.load(open(os.path.join(C, "portfolio", addr + ".json"))))["allTime"]["accountValueHistory"]
    eqs = [float(v) for t, v in eq if T0 <= t <= T1] or [0.0]
    return {"addr": addr, **allst, "per_day": allst["n"] / DAYS, "weeks_active": len(weeks) / (DAYS / 7),
            "hold_h": float(hold), "h1": h1, "h2": h2, "eq_med": float(np.median(eqs)),
            "down_adds": sum(t["down"] for t in done) / max(sum(t["adds"] for t in done), 1)}

def passes(r):
    if not r: return False
    ok = (r["per_day"] >= 0.5 and r["weeks_active"] >= 0.6 and r["hold_h"] <= 72 and r["win"] >= 0.65
          and r["pnl"] + min(r["open_pnl"], 0) >= 50_000 and r["pf"] >= 1.5)
    halves = all(h and h["win"] >= 0.6 and h["pnl"] > 0 for h in (r["h1"], r["h2"]))
    return ok and halves

if __name__ == "__main__":
    rows = [r for f in sorted(glob.glob(os.path.join(C, "long", "*.json"))) if (r := screen(os.path.basename(f)[:-5]))]
    json.dump(rows, open(os.path.join(C, "daily_screen.json"), "w"))
    act = [r for r in rows if r["per_day"] >= 0.5 and r["weeks_active"] >= 0.6 and r["hold_h"] <= 72]
    hi = [r for r in act if r["win"] >= 0.65 and r["pnl"] >= 50_000 and r["pf"] >= 1.5]
    win = [r for r in rows if passes(r)]
    print(f"screened {len(rows)} | trade daily/bi-daily {len(act)} | + win>=65%, profit>=$50k, PF>=1.5: {len(hi)} | + held up in both halves: {len(win)}")
    print(f"\n{'wallet':44s} {'trips/d':>7s} {'hold':>6s} {'win':>4s} {'profit':>9s} {'PF':>5s} {'worst':>9s} {'open now':>9s} {'equity':>8s} {'avgdown':>7s}  1st half      2nd half")
    for r in sorted(hi, key=lambda r: -r["pnl"]):
        h = lambda x: f"{x['win']:.0%} ${x['pnl']/1e3:+6.0f}k" if x else "—"
        print(f"{r['addr']} {r['per_day']:7.1f} {r['hold_h']:5.0f}h {r['win']:4.0%} ${r['pnl']/1e3:+7.0f}k {min(r['pf'],99):5.1f} ${r['worst']/1e3:+7.0f}k ${r['open_pnl']/1e3:+7.0f}k ${r['eq_med']/1e3:6.0f}k {r['down_adds']:7.0%}  {h(r['h1']):13s} {h(r['h2'])}{'  PASS' if passes(r) else ''}")
