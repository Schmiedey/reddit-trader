"""15-minute desk: forward PAPER test of one fixed rule on Kalshi's 15-minute crypto markets. No keys, no orders.

Everything the rule needs is public and kept after the fact (Kalshi's settled markets and their 1-minute quotes,
Coinbase's 15-minute candles), so this does not need to run all the time. Each run (a short GitHub Actions job)
replays every window that closed since the last run, exactly as if a bot had been watching live.

The rule, fixed on 2026-11-13 and never edited (a new rule would get a new ledger):
- Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M ("price at the end >= price at the start" for each 15 minutes).
- Model: logistic regression for "this window finishes opposite to the previous one", refit at the start of each
  calendar month on the previous 180 days of Coinbase 15-minute candles of the four coins (L2 penalty 1, inputs
  standardised). Inputs, all known when the window opens: size of the last two moves in units of the last day's
  volatility (capped at 5), run length (1-3), whether the last move went with the 4-hour trend, minute of the
  hour, and two interactions. Threshold: the 90th percentile of the model's own training-period predictions.
- Bet: if the model is at or above the threshold, and Kalshi's previous window settled the same way as the
  Coinbase candle, buy the side that reverses the previous window at Kalshi's ask at the end of minute 1.
  Skipped if the quote is one-sided, the spread is over 3c, or the price is outside 20-80c.
- Settled at Kalshi's result. Taker fee 7% x price x (1 - price) per contract. One contract per bet.

    python3 bot/k15desk/desk.py k15_ledger.json      # create or extend the ledger in place
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KAL = "https://api.elections.kalshi.com/trade-api/v2"
CB = "https://api.exchange.coinbase.com/products/{}/candles?granularity=900&start={}&end={}"
SERIES = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD", "KXXRP15M": "XRP-USD"}
START = int(datetime(2026, 11, 14, 1, 30, tzinfo=timezone.utc).timestamp())   # first window of the forward test
STEP, LOOK, TOP, FEE = 900, 180 * 86400, 0.90, 0.07
MAX_SPREAD, LO, HI = 0.03, 0.20, 0.80
FEATURES = ["absz", "absz2", "run", "with_trend", "m15", "m30", "m45", "x_run_z", "x_trend_z"]
UA = {"User-Agent": "paper-desk/0.1", "Accept": "application/json"}


def get(url, tries=6):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(1.5 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"unavailable: {url[:90]}")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def candles(prod, t0, t1):
    """{window start: (open, close)} for Coinbase 15-minute candles in [t0, t1)."""
    out, a, t1 = {}, int(t0), int(t1)
    while a < t1:
        b = min(a + 300 * STEP, t1)
        for k in get(CB.format(prod, a, b)) or []:
            out[int(k[0])] = (float(k[3]), float(k[4]))
        a = b
        time.sleep(0.15)
    return out


def features(c):
    """Rows of (t, inputs, reversed?, previous up?) by position in the sorted candle list, like the backtest."""
    t = sorted(c)
    o = [c[x][0] for x in t]
    cl = [c[x][1] for x in t]
    ret = [math.log(b / a) if a > 0 and b > 0 else float("nan") for a, b in zip(o, cl)]
    up = [1 if b >= a else 0 for a, b in zip(o, cl)]
    rows = {}
    for i in range(97, len(t)):
        if t[i] - t[i - 3] != 3 * STEP:
            continue
        w = ret[i - 96:i]
        if any(x != x for x in w) or ret[i - 1] != ret[i - 1] or ret[i - 2] != ret[i - 2]:
            continue
        m = sum(w) / 96
        vol = math.sqrt(sum((x - m) ** 2 for x in w) / 95)
        if not vol > 0 or i < 16:
            continue
        absz, absz2 = min(abs(ret[i - 1]) / vol, 5.0), min(abs(ret[i - 2]) / vol, 5.0)
        run = 3 if up[i - 1] == up[i - 2] == up[i - 3] else 2 if up[i - 1] == up[i - 2] else 1
        h4 = math.log(o[i] / o[i - 16])
        wt = 1 if (h4 > 0 and up[i - 1] == 1) or (h4 < 0 and up[i - 1] == 0) else 0
        mi = (t[i] // 60) % 60
        x = [absz, absz2, run, wt, int(mi == 15), int(mi == 30), int(mi == 45), absz * (run >= 2), absz * wt]
        rows[t[i]] = (x, int(up[i] != up[i - 1]), up[i - 1])
    return rows


def solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(X, y):
    """Logistic regression, L2 penalty 1 on the slopes (not the intercept), standardised inputs; Newton steps."""
    n, k = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(k)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / (n - 1)) or 1.0 for j in range(k)]
    Z = [[1.0] + [(r[j] - mu[j]) / sd[j] for j in range(k)] for r in X]
    w = [0.0] * (k + 1)
    for _ in range(12):
        g = [0.0] * (k + 1)
        H = [[0.0] * (k + 1) for _ in range(k + 1)]
        for z, yy in zip(Z, y):
            s = sum(a * b for a, b in zip(z, w))
            p = 1 / (1 + math.exp(-s))
            e, q = p - yy, p * (1 - p)
            for i in range(k + 1):
                g[i] += e * z[i]
                qi = q * z[i]
                Hi = H[i]
                for j in range(i, k + 1):
                    Hi[j] += qi * z[j]
        for i in range(k + 1):
            for j in range(i):
                H[i][j] = H[j][i]
            if i:
                g[i] += w[i]
                H[i][i] += 1.0
        step = solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return {"mu": mu, "sd": sd, "w": w}


def score(m, x):
    s = m["w"][0] + sum(wi * (xi - a) / b for wi, xi, a, b in zip(m["w"][1:], x, m["mu"], m["sd"]))
    return 1 / (1 + math.exp(-s))


def quantile(v, q):
    v = sorted(v)
    i = q * (len(v) - 1)
    lo = int(i)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)


def month_start(t):
    d = datetime.fromtimestamp(t, timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())


def model_for(led, t0):
    """The month's model, fitted once and stored in the ledger so every bet can be checked later."""
    ms = month_start(t0)
    key = datetime.fromtimestamp(ms, timezone.utc).strftime("%Y-%m")
    if key in led["models"]:
        return led["models"][key]
    X, y = [], []
    for prod in SERIES.values():
        for t, (x, rev, _) in features(candles(prod, ms - LOOK - 2 * 86400, ms)).items():
            if ms - LOOK <= t < ms:
                X.append(x)
                y.append(rev)
    m = fit(X, y)
    m["threshold"] = quantile([score(m, x) for x in X], TOP)
    m["trained_on"] = len(X)
    m["fitted_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    led["models"][key] = m
    print(f"{key}: model fitted on {len(X)} candle windows, threshold {m['threshold']:.4f}", flush=True)
    return m


def settled(series, since):
    out, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 1000, "min_close_ts": int(since)}
        if cursor:
            p["cursor"] = cursor
        d = get(f"{KAL}/markets?{urllib.parse.urlencode(p)}") or {}
        out += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return out


