"""Final comparison: account simulations of the leading methods, using the bot's own trade rules
(next-open fills, costs, stops, position limits), against SPY and random picks.

    uv run python -m bot.backtest.final [--since 2025-08]
"""
import argparse
import json
from collections import defaultdict

from ..db import DATA, connect
from ..sources.prices import load_valid_symbols
from .build import bt_connect, load_bars, load_posts
from .research import Study, _sig, fresh
from .sim import Bars, simulate

BASE = {"scoring.buy_threshold": 0.5, "scoring.min_authors": 1, "scoring.exit_threshold": -9,
        "scoring.min_agreeing_sources": 1, "trading.max_positions": 10, "trading.max_new_per_day": 3,
        "trading.position_pct": 0.10, "trading.risk_per_trade_pct": 0.02, "trading.max_ret5": 9}
STOPS = {"std": {"trading.atr_stop_mult": 2.0, "trading.max_stop_pct": 0.12, "trading.reward_risk": 2.0},
         "wide": {"trading.atr_stop_mult": 4.0, "trading.max_stop_pct": 0.25, "trading.reward_risk": 50}}


def conditions(S, t, i, p):
    W, L = S.win(t, i, 5, "W"), S.win(t, i, 5, "L")
    B5, S5 = S.win(t, i, 5, "B"), S.win(t, i, 5, "S")
    b5 = len(B5)
    lossy = L / (W + L) >= 0.65 if W + L else False
    if L >= 2 and lossy:
        yield "loss bounce 2+", L
    if L >= 3 and lossy:
        yield "loss bounce 3+", L
        if p["ret5"] <= -0.10:
            yield "loss bounce 3+ & down 10%", L
    if L >= 2 and lossy and p["ret5"] <= -0.15:
        yield "loss bounce 2+ & down 15%", L
    if L >= 2 and lossy and p["ret5"] <= -0.20:
        yield "loss bounce 2+ & down 20%", L
    if lossy and ((L >= 3 and p["ret5"] <= -0.10) or (L >= 2 and p["ret5"] <= -0.15)):
        yield "loss bounce CAPITULATION (3+ & -10%, or 2+ & -15%)", L - p["ret5"] * 10
    if b5 >= 5 and b5 > len(S5):
        yield "most bought", b5
    if b5 >= 4 and b5 >= 2 * max(len(S.win(t, i, 5, "B", start=5)), 1) and p["ret5"] < 0.10:
        yield "early build-up", b5
    m3 = len(S.win(t, i, 3, "M"))
    if m3 >= 6 and p["ret5"] <= -0.10:
        yield "sell-off bounce (6+ talking, down 10%)", m3
    if len(S.win(t, i, 5, "M")) >= 5:
        yield "any talked-about stock", m3


