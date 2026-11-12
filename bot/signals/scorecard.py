"""Grade past signals: did high scores actually beat SPY over the next 10 trading days?

A signal made after the close on day D is measured from D+1's open (when the bot would
have bought) to the close 10 trading days later, minus SPY over the same window.
"""
from datetime import date, timedelta

from ..util import log

HORIZON = 10


def buckets(sc):
    """(label, low, high) score buckets tied to the configured buy/exit thresholds."""
    buy, ex = sc["buy_threshold"], sc["exit_threshold"]
    mild = round(buy / 2.5, 2)
    return [
        (f"Buy zone ({buy:+.2f} and up)", buy, 99), (f"Mild bullish ({mild:+.2f} to {buy:+.2f})", mild, buy),
        (f"Neutral ({ex:+.2f} to {mild:+.2f})", ex, mild), (f"Bearish (under {ex:+.2f})", -99, ex),
    ]


def fwd_from_next_open(bars, day, horizon=HORIZON):
    i = next((i for i, b in enumerate(bars) if b["d"] > day), None)
    if i is None or i + horizon - 1 >= len(bars):
        return None
    return bars[i + horizon - 1]["c"] / bars[i]["o"] - 1


def grade_signals(con, today=None):
    from ..sources.prices import daily_bars

    today = today or date.today()
    rows = con.execute(
        """SELECT s.day, s.ticker, s.score FROM signals s
           LEFT JOIN signal_grades g ON g.day = s.day AND g.ticker = s.ticker
           WHERE g.day IS NULL AND s.day <= ?""", ((today - timedelta(days=16)).isoformat(),),
    ).fetchall()
    if not rows:
        return 0
    start = max(date.fromisoformat(min(r["day"] for r in rows)), today - timedelta(days=200))
    bars = daily_bars(con, {r["ticker"] for r in rows} | {"SPY"}, start - timedelta(days=3))
    stale = (today - timedelta(days=60)).isoformat()
    n = 0
    for r in rows:
        tr = fwd_from_next_open(bars.get(r["ticker"], []), r["day"])
        sr = fwd_from_next_open(bars.get("SPY", []), r["day"])
        if tr is None or sr is None:
            if r["day"] < stale:  # no data will ever come; stop retrying
                con.execute("INSERT OR IGNORE INTO signal_grades VALUES(?,?,?,NULL)", (r["day"], r["ticker"], r["score"]))
            continue
        con.execute("INSERT OR IGNORE INTO signal_grades VALUES(?,?,?,?)", (r["day"], r["ticker"], r["score"], tr - sr))
        n += 1
    con.commit()
    log(f"scorecard: graded {n} signals")
    return n


def scorecard(con):
    """[(label, count, avg excess %, % that beat SPY)] per score bucket."""
    from ..config import CFG

    out = []
    for label, lo, hi in buckets(CFG["scoring"]):
        r = con.execute(
            """SELECT COUNT(*) n, AVG(excess) avg, AVG(excess > 0) hit FROM signal_grades
               WHERE excess IS NOT NULL AND score >= ? AND score < ?""", (lo, hi),
        ).fetchone()
        out.append((label, r["n"], (r["avg"] or 0) * 100, (r["hit"] or 0) * 100))
    return out