def minute1(series, m, t0):
    """Kalshi's YES bid and ask at the end of the window's first minute."""
    p = urllib.parse.urlencode({"start_ts": int(t0), "end_ts": int(t0 + 300), "period_interval": 1})
    d = get(f"{KAL}/series/{series}/markets/{m['ticker']}/candlesticks?{p}") or get(f"{KAL}/historical/markets/{m['ticker']}/candlesticks?{p}") or {}
    for c in d.get("candlesticks") or []:
        if round((c["end_period_ts"] - t0) / 60) == 1:
            b, a = (c.get("yes_bid") or {}).get("close_dollars"), (c.get("yes_ask") or {}).get("close_dollars")
            return (float(b), float(a)) if b is not None and a is not None else None
    return None


def summarise(bets):
    done = [b for b in bets if b.get("net") is not None]
    if not done:
        return {"bets": 0}
    cost = [b["price"] + FEE * b["price"] * (1 - b["price"]) for b in done]
    net = [b["net"] for b in done]
    by_w = {}
    for b in done:
        by_w.setdefault(b["t0"], []).append(b["net"])
    w = [sum(v) / len(v) for v in by_w.values()]
    mw = sum(w) / len(w)
    sw = math.sqrt(sum((x - mw) ** 2 for x in w) / (len(w) - 1)) if len(w) > 1 else float("nan")
    s = {"bets": len(done), "windows": len(w), "wins": sum(b["won"] for b in done), "avg_price": round(sum(b["price"] for b in done) / len(done), 4),
         "net_per_share": round(sum(net) / len(net), 4), "return_per_bet": round(sum(net) / sum(cost), 4),
         "t": round(mw / (sw / math.sqrt(len(w))), 2) if sw == sw and sw > 0 else None}
    s["by_coin"] = {}
    for ser in SERIES:
        d = [b for b in done if b["series"] == ser]
        if d:
            s["by_coin"][ser] = {"bets": len(d), "wins": sum(b["won"] for b in d), "net_per_share": round(sum(b["net"] for b in d) / len(d), 4)}
    return s