def run(since=None, seeds=10, window_start=None, stops=("wide",), window_end=None):
    con = bt_connect()
    load_posts(con, load_valid_symbols(connect()), since or "0000")
    B = Bars(load_bars(con, 6))
    S = Study(con, B)
    days = [i for i in range(60, len(S.dates)) if S.valid[i]]
    runs, start = [], days[0]
    for a, b in zip(days, days[1:] + [None]):
        if b != a + 1:
            runs.append((start, a))
            start = b
    seg = max(runs, key=lambda r: r[1] - r[0])
    if window_start:  # second window: does the ranking survive a different start date?
        seg = (max(seg[0], next(i for i, d in enumerate(S.dates) if d >= window_start)), seg[1])
    if window_end:
        seg = (seg[0], min(seg[1], max(i for i, d in enumerate(S.dates) if d <= window_end)))
    sig, tradable = defaultdict(list), defaultdict(list)
    for i in range(seg[0], seg[1] + 1):
        for t in B.b:
            if t == "SPY":
                continue
            p = S.px(t, i)
            if not p:
                continue
            tradable[i].append(t)
            for name, rank in conditions(S, t, i, p):
                sig[name].append((i, t, rank))
    # the two leaders in one account: loss-bounce signals get priority, early build-up fills the rest
    sig["COMBINED: loss bounce 3+ and early build-up"] = (
        [(i, t, r + 100) for i, t, r in sig["loss bounce 3+"]] + sig["early build-up"])
    sig["COMBINED: loss bounce 3+ and most bought"] = (
        [(i, t, r + 100) for i, t, r in sig["loss bounce 3+"]] + sig["most bought"])
    out = {"span": [S.dates[seg[0]], S.dates[seg[1]]], "methods": {}}
    for name, events in sig.items():
        res = {}
        for hold in (5, 10):
            picks = defaultdict(dict)
            for i, t, rank in fresh(events, hold):
                picks[i][t] = rank
            lean = {S.dates[i]: {t: _sig(t, 1 + r / 1000) for t, r in picks[i].items()} for i in range(seg[0], seg[1] + 1)}
            for stop, so in ((k, STOPS[k]) for k in stops):
                over = {**BASE, **so, "trading.max_hold_days": hold - 1}
                m = simulate(lean, B, over, mode="momentum", use_regime=False, use_veto=False)
                key = f"{hold}d {stop}"
                res[key] = {k: m.get(k) for k in ("return", "spy_return", "sharpe", "max_dd", "trades", "win_rate", "avg_trade", "months")}
                if stop == "wide" and (hold == 10 or name.startswith("loss bounce")):
                    full = {S.dates[i]: ({t: _sig(t, 1 + picks[i][t] / 1000 if t in picks[i] else 0.0) for t in tradable[i]}
                                         if picks[i] else {}) for i in range(seg[0], seg[1] + 1)}
                    rnd = sorted(simulate(full, B, over, mode="random", use_regime=False, use_veto=False, seed=s)["return"]
                                 for s in range(seeds))
                    res[key]["random_mean"] = round(sum(rnd) / len(rnd), 4)
                    res[key]["beats_random"] = f"{sum(m['return'] > r for r in rnd)}/{len(rnd)}"
                    big = simulate(lean, B, {**over, "trading.position_pct": 0.20, "trading.risk_per_trade_pct": 0.05,
                                             "trading.max_positions": 5}, mode="momentum", use_regime=False, use_veto=False)
                    res[key]["size20"] = {k: big.get(k) for k in ("return", "max_dd", "months")}
                    res[key]["size20_dd"] = big.get("max_dd")
        out["methods"][name] = res
    # the live recipe: capitulation at double size, optionally with early build-up at normal size
    cap = "loss bounce CAPITULATION (3+ & -10%, or 2+ & -15%)"
    for label, parts in (("LIVE v2: capitulation x2 size", [(cap, 2.0, 100)]),
                         ("LIVE v2: capitulation x2 + early build-up x1", [(cap, 2.0, 100), ("early build-up", 1.0, 0)])):
        picks = defaultdict(dict)
        for name, mult, bonus in reversed(parts):  # later parts are overwritten by higher-priority ones
            for i, t, rank in fresh(sig[name], 10):
                picks[i][t] = (rank + bonus, mult)
        lean = {S.dates[i]: {t: {**_sig(t, 1 + r / 1000), "size_mult": m} for t, (r, m) in picks[i].items()}
                for i in range(seg[0], seg[1] + 1)}
        m = simulate(lean, B, {**BASE, **STOPS["wide"], "trading.max_hold_days": 9}, mode="momentum", use_regime=False, use_veto=False)
        out["methods"][label] = {"10d wide": {k: m.get(k) for k in ("return", "spy_return", "sharpe", "max_dd", "trades", "win_rate", "avg_trade", "months")}}
    (DATA / "backtest" / f"final{'_' + window_start if window_start else ''}.json").write_text(json.dumps(out, indent=1))
    return out


def summarize(out):
    print(f"account simulation {out['span'][0]} .. {out['span'][1]} ($100k, 10% per trade, max 10 positions)")
    print(f"{'method':<40}{'rules':>9}{'return':>8}{'SPY':>7}{'worst drop':>11}{'trades':>7}{'win':>5}{'avg trade':>10}{'avg month':>10}{'mo+':>6}{'worst mo':>9}{'random':>8}{'beats':>7}{'20% size':>9}{'its drop':>9}")
    for name, res in out["methods"].items():
        for key, m in res.items():
            mo = list((m.get("months") or {}).values()) or [0]
            b = m.get("size20") or {}
            print(f"{name:<40}{key:>9}{m['return']:>+8.1%}{m['spy_return']:>+7.1%}{m['max_dd']:>11.1%}{m['trades']:>7}{(m['win_rate'] or 0):>5.0%}"
                  f"{(m['avg_trade'] or 0):>+10.1%}{sum(mo) / len(mo):>+10.2%}{sum(v > 0 for v in mo):>3}/{len(mo):<2}{min(mo):>+9.1%}"
                  f"{m.get('random_mean', float('nan')):>+8.1%}{m.get('beats_random', ''):>7}{b.get('return', float('nan')):>+9.1%}{b.get('max_dd', float('nan')):>8.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--start", default=None, help="also useful: 2025-11-14 for the 12-month window")
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    summarize(run(a.since, window_start=a.start, window_end=a.end))
