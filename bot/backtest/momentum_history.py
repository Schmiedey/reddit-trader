"""Does the live Momentum rule hold up over a long price history, without Reddit?

The rule (as the live desk trades it, minus "people are talking about it"): close $5+, $5M+ traded a day,
close at least double the lowest low of the last 60 trading days, average daily range (14-day ATR) under 8%.
Buy at the next open, sell at the open 10 trading days later, 0.1% cost per side, one signal per stock per
10 trading days. Compared with SPY over the same days and with every other stock that passed the same
price, volume and range filters that day ("vs others").

Universe: the ~1,700 tickers in the project's price file (stocks Reddit mentioned in 2024-2026), with
history back to 2010. They are today's survivors, so stocks that went to zero are missing.

    uv run python -m bot.backtest.momentum_history [--start 2010-01-01]
"""
import argparse
import gzip
import json
import math
import pickle
from collections import defaultdict

import numpy as np

from ..config import DATA
from .build import BARS

LONG = DATA / "history" / "long_bars.pkl"
HOLD, COST, MIN_PRICE, MIN_DV, UP60, MAX_ATR = 10, 0.001, 5.0, 5e6, 1.0, 0.08


def download(syms, start):
    """Long daily history per symbol, saved as it goes. Yahoo answers "no data" for everything when it is
    rate-limiting, so a chunk that comes back empty is waited out and retried, never recorded as missing."""
    import time

    import yfinance as yf
    have = {k: v for k, v in (pickle.loads(LONG.read_bytes()) if LONG.exists() else {}).items() if v is not None}
    miss = have.setdefault("__miss__", {})
    todo = [s for s in syms if s not in have and miss.get(s, 0) < 2]
    k, waits = 0, 0
    while k < len(todo):
        chunk = todo[k:k + 25]
        got = {}
        try:
            df = yf.download(chunk, start=start, interval="1d", auto_adjust=True, progress=False, threads=False, group_by="ticker")
            for s in chunk:
                try:
                    f = df[s].dropna(subset=["Close"])
                except KeyError:
                    continue
                if len(f) >= 80:
                    got[s] = {"d": np.array([i.strftime("%Y-%m-%d") for i in f.index]), "o": f["Open"].to_numpy(float),
                              "h": f["High"].to_numpy(float), "l": f["Low"].to_numpy(float), "c": f["Close"].to_numpy(float),
                              "v": f["Volume"].to_numpy(float)}
        except Exception as e:
            print(f"yfinance error: {type(e).__name__}", flush=True)
        if not got and waits < 6:  # nothing at all: almost certainly rate-limited
            waits += 1
            print(f"empty chunk at {k}, waiting {60 * waits}s", flush=True)
            time.sleep(60 * waits)
            continue
        waits = 0
        have.update(got)
        for s in chunk:
            if s not in got:
                miss[s] = miss.get(s, 0) + 1
        LONG.write_bytes(pickle.dumps(have))
        k += 25
        print(f"downloaded {min(k, len(todo))}/{len(todo)} ({len(got)} with data)", flush=True)
        time.sleep(4)
    return {s: v for s, v in have.items() if s != "__miss__"}


def rolling(x, n, fn):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(np.lib.stride_tricks.sliding_window_view(x, n), axis=1)
    return out


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    a = ap.parse_args()
    syms = ["SPY"] + sorted(s for s, v in json.load(gzip.open(BARS)).items() if v and s != "SPY")  # names with prices in the project
    bars = download(syms, a.start)
    spy = bars["SPY"]
    spy_i = {d: k for k, d in enumerate(spy["d"])}
    events, pool = [], defaultdict(list)  # events: (day, sym, ret, spy_ret); pool: day -> returns of every eligible stock
    for s, b in bars.items():
        if s == "SPY" or len(b["c"]) < 90:
            continue
        o, h, l, c, v, d = b["o"], b["h"], b["l"], b["c"], b["v"], b["d"]
        pc = np.roll(c, 1)
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        tr[0] = h[0] - l[0]
        atr = rolling(tr, 14, np.mean) / c
        dv = rolling(c * v, 20, np.mean)
        low60 = rolling(l, 60, np.min)
        jump = np.zeros(len(c), bool)  # unadjusted splits in the data: price jumps with no volume, or absurd moves
        r1 = c[1:] / np.maximum(c[:-1], 1e-9)
        jump[1:] = ((r1 >= 3) & (v[1:] <= 0.3 * v[:-1])) | (r1 >= 8) | (r1 <= 1 / 8)
        dirty = rolling(jump.astype(float), 36, np.max)  # any such jump in the 25 days before or the hold after
        ok = (c >= MIN_PRICE) & (dv >= MIN_DV) & (atr < MAX_ATR) & (low60 > 0)
        sig = ok & (c >= (1 + UP60) * low60)
        last = -99
        for i in np.flatnonzero(ok):
            j = i + 1 + HOLD
            if j >= len(c) or o[i + 1] <= 0 or (i + 11 < len(dirty) and dirty[min(i + 11, len(dirty) - 1)] > 0):
                continue
            k = spy_i.get(d[i + 1])
            if k is None or k + HOLD >= len(spy["o"]):
                continue
            ret = o[j] / o[i + 1] - 1 - 2 * COST
            pool[d[i]].append(ret)
            if sig[i] and i - last >= HOLD:
                last = i
                events.append((d[i], s, ret, spy["o"][k + HOLD] / spy["o"][k] - 1))
    ev = sorted(events)
    days = np.array([e[0] for e in ev])
    r = np.array([e[2] for e in ev])
    sp = np.array([e[3] for e in ev])
    oth = np.array([np.mean(pool[e[0]]) for e in ev])

    def line(label, m):
        if m.sum() < 5:
            return print(f"{label:<12}{int(m.sum()):>6}  too few")
        byd = defaultdict(list)
        for dd, x in zip(days[m], (r - oth)[m]):
            byd[dd].append(x)
        rr = r[m]
        print(f"{label:<12}{int(m.sum()):>6}{rr.mean():>+9.1%}{np.median(rr):>+9.1%}{(rr - sp[m]).mean():>+9.1%}{(rr - oth[m]).mean():>+10.1%}"
              f"{tstat([np.mean(x) for x in byd.values()]):>7.1f}{(rr > 0).mean():>6.0%}{np.sort(rr)[:max(1, int(len(rr) * .95))].mean():>+11.1%}{sp[m].mean():>+9.1%}")

    print(f"{len(ev):,} momentum signals, {days[0]} .. {days[-1]}, {len({e[1] for e in ev})} stocks of {len(bars)} with data")
    print(f"{'':<12}{'trades':>6}{'return':>9}{'median':>9}{'vs SPY':>9}{'vs others':>10}{'t':>7}{'win':>6}{'no top 5%':>11}{'SPY':>9}")
    line("ALL YEARS", np.ones(len(ev), bool))
    years = np.array([dd[:4] for dd in days])
    out = {}
    for y in sorted(set(years)):
        line(y, years == y)
        m = years == y
        out[y] = {"n": int(m.sum()), "ret": float(r[m].mean()), "vs_spy": float((r - sp)[m].mean()), "vs_others": float((r - oth)[m].mean())}
    print("\nBy what the market did over the same 10 days:")
    for lab, m in (("SPY down 3%+", sp <= -0.03), ("SPY -3..0%", (sp > -0.03) & (sp <= 0)), ("SPY 0..3%", (sp > 0) & (sp <= 0.03)), ("SPY up 3%+", sp > 0.03)):
        line(lab, m)
    (DATA / "backtest" / "momentum_history.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
