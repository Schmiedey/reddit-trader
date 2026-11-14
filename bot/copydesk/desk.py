"""Copy desk: paper-copies a fixed list of public Hyperliquid wallets. PAPER ONLY: no keys, no orders.

Every leader trade is public and timestamped, so this does not need to run all the time. Each run
(a short GitHub Actions job, a few times a day) replays everything the leaders did since the last
run, as if a bot had copied each of their fills 1, 15 or 60 minutes later. Standard library only.

Rules, fixed in advance:
- Three $100k accounts copy the wallets in leaders.json, one per copy delay (1, 15, 60 min). A fourth
  account (added 2026-11-13) copies the daily traders in daily.json 1 minute late. Each account splits
  its money equally between its wallets that had >= $10k when the account opened ("slices").
- A slice holds the leader's positions scaled to the slice's money (the same leverage as the
  leader), capped at 5x in total. It trades only when the leader trades.
- Fills: the open price of the first 1-minute candle at/after (leader fill + delay), plus 0.045%
  taker fee and 0.05% slippage. Hourly funding is paid or received like a real position.
- If a leader moves out more than half of the money it had when copying started (withdrawals,
  not trading losses), the slice closes its positions and stops copying that leader.

    python3 bot/copydesk/desk.py copy_ledger.json      # create or extend the ledger in place
"""
import bisect
import json
import math
import os
import sys
import time
import urllib.request

API = "https://api.hyperliquid.xyz/info"
HERE = os.path.dirname(os.path.abspath(__file__))
START_CASH = 100_000.0
ACCOUNTS = {  # existing accounts are never edited (that would spoil the comparison): add new ones
    "fast": {"name": "1-minute copier", "delay": 60_000, "group": "leaders.json"},
    "slow": {"name": "15-minute copier", "delay": 900_000, "group": "leaders.json"},
    "hourly": {"name": "1-hour copier", "delay": 3_600_000, "group": "leaders.json"},
    "daily": {"name": "Daily traders", "delay": 60_000, "group": "daily.json"},
}
FEE, SLIP = 0.00045, 0.0005
MAX_LEV = 5.0
MIN_LEADER_EQ = 10_000.0
LEFT_FRAC = 0.5
SETTLE = 120_000           # leave the last 2 minutes for the next run (fills may still be arriving)
MIN_STEP = 600_000         # don't bother running for less than 10 new minutes
H, MIN = 3_600_000, 60_000
MAX_DELAY = max(a["delay"] for a in ACCOUNTS.values())
CALLS = {"n": 0}


def post(body, tries=4):
    data, err = json.dumps(body).encode(), None
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
            CALLS["n"] += 1
            time.sleep(0.4)  # stay well inside the public rate limit
            return out
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Hyperliquid API failed for {body.get('type')}: {err}")  # abort: nothing is saved


def main_dex(coin):
    return not coin.startswith(("@", "#")) and "/" not in coin and ":" not in coin


# ---------------------------------------------------------------- public data

def leader_fills(addr, t0, t1):
    """Main-exchange perp fills in (t0, t1], including TWAP slices (a separate endpoint)."""
    out = {}
    for kind in ("userFillsByTime", "userTwapSliceFillsByTime"):
        start = t0 + 1
        while True:
            body = {"type": kind, "user": addr, "startTime": start, "endTime": t1}
            if kind == "userFillsByTime":
                body["aggregateByTime"] = True
            r = post(body) or []
            r = [x["fill"] if "fill" in x else x for x in r]
            for x in r:
                if main_dex(x["coin"]) and t0 < x["time"] <= t1:
                    out[(x["tid"], x["time"], x["coin"])] = x
            if len(r) < 2000:
                break
            start = max(x["time"] for x in r) + 1
    return sorted(out.values(), key=lambda x: (x["time"], x["tid"]))


