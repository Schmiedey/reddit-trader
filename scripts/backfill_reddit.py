"""Download historical Reddit posts from the keyless Arctic Shift archive, for backtesting.

Resumable: each (subreddit, month) is saved once to data/history/reddit/<sub>/<YYYY-MM>.json.gz
and never fetched again (a partial current month is refetched when --end moves). Polite: one request at a time, backs off when the server asks.

    uv run python scripts/backfill_reddit.py --start 2024-07-01 --end 2026-11-01
"""
import argparse
import gzip
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://arctic-shift.photon-reddit.com/api/posts/search"
FIELDS = "id,author,created_utc,title,selftext,score,num_comments,link_flair_text,subreddit"
SUBS = ["wallstreetbets", "stocks", "investing", "StockMarket", "options", "ValueInvesting",
        "SecurityAnalysis", "pennystocks"]
OUT = Path(__file__).resolve().parent.parent / "data" / "history" / "reddit"


def get(params, tries=8):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-research/0.1"})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = json.load(r)
                remaining = r.headers.get("x-ratelimit-remaining")
                if remaining is not None and int(remaining) < 3:
                    time.sleep(float(r.headers.get("x-ratelimit-reset", 10)))
            if body.get("data") is not None:
                return body["data"]
            err = body.get("error")
        except Exception as e:  # 422 "slow down", 429, timeouts
            err = getattr(e, "code", None) or type(e).__name__
        time.sleep([3, 8, 15, 30, 60, 90, 120, 120][i])  # first query of a range often times out server-side
    raise RuntimeError(f"giving up on {params}: {err}")


def fetch_range(sub, after, before):
    """All posts in [after, before), 100 per request, paging forward by timestamp."""
    lo, rows = int(after.timestamp()), []
    while True:
        batch = get({"subreddit": sub, "after": lo, "before": int(before.timestamp()), "limit": 100,
                     "sort": "asc", "fields": FIELDS})
        rows += batch
        if len(batch) < 100:
            return rows
        last = int(batch[-1]["created_utc"])
        lo = last if last > lo else lo + 1  # next page starts at the last timestamp (dupes removed by id)
        time.sleep(1)


def months(d0, d1):
    m = date(d0.year, d0.month, 1)
    while m < d1:
        nxt = date(m.year + m.month // 12, m.month % 12 + 1, 1)
        yield m, min(nxt, d1)
        m = nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--subs", nargs="*", default=SUBS)
    a = ap.parse_args()
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    t0, done = time.time(), 0
    for m0, m1 in reversed(list(months(d0, d1))):  # newest first: the most relevant data lands first
        for sub in a.subs:
            path = OUT / sub / f"{m0:%Y-%m}.json.gz"
            if path.exists() and json.load(gzip.open(path, "rt")).get("end") == m1.isoformat():
                continue
            utc = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            try:
                rows = {r["id"]: r for r in fetch_range(sub, utc(m0), utc(m1))}
            except RuntimeError as e:  # archive overloaded: skip, a rerun picks it up
                print(f"skip {sub} {m0:%Y-%m}: {e}"[:200], flush=True)
                time.sleep(60)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wt") as f:
                json.dump({"start": m0.isoformat(), "end": m1.isoformat(), "posts": list(rows.values())}, f)
            tmp.rename(path)
            done += 1
            print(f"{datetime.now():%H:%M:%S} {sub} {m0:%Y-%m}: {len(rows)} posts ({time.time() - t0:.0f}s)", flush=True)
    print(f"done: {done} sub-months fetched in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
