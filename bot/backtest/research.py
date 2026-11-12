"""Research: which WSB-style crowd signals were followed by gains?

For each method: an event study (buy next open, hold 3/5/10 days, minus SPY) and a portfolio
simulation with the bot's real trade rules. Thresholds are fixed in advance (loose/medium/strict);
nothing is tuned on the data, so the whole period is out of sample.

    uv run python -m bot.backtest.research [--since 2025-08]
"""
import argparse
import bisect
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..db import DATA, connect
from ..signals.stance import stance_for
from ..sources.prices import load_valid_symbols
from .build import BARS, bt_connect, load_bars, load_posts
from .sim import Bars, simulate

ET = ZoneInfo("America/New_York")
JUNK = {None, "", "[deleted]", "AutoModerator"}
CORE, PUMP = "wallstreetbets", {"pennystocks"}
LEVELS = {"loose": 0.6, "medium": 1.0, "strict": 1.6}
HORIZONS = (3, 5, 10)
SEEN_FROM = "2026-06-12"  # first results were computed on data from this day on
SIM = {"scoring.buy_threshold": 0.5, "scoring.min_authors": 1, "scoring.exit_threshold": -9,
       "scoring.min_agreeing_sources": 1, "trading.max_positions": 8, "trading.max_new_per_day": 3,
       "trading.position_pct": 0.10, "trading.risk_per_trade_pct": 0.02, "trading.max_ret5": 9,
       "trading.risk_off_max_new": 1, "trading.risk_off_threshold_bump": 0.0,
       "trading.atr_stop_mult": 2.0, "trading.reward_risk": 2.0}


def load_events(con):
    rows = con.execute(
        """SELECT m.post_id, m.ticker, m.author, m.created, m.sentiment, p.community, p.title, p.body,
                  p.score, p.flair FROM mentions m JOIN posts p ON p.id = m.post_id""").fetchall()
    n_t = defaultdict(int)
    for r in rows:
        n_t[r["post_id"]] += 1
    out = []
    for r in rows:
        com = (r["community"] or "").lower()
        if r["author"] in JUNK or com in PUMP or n_t[r["post_id"]] > 5:
            continue
        st, strength = stance_for(f"{r['title']}\n{r['body']}".strip(), r["ticker"], n_t[r["post_id"]],
                                  r["flair"], r["sentiment"] or 0.0)
        kind = "yolo" if "yolo" in (r["flair"] or "").lower() else ("dd" if st == "DD" else "text")
        out.append((r["created"], com, r["author"], r["ticker"], st, strength, r["score"] or 0,
                    len(r["body"] or ""), r["sentiment"] or 0.0, kind))
    return out


