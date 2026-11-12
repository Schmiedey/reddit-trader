"""Daily trading run, after the US market closes.

    python -m bot.trader.run_daily              # run now
    python -m bot.trader.run_daily --dry-run    # score and print, queue no orders
    python -m bot.trader.run_daily --scheduled  # launchd mode: weekdays after 16:30 ET, once a day

Order of a run: fill yesterday's orders at today's open, trigger stops/targets on today's
high/low, grade old calls, score tickers, block pumps, then queue exits and buys for
tomorrow's open. Writes data/report.html at the end.
"""
import argparse
import json
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import numpy as np

from ..config import CFG
from ..db import connect
from ..filter.credibility import score_calls
from ..filter.pump_guard import check
from ..report import write_report
from ..signals.crowd import crowd_signals, finalize
from ..signals.score import compute_scores
from ..signals.strategy import apply_strategy
from ..signals.scorecard import grade_signals
from ..signals.technicals import entry_veto, market_regime, technicals
from ..sources.prices import daily_bars
from ..util import log, lower_priority
from .broker import LocalPaperBroker
from .risk import plan_entries, plan_exits, position_size, stop_and_target

ET = ZoneInfo("America/New_York")
BARS_FN = None  # replays swap in cached historical prices (bot/backtest/replay_live.py)


def _load_bars(con, syms, start, cutoff=None):
    bars = (BARS_FN or daily_bars)(con, syms, start)
    if cutoff:  # replay: the bot must not see prices after the replayed day
        c = cutoff.isoformat()
        bars = {s: [b for b in v if b["d"] <= c] for s, v in bars.items()}
    return bars


def _decide(con, sym, action, qty, price, score, reasons, dry):
    con.execute(
        "INSERT INTO decisions(ts,ticker,action,qty,price,score,reasons,dry_run) VALUES(?,?,?,?,?,?,?,?)",
        (time.time(), sym, action, qty, price, score, json.dumps(reasons), int(dry)),
    )
    log(f"{'DRY ' if dry else ''}{action} {qty} {sym} (~${price:.2f})  {reasons.get('why', '')}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--as-of", help="replay: act as if it were 16:35 ET on this date (YYYY-MM-DD)")
    ap.add_argument("--db", help="database file; replays use a copy, never the live one")
    args = ap.parse_args(argv)
    con = connect(args.db, timeout=900)  # the collector can hold the database for minutes; wait for it
    now_et = (datetime.fromisoformat(args.as_of).replace(hour=16, minute=35, tzinfo=ET) if args.as_of
              else datetime.now(ET))
    now_ts, today = now_et.timestamp(), now_et.date()

    if args.scheduled:
        if now_et.weekday() >= 5 or now_et.time() < dtime(16, 30):
            return
        if con.execute("SELECT 1 FROM equity WHERE day=?", (today.isoformat(),)).fetchone():
            return
    lower_priority()
    tc, sc, fc = CFG["trading"], CFG["scoring"], CFG["filter"]
    dry = args.dry_run or tc["dry_run"]
    broker = LocalPaperBroker(con, CFG)

    signals = {t: apply_strategy(s, sc.get("strategy_weights"), sc.get("strategy", "momentum"))
               for t, s in compute_scores(con, CFG, now_ts).items()}
    if sc.get("strategy") == "crowd_swing":
        signals = crowd_signals(con, CFG, signals, now_ts)
    held = broker.positions()
    pending = {o["sym"] for o in broker.pending()}
    entry_days = [p["entry_day"] for p in held.values()]
    start = min([today - timedelta(days=100)] + [datetime.fromisoformat(d).date() - timedelta(days=3) for d in entry_days])
    syms = {t for t, s in signals.items() if not s["thin"]} | set(held) | pending | {"SPY"}
    bars = _load_bars(con, syms, start, today if args.as_of else None)

    for e in broker.process(bars):
        log(e)
    held = broker.positions()
    score_calls(con, today)
    grade_signals(con, today)
    regime = market_regime(bars.get("SPY"))
    log(f"market regime: {regime}")

    day = today.isoformat()
    for t, s in signals.items():
        if s["thin"]:
            continue
        reason = check(s, bars.get(t), fc)
        tech = technicals(bars.get(t))
        s["tech"] = {k: round(v, 3) for k, v in tech.items()} if tech else None
        finalize(s, tech, sc)
        if reason:
            s["blocked"] = reason
            con.execute("INSERT OR REPLACE INTO blocked VALUES(?,?,?)", (day, t, reason))
        elif s["score"] > 0 and tc.get("use_entry_vetoes", True):
            s["veto"] = entry_veto(tech, tc)
        con.execute("INSERT OR REPLACE INTO signals VALUES(?,?,?,?)", (day, t, s["score"], json.dumps(s)))

    held_days = {sym: int(np.busday_count(datetime.fromisoformat(p["entry_day"]).date(), today)) for sym, p in held.items()}
    for sym, why in plan_exits(held_days, signals, tc, sc):
        p = held[sym]
        last = bars.get(sym, [{"c": p["entry_price"]}])[-1]["c"]
        _decide(con, sym, "SELL", p["qty"], last, signals.get(sym, {}).get("score"), {"why": why}, dry)
        if not dry:
            broker.queue(day, sym, "SELL", p["qty"], why)

    equity = broker.equity(bars)
    for s in plan_entries(signals, set(held) | pending, tc, sc, regime):
        price = bars[s["ticker"]][-1]["c"]
        stop_pct, take_pct = stop_and_target((s["tech"] or {}).get("atr_pct"), tc)
        m = s.get("size_mult", 1.0)  # stronger signals may get a bigger position
        qty = position_size(equity, price, tc["position_pct"] * m, stop_pct, tc["risk_per_trade_pct"] * m)
        if qty < 1:
            continue
        if s.get("swing_kind"):
            why = f"{s['swing_kind']}: {s['swing_reason']}; stop -{stop_pct:.0%}, sell after {tc['max_hold_days'] + 1} days"
        else:
            why = (f"score {s['score']:+.2f}, agree: {', '.join(s['agree'])}; {s['voices']} voices, "
                   f"stop -{stop_pct:.0%} / target +{take_pct:.0%}")
        _decide(con, s["ticker"], "BUY", qty, price, s["score"],
                {"why": why, "top_voices": s["top_voices"], "headlines": s["headlines"]}, dry)
        if not dry:
            broker.queue(day, s["ticker"], "BUY", qty, why, stop_pct, take_pct)

    spy = (bars.get("SPY") or [{}])[-1].get("c")
    if not dry:
        con.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)", (day, equity, spy))
    con.commit()
    path = write_report(con, bars) if not args.as_of else "(replay, no report)"
    tradable = [s for s in signals.values() if not s["thin"]]
    log(f"run done: {len(signals)} tickers scored, {len(tradable)} with enough voices, "
        f"paper equity ${equity:,.0f}. Report: {path}")


if __name__ == "__main__":
    main()
