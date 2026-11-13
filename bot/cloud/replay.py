"""Replay the cloud engine over history: the same Engine.daily() the live service runs, once per past
trading day, with prices cut off at that day. Also answers "should idle cash sit in SPY?".

    uv run python -m bot.cloud.replay [--start 2024-08-15] [--end 2026-11-12] [--park] [--save]
"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from ..config import DATA
from ..db import connect
from ..signals.crowd import ensure_stances
from ..backtest.build import BT_DB, bt_connect, load_bars
from .engine import ACCOUNTS, ET, Engine

HOME = DATA / "backtest" / "cloud_replay"
STATIC = Path(__file__).resolve().parent / "static"


def run(start, end, park, bars=None):
    con = bt_connect()
    con.execute("CREATE TABLE IF NOT EXISTS stances(post_id TEXT, ticker TEXT, community TEXT, author TEXT, created REAL, "
                "stance TEXT, PRIMARY KEY(post_id, ticker))")
    ensure_stances(con)  # once, in the history database itself
    bars = bars or load_bars(con, 6)
    con.close()
    shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True)
    src = connect(BT_DB)
    dst = connect(HOME / "shared.db")
    dst.executemany("INSERT OR IGNORE INTO stances VALUES(?,?,?,?,?,?)", src.execute("SELECT * FROM stances").fetchall())
    dst.commit()
    cur = {"day": None}

    def fake(_con, syms, since):
        lo = since.isoformat()
        return {s: [b for b in bars.get(s, []) if lo <= b["d"] <= cur["day"]] for s in syms if s in bars}

    eng = Engine(HOME, park=park, bars_fn=fake)
    for d in [b["d"] for b in bars["SPY"] if start <= b["d"] <= end]:
        cur["day"] = d
        eng.daily(datetime.fromisoformat(d).replace(hour=16, minute=35, tzinfo=ET).timestamp())
    return eng, bars


def summarize(eng, split="2025-08-14"):
    st = eng.state()
    out = {}
    for a in st["accounts"]:
        c = a["curve"]
        peak, dd = c[0][1], 0.0
        for _, e, _ in c:
            peak = max(peak, e)
            dd = min(dd, e / peak - 1)
        i = max(k for k, r in enumerate(c) if r[0] < split) if c[0][0] < split < c[-1][0] else None
        months, prev, last = {}, c[0][1], {}
        for d, e, _ in c:
            last[d[:7]] = e
        for m in sorted(last):
            months[m] = last[m] / prev - 1
            prev = last[m]
        acon = eng.acct(a["id"])
        rets = [r[0] for r in acon.execute(
            "SELECT s.fill_price / b.fill_price - 1 FROM paper_orders s JOIN paper_orders b ON b.sym = s.sym AND b.side='BUY' "
            "AND b.status='filled' AND b.fill_day = (SELECT MAX(fill_day) FROM paper_orders x WHERE x.sym = s.sym AND x.side='BUY' "
            "AND x.status='filled' AND x.fill_day <= s.fill_day) WHERE s.side='SELL' AND s.status='filled'")]
        mv = list(months.values())
        out[a["id"]] = {
            "name": a["name"], "total": c[-1][1] / c[0][1] - 1, "spy": c[-1][2] / c[0][2] - 1, "dd": dd,
            "first": (c[i][1] / c[0][1] - 1, c[i][2] / c[0][2] - 1) if i else None,
            "second": (c[-1][1] / c[i][1] - 1, c[-1][2] / c[i][2] - 1) if i else None,
            "trades": len(rets), "win": sum(r > 0 for r in rets) / max(len(rets), 1), "avg": sum(rets) / max(len(rets), 1),
            "big_wins": sum(r >= 0.10 for r in rets), "big_losses": sum(r <= -0.10 for r in rets),
            "months_pos": sum(v > 0 for v in mv), "months": len(mv), "avg_month": sum(mv) / len(mv), "worst_month": min(mv),
            "monthly": {m: round(v, 4) for m, v in months.items()},
            "curve": [[d, round(e / c[0][1], 4), round(s / c[0][2], 4)] for d, e, s in c],
        }
    return out


def show(res, label):
    print(f"\n{label}")
    print(f"{'account':<16}{'total':>8}{'SPY':>8}{'worst drop':>11}{'unseen yr':>10}{'(SPY)':>8}{'later':>8}{'(SPY)':>8}"
          f"{'trades':>7}{'win':>5}{'avg':>7}{'+10%':>5}{'-10%':>5}{'mo+':>7}{'avg mo':>8}{'worst mo':>9}")
    for r in res.values():
        f, s = r["first"] or (0, 0), r["second"] or (0, 0)
        print(f"{r['name']:<16}{r['total']:>+8.1%}{r['spy']:>+8.1%}{r['dd']:>11.1%}{f[0]:>+10.1%}{f[1]:>+8.1%}{s[0]:>+8.1%}{s[1]:>+8.1%}"
              f"{r['trades']:>7}{r['win']:>5.0%}{r['avg']:>+7.1%}{r['big_wins']:>5}{r['big_losses']:>5}"
              f"{r['months_pos']:>4}/{r['months']:<2}{r['avg_month']:>+8.2%}{r['worst_month']:>+9.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-08-15")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--park", action="store_true", help="only run with idle cash parked in SPY")
    ap.add_argument("--save", action="store_true", help="write the dashboard's backtest file")
    a = ap.parse_args()
    eng, bars = run(a.start, a.end, park=False)
    plain = summarize(eng)
    show(plain, f"CLOUD ENGINE REPLAY {a.start} .. {a.end}: idle cash earns nothing")
    eng, _ = run(a.start, a.end, park=True, bars=bars)
    parked = summarize(eng)
    show(parked, "Same, with idle cash parked in SPY")
    if a.save:
        STATIC.mkdir(exist_ok=True)
        (STATIC / "backtest.json").write_text(json.dumps({"start": a.start, "end": a.end, "split": "2025-08-14",
                                                           "plain": plain, "parked": parked}))
        print("saved", STATIC / "backtest.json")