class Study:
    def __init__(self, con, B):
        self.B, self.dates = B, B.dates["SPY"]
        cut = [datetime.fromisoformat(d).replace(hour=16, minute=30, tzinfo=ET).timestamp() for d in self.dates]
        self.F = defaultdict(lambda: defaultdict(lambda: {"B": {}, "S": set(), "H": set(), "M": set(), "W": 0, "L": 0,
                                                          "DD": [], "sent": []}))
        core = defaultdict(int)
        self.stance_counts = defaultdict(int)
        for created, com, author, t, st, strength, ups, blen, sent, kind in load_events(con):
            i = bisect.bisect_right(cut, created)
            if i >= len(self.dates):
                continue
            f = self.F[t][i]
            f["M"].add(author)
            f["sent"].append(sent)
            core[i] += com == CORE
            self.stance_counts[st] += 1
            if st in ("BUY", "DD"):
                f["B"][author] = (com, kind)
            elif st == "SELL":
                f["S"].add(author)
            elif st == "HOLD":
                f["H"].add(author)
            elif st == "WON":
                f["W"] += 1
            elif st == "LOST":
                f["L"] += 1
            if st == "DD":
                f["DD"].append((ups, blen))
        counts = sorted(v for v in core.values() if v)
        med = counts[len(counts) // 2] if counts else 1
        # a day is usable when WSB data exists for each of the 10 days before it (no download gap)
        self.valid = [all(core.get(j, 0) > 0 for j in range(i - 9, i + 1))
                      and sum(core.get(j, 0) for j in range(i - 9, i + 1)) >= 2.0 * med for i in range(len(self.dates))]
        self._px = {}

    def px(self, t, i):
        if (t, i) not in self._px:
            tb, out = self.B.upto(t, self.dates[i], 60), None
            if tb and tb[-1]["d"] == self.dates[i] and len(tb) >= 51:
                c = [b["c"] for b in tb]
                vol20 = sum(b["v"] for b in tb[-20:]) / 20
                if c[-1] >= 5 and vol20 >= 1e6:
                    out = {"ret5": c[-1] / c[-6] - 1, "hi20": c[-1] >= max(c[-21:-1]), "volx": tb[-1]["v"] / vol20,
                           "off10": c[-1] / max(b["h"] for b in tb[-10:]) - 1, "sma50": c[-1] > sum(c[-50:]) / 50}
            self._px[(t, i)] = out
        return self._px[(t, i)]

    def win(self, t, i, k, key, start=0):
        """Combine a feature over trading days i-start-k+1 .. i-start."""
        days = self.F.get(t, {})
        acc = None
        for j in range(i - start - k + 1, i - start + 1):
            f = days.get(j)
            if f is None:
                continue
            v = f[key]
            if acc is None:
                acc = (dict(v) if isinstance(v, dict) else set(v) if isinstance(v, set) else list(v) if isinstance(v, list) else v)
            elif isinstance(v, dict):
                acc.update(v)
            elif isinstance(v, set):
                acc |= v
            else:
                acc = acc + v
        return acc if acc is not None else (0 if key in ("W", "L") else {})

    def fwd(self, t, i, h):
        if i + h >= len(self.dates):
            return None
        a, b = self.B.on(t, self.dates[i + 1]), self.B.on(t, self.dates[i + h])
        sa, sb = self.B.on("SPY", self.dates[i + 1]), self.B.on("SPY", self.dates[i + h])
        if not (a and b and sa and sb):
            return None
        raw = b["c"] / a["o"] - 1
        return raw - (sb["c"] / sa["o"] - 1), raw


def methods(S, t, i, p, m, wsum):
    """Yield (name, rank) for every method this ticker satisfies today. m scales the count thresholds."""
    need = lambda x: max(1, round(x * m))
    B5, S5 = S.win(t, i, 5, "B"), S.win(t, i, 5, "S")
    b5, s5 = len(B5), len(S5)
    W, L = S.win(t, i, 5, "W"), S.win(t, i, 5, "L")
    if b5 >= need(5) and b5 > s5:
        yield "1 most bought", b5
        w = wsum(B5)
        if w >= need(5):
            yield "7 proven buyers", w
    if W >= need(3) and W / (W + L) >= 0.65 and b5 >= need(3):
        yield "2 crowd is winning", W
    if b5 >= need(4) and b5 >= 2 * max(len(S.win(t, i, 5, "B", start=5)), 1) and p["ret5"] < 0.10:
        yield "3 early build-up", b5
    if b5 >= need(4) and p["hi20"] and p["volx"] >= 1.5:
        yield "4 buying + breakout", b5
    dd = S.win(t, i, 2, "DD", start=1)  # at least a day old, so its upvotes were visible
    if dd and max(u for u, _ in dd) >= need(100) and max(bl for _, bl in dd) >= 1500:
        yield "5 fresh DD", max(u for u, _ in dd)
    b10 = len(S.win(t, i, 10, "B"))
    if b10 >= need(8) and len(S.win(t, i, 3, "B")) >= 2 and -0.12 <= p["off10"] <= -0.05 and p["sma50"]:
        yield "6 dip in a favorite", b10
    yolo = sum(1 for g in B5.values() if g[1] == "yolo")
    if yolo >= need(3):
        yield "8 YOLO cluster", yolo
    if b5 >= need(5) and b5 > s5 and p["ret5"] < 0.05 and W == 0:
        yield "9 bought, not yet run", b5
    # benchmarks and contrasts
    M5 = S.win(t, i, 5, "M")
    if len(M5) >= need(5):
        yield "b talked about (any stance)", len(M5)
        sent = S.win(t, i, 3, "sent")
        if sent and sum(sent) / len(sent) >= 0.3:
            yield "b positive wording", len(M5)
    if s5 >= need(4) and s5 > b5:
        yield "c crowd is selling", s5
    if L >= need(3) and L / (W + L) >= 0.65:
        yield "c crowd is losing", L
    if p["hi20"] and p["volx"] >= 1.5:
        yield "p price breakout only", p["volx"]
    if -0.12 <= p["off10"] <= -0.05 and p["sma50"]:
        yield "p price dip only", -p["off10"]


def run(since=None, sims=True):
    con = bt_connect()
    load_posts(con, load_valid_symbols(connect()), since or "0000")
    first = con.execute("SELECT MIN(created) FROM mentions").fetchone()[0]
    if BARS.exists():
        import gzip
        spy0 = json.load(gzip.open(BARS)).get("SPY", [{}])[0].get("d", "9")
        if spy0 > (datetime.fromtimestamp(first) - timedelta(days=120)).date().isoformat():
            BARS.unlink()  # history now reaches further back than the cached prices
    B = Bars(load_bars(con))
    S = Study(con, B)
    universe = [t for t in B.b if t != "SPY"]
    days = [i for i in range(60, len(S.dates)) if S.valid[i]]
    # walk-forward track records for method 7: author and group (subreddit + post type) hit rates
    pending, a_rec, g_rec = defaultdict(list), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])

    def wsum(buyers):
        tot = 0.0
        for author, grp in buyers.items():
            g = g_rec[grp]
            p0 = (g[0] + 10) / (g[0] + g[1] + 20)
            a = a_rec[author]
            cred = (a[0] + 4 * p0) / (a[0] + a[1] + 4)
            tot += min(2.0, max(0.3, (cred / 0.5) ** 2))
        return tot

    sig = {lv: defaultdict(list) for lv in LEVELS}
    tradable = defaultdict(list)
    for i in range(60, len(S.dates)):
        for author, grp, hit in pending.pop(i, []):  # outcomes that became known today
            a_rec[author][0 if hit else 1] += 1
            g_rec[grp][0 if hit else 1] += 1
        for t in universe:
            f = S.F.get(t, {}).get(i)
            if f and f["B"]:
                r = S.fwd(t, i, 5)
                if r:
                    for author, grp in f["B"].items():
                        pending[i + 6].append((author, grp, r[0] > 0))
        if not S.valid[i]:
            continue
        for t in universe:
            p = S.px(t, i)
            if not p:
                continue
            tradable[i].append(t)
            for lv, m in LEVELS.items():
                for name, rank in methods(S, t, i, p, m, wsum):
                    sig[lv][name].append((i, t, rank))
    runs, start = [], days[0]  # simulate only the longest gap-free stretch of history
    for a, b in zip(days, days[1:] + [None]):
        if b != a + 1:
            runs.append((start, a))
            start = b
    seg = max(runs, key=lambda r: r[1] - r[0])
    out = {"span": [S.dates[days[0]], S.dates[days[-1]]], "valid_days": len(days), "stances": dict(S.stance_counts),
           "sim_span": [S.dates[seg[0]], S.dates[seg[1]]],
           "groups": {f"{g[0]} {g[1]}": {"n": h + m, "hit": round(h / (h + m), 3)} for g, (h, m) in g_rec.items() if h + m >= 30},
           "methods": {}}
    for name in sorted(sig["medium"]):
        row = {"event": {lv: event_study(S, sig[lv][name]) for lv in LEVELS}}
        if sims and not name.startswith("c "):
            for hold in (3, 5, 10):
                signals = {}
                picks = defaultdict(dict)
                for i, t, rank in fresh(sig["medium"][name]):  # buy the first day of a signal, like the event study
                    picks[i][t] = rank
                for i in range(seg[0], seg[1] + 1):
                    mx = max(picks[i].values(), default=1)
                    signals[S.dates[i]] = {t: _sig(t, 1 + picks[i][t] / mx / 10 if t in picks[i] else 0.0)
                                           for t in tradable.get(i, [])}
                over = {**SIM, "trading.max_hold_days": hold}
                row[f"sim{hold}"] = simulate(signals, B, over, mode="momentum", use_veto=False)
                if hold == 5:
                    row["sim5wide"] = simulate(signals, B, {**over, "trading.atr_stop_mult": 4.0, "trading.max_stop_pct": 0.25,
                                                            "trading.reward_risk": 50}, mode="momentum", use_veto=False)
                    rnd = [simulate(signals, B, over, mode="random", use_veto=False, seed=s) for s in range(3)]
                    row["random5"] = round(sum(r["return"] for r in rnd) / 3, 4)
        out["methods"][name] = row
    (DATA / "backtest").mkdir(exist_ok=True)
    (DATA / "backtest" / "research.json").write_text(json.dumps(out, indent=1))
    return out


