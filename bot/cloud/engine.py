"""Cloud paper-trading engine: three $100k PAPER accounts trade Reddit crowd-buying signals side by side.

There is no broker connection anywhere in this package. Orders are rows in SQLite and fills are
computed from Yahoo daily prices, so nothing here can place a real trade.

Data:    Reddit posts from the keyless Arctic Shift archive (bot/sources/arctic.py).
Signals: who BOUGHT what (bot/signals/stance.py), counted per stock over the last 5 trading days.
  - Early build-up: 4+ different buyers, at least double the 5 days before, price up < 10% in 5 days.
  - Most bought:    5+ different buyers, and more buyers than sellers.
Trades:  rules from config.yaml `trading:` (10% per trade, max 10 positions, 3 new a day, wide stop,
         sell at the open after 10 trading days). Buys and sells fill at the next market open.
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ..config import CFG, DATA
from ..db import connect
from ..filter.pump_guard import check
from ..ingest import store_item
from ..signals.crowd import _cut, ensure_stances
from ..signals.technicals import technicals
from ..sources import arctic
from ..sources.prices import daily_bars, load_valid_symbols
from ..trader.broker import LocalPaperBroker
from ..trader.risk import plan_entries, plan_exits, position_size, stop_and_target

ET = ZoneInfo("America/New_York")
SUBS = ["wallstreetbets", "stocks", "investing", "StockMarket", "options"]  # the subreddits the backtest used
ACCOUNTS = {
    "buildup": {"name": "Early build-up", "rules": ("buildup",),
                "blurb": "Buyers doubled versus the week before, and the price has not run yet."},
    "mostbought": {"name": "Most bought", "rules": ("mostbought",),
                   "blurb": "Five or more different buyers this week, and more buyers than sellers."},
    "both": {"name": "Both signals", "rules": ("buildup", "mostbought"),
             "blurb": "Buys when either signal fires."},
}
RULE_NAMES = {"buildup": "Early build-up", "mostbought": "Most bought"}
BUILD_MIN, BUILD_RATIO, BUILD_MAX_RET5, MOST_MIN = 4, 2, 0.10, 5
COOLDOWN = 10  # trading days. A signal is only bought on the day it first appears, then ignored this long.
KEEP_DAYS = 40


def rule_hits(f, ret5):
    """Which signals fire for one stock, from its crowd counts and 5-day price change."""
    hits = []
    b, prev, s = f["buyers5"], f["buyers_prev5"], f["sellers5"]
    if prev is not None and ret5 is not None and b >= BUILD_MIN and b >= BUILD_RATIO * max(prev, 1) and ret5 < BUILD_MAX_RET5:
        hits.append("buildup")
    if f["warm5"] and b >= MOST_MIN and b > s:
        hits.append("mostbought")
    return hits


class Engine:
    def __init__(self, home=None, park=None, bars_fn=None):
        self.home = Path(home or os.environ.get("CLOUD_HOME") or DATA / "cloud")
        self.home.mkdir(parents=True, exist_ok=True)
        self.con = connect(self.home / "shared.db")
        self.con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, val TEXT)")
        self.park = bool(CFG.get("cloud", {}).get("park_idle_cash_in_spy")) if park is None else park
        self.bars_fn = bars_fn or (lambda con, syms, start: daily_bars(con, syms, start, ttl=0))
        self._acct = {}

    # ---------- small helpers ----------
    def get(self, key, default=None, con=None):
        r = (con or self.con).execute("SELECT val FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, val, con=None):
        (con or self.con).execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(val)))
        (con or self.con).commit()

    def acct(self, aid):
        if aid not in self._acct:
            c = connect(self.home / f"acct_{aid}.db")
            c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, val TEXT)")
            self._acct[aid] = c
        return self._acct[aid]

    def brokers(self):
        return {aid: LocalPaperBroker(self.acct(aid), CFG) for aid in ACCOUNTS}

    # ---------- collect ----------
    def _store(self, item, valid):
        row = self.con.execute("SELECT body FROM posts WHERE id=?", (item["id"],)).fetchone()
        if not item["body"] and (row is None or row["body"]):
            # Removed by moderators since we first saw it. The backtest only ever saw such posts
            # without their text, so forget the text here too and re-read the post from its title.
            self.con.execute("DELETE FROM posts WHERE id=?", (item["id"],))
            self.con.execute("DELETE FROM mentions WHERE post_id=?", (item["id"],))
            self.con.execute("DELETE FROM stances WHERE post_id=?", (item["id"],))
        return store_item(self.con, item, valid)

    def collect(self, now=None, backfill_days=21, overlap_h=3, log=print):
        """Fetch new posts for every subreddit. Re-reads the last `overlap_h` hours to catch late arrivals
        and removals. Returns {"new", "failed"}; a subreddit that fails is retried on the next call."""
        now = now or time.time()
        valid = load_valid_symbols(self.con)
        new, failed = 0, []
        for sub in SUBS:
            seen = self.get(f"seen:{sub}")
            lo = seen - overlap_h * 3600 if seen else now - backfill_days * 86400
            try:
                while lo < now:
                    hi = min(lo + 43200, now)  # half-day slices: the archive times out on long busy ranges
                    for p in arctic.fetch_range(sub, lo, hi, tries=6):
                        new += self._store(arctic.to_item(p, sub), valid)
                    lo = hi
                    self.set(f"seen:{sub}", hi)
            except RuntimeError as e:
                failed.append(sub)
                log(str(e))
        ensure_stances(self.con)
        old = now - KEEP_DAYS * 86400
        for table in ("posts", "mentions", "stances"):
            self.con.execute(f"DELETE FROM {table} WHERE created < ?", (old,))
        self.con.commit()
        out = {"ts": now, "new": new, "failed": failed}
        self.set("collect", out)
        if not failed:
            self.set("collect_ok", now)
        return out

    # ---------- signals ----------
    def features(self, now):
        """{ticker: crowd counts} from posts in the last 10 trading sessions (5 recent + 5 before)."""
        pump = {s.lower() for s in CFG["reddit"].get("pump_watch", [])}
        c5, c10 = _cut(now, 5), _cut(now, 10)
        b5, bprev, s5 = defaultdict(set), defaultdict(set), defaultdict(set)
        gain, loss = defaultdict(int), defaultdict(int)
        for r in self.con.execute("SELECT ticker, community, author, created, stance FROM stances "
                                  "WHERE created > ? AND created <= ? AND stance != 'NONE'", (c10, now)):
            if r["community"] in pump:
                continue
            t, recent = r["ticker"], r["created"] > c5
            if r["stance"] in ("BUY", "DD"):
                (b5 if recent else bprev)[t].add(r["author"])
            elif recent and r["stance"] == "SELL":
                s5[t].add(r["author"])
            elif recent and r["stance"] == "LOST":
                loss[t] += 1
            elif recent and r["stance"] == "WON":
                gain[t] += 1
        first = self.con.execute("SELECT MIN(created) FROM stances").fetchone()[0]
        warm10, warm5 = first is not None and first <= c10, first is not None and first <= c5
        return {t: {"buyers5": len(b5[t]), "buyers_prev5": len(bprev[t]) if warm10 else None, "sellers5": len(s5[t]),
                    "gain5": gain[t], "loss5": loss[t], "warm5": warm5}
                for t in set(b5) | set(s5) | set(gain) | set(loss)}

    # ---------- the evening run ----------
    def daily(self, now=None):
        """After the close: check stops, queue sells for positions held 10 days, queue tomorrow's buys."""
        now = now or time.time()
        today = datetime.fromtimestamp(now, ET).date()
        day = today.isoformat()
        tc, sc, fc = CFG["trading"], CFG["scoring"], CFG["filter"]
        feats = self.features(now)
        cands = sorted(t for t, f in feats.items() if f["buyers5"] >= BUILD_MIN and t != "SPY")  # SPY is the yardstick
        brokers = self.brokers()
        held_all = {s for b in brokers.values() for s in b.positions()} | {o["sym"] for b in brokers.values() for o in b.pending()}
        bars = self.bars_fn(self.con, set(cands) | held_all | {"SPY"}, today - timedelta(days=130))
        bars = {s: [b for b in v if b["d"] <= day] for s, v in bars.items()}
        spy = bars.get("SPY") or []
        if not spy or spy[-1]["d"] != day:
            return {"ok": False, "why": f"no market data for {day} yet (holiday or data delay)"}
        rows = []
        for t in cands:
            f, tb = feats[t], bars.get(t) or []
            tech = technicals(tb)
            if not tb or tb[-1]["d"] != day:
                blocked = "no price today"
            elif len(tb) < 51:
                blocked = "under 51 days of price history"
            else:
                blocked = check({}, tb, fc)
            rows.append({"ticker": t, **f, "price": tb[-1]["c"] if tb else None,
                         "ret5": round(tech["ret5"], 4) if tech else None, "atr": tech["atr_pct"] if tech else None,
                         "hits": rule_hits(f, tech["ret5"]) if tech and not blocked else [], "blocked": blocked})
        rows += [{"ticker": t, **f, "price": None, "ret5": None, "atr": None, "hits": [], "blocked": None}
                 for t, f in feats.items() if 2 <= f["buyers5"] < BUILD_MIN and t != "SPY"]
        rows.sort(key=lambda r: (-len(r["hits"]), -r["buyers5"], r["ticker"]))
        out = {}
        for aid, a in ACCOUNTS.items():
            acon, broker = self.acct(aid), brokers[aid]
            if self.park:
                self._unpark(aid, broker, spy[-1]["c"])
            events = broker.process(bars)
            held = broker.positions()
            pending = {o["sym"] for o in broker.pending()}
            held_days = {s: int(np.busday_count(p["entry_day"], day)) for s, p in held.items()}
            for sym, why in plan_exits(held_days, {}, tc, sc):
                if sym not in pending:
                    broker.queue(day, sym, "SELL", held[sym]["qty"], why)
            equity = broker.equity(bars)
            # Only a NEW signal is bought (as in the backtest): the first day it fires, then a cooldown.
            marks = self.get("signal_days", con=acon) or {}
            signals = {}
            for r in rows:
                if not set(r["hits"]) & set(a["rules"]):
                    continue
                last = marks.get(r["ticker"])
                if last is None or np.busday_count(last, day) >= COOLDOWN:
                    marks[r["ticker"]] = day
                    r.setdefault("fresh", []).append(aid)
                    signals[r["ticker"]] = {"ticker": r["ticker"], "score": round(0.5 + min(r["buyers5"], 49) / 100, 3),
                                            "agree": ["reddit"], "thin": False, "row": r}
            self.set("signal_days", {t: d for t, d in marks.items() if np.busday_count(d, day) < COOLDOWN}, con=acon)
            buys = []
            for s in plan_entries(signals, set(held) | pending, tc, sc, "risk-on"):
                r = s["row"]
                stop_pct, take_pct = stop_and_target(r["atr"], tc)
                qty = position_size(equity, r["price"], tc["position_pct"], stop_pct, tc["risk_per_trade_pct"])
                if qty < 1:
                    continue
                rule = next(h for h in r["hits"] if h in a["rules"])
                prev = "" if r["buyers_prev5"] is None else f", up from {r['buyers_prev5']} the week before"
                why = (f"{RULE_NAMES[rule]}: {r['buyers5']} buyers in 5 days{prev}, {r['sellers5']} sellers; "
                       f"stop -{stop_pct:.0%}, sell after {tc['max_hold_days'] + 1} trading days")
                broker.queue(day, r["ticker"], "BUY", qty, why, stop_pct, take_pct)
                buys.append(r["ticker"])
                r.setdefault("bought", []).append(aid)
            acon.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)", (day, equity, spy[-1]["c"]))
            if self.park:
                self._park(aid, broker, spy[-1]["c"])
            acon.commit()
            out[aid] = {"events": events, "buys": buys, "equity": round(equity, 2)}
        self.set("signals", {"day": day, "ts": now, "rows": rows[:60]})
        self.set("prices", {"ts": now, "px": {s: v[-1]["c"] for s, v in bars.items() if v}})
        self.set("last_daily", day)
        return {"ok": True, "day": day, "accounts": out}

    def _unpark(self, aid, broker, spy_close):
        m = self.get("parked", con=self.acct(aid))
        if m:
            broker._add_cash(m["shares"] * (spy_close - m["px"]))
            self.set("parked", None, con=self.acct(aid))

    def _park(self, aid, broker, spy_close):
        idle = broker.cash()
        if idle > 0:
            self.set("parked", {"shares": idle / spy_close, "px": spy_close}, con=self.acct(aid))

    # ---------- the morning run ----------
    def morning_fills(self, now=None):
        """After the open: fill yesterday's orders at today's opening price (stops wait for the close)."""
        now = now or time.time()
        day = datetime.fromtimestamp(now, ET).date()
        brokers = self.brokers()
        syms = {o["sym"] for b in brokers.values() for o in b.pending()}
        if not syms:
            return {"ok": True, "fills": []}
        bars = self.bars_fn(self.con, syms | {"SPY"}, day - timedelta(days=10))
        spy = bars.get("SPY") or []
        if not spy or spy[-1]["d"] != day.isoformat():
            return {"ok": False, "why": "today's opening prices are not available yet"}
        fills = [f"{ACCOUNTS[aid]['name']}: {e}" for aid, b in brokers.items() for e in b.fill_orders(bars)]
        self.mark(now, bars)
        return {"ok": True, "fills": fills}

    def mark(self, now=None, bars=None):
        """Refresh the latest prices of everything held, for the dashboard. Trades nothing."""
        now = now or time.time()
        day = datetime.fromtimestamp(now, ET).date()
        syms = {s for b in self.brokers().values() for s in b.positions()} | {"SPY"}
        bars = bars if bars is not None and syms <= set(bars) else self.bars_fn(self.con, syms, day - timedelta(days=10))
        px = (self.get("prices") or {"px": {}})["px"]
        px.update({s: v[-1]["c"] for s, v in bars.items() if v})
        self.set("prices", {"ts": now, "px": px})

    # ---------- dashboard state ----------
    def state(self, now=None):
        now = now or time.time()
        prices = self.get("prices") or {"ts": None, "px": {}}
        px = prices["px"]
        tc = CFG["trading"]
        today = datetime.fromtimestamp(now, ET).date().isoformat()
        accounts = []
        for aid, a in ACCOUNTS.items():
            acon = self.acct(aid)
            broker = LocalPaperBroker(acon, CFG)
            reasons = {r["sym"]: r["reason"] for r in acon.execute(
                "SELECT sym, reason FROM paper_orders WHERE side='BUY' AND status='filled' ORDER BY id")}
            positions, mv = [], 0.0
            for p in broker.positions().values():
                last = px.get(p["sym"], p["entry_price"])
                mv += p["qty"] * last
                held = int(np.busday_count(p["entry_day"], today))
                positions.append({"sym": p["sym"], "qty": p["qty"], "entry": round(p["entry_price"], 2),
                                  "entry_day": p["entry_day"], "last": round(last, 2), "value": round(p["qty"] * last, 2),
                                  "pnl": round(p["qty"] * (last - p["entry_price"]), 2),
                                  "pnl_pct": round(last / p["entry_price"] - 1, 4), "stop": round(p["stop"], 2),
                                  "days_held": held, "days_left": max(0, tc["max_hold_days"] + 1 - held),
                                  "reason": reasons.get(p["sym"], "")})
            positions.sort(key=lambda p: -p["value"])
            parked = self.get("parked", con=acon)
            parked_pnl = parked["shares"] * (px.get("SPY", parked["px"]) - parked["px"]) if parked else 0.0
            cash = broker.cash()
            value = cash + mv + parked_pnl
            open_, trades = {}, []
            for o in acon.execute("SELECT * FROM paper_orders WHERE status='filled' ORDER BY fill_day, id"):
                if o["side"] == "BUY":
                    open_[o["sym"]] = o
                elif o["sym"] in open_:
                    b = open_.pop(o["sym"])
                    trades.append({"sym": o["sym"], "in_day": b["fill_day"], "out_day": o["fill_day"],
                                   "entry": round(b["fill_price"], 2), "exit": round(o["fill_price"], 2), "qty": o["qty"],
                                   "ret": round(o["fill_price"] / b["fill_price"] - 1, 4),
                                   "pnl": round(o["qty"] * (o["fill_price"] - b["fill_price"]), 2),
                                   "why_in": b["reason"], "why_out": o["reason"]})
            curve = [[r["day"], round(r["equity"], 2), r["spy"]] for r in acon.execute("SELECT * FROM equity ORDER BY day")]
            start_cash = CFG["paper"]["starting_cash"]
            spy0 = curve[0][2] if curve else None
            rets = [t["ret"] for t in trades]
            accounts.append({
                "id": aid, "name": a["name"], "blurb": a["blurb"], "rules": [RULE_NAMES[r] for r in a["rules"]],
                "start": start_cash, "value": round(value, 2), "cash": round(cash, 2), "invested": round(mv, 2),
                "ret": round(value / start_cash - 1, 4),
                "spy_ret": round(px["SPY"] / spy0 - 1, 4) if spy0 and px.get("SPY") else 0.0,
                "day_change": round(value / curve[-1][1] - 1, 4) if curve else 0.0,
                "positions": positions,
                "pending": [{"sym": o["sym"], "side": o["side"], "qty": o["qty"], "day": o["day"], "reason": o["reason"]}
                            for o in broker.pending()],
                "trades": trades[::-1][:100], "curve": curve,
                "stats": {"closed": len(rets), "wins": sum(r > 0 for r in rets),
                          "avg": round(sum(rets) / len(rets), 4) if rets else None,
                          "best": max(rets) if rets else None, "worst": min(rets) if rets else None,
                          "pnl": round(sum(t["pnl"] for t in trades), 2)},
            })
        first = self.con.execute("SELECT MIN(created), MAX(created), COUNT(*) FROM stances").fetchone()
        n24 = self.con.execute("SELECT COUNT(DISTINCT post_id) FROM stances WHERE created > ?", (now - 86400,)).fetchone()[0]
        return {
            "now": now, "mode": "paper", "starting_cash": CFG["paper"]["starting_cash"], "accounts": accounts,
            "signals": self.get("signals") or {"day": None, "rows": []},
            "prices_ts": prices["ts"], "spy": px.get("SPY"), "last_daily": self.get("last_daily"),
            "collect": self.get("collect"), "collect_ok": self.get("collect_ok"),
            "data": {"first_post": first[0], "latest_post": first[1], "stances": first[2], "posts_24h": n24,
                     "warm": bool(first[0] and first[0] <= _cut(now, 10))},
            "rules": {"position_pct": tc["position_pct"], "max_positions": tc["max_positions"],
                      "max_new_per_day": tc["max_new_per_day"], "hold_days": tc["max_hold_days"] + 1,
                      "max_stop_pct": tc["max_stop_pct"], "park": self.park, "subs": SUBS},
        }

    # ---------- persistence (the ledger survives restarts on a git branch) ----------
    def export(self, now=None):
        now = now or time.time()
        accounts = {}
        for aid in ACCOUNTS:
            acon = self.acct(aid)
            LocalPaperBroker(acon, CFG)
            accounts[aid] = {
                "cash": acon.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0],
                "positions": [dict(r) for r in acon.execute("SELECT * FROM paper_positions")],
                "orders": [dict(r) for r in acon.execute("SELECT * FROM paper_orders ORDER BY id")],
                "equity": [list(r) for r in acon.execute("SELECT day, equity, spy FROM equity ORDER BY day")],
                "meta": {r[0]: json.loads(r[1]) for r in acon.execute("SELECT key, val FROM meta")},
            }
        return {"v": 1, "saved": now, "accounts": accounts,
                "meta": {r[0]: json.loads(r[1]) for r in self.con.execute("SELECT key, val FROM meta")},
                "stances": [list(r) for r in self.con.execute(
                    "SELECT post_id, ticker, community, author, created, stance FROM stances WHERE created > ? ORDER BY created",
                    (now - KEEP_DAYS * 86400,))]}

    def load(self, led):
        self.con.execute("DELETE FROM stances")
        self.con.execute("DELETE FROM meta")
        self.con.executemany("INSERT OR IGNORE INTO stances VALUES(?,?,?,?,?,?)", led.get("stances", []))
        self.con.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v)) for k, v in led.get("meta", {}).items()])
        self.con.commit()
        for aid, a in led.get("accounts", {}).items():
            if aid not in ACCOUNTS:
                continue
            acon = self.acct(aid)
            LocalPaperBroker(acon, CFG)
            for t in ("paper_positions", "paper_orders", "equity", "meta"):
                acon.execute(f"DELETE FROM {t}")
            acon.execute("UPDATE paper_account SET cash=? WHERE id=1", (a["cash"],))
            for p in a["positions"]:
                acon.execute("INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?)",
                             [p[k] for k in ("sym", "qty", "entry_price", "entry_day", "stop", "target", "checked_day")])
            for o in a["orders"]:
                acon.execute("INSERT INTO paper_orders(id,day,sym,side,qty,reason,status,fill_day,fill_price,stop_pct,take_pct) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             [o.get(k) for k in ("id", "day", "sym", "side", "qty", "reason", "status", "fill_day",
                                                 "fill_price", "stop_pct", "take_pct")])
            acon.executemany("INSERT INTO equity VALUES(?,?,?)", a["equity"])
            acon.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v)) for k, v in a.get("meta", {}).items()])
            acon.commit()