def leader_targets(fills):
    """One target per (coin, minute): the leader's position right after its last fill in that minute,
    as recorded by the exchange (startPosition + size), so a missed event corrects itself."""
    last = {}
    for x in fills:
        size = (1 if x["side"] == "B" else -1) * float(x["sz"])
        last[(x["coin"], x["time"] // MIN)] = (x["time"], x["coin"], float(x["startPosition"]) + size)
    return sorted(last.values())


def account_history(addr, since, now):
    """(times, equity, pnl) for the shortest portfolio window that covers `since`."""
    span = now - since
    key = "week" if span < 6.5 * 86400_000 else "month" if span < 29 * 86400_000 else "allTime"
    p = dict(post({"type": "portfolio", "user": addr}))[key]
    t = [x[0] for x in p["accountValueHistory"]]
    eq = [float(x[1]) for x in p["accountValueHistory"]]
    pnl_t = [x[0] for x in p["pnlHistory"]]
    pnl = [float(x[1]) for x in p["pnlHistory"]]
    return t, eq, pnl_t, pnl


def interp(ts, vs, t):
    if not ts:
        return 0.0
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = bisect.bisect_right(ts, t)
    t0, t1, v0, v1 = ts[i - 1], ts[i], vs[i - 1], vs[i]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


class Prices:
    """Candle opens for each coin over the run's window, at the finest interval that fits (the API
    serves the latest 5,000 candles: 1m covers 3.4 days, 5m 17 days, 1h 200 days)."""

    def __init__(self, t0, t1, mids):
        span = t1 - t0
        self.interval = "1m" if span < 3.3 * 86400_000 else "5m" if span < 16 * 86400_000 else "1h"
        self.t0, self.t1, self.mids, self.c = t0, t1, mids, {}

    def _load(self, coin):
        if coin not in self.c:
            r = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": self.interval,
                                                        "startTime": self.t0 - H, "endTime": self.t1 + H}}) or []
            self.c[coin] = ([x["t"] for x in r], [float(x["o"]) for x in r], [float(x["c"]) for x in r])
        return self.c[coin]

    def at(self, coin, t):
        """Open of the first candle starting at/after t; the last close if t is past the data."""
        ts, o, c = self._load(coin)
        if not ts:
            return self.mids.get(coin)
        i = bisect.bisect_left(ts, t)
        return o[i] if i < len(ts) else c[-1]


def funding(coins, t0, t1):
    """{coin: [(hour_ts, rate), ...]} for funding settlements in (t0, t1]."""
    out = {}
    for c in coins:
        rows, start = [], t0 + 1
        while True:
            r = post({"type": "fundingHistory", "coin": c, "startTime": start, "endTime": t1}) or []
            rows += [(x["time"], float(x["fundingRate"])) for x in r if t0 < x["time"] <= t1]
            if len(r) < 500:
                break
            start = max(x["time"] for x in r) + 1
        out[c] = rows
    return out


# ---------------------------------------------------------------- paper maths (pure)

def new_slice(cash):
    return {"eq": cash, "pos": {}, "fees": 0.0, "funding": 0.0, "trades": 0, "blown": False}


def mark(sl, price_of):
    for coin, p in sl["pos"].items():
        px = price_of(coin)
        if px:
            sl["eq"] += p[0] * (px - p[1])
            p[1] = px
    if sl["eq"] <= 0 and not sl["blown"]:
        sl.update(eq=0.0, pos={}, blown=True)


def gross(sl):
    return sum(abs(p[0] * p[1]) for p in sl["pos"].values())


def rebalance(sl, coin, target, px):
    """Move the slice's position in `coin` to `target` at `px`, within the leverage cap.
    Returns (delta, fee) or None when there is nothing to trade."""
    if sl["blown"] or not px:
        return None
    cur = sl["pos"].get(coin, [0.0, px])[0]
    other = gross(sl) - abs(cur * sl["pos"].get(coin, [0.0, px])[1])
    room = max(MAX_LEV * sl["eq"] - other, 0.0)
    if abs(target) * px > room:
        target = math.copysign(room / px, target)
    delta = target - cur
    if abs(delta) * px < 1.0:
        return None
    fee = abs(delta) * px * (FEE + SLIP)
    sl["eq"] -= fee
    sl["fees"] += fee
    sl["trades"] += 1
    if abs(target) * px < 1.0:
        sl["pos"].pop(coin, None)
    else:
        sl["pos"][coin] = [target, px]
    return delta, fee


def pay_funding(sl, coin, rate, px):
    p = sl["pos"].get(coin)
    if not p or sl["blown"] or not px:
        return 0.0
    amt = -p[0] * px * rate  # longs pay a positive rate, shorts receive it
    sl["eq"] += amt
    sl["funding"] += amt
    return amt


def account_equity(acct):
    return sum(s["eq"] for s in acct["slices"].values()) + acct.get("cash", 0.0)


# ---------------------------------------------------------------- the run