def _sig(t, score):
    return {"ticker": t, "score": score, "crowd": 0.0, "news": 0.0, "buzz": 0.0, "parts": {}, "agree": ["reddit"],
            "agreement": 1.0, "eff_voices": 99, "voices": 99, "spike_ratio": 1.0, "cred_share": 1.0,
            "lowcred_share": 0.0, "pump_sub_share": 0.0}


def fresh(events, cooldown=5):
    """First day of each run of signals: one event per ticker per cooldown, not one per day."""
    last, kept = {}, []
    for i, t, rank in sorted(events):
        if i - last.get(t, -99) >= cooldown:
            kept.append((i, t, rank))
            last[t] = i
    return kept


def event_study(S, events, cooldown=5):
    kept = [(i, t) for i, t, _ in fresh(events, cooldown)]
    res = {"n": len(kept)}
    for h in HORIZONS:
        rs = [(i, r) for i, r in ((i, S.fwd(t, i, h)) for i, t in kept) if r]
        if len(rs) < 10:
            continue
        ex = [r[0] for _, r in rs]
        res[f"ex{h}"] = round(sum(ex) / len(ex), 4)
        res[f"raw{h}"] = round(sum(r[1] for _, r in rs) / len(rs), 4)
        res[f"hit{h}"] = round(sum(e > 0 for e in ex) / len(ex), 3)
        if h == 5:
            by_day, by_month = defaultdict(list), defaultdict(list)
            for i, r in rs:
                by_day[i].append(r[0])
                by_month[S.dates[i][:7]].append(r[0])
            dm = [sum(v) / len(v) for v in by_day.values()]
            mu = sum(dm) / len(dm)
            sd = math.sqrt(sum((x - mu) ** 2 for x in dm) / max(len(dm) - 1, 1))
            res["t5"] = round(mu / sd * math.sqrt(len(dm)), 2) if sd else None
            mm = [sum(v) / len(v) for v in by_month.values() if len(v) >= 3]
            res["months_pos"] = f"{sum(x > 0 for x in mm)}/{len(mm)}"
            res["by_month"] = {m: [len(v), round(sum(v) / len(v), 4)] for m, v in sorted(by_month.items())}
            for key, part in (("old", [r[0] for i, r in rs if S.dates[i] < SEEN_FROM]),
                              ("seen", [r[0] for i, r in rs if S.dates[i] >= SEEN_FROM])):
                if len(part) >= 8:  # "old" months were downloaded after the methods were fixed: a clean test
                    res[f"ex5_{key}"], res[f"n_{key}"] = round(sum(part) / len(part), 4), len(part)
    return res