def run(path):
    led = json.load(open(path)) if os.path.exists(path) else {"rule": __doc__.split("\n\n")[2], "start": START, "models": {}, "last": {}, "bets": []}
    now = time.time()
    horizon = now - 600                                   # only windows that closed at least 10 minutes ago
    new = 0
    feats = {}
    for series, prod in SERIES.items():
        last = led["last"].get(series)                    # {"t0": ..., "up": ...} of the last window processed
        since = (last["t0"] if last else START - 2 * STEP)
        ms = sorted((m for m in settled(series, since - STEP) if m.get("result") in ("yes", "no")), key=lambda m: m["open_time"])
        ms = [m for m in ms if ts(m["open_time"]) > since - 1 and ts(m["close_time"]) <= horizon]
        if not ms:
            continue
        if prod not in feats:
            feats[prod] = features(candles(prod, ts(ms[0]["open_time"]) - 2 * 86400, min(now, ts(ms[-1]["close_time"]) + STEP)))
        f = feats[prod]
        prev = last
        for m in ms:
            t0, up = int(ts(m["open_time"])), int(m["result"] == "yes")
            if last and t0 <= last["t0"]:
                continue
            if t0 >= START and prev and t0 - prev["t0"] == STEP and t0 in f and f[t0][2] == prev["up"]:
                mdl = model_for(led, t0)
                p = score(mdl, f[t0][0])
                if p >= mdl["threshold"]:
                    q = minute1(series, m, t0)
                    rev_yes = prev["up"] == 0
                    row = {"t0": t0, "series": series, "ticker": m["ticker"], "side": "yes" if rev_yes else "no", "p_rev": round(p, 4),
                           "threshold": round(mdl["threshold"], 4), "bid": None, "ask": None, "price": None, "result": m["result"], "won": None, "net": None, "skip": None}
                    if q is None or not (0 < q[0] < q[1] < 1):
                        row["skip"] = "no two-sided quote at minute 1"
                    else:
                        bid, ask = q if rev_yes else (1 - q[1], 1 - q[0])
                        row.update(bid=round(bid, 4), ask=round(ask, 4))
                        if ask - bid > MAX_SPREAD + 1e-9:
                            row["skip"] = "spread over 3c"
                        elif not (LO <= ask <= HI):
                            row["skip"] = "price outside 20-80c"
                        else:
                            won = int((up == 1) == rev_yes)
                            row.update(price=round(ask, 4), won=won, net=round(won - ask - FEE * ask * (1 - ask), 4))
                    led["bets"].append(row)
                    new += 1
            prev = {"t0": t0, "up": up}
        led["last"][series] = prev
    led["bets"].sort(key=lambda b: (b["t0"], b["series"]))
    led["summary"] = summarise(led["bets"])
    led["updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    tmp = path + ".tmp"
    json.dump(led, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, path)
    s = led["summary"]
    print(f"{new} new bets; total {s.get('bets', 0)} settled", flush=True)
    if s.get("bets"):
        print(f"wins {s['wins']} of {s['bets']} at {s['avg_price']:.3f}; net {100*s['net_per_share']:+.2f}c a share; return per bet {100*s['return_per_bet']:+.2f}%; t {s['t']}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "k15_ledger.json")