def load_group(name):
    return json.load(open(os.path.join(HERE, name)))


def snapshot_leader(addr):
    st = post({"type": "clearinghouseState", "user": addr})
    pos = {p["position"]["coin"]: float(p["position"]["szi"]) for p in st["assetPositions"] if main_dex(p["position"]["coin"])}
    return pos, {p["position"]["coin"]: float(p["position"]["positionValue"]) for p in st["assetPositions"] if main_dex(p["position"]["coin"])}


def ensure_leader(led, addr, label, t, now):
    """Add a leader the first time any account copies it. Returns (current positions, equity at t)."""
    ts, eq, _, _ = account_history(addr, t - H, now)
    e = interp(ts, eq, t)
    pos, vals = snapshot_leader(addr)
    if addr not in led["leaders"]:
        recent = post({"type": "userFills", "user": addr}) or []
        ok = e >= MIN_LEADER_EQ
        led["leaders"][addr] = {"label": label, "status": "active" if ok else "left",
                                "note": "" if ok else f"had only ${e:,.0f} when copying started",
                                "base_eq": e, "eq": e, "flow": 0.0,
                                "last_fill": max((x["time"] for x in recent), default=None), "pos": {}}
    led["leaders"][addr]["pos"] = {c: round(v) for c, v in vals.items()}
    return pos, e


def open_account(led, aid, t, now, mids):
    """Open a $100k account at time t holding its leaders' current positions (scaled), at current prices."""
    cfg = ACCOUNTS[aid]
    live = []
    for addr, label in load_group(cfg["group"]).items():
        pos, e = ensure_leader(led, addr, label, t, now)
        if led["leaders"][addr]["status"] == "active" and e >= MIN_LEADER_EQ:
            live.append((addr, pos, e))
    acct = {"name": cfg["name"], "delay": cfg["delay"], "group": cfg["group"], "opened": t,
            "cash": 0.0 if live else START_CASH, "slices": {}, "curve": [], "trades": []}
    for addr, pos, e in live:
        sl = acct["slices"][addr] = new_slice(START_CASH / len(live))
        for coin, size in pos.items():
            if coin in mids and size:
                r = rebalance(sl, coin, size * sl["eq"] / e, mids[coin])
                if r:
                    acct["trades"].append([t, addr, coin, r[0], mids[coin], r[1]])
    acct["curve"].append([t, round(account_equity(acct), 2), mids.get("BTC")])
    led["accounts"][aid] = acct
    led["log"].append({"ts": t, "msg": f"Opened {cfg['name']}: copying {len(live)} of "
                                         f"{len(load_group(cfg['group']))} wallets (${START_CASH / max(len(live), 1):,.0f} each)"})


def start_ledger(now):
    t = now - SETTLE
    mids = {k: float(v) for k, v in post({"type": "allMids"}).items()}
    led = {"version": 1, "created": t, "synced_to": t, "runs": 0, "leaders": {}, "accounts": {}, "log": []}
    for aid in ACCOUNTS:
        open_account(led, aid, t, now, mids)
    return led


