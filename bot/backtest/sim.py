"""Stage B of the backtest: trade the stored daily signals with the live bot's rules.

Uses the same functions as bot/trader/run_daily.py (pump guard, entry vetoes, market regime,
plan_entries / plan_exits, ATR stops, risk sizing) and the paper broker's fill rules: orders
fill at the next open plus slippage, stops/targets are checked against each day's high/low
(stop first if both), gaps through the stop fill at the open, stop moves to breakeven at +1R.
"""
import bisect
import copy
import json
import math
import random
from collections import defaultdict

from ..config import CFG
from ..filter.pump_guard import check
from ..signals.strategy import apply_strategy
from ..signals.technicals import entry_veto, market_regime, technicals
from ..trader.risk import plan_entries, plan_exits, position_size, stop_and_target

BT_DEFAULTS = {"min_agreeing_sources": 1}  # history is Reddit-only, so "2 sources agree" can't happen


def load_signals(con, variant):
    out = defaultdict(dict)
    for day, t, sig in con.execute("SELECT day, ticker, sig FROM bt_signals WHERE variant=?", (variant,)):
        s = json.loads(sig)
        s["ticker"] = t
        out[day][t] = s
    return dict(out)


def merged_cfg(over=None):
    cfg = copy.deepcopy(CFG)
    cfg["scoring"].update(BT_DEFAULTS)
    for dotted, v in (over or {}).items():
        d = cfg
        ks = dotted.split(".")
        for k in ks[:-1]:
            d = d.setdefault(k, {})
        d[ks[-1]] = v
    return cfg


class Bars:
    def __init__(self, bars):
        self.b = bars
        self.dates = {s: [x["d"] for x in v] for s, v in bars.items()}

    def upto(self, sym, day, n=60):
        """Last n bars on or before day (what the bot could see that evening)."""
        ds = self.dates.get(sym)
        if not ds:
            return None
        j = bisect.bisect_right(ds, day)
        return self.b[sym][max(0, j - n):j] if j else None

    def on(self, sym, day):
        ds = self.dates.get(sym)
        if not ds:
            return None
        j = bisect.bisect_left(ds, day)
        return self.b[sym][j] if j < len(ds) and ds[j] == day else None


def rescore(s, weights, mode):
    return apply_strategy(s, weights, mode)


