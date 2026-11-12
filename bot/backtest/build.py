"""Stage A of the backtest: replay history day by day and store what the bot would have scored.

1. Load archived Reddit posts (scripts/backfill_reddit.py) into data/backtest.db with the live
   ingest code (same ticker parser, same per-ticker sentiment).
2. Download daily bars for every ticker that was mentioned enough (cached in data/history/).
3. For each trading day, as of 16:30 ET: update author credibility using only calls whose
   10-day outcome was already known, run the live compute_scores(), store the signals.

    uv run python -m bot.backtest.build [--variant base] [--set scoring.recency_half_life_hours=12]
"""
import argparse
import copy
import gzip
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import CFG
from ..db import DATA, connect
from ..filter.credibility import HORIZON
from ..ingest import store_item
from ..signals.score import compute_scores
from ..sources.prices import load_valid_symbols
from ..util import chunks, log

ET = ZoneInfo("America/New_York")
HIST = DATA / "history"
BT_DB = DATA / "backtest.db"
BARS = HIST / "bars.json.gz"
KEEP = ("score", "crowd", "news", "buzz", "parts", "agree", "agreement", "eff_voices", "voices",
        "spike_ratio", "cred_share", "lowcred_share", "pump_sub_share")
DEAD = ("", "[removed]", "[deleted]")