def run(led, now, min_step=MIN_STEP):
    t0, t1 = led["synced_to"], now - SETTLE
    if t1 - t0 < min_step:
        return led, False
    began, CALLS["n"] = time.time(), 0
    mids = {k: float(v) for k, v in post({"type": "allMids"}).items()}
    fetch_from = t0 - MAX_DELAY
    events, coins, n_fills = [], {"BTC"}, 0
    hist = {}
    for addr, L in led["leaders"].items():
        if L["status"] != "active":
            continue
        fills = leader_fills(addr, fetch_from, t1)
        n_fills += len([f for f in fills if f["time"] > t0])
        if fills:
            L["last_fill"] = max(L["last_fill"] or 0, fills[-1]["time"])
        hist[addr] = account_history(addr, fetch_from, now)
        ts, eq, _, _ = hist[addr]
        for t, coin, lpos in leader_targets(fills):
            coins.add(coin)
            events.append((t, "fill", addr, coin, lpos, interp(ts, eq, t)))
    for acct in led["accounts"].values():
        for sl in acct["slices"].values():
            coins.update(sl["pos"])
    prices = Prices(fetch_from, t1, mids)
    fund = funding(sorted(coins), t0, t1)
    hours = list(range((t0 // H + 1) * H, t1 + 1, H))
    for aid, acct in led["accounts"].items():
        d = acct["delay"]
        evs = [(t + d, 0, a, c, p, e) for t, _, a, c, p, e in events if t0 < t + d <= t1]
        evs += [(ft, 1, None, c, r, None) for c, rows in fund.items() for ft, r in rows]
        evs += [(h, 2, None, None, None, None) for h in hours]
        for t, kind, addr, coin, val, leq in sorted(evs, key=lambda e: (e[0], e[1])):
            if kind == 0:
                sl = acct["slices"].get(addr)
                if sl is None or sl["blown"]:
                    continue
                mark(sl, lambda c: prices.at(c, t))
                px = prices.at(coin, t)
                target = val * sl["eq"] / leq if leq and leq > 0 else 0.0
                r = rebalance(sl, coin, target, px)
                if r:
                    acct["trades"].append([t, addr, coin, round(r[0], 8), px, round(r[1], 4)])
            elif kind == 1:
                for sl in acct["slices"].values():
                    if coin in sl["pos"]:
                        pay_funding(sl, coin, val, prices.at(coin, t))
            else:
                for sl in acct["slices"].values():
                    mark(sl, lambda c: prices.at(c, t))
                acct["curve"].append([t, round(account_equity(acct), 2), prices.at("BTC", t)])
        for sl in acct["slices"].values():
            mark(sl, lambda c: mids.get(c) or prices.at(c, t1))
        acct["curve"].append([t1, round(account_equity(acct), 2), mids.get("BTC") or prices.at("BTC", t1)])
        acct["trades"] = acct["trades"][-1500:]
        acct["curve"] = compact(acct["curve"], t1)
    # leader money flows: equity change that trading PnL doesn't explain = deposits/withdrawals
    for addr, (ts, eq, pts, pnl) in hist.items():
        L = led["leaders"][addr]
        e0, e1 = interp(ts, eq, t0), interp(ts, eq, t1)
        flow = (e1 - e0) - (interp(pts, pnl, t1) - interp(pts, pnl, t0))
        L["flow"] += flow
        L["eq"] = e1
        if L["flow"] < -LEFT_FRAC * L["base_eq"]:
            L["status"], L["note"] = "left", f"moved out ${-L['flow']:,.0f} since copying started"
            for acct in led["accounts"].values():
                sl = acct["slices"].get(addr)
                for coin in list((sl or {}).get("pos", {})):
                    px = mids.get(coin)
                    r = rebalance(sl, coin, 0.0, px)
                    if r:
                        acct["trades"].append([t1, addr, coin, round(r[0], 8), px, round(r[1], 4)])
            led["log"].append({"ts": t1, "msg": f"Stopped copying {L['label']}: {L['note']}"})
    for addr, L in led["leaders"].items():
        if L["status"] == "active":
            L["pos"] = {c: round(v) for c, v in snapshot_leader(addr)[1].items()}
    for aid in ACCOUNTS:
        if aid not in led["accounts"]:
            open_account(led, aid, t1, now, mids)
    led["synced_to"] = t1
    led["runs"] += 1
    led["last_run"] = {"ts": now, "from": t0, "to": t1, "leader_fills": n_fills, "api_calls": CALLS["n"],
                       "seconds": round(time.time() - began, 1), "price_interval": prices.interval}
    led["log"] = led["log"][-100:]
    return led, True


def compact(curve, now):
    """Hourly points for the last 30 days, one per 6 hours before that."""
    keep, last = [], None
    for p in curve:
        if p[0] >= now - 30 * 86400_000 or last is None or p[0] - last >= 6 * H:
            keep.append(p)
            last = p[0]
    return keep


def main(path):
    now = int(time.time() * 1000)
    if os.path.exists(path):
        led, changed = run(json.load(open(path)), now)
    else:
        led, changed = start_ledger(now), True
    if changed:
        tmp = path + ".tmp"
        json.dump(led, open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, path)
    lr = led.get("last_run") or {}
    print(f"synced to {time.strftime('%Y-%m-%d %H:%M', time.gmtime(led['synced_to'] / 1000))} UTC | "
          f"changed={changed} | leader fills {lr.get('leader_fills', 0)} | api calls {lr.get('api_calls', CALLS['n'])} | "
          + " | ".join(f"{a['name']} ${account_equity(a):,.0f}" for a in led["accounts"].values()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "copy_ledger.json")