def summarize(out):
    print(f"span {out['span'][0]}..{out['span'][1]}, {out['valid_days']} valid days, sims on {out['sim_span']}; stances {out['stances']}")
    print("groups (5-day hit rate of buys vs SPY):", out["groups"])
    print(f"{'method':<30}{'n':>5}{'ex3':>8}{'ex5':>8}{'ex10':>8}{'raw5':>8}{'hit5':>6}{'t5':>6}{'mo+':>6} |{'loose':>7}{'strict':>7} |"
          f"{'old ex5':>8}{'n':>5}{'seen':>7} |{'sim3':>7}{'5wide':>7}{'sim5':>7}{'rnd5':>7}{'sim10':>7}{'spy':>7}{'dd5':>7}{'tr5':>5}{'mo5+':>6}")
    for name, r in out["methods"].items():
        e, pct = r["event"]["medium"], lambda v: f"{v:+.1%}" if isinstance(v, (int, float)) else "   -"
        s5, s10 = r.get("sim5", {}), r.get("sim10", {})
        mo = s5.get("months") or {}
        print(f"{name:<30}{e['n']:>5}{pct(e.get('ex3')):>8}{pct(e.get('ex5')):>8}{pct(e.get('ex10')):>8}{pct(e.get('raw5')):>8}"
              f"{e.get('hit5', 0):>6.2f}{e.get('t5') or 0:>6.1f}{e.get('months_pos', '-'):>6} |"
              f"{pct(r['event']['loose'].get('ex5')):>7}{pct(r['event']['strict'].get('ex5')):>7} |"
              f"{pct(e.get('ex5_old')):>8}{e.get('n_old', 0):>5}{pct(e.get('ex5_seen')):>7} |{pct(r.get('sim3', {}).get('return')):>7}{pct(r.get('sim5wide', {}).get('return')):>7}"
              f"{pct(s5.get('return')):>7}{pct(r.get('random5')):>7}{pct(s10.get('return')):>7}{pct(s5.get('spy_return')):>7}"
              f"{pct(s5.get('max_dd')):>7}{s5.get('trades', 0):>5}{sum(v > 0 for v in mo.values()):>3}/{len(mo):<2}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--no-sim", action="store_true")
    a = ap.parse_args()
    summarize(run(a.since, sims=not a.no_sim))
