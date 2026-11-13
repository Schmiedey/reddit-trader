"""Does the MAJORITY of r/wallstreetbets know what it is doing?

Every WSB comment and post is read for what its author did with a stock (bought / sold / holding). For
each stock on each trading day, authors are counted once, by their latest stance in the last 5 trading
days. Then: do stocks the majority is buying beat stocks the majority is selling, the other talked-about
stocks, and SPY over the next 5 and 10 trading days?

All thresholds below were fixed before any result was seen. Nothing here is tuned.

    uv run python -m bot.backtest.majority [--sub wallstreetbets]
"""
import argparse
import gzip
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ..cloud.engine import FUNDS
from ..config import DATA
from ..sources.prices import load_valid_symbols
from .build import bt_connect, load_bars
from .comments_probe import JARGON, classify_comment

ET = ZoneInfo("America/New_York")
SRC = DATA / "history" / "comments_stance"
CACHE = DATA / "history" / "comment_stances"
WINDOW = 5          # trading days of talk that count
MIN_AUTHORS = 10    # a stock needs this many authors with a stance to have a "majority"
MAJORITY = 0.5      # more than half of them on one side
COOLDOWN = 10       # a stock's signal counts once, then not again for this many trading days
MIN_PRICE, COST = 5.0, 0.001  # per side
CVER = 1           # bump when the comment rules change, so cached classifications are redone
MIN_CALLS = 3       # an author's past calls needed before they count as "proven"


def classified(sub, valid, one):
    """[(ts, author, ticker, stance)] for every comment, cached per day file."""
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in sorted((SRC / sub).glob("*.jsonl.gz")):
        c = CACHE / f"{sub}_{f.stem[:10]}_v{CVER}.json"
        if not c.exists():
            day = []
            with gzip.open(f, "rt") as g:
                for line in g:
                    x = json.loads(line)
                    for tier, t, st in classify_comment(x, valid, one):
                        if tier != "C" and st in ("BUY", "SELL", "HOLD"):  # C (ticker guessed from the post) was too noisy
                            day.append([x["created_utc"], x["author"], t, "BUY" if st == "DD" else st])
            c.write_text(json.dumps(day))
        rows += json.loads(c.read_text())
    return rows


