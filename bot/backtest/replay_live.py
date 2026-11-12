"""Replay: run the LIVE daily job (bot.trader.run_daily, unchanged) once per past trading day on a
copy of the history database, with prices cut off at each day. This is the real bot end to end -
scoring, pump guard, orders, next-open fills, stops, 10-day exits - not a separate simulator.

    uv run python -m bot.backtest.replay_live [--start 2025-08-14] [--end 2026-11-12]
"""
import argparse
import shutil
from collections import defaultdict

from .. import config  # noqa: F401
from ..db import DATA, connect
from ..sources import prices
from ..trader import run_daily
from .build import BT_DB, bt_connect, load_bars

REPLAY_DB = DATA / "backtest" / "replay.db"


def main(start, end, quiet=True):
    shutil.copy(BT_DB, REPLAY_DB)
    con = connect(REPLAY_DB)
    for t in ("paper_account", "paper_positions", "paper_orders", "equity", "signals", "decisions", "blocked",
              "signal_grades"):
        con.execute(f"DELETE FROM {t}")
    con.execute("UPDATE authors SET hits=0, misses=0")   # track records must be re-earned day by day
    con.execute("UPDATE calls SET scored=0, excess=NULL")
    con.commit()
    con.close()
    all_bars = load_bars(bt_connect(), 6)
    cur = {"day": None}

    def fake_bars(_con, syms, since):
        lo = since.isoformat()
        return {s: [b for b in all_bars.get(s, []) if lo <= b["d"] <= cur["day"]] for s in syms if s in all_bars}

    prices.daily_bars = fake_bars
    run_daily.BARS_FN = fake_bars
    if quiet:
        run_daily.log = lambda *_: None
        from ..filter import credibility
        credibility.log = lambda *_: None
    days = [b["d"] for b in all_bars["SPY"] if start <= b["d"] <= end]
    for n, d in enumerate(days):
        cur["day"] = d
        run_daily.main(["--as-of", d, "--db", str(REPLAY_DB)])
        if n % 40 == 0:
            print(f"replayed {d} ({n + 1}/{len(days)})", flush=True)
    return summarize()


def summarize():
    con = connect(REPLAY_DB)
    eq = con.execute("SELECT day, equity, spy FROM equity ORDER BY day").fetchall()
    orders = con.execute("SELECT sym, side, qty, fill_day, fill_price, reason FROM paper_orders "
                         "WHERE status='filled' ORDER BY fill_day, id").fetchall()
    open_, trades = {}, []
    for o in orders:
        if o["side"] == "BUY":
            open_[o["sym"]] = o
        elif o["sym"] in open_:
            b = open_.pop(o["sym"])
            trades.append((o["fill_price"] / b["fill_price"] - 1, b["reason"].split(":")[0], o["reason"], b["fill_day"], o["sym"]))
    months, prev = {}, eq[0]["equity"]
    last = {}
    for r in eq:
        last[r["day"][:7]] = r["equity"]
    for m in sorted(last):
        months[m] = last[m] / prev - 1
        prev = last[m]
    peak, dd = eq[0]["equity"], 0.0
    for r in eq:
        peak = max(peak, r["equity"])
        dd = min(dd, r["equity"] / peak - 1)
    ret = [t[0] for t in trades]
    print(f"\nLIVE-CODE REPLAY {eq[0]['day']} .. {eq[-1]['day']}")
    print(f"  account {eq[-1]['equity'] / eq[0]['equity'] - 1:+.1%}   SPY {eq[-1]['spy'] / eq[0]['spy'] - 1:+.1%}   worst drop {dd:.1%}")
    print(f"  closed trades {len(ret)}, winners {sum(r > 0 for r in ret) / max(len(ret), 1):.0%}, average {sum(ret) / max(len(ret), 1):+.1%}, "
          f"trades of +10% or more {sum(r >= 0.10 for r in ret)}, of -10% or worse {sum(r <= -0.10 for r in ret)}, still open {len(open_)}")
    mv = list(months.values())
    print(f"  months positive {sum(v > 0 for v in mv)}/{len(mv)}, average month {sum(mv) / len(mv):+.2%}, best {max(mv):+.1%}, worst {min(mv):+.1%}")
    by = defaultdict(list)
    for r, kind, *_ in trades:
        by[kind].append(r)
    for k, v in by.items():
        print(f"  {k:<16} {len(v):>4} trades, winners {sum(x > 0 for x in v) / len(v):.0%}, average {sum(v) / len(v):+.1%}")
    exits = defaultdict(int)
    for _, _, why, *_ in trades:
        exits["stop" if "stop" in why else "time" if "held" in why else why[:12]] += 1
    print("  exits:", dict(exits))
    print("  months:", " ".join(f"{m[2:]}:{v:+.1%}" for m, v in months.items()))
    return {"return": eq[-1]["equity"] / eq[0]["equity"] - 1, "trades": len(ret)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-08-14")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args()
    summarize() if a.summary_only else main(a.start, a.end)
