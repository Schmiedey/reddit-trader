"""Author credibility: how often has this person's bullish/bearish call beaten SPY?"""
from datetime import date, timedelta

from ..util import log

PRIOR_STRENGTH = 4   # a new author counts as 4 "virtual" calls at their prior hit rate
HORIZON = 10         # trading days a call is judged over
JUNK = {"[deleted]", "AutoModerator"}


def prior(age_days=None, karma=None, followers=None):
    """Starting hit-rate guess from account metadata, before any calls are scored."""
    p = 0.45
    if age_days is not None:
        p += -0.15 if age_days < 30 else (0.05 if age_days > 365 else 0)
    if karma is not None:
        p += 0.05 if karma > 5000 else (-0.1 if karma < 50 else 0)
    if followers is not None and followers > 5000:
        p += 0.05
    return min(0.65, max(0.2, p))


def credibility(hits, misses, p0):
    return (hits + PRIOR_STRENGTH * p0) / (hits + misses + PRIOR_STRENGTH)


def author_weight(name, row, fc):
    """Weight of one author's opinion. 0 = ignored (deleted, brand-new, or near-zero karma)."""
    if not name or name in JUNK:
        return 0.0
    age = row["age_days"] if row else None
    karma = row["karma"] if row else None
    followers = row["followers"] if row else None
    if age is not None and age < fc["min_account_age_days"]:
        return 0.0
    if karma is not None and karma < fc["min_karma"]:
        return 0.0
    hits, misses = (row["hits"] or 0, row["misses"] or 0) if row else (0, 0)
    c = credibility(hits, misses, prior(age, karma, followers))
    return (c / 0.5) ** 2  # 0.7 credibility -> ~2x weight, 0.3 -> ~0.36x


def fwd_return(bars, day, horizon=HORIZON):
    idx = next((i for i, b in enumerate(bars) if b["d"] >= day), None)
    if idx is None or idx + horizon >= len(bars):
        return None
    return bars[idx + horizon]["c"] / bars[idx]["c"] - 1


def score_calls(con, today=None):
    """Grade calls old enough to judge; update each author's hits/misses. Returns calls graded."""
    from ..sources.prices import daily_bars

    today = today or date.today()
    rows = con.execute(
        "SELECT rowid, * FROM calls WHERE scored=0 AND day<=?", ((today - timedelta(days=16)).isoformat(),)
    ).fetchall()
    if not rows:
        return 0
    oldest = max(date.fromisoformat(min(r["day"] for r in rows)), today - timedelta(days=200))
    bars = daily_bars(con, {r["ticker"] for r in rows} | {"SPY"}, oldest - timedelta(days=3))
    stale = (today - timedelta(days=60)).isoformat()
    graded = 0
    for r in rows:
        tr = fwd_return(bars.get(r["ticker"], []), r["day"])
        sr = fwd_return(bars.get("SPY", []), r["day"])
        if tr is None or sr is None:
            if r["day"] < stale:
                con.execute("UPDATE calls SET scored=-1 WHERE rowid=?", (r["rowid"],))
            continue
        excess = r["direction"] * (tr - sr)
        con.execute("UPDATE calls SET scored=1, excess=? WHERE rowid=?", (excess, r["rowid"]))
        con.execute("INSERT OR IGNORE INTO authors(source, author) VALUES(?,?)", (r["source"], r["author"]))
        col = "hits" if excess > 0 else "misses"
        con.execute(f"UPDATE authors SET {col}={col}+1 WHERE source=? AND author=?", (r["source"], r["author"]))
        graded += 1
    con.commit()
    log(f"credibility: graded {graded} calls")
    return graded