def simulate(signals, bars, over=None, start=None, end=None, mode=None, weights=None,
             use_regime=True, use_veto=None, use_pump=True, seed=0):
    """mode/weights/use_veto default to the live config (scoring.strategy, trading.use_entry_vetoes)."""
    cfg = merged_cfg(over)
    tc, sc, fc, pc = cfg["trading"], cfg["scoring"], cfg["filter"], cfg["paper"]
    if mode is None:
        mode, weights = sc.get("strategy", "momentum"), weights or sc.get("strategy_weights")
    if use_veto is None:
        use_veto = tc.get("use_entry_vetoes", True)
    rng = random.Random(seed)
    B = bars if isinstance(bars, Bars) else Bars(bars)
    days = [d for d in B.dates["SPY"] if d in signals and (not start or d >= start) and (not end or d <= end)]
    di = {d: i for i, d in enumerate(days)}
    cash, pos, pending, trades, curve = pc["starting_cash"], {}, [], [], []
    slip = pc["slippage_pct"]
    for day in days:
        # 1. fill yesterday's orders at today's open (sells first, like the broker's queue order)
        for o in sorted(pending, key=lambda o: o["side"] != "SELL"):
            b = B.on(o["sym"], day)
            if b is None:
                continue
            if o["side"] == "SELL":
                p = pos.pop(o["sym"], None)
                if p:
                    px = b["o"] * (1 - slip)
                    cash += p["qty"] * px
                    trades.append((p, day, px, o["why"]))
            elif o["sym"] not in pos:
                px = b["o"] * (1 + slip)
                qty = min(o["qty"], int(cash // px))
                if qty >= 1:
                    cash -= qty * px
                    pos[o["sym"]] = {"sym": o["sym"], "qty": qty, "entry": px, "entry_day": day,
                                     "stop": px * (1 - o["stop"]), "target": px * (1 + o["take"])}
        pending = []
        # 2. stops and targets on today's bar
        for sym in list(pos):
            p, b = pos[sym], B.on(sym, day)
            if b is None:
                continue
            gap = day > p["entry_day"]
            if b["l"] <= p["stop"]:
                px = min(b["o"], p["stop"]) if gap else p["stop"]
                why = "stop" if p["stop"] < p["entry"] else "breakeven"
            elif b["h"] >= p["target"]:
                px, why = (max(b["o"], p["target"]) if gap else p["target"]), "target"
            else:
                if p["stop"] < p["entry"] and b["c"] >= 2 * p["entry"] - p["stop"]:
                    p["stop"] = p["entry"]
                continue
            cash += p["qty"] * px
            trades.append((pos.pop(sym), day, px, why))
        # 3. evening: score, filter, decide
        spy = B.upto("SPY", day)
        regime = market_regime(spy) if use_regime else "risk-on"
        sigs = {}
        for t, s in signals[day].items():
            s = rescore(s, weights, mode)
            s["thin"] = s["voices"] < sc["min_authors"]
            if s["thin"]:
                continue
            tb = B.upto(t, day)
            if tb is None or tb[-1]["d"] < day:  # no price today (delisted / not downloaded)
                continue
            reason = check(s, tb, fc) if use_pump else (None if tb[-1]["c"] >= fc["min_price"] else "price")
            if reason:
                continue
            tech = technicals(tb)
            s["tech"] = tech
            s["veto"] = entry_veto(tech, tc) if (use_veto and s["score"] > 0) else (None if tech else "no history")
            sigs[t] = s
        held_days = {sym: di[day] - di[p["entry_day"]] for sym, p in pos.items()}
        exit_sigs = {t: sigs.get(t) or rescore(signals[day][t], weights, mode) for t in pos if t in signals[day]}
        for sym, why in plan_exits(held_days, exit_sigs, tc, sc):
            pending.append({"sym": sym, "side": "SELL", "why": why})
        equity = cash + sum(p["qty"] * (B.upto(s_, day, 1) or [{"c": p["entry"]}])[-1]["c"] for s_, p in pos.items())
        picks = plan_entries(sigs, set(pos), tc, sc, regime)
        if mode == "random" and picks:  # same number of buys, random tradable names: is the score adding anything?
            pool = [s for s in sigs.values() if s["ticker"] not in pos and not s.get("veto")]
            picks = rng.sample(pool, min(len(picks), len(pool)))
        for s in picks:
            price = B.upto(s["ticker"], day, 1)[-1]["c"]
            stop_pct, take_pct = stop_and_target((s["tech"] or {}).get("atr_pct"), tc)
            m = s.get("size_mult", 1.0)  # stronger signals may get a bigger position
            qty = position_size(equity, price, tc["position_pct"] * m, stop_pct, tc["risk_per_trade_pct"] * m)
            if qty >= 1:
                pending.append({"sym": s["ticker"], "side": "BUY", "qty": qty, "stop": stop_pct, "take": take_pct})
        curve.append((day, equity, spy[-1]["c"], sum(1 for _ in pos)))
    return metrics(curve, trades, B)


def metrics(curve, trades, B):
    if len(curve) < 2:
        return {"days": len(curve), "trades": 0}
    eq = [c[1] for c in curve]
    spy = [c[2] for c in curve]
    rets = [b / a - 1 for a, b in zip(eq, eq[1:])]
    n = len(rets)
    mu = sum(rets) / n
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(n - 1, 1))
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    tr = [(px / p["entry"] - 1, why) for p, _, px, why in trades]
    years = n / 252
    total = eq[-1] / eq[0] - 1
    spy_total = spy[-1] / spy[0] - 1
    return {
        "start": curve[0][0], "end": curve[-1][0], "days": len(curve),
        "return": round(total, 4), "spy_return": round(spy_total, 4), "excess": round(total - spy_total, 4),
        "cagr": round((1 + total) ** (1 / years) - 1, 4) if years > 0 and total > -1 else None,
        "sharpe": round(mu / sd * math.sqrt(252), 2) if sd else 0.0,
        "max_dd": round(mdd, 4), "trades": len(tr),
        "win_rate": round(sum(r > 0 for r, _ in tr) / len(tr), 3) if tr else None,
        "avg_trade": round(sum(r for r, _ in tr) / len(tr), 4) if tr else None,
        "exits": {k: sum(1 for _, w in tr if w.startswith(k)) for k in ("stop", "breakeven", "target", "held", "sentiment")},
        "avg_positions": round(sum(c[3] for c in curve) / len(curve), 2),
        "months": _monthly(curve),
    }


def _monthly(curve):
    """{YYYY-MM: return} from the equity curve."""
    out, prev = {}, curve[0][1]
    last = {}
    for day, eq, _, _ in curve:
        last[day[:7]] = eq
    for m in sorted(last):
        out[m] = round(last[m] / prev - 1, 4)
        prev = last[m]
    return out


def signal_study(signals, bars, min_voices=5, horizons=(1, 5, 10), min_price=5, min_vol=1_000_000):
    """Does the score predict returns at all? Buy at next open, hold h days, minus SPY.
    Returns per-quintile mean excess return and hit rate, plus mean daily rank correlation (IC)."""
    B = bars if isinstance(bars, Bars) else Bars(bars)
    spy_d = B.dates["SPY"]
    rows = []
    for day, sigs in signals.items():
        i = bisect.bisect_right(spy_d, day)
        for h in horizons:
            if i + h > len(spy_d):
                continue
        for t, s in sigs.items():
            if s["voices"] < min_voices:
                continue
            tb = B.upto(t, day, 20)
            if not tb or tb[-1]["d"] != day or tb[-1]["c"] < min_price or sum(b["v"] for b in tb) / len(tb) < min_vol:
                continue
            fwd = {}
            for h in horizons:
                if i + h - 1 >= len(spy_d):
                    break
                d_in, d_out = spy_d[i], spy_d[i + h - 1]
                a, b = B.on(t, d_in), B.on(t, d_out)
                sa, sb = B.on("SPY", d_in), B.on("SPY", d_out)
                if a and b:
                    fwd[h] = (b["c"] / a["o"] - 1) - (sb["c"] / sa["o"] - 1)
            if fwd:
                rows.append((day, s["score"], fwd))
    out = {"n": len(rows)}
    for h in horizons:
        rs = sorted((r[1], r[2][h]) for r in rows if h in r[2])
        if len(rs) < 50:
            continue
        q = len(rs) // 5
        buckets = [rs[k * q:(k + 1) * q] if k < 4 else rs[4 * q:] for k in range(5)]
        out[f"h{h}"] = [{"score_min": round(b[0][0], 3), "score_max": round(b[-1][0], 3), "n": len(b),
                         "mean_excess": round(sum(x for _, x in b) / len(b), 4),
                         "hit": round(sum(x > 0 for _, x in b) / len(b), 3)} for b in buckets]
        # daily Spearman IC
        by_day = defaultdict(list)
        for d, sc_, f in rows:
            if h in f:
                by_day[d].append((sc_, f[h]))
        ics = [ic for ic in (_spearman(v) for v in by_day.values() if len(v) >= 5) if ic is not None]
        if ics:
            m = sum(ics) / len(ics)
            sd = math.sqrt(sum((x - m) ** 2 for x in ics) / max(len(ics) - 1, 1))
            out[f"ic{h}"] = {"mean": round(m, 4), "t": round(m / sd * math.sqrt(len(ics)), 2) if sd else None,
                             "days": len(ics)}
    return out


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for k, i in enumerate(order):
        r[i] = k
    return r


def _spearman(pairs):
    a, b = _rank([p[0] for p in pairs]), _rank([p[1] for p in pairs])
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if not va or not vb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)
