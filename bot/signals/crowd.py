"""Crowd swing: the Reddit signals that held up across 27 months of history. See BACKTEST.md.

scoring.loss_rule picks the first signal:
- "bounce" (default, live): 3+ Loss-flair posts on a stock in 5 trading days, losses >= 65% of its
  gain+loss posts. Priority over build-up.
- "capitulation": the same but only after a sharp fall ((3+ and down 10%) or (2+ and down 15%)).
  It looked best on Aug 2025 - Nov 2026 and then LOST money on the unseen Aug 2024 - Aug 2025.
scoring.early_build_up (default on): 4+ distinct buyers in the last 5 trading days, at least double
the 5 days before, price up less than 10% in 5 days.

Backtest of bounce + build-up, rules fixed before the unseen year was downloaded:
+68.9% (SPY +18.1%) Aug 2024 - Aug 2025, +20.5% (SPY +19.1%) Aug 2025 - Nov 2026.

Counts use Reddit posts only (not comments) and skip pump-watch subreddits, like the backtest.
"""
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .stance import stance_for

ET = ZoneInfo("America/New_York")
JUNK = {None, "", "[deleted]", "AutoModerator"}
LOSS_SHARE, LOSS_MIN = 0.65, 3
CAPITULATION = ((3, -0.10), (2, -0.15))  # (loss posts needed, 5-day drop needed)
BUYERS_MIN, BUILD_RATIO, MAX_RET5 = 4, 2, 0.10


STANCE_VERSION = 2  # bump whenever stance.py changes: stored stances are then classified again


def stances_current(con):
    con.execute("CREATE TABLE IF NOT EXISTS stance_version(v INTEGER)")
    r = con.execute("SELECT v FROM stance_version").fetchone()
    return bool(r) and r[0] == STANCE_VERSION


def mark_stances_current(con):
    con.execute("CREATE TABLE IF NOT EXISTS stance_version(v INTEGER)")
    con.execute("DELETE FROM stance_version")
    con.execute("INSERT INTO stance_version VALUES(?)", (STANCE_VERSION,))
    con.commit()


def ensure_stances(con):
    """Classify every Reddit post mention that has no stance yet (also backfills an old database).
    Stances from an older classifier are dropped and redone first."""
    if not stances_current(con):
        con.execute("DELETE FROM stances")
        mark_stances_current(con)
    tickers = defaultdict(list)
    for r in con.execute("SELECT m.post_id, m.ticker FROM mentions m LEFT JOIN stances s ON s.post_id = m.post_id "
                         "AND s.ticker = m.ticker WHERE m.source = 'reddit' AND s.post_id IS NULL"):
        tickers[r[0]].append(r[1])
    rows = con.execute(
        """SELECT m.post_id, m.ticker, m.author, m.created, m.sentiment, p.community, p.title, p.body, p.flair,
                  (SELECT COUNT(*) FROM mentions x WHERE x.post_id = m.post_id) n
           FROM mentions m JOIN posts p ON p.id = m.post_id
           LEFT JOIN stances s ON s.post_id = m.post_id AND s.ticker = m.ticker
           WHERE m.source = 'reddit' AND p.title != '' AND s.post_id IS NULL""").fetchall()
    for r in rows:
        st = None
        if r["author"] not in JUNK and r["n"] <= 5:  # 6+ tickers is a watchlist, not a position
            st, _ = stance_for(f"{r['title']}\n{r['body']}".strip(), r["ticker"], r["n"], r["flair"], r["sentiment"] or 0.0,
                              title=r["title"], others=tickers[r["post_id"]])
        con.execute("INSERT OR IGNORE INTO stances VALUES(?,?,?,?,?,?)",
                    (r["post_id"], r["ticker"], (r["community"] or "").lower(), r["author"], r["created"], st or "NONE"))
    con.commit()
    return len(rows)


def _cut(now, sessions_back):
    """16:30 ET on the trading day `sessions_back` sessions before today (weekends skipped)."""
    today = datetime.fromtimestamp(now, ET).date()
    d = np.busday_offset(np.datetime64(today), -sessions_back, roll="backward").astype("datetime64[D]").astype(object)
    return datetime(d.year, d.month, d.day, 16, 30, tzinfo=ET).timestamp()