def session_of(ts, days):
    """Index of the trading day whose 16:00 ET close first includes this timestamp."""
    dt = datetime.fromtimestamp(ts, ET)
    d = dt.date().isoformat()
    i = int(np.searchsorted(days, d))
    if i < len(days) and days[i] == d and dt.hour >= 16:
        i += 1
    return i


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="wallstreetbets")
    a = ap.parse_args()
    con = bt_connect()
    valid = load_valid_symbols(con)
    one = {}
    for pid, tk, n in con.execute("SELECT post_id, MIN(ticker), COUNT(*) FROM mentions WHERE source='reddit' GROUP BY post_id"):
        if n == 1 and tk not in JARGON:
            one[pid.split(":")[-1]] = tk
    rows = classified(a.sub, valid, one)
    first_ts = min(r[0] for r in rows)
    n_comment = len(rows)
    rows += [[r[0], r[1], r[2], "BUY" if r[3] == "DD" else r[3]] for r in con.execute(
        "SELECT created, author, ticker, stance FROM stances WHERE community=? AND created>=? AND "
        "stance IN ('BUY','DD','SELL','HOLD')", (a.sub.lower(), first_ts))]
    bars = load_bars(con, 6)
    days = [b["d"] for b in bars["SPY"]]
    px = {s: {b["d"]: b for b in v} for s, v in bars.items()}
    n_all = len(rows)
    rows = [r for r in rows if r[2] not in FUNDS and r[2] not in JARGON and r[2] in px]
    rows.sort()
    by_sess = defaultdict(list)
    for ts, au, t, st in rows:
        by_sess[session_of(ts, days)].append((ts, au, t, st))
    lo, hi = min(by_sess), min(max(by_sess), len(days) - 1)
    print(f"{a.sub}: {n_comment:,} comment stances + {n_all - n_comment:,} post stances, {len(rows):,} on single stocks with prices, "
          f"{days[lo]} .. {days[hi]} ({hi - lo + 1} trading days), {len({r[1] for r in rows}):,} authors")

    def fwd(sym, i, k):
        """Buy at the next open, sell at the open k trading days later, minus costs. None when unknown."""
        if i + 1 + k >= len(days):
            return None
        b0, b1 = px[sym].get(days[i + 1]), px[sym].get(days[i + 1 + k])
        return (b1["o"] / b0["o"] - 1 - 2 * COST) if b0 and b1 and b0["o"] > 0 else None

    def back(sym, i, k=5):
        b0, b1 = px[sym].get(days[i - k]), px[sym].get(days[i])
        return (b1["c"] / b0["c"] - 1) if b0 and b1 else None

    # 1. the daily table: one row per stock per day with enough authors
    table, calls = [], []  # calls: (session, author, ticker, +1/-1) for the track-record test
    have = {f.stem[:10] for f in (SRC / a.sub).glob("*.jsonl.gz")}
    scored = 0
    for i in range(lo + WINDOW - 1, hi + 1):
        if any(days[j] not in have for j in range(i - WINDOW + 1, i + 1)):
            continue  # only days whose whole 5-day window of comments was downloaded
        scored += 1
        latest = {}
        for j in range(i - WINDOW + 1, i + 1):
            for ts, au, t, st in by_sess.get(j, ()):
                latest[(t, au)] = st
                if j == i and st in ("BUY", "SELL"):
                    calls.append((i, au, t, 1 if st == "BUY" else -1))
        cnt = defaultdict(lambda: defaultdict(set))
        for (t, au), st in latest.items():
            cnt[t][st].add(au)
        for t, c in cnt.items():
            b, s, h = len(c["BUY"]), len(c["SELL"]), len(c["HOLD"])
            bar = px[t].get(days[i])
            if b + s + h < MIN_AUTHORS or not bar or bar["c"] < MIN_PRICE:
                continue
            table.append({"i": i, "t": t, "b": b, "s": s, "h": h, "n": b + s + h, "buy": b / (b + s + h),
                          "sell": s / (b + s + h), "net": (b - s) / (b + s + h), "back5": back(t, i),
                          "f5": fwd(t, i, 5), "f10": fwd(t, i, 10), "spy5": fwd("SPY", i, 5), "spy10": fwd("SPY", i, 10),
                          "authors": c})
    print(f"days scored (full {WINDOW}-day window of comments): {scored}; stock-days with {MIN_AUTHORS}+ authors: {len(table):,} "
          f"({len({r['t'] for r in table})} stocks, {len(table) / max(scored, 1):.1f} a day)")
    if not table:
        return
    side = lambda r: "buy" if r["buy"] > MAJORITY else "sell" if r["sell"] > MAJORITY else "split"
    out = {"days": [days[lo], days[hi]], "scored_days": scored, "stock_days": len(table)}

    # 2. what the majority does, and when
    print("\nWhat the majority is doing (share of stock-days), and the stock's move over the 5 days before:")
    for sd in ("buy", "sell", "split"):
        rs = [r for r in table if side(r) == sd]
        bk = [r["back5"] for r in rs if r["back5"] is not None]
        print(f"  majority {sd:<6}{len(rs):>6} ({len(rs) / max(len(table), 1):>4.0%})   prior 5-day move {np.mean(bk) if bk else float('nan'):>+7.1%}"
              f"   median {np.median(bk) if bk else float('nan'):>+7.1%}")

    # 3. does it predict? Every stock-day, by how lopsided the crowd is.
    def report(label, rs, k):
        f, spy = f"f{k}", f"spy{k}"
        rs = [r for r in rs if r[f] is not None]
        if len(rs) < 5:
            print(f"  {label:<34}{len(rs):>5}  too few")
            return None
        pool = defaultdict(list)
        for r in table:
            if r[f] is not None:
                pool[r["i"]].append(r[f])
        ex_spy = [r[f] - r[spy] for r in rs]
        ex_pool = [r[f] - np.mean(pool[r["i"]]) for r in rs]
        byday = defaultdict(list)
        for r, e in zip(rs, ex_pool):
            byday[r["i"]].append(e)
        dm = [np.mean(v) for v in byday.values()]
        res = {"n": len(rs), "ret": float(np.mean([r[f] for r in rs])), "vs_spy": float(np.mean(ex_spy)),
               "vs_pool": float(np.mean(ex_pool)), "win": float(np.mean([e > 0 for e in ex_spy])), "t": tstat(dm), "days": len(dm)}
        print(f"  {label:<34}{res['n']:>5}{res['ret']:>+8.1%}{res['vs_spy']:>+9.1%}{res['vs_pool']:>+11.1%}{res['win']:>6.0%}{res['t']:>7.1f}")
        return res

    head = f"  {'':<34}{'n':>5}{'return':>8}{'vs SPY':>9}{'vs others':>11}{'win':>6}{'t':>7}"
    for k in (5, 10):
        print(f"\nNext {k} trading days, every stock-day (\"vs others\" = minus the same day's average talked-about stock; t is by day):")
        print(head)
        out[f"all_{k}"] = {
            "majority buying": report("majority buying", [r for r in table if side(r) == "buy"], k),
            "majority selling": report("majority selling", [r for r in table if side(r) == "sell"], k),
            "split": report("no majority", [r for r in table if side(r) == "split"], k)}
        for lab, a0, a1 in (("buyers under 35%", 0, .35), ("35-50%", .35, .5), ("50-65%", .5, .65), ("65-80%", .65, .8), ("80%+", .8, 1.01)):
            out[f"all_{k}"][lab] = report("  " + lab, [r for r in table if a0 <= r["buy"] < a1], k)
        ic = []
        for i in sorted({r["i"] for r in table}):
            rs = [r for r in table if r["i"] == i and r[f"f{k}"] is not None]
            if len(rs) >= 5:
                ic.append(spearman([r["net"] for r in rs], [r[f"f{k}"] for r in rs]))
        out[f"ic_{k}"] = {"mean": float(np.mean(ic)) if ic else None, "t": tstat(ic), "days": len(ic)}
        print(f"  daily rank correlation, crowd lean vs next {k}-day return: {np.mean(ic) if ic else float('nan'):+.3f} "
              f"(t {tstat(ic):.1f}, {len(ic)} days)")

    # 3b. is it only that they buy falling stocks, and falling stocks kept falling?
    print("\nSame prior move, different crowd (next 5 days, vs SPY):")
    for lab, cond in (("fell 5%+ in the prior 5 days", lambda r: r["back5"] is not None and r["back5"] <= -.05),
                      ("moved less than 5%", lambda r: r["back5"] is not None and abs(r["back5"]) < .05),
                      ("rose 5%+", lambda r: r["back5"] is not None and r["back5"] >= .05)):
        parts = []
        for sd in ("buy", "sell"):
            rs = [r["f5"] - r["spy5"] for r in table if cond(r) and side(r) == sd and r["f5"] is not None]
            parts.append(f"majority {sd}ing {np.mean(rs) if rs else float('nan'):>+6.1%} (n {len(rs):>3})")
        print(f"  {lab:<30}{parts[0]}   {parts[1]}")

    # 4. tradable signals: first day a majority forms or flips, then a cooldown
    def events(pick):
        last, ev = {}, []
        for r in sorted(table, key=lambda r: r["i"]):
            if pick(r) and r["i"] - last.get(r["t"], -99) >= COOLDOWN:
                last[r["t"]] = r["i"]
                ev.append(r)
        return ev

    prev = {(r["t"], r["i"]): side(r) for r in table}
    flipped = lambda r, to: side(r) == to and any(prev.get((r["t"], r["i"] - d)) not in (None, to) for d in range(1, WINDOW + 1))
    sig = {"majority starts buying": events(lambda r: side(r) == "buy"),
           "majority starts selling": events(lambda r: side(r) == "sell"),
           "flips to buying": events(lambda r: flipped(r, "buy")),
           "flips to selling": events(lambda r: flipped(r, "sell")),
           "strong majority buying (65%+)": events(lambda r: r["buy"] >= .65),
           "any talked-about stock (control)": events(lambda r: True)}
    for k in (5, 10):
        print(f"\nSignals, counted once per stock then a {COOLDOWN}-day cooldown; next {k} trading days:")
        print(head)
        out[f"signals_{k}"] = {lab: report(lab, ev, k) for lab, ev in sig.items()}
    mid = (lo + hi) // 2
    print("\nSame signals, 10 days, first half / second half of the period (vs others):")
    for lab, ev in sig.items():
        halves = []
        for part in (lambda r: r["i"] <= mid, lambda r: r["i"] > mid):
            rs = [r for r in ev if part(r) and r["f10"] is not None]
            pool = defaultdict(list)
            for r in table:
                if r["f10"] is not None:
                    pool[r["i"]].append(r["f10"])
            halves.append((len(rs), np.mean([r["f10"] - np.mean(pool[r["i"]]) for r in rs]) if rs else float("nan")))
        print(f"  {lab:<34}{halves[0][0]:>5}{halves[0][1]:>+8.1%}   |{halves[1][0]:>5}{halves[1][1]:>+8.1%}")

    # 5. the ones who "know what they're doing": authors whose earlier calls were right
    graded = defaultdict(list)  # author -> [(session the outcome was known, signed excess)]
    for i, au, t, sgn in calls:
        f, s = fwd(t, i, 5), fwd("SPY", i, 5)
        if f is not None and s is not None:
            graded[au].append((i + 6, sgn * (f - s)))
    n_auth = sum(1 for v in graded.values() if len(v) >= MIN_CALLS)
    hit = [np.mean([e > 0 for _, e in v]) for v in graded.values() if len(v) >= MIN_CALLS]
    allc = [e for v in graded.values() for _, e in v]
    print(f"\nTrack records: {len(allc):,} graded calls by {len(graded):,} authors; average call {np.mean(allc) if allc else float('nan'):+.2%} vs SPY "
          f"over 5 days, {np.mean([e > 0 for e in allc]) if allc else float('nan'):.0%} right.")
    print(f"Authors with {MIN_CALLS}+ graded calls: {n_auth:,}; of those, right more often than wrong: {np.mean([h > .5 for h in hit]) if hit else float('nan'):.0%}")
    # persistence: split each author's calls in time order; does a good first half predict the second?
    pairs = []
    for v in graded.values():
        if len(v) >= 6:
            v = sorted(v)
            h = len(v) // 2
            pairs.append((np.mean([e for _, e in v[:h]]), np.mean([e for _, e in v[h:]])))
    if len(pairs) >= 10:
        good = [b for a_, b in pairs if a_ > 0]
        bad = [b for a_, b in pairs if a_ <= 0]
        print(f"Do good callers stay good? {len(pairs)} authors with 6+ calls: rank correlation of early vs later results "
              f"{spearman([p[0] for p in pairs], [p[1] for p in pairs]):+.2f}; later average of early winners {np.mean(good) if good else float('nan'):+.2%}, "
              f"of early losers {np.mean(bad) if bad else float('nan'):+.2%}")
        out["persistence"] = {"n": len(pairs), "rho": spearman([p[0] for p in pairs], [p[1] for p in pairs]),
                              "winners_later": float(np.mean(good)) if good else None, "losers_later": float(np.mean(bad)) if bad else None}

    def proven_net(r):
        b = s = 0
        for st, sgn in (("BUY", 1), ("SELL", -1)):
            for au in r["authors"][st]:
                past = [e for known, e in graded.get(au, ()) if known <= r["i"]]
                if len(past) >= MIN_CALLS and np.mean(past) > 0:
                    b, s = b + (sgn > 0), s + (sgn < 0)
        return (b - s) / (b + s) if b + s >= 3 else None

    for r in table:
        r["pnet"] = proven_net(r)
    pv = [r for r in table if r["pnet"] is not None]
    print(f"\nStock-days with 3+ proven authors (right on average over {MIN_CALLS}+ earlier calls): {len(pv)}")
    if pv:
        print(head)
        for k in (5, 10):
            out[f"proven_{k}"] = {"buying": report(f"proven authors mostly buying, {k}d", [r for r in pv if r["pnet"] > 0], k),
                                  "selling": report(f"proven authors mostly selling, {k}d", [r for r in pv if r["pnet"] < 0], k)}
    last = max(r["i"] for r in table)
    print(f"\nWhat the crowd is doing now (5 trading days to the close on {days[last]}):")
    print(f"  {'stock':<7}{'buying':>7}{'selling':>8}{'holding':>8}   majority      5-day move")
    for r in sorted((r for r in table if r["i"] == last), key=lambda r: -r["n"])[:15]:
        sd = side(r)
        share = {"buy": r["buy"], "sell": r["sell"]}.get(sd)
        print(f"  {r['t']:<7}{r['b']:>7}{r['s']:>8}{r['h']:>8}   {(sd + f' {share:.0%}') if share else 'split':<12}{r['back5'] if r['back5'] is not None else float('nan'):>+9.1%}")
    (DATA / "backtest" / "majority.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