def bt_connect():
    con = connect(BT_DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS bt_loaded(path TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS bt_signals(variant TEXT, day TEXT, ticker TEXT, sig TEXT, PRIMARY KEY(variant, day, ticker));
    CREATE TABLE IF NOT EXISTS bt_days(variant TEXT, day TEXT, PRIMARY KEY(variant, day));
    """)
    return con


def load_posts(con, valid, since="0000"):
    files = sorted(f for f in (HIST / "reddit").glob("*/*.json.gz") if f.name >= since)
    done = {r[0] for r in con.execute("SELECT path FROM bt_loaded")}
    new = 0
    for f in files:
        key = f"{f.parent.name}/{f.name}"
        if key in done:
            continue
        data = json.load(gzip.open(f))
        for p in data["posts"] if isinstance(data, dict) else data:
            body = p.get("selftext") or ""
            new += store_item(con, {
                "id": f"reddit:{p['id']}", "source": "reddit", "community": p.get("subreddit") or f.parent.name,
                "author": p.get("author"), "created": float(p["created_utc"]), "title": p.get("title") or "",
                "body": "" if body in DEAD else body, "score": p.get("score") or 0,
                "flair": p.get("link_flair_text") or "",
            }, valid)
        con.execute("INSERT INTO bt_loaded VALUES(?)", (key,))
        con.commit()
    log(f"loaded {new} new mentions from {len(files) - len(done)} files")
    if len(files) > len(done):  # new history changes every later score: rebuild signals
        con.execute("DELETE FROM bt_signals")
        con.execute("DELETE FROM bt_days")
        con.commit()


def load_bars(con, min_mentions=15):
    """{sym: [bar, ...]} for SPY + every ticker with enough mentions. Cached; only fetches new symbols."""
    import yfinance as yf

    from ..sources.prices import _frame_for

    have = json.load(gzip.open(BARS)) if BARS.exists() else {}
    first = con.execute("SELECT MIN(created) FROM mentions").fetchone()[0]
    start = (datetime.fromtimestamp(first) - timedelta(days=150)).date()
    want = {r[0] for r in con.execute(
        "SELECT ticker FROM mentions GROUP BY ticker HAVING COUNT(*) >= ?", (min_mentions,))} | {"SPY"}
    missing = sorted(want - set(have))
    for chunk in chunks(missing, 50):
        try:
            df = yf.download(chunk, start=start.isoformat(), interval="1d", auto_adjust=True,
                             progress=False, threads=False, group_by="ticker")
        except Exception as e:
            log(f"yfinance failed: {type(e).__name__}")
            continue
        for s in chunk:
            f = _frame_for(df, s)
            if f is None or "Close" not in f:
                have[s] = []
                continue
            f = f.dropna(subset=["Close"])
            have[s] = [{"d": i.strftime("%Y-%m-%d"), "o": float(r.Open), "h": float(r.High), "l": float(r.Low),
                        "c": float(r.Close), "v": float(r.Volume)} for i, r in f.iterrows()]
    if missing:
        HIST.mkdir(parents=True, exist_ok=True)
        with gzip.open(BARS, "wt") as fh:
            json.dump(have, fh)
        log(f"bars: fetched {len(missing)} symbols, {sum(1 for v in have.values() if v)} usable")
    return {s: b for s, b in have.items() if b}


def grade_calls(con, bars):
    """Every call's outcome and the day it became knowable: [(known_day, source, author, hit)]."""
    idx = {s: {b["d"]: i for i, b in enumerate(v)} for s, v in bars.items()}
    spy = bars["SPY"]
    days = [b["d"] for b in spy]
    out = []
    import bisect
    for src, author, t, day, direction in con.execute("SELECT source, author, ticker, day, direction FROM calls"):
        tb = bars.get(t)
        if not tb:
            continue
        i = bisect.bisect_left(days, day)
        if i + HORIZON >= len(days):
            continue
        d0, d1 = days[i], days[i + HORIZON]
        a, b = idx[t].get(d0), idx[t].get(d1)
        if a is None or b is None:
            continue
        excess = direction * ((tb[b]["c"] / tb[a]["c"] - 1) - (spy[i + HORIZON]["c"] / spy[i]["c"] - 1))
        out.append((d1, src, author, excess > 0))
    out.sort()
    return out


def deep_set(cfg, dotted, value):
    keys = dotted.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = json.loads(value) if value[:1] in "[{0123456789-tf" else value


def build_signals(con, bars, variant, cfg, credibility=True, warmup_days=10):
    graded = grade_calls(con, bars) if credibility else []
    first = con.execute("SELECT MIN(created) FROM mentions").fetchone()[0]
    last = con.execute("SELECT MAX(created) FROM mentions").fetchone()[0]
    d_first = date.fromtimestamp(first) + timedelta(days=warmup_days)
    d_last = date.fromtimestamp(last)
    done = {r[0] for r in con.execute("SELECT day FROM bt_days WHERE variant=?", (variant,))}
    days = [b["d"] for b in bars["SPY"] if d_first.isoformat() <= b["d"] <= d_last.isoformat()]
    con.execute("UPDATE authors SET hits=0, misses=0")
    gi, t0, n = 0, time.time(), 0
    stats = defaultdict(lambda: [0, 0])
    for day in days:
        while gi < len(graded) and graded[gi][0] <= day:  # outcomes known by this evening
            _, src, author, hit = graded[gi]
            stats[(src, author)][0 if hit else 1] += 1
            gi += 1
        if day in done:
            continue
        con.executemany("INSERT INTO authors(source, author, hits, misses) VALUES(?,?,?,?) "
                        "ON CONFLICT(source, author) DO UPDATE SET hits=excluded.hits, misses=excluded.misses",
                        [(s, a, h, m) for (s, a), (h, m) in stats.items()])
        now = datetime.fromisoformat(day).replace(hour=16, minute=30, tzinfo=ET).timestamp()
        sigs = compute_scores(con, cfg, now)
        con.executemany("INSERT OR REPLACE INTO bt_signals VALUES(?,?,?,?)", [
            (variant, day, t, json.dumps({k: s[k] for k in KEEP}))
            for t, s in sigs.items() if s["voices"] >= 2])
        con.execute("INSERT INTO bt_days VALUES(?,?)", (variant, day))
        con.commit()
        n += 1
        if n % 50 == 0:
            log(f"{variant}: {day} ({n} days, {time.time() - t0:.0f}s)")
    log(f"{variant}: {n} new days scored")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--no-cred", action="store_true", help="ignore author track records")
    ap.add_argument("--since", default="0000", help="only load history months >= this, e.g. 2026-07")
    ap.add_argument("--set", action="append", default=[], help="config override, e.g. scoring.lookback_hours=48")
    a = ap.parse_args(argv)
    cfg = copy.deepcopy(CFG)
    for kv in a.set:
        deep_set(cfg, *kv.split("=", 1))
    con = bt_connect()
    load_posts(con, load_valid_symbols(connect()), a.since)
    bars = load_bars(con)
    build_signals(con, bars, a.variant, cfg, credibility=not a.no_cred)


if __name__ == "__main__":
    main()