def crowd_features(con, cfg, now=None):
    """{ticker: {"loss5", "gain5", "buyers5", "buyers_prev5"}} from stances in the last 10 sessions."""
    now = now or time.time()
    ensure_stances(con)
    pump = {s.lower() for s in cfg["reddit"].get("pump_watch", [])}
    c5, c10 = _cut(now, 5), _cut(now, 10)
    loss, gain = defaultdict(int), defaultdict(int)
    b5, bprev = defaultdict(set), defaultdict(set)
    for r in con.execute("SELECT ticker, community, author, created, stance FROM stances "
                         "WHERE created > ? AND created <= ? AND stance != 'NONE'", (c10, now)):
        if r["community"] in pump:
            continue
        recent = r["created"] > c5
        if r["stance"] in ("BUY", "DD"):
            (b5 if recent else bprev)[r["ticker"]].add(r["author"])
        elif recent and r["stance"] == "LOST":
            loss[r["ticker"]] += 1
        elif recent and r["stance"] == "WON":
            gain[r["ticker"]] += 1
    # without two full weeks of Reddit history the "week before" count is unknown, not zero
    first = con.execute("SELECT MIN(created) FROM stances").fetchone()[0]
    warm = first is not None and first <= c10
    return {t: {"loss5": loss[t], "gain5": gain[t], "buyers5": len(b5[t]),
                "buyers_prev5": len(bprev[t]) if warm else None}
            for t in set(loss) | set(gain) | set(b5)}


def _lossy(f):
    L, W = f["loss5"], f["gain5"]
    return L >= 2 and L / (L + W) >= LOSS_SHARE


def crowd_kind(f, ret5, sc=None):
    """(kind, score, reason, size multiplier) for one ticker, or (None, 0, "", 1). ret5 = 5-day price change."""
    sc = sc or {}
    L, W = f["loss5"], f["gain5"]
    rule = sc.get("loss_rule", "bounce")
    if rule == "capitulation":
        hit = ret5 is not None and _lossy(f) and any(L >= n and ret5 <= drop for n, drop in CAPITULATION)
    else:
        hit = _lossy(f) and L >= LOSS_MIN
    if hit:
        score = round(0.9 + min(0.099, (L + max(0.0, -(ret5 or 0)) * 10) / 100), 3)  # more losses, deeper drop first
        why = f"{L} loss posts vs {W} gain posts" + (f", stock {ret5:+.0%} in 5 days" if ret5 is not None else "")
        return ("loss bounce" if rule != "capitulation" else "capitulation", score, why,
                float(sc.get("loss_size", 1.0)))
    b, prev = f["buyers5"], f["buyers_prev5"]
    if (sc.get("early_build_up", True) and ret5 is not None and prev is not None and b >= BUYERS_MIN
            and b >= BUILD_RATIO * max(prev, 1) and ret5 < MAX_RET5):
        return "early build-up", round(0.5 + min(b, 39) / 100, 3), f"{b} buyers in 5 days, up from {prev} the week before", 1.0
    return None, 0.0, "", 1.0


def _blank(t):
    return {"ticker": t, "score": 0.0, "crowd": 0.0, "news": 0.0, "buzz": 0.0, "parts": {}, "agree": [],
            "agreement": 0.0, "eff_voices": 0.0, "voices": 0, "thin": True, "spike_ratio": 1.0, "cred_share": 0.0,
            "lowcred_share": 0.0, "pump_sub_share": 0.0, "top_voices": [], "headlines": []}


def crowd_signals(con, cfg, signals, now=None):
    """Swap the sentiment score for the crowd-swing score (sentiment stays as `sentiment_score`).
    Candidates are marked here; `finalize` scores them once the 5-day price change is known."""
    sc = cfg["scoring"]
    out = {t: {**s, "sentiment_score": s["score"], "score": 0.0} for t, s in signals.items()}
    for t, f in crowd_features(con, cfg, now).items():
        s = out.get(t) or _blank(t)
        s["swing"] = f
        build = sc.get("early_build_up", True) and f["buyers_prev5"] is not None and f["buyers5"] >= BUYERS_MIN
        if _lossy(f) or build:
            s["thin"] = False  # a candidate: fetch its prices and judge it in finalize()
        out[t] = s
    return out


def finalize(s, tech, sc=None):
    """Decide a candidate once its price data is in. No price history means no trade."""
    f = s.get("swing")
    if not f:
        return
    kind, score, reason, mult = crowd_kind(f, tech["ret5"] if tech else None, sc)
    if kind:
        s.update(score=score, agree=["reddit"], swing_kind=kind, swing_reason=reason, size_mult=mult)
