"""Download Reddit COMMENTS from the keyless Arctic Shift archive, one file per subreddit and UTC day.

Resumable: a finished day is saved once to data/history/comments/<sub>/<YYYY-MM-DD>.jsonl.gz and never
fetched again; an interrupted day continues from its .part.jsonl file. Polite: one request at a time, backs off when asked.
r/wallstreetbets has about 30,000 comments a day, so one day takes about 15 minutes.

    uv run python scripts/backfill_comments.py --start 2026-11-05 --end 2026-11-12 [--stance-only] [--weekdays] [--newest-first]
"""
import argparse
import gzip
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://arctic-shift.photon-reddit.com/api/comments/search"
FIELDS = "id,author,created_utc,body,score,link_id,parent_id"
OUT = Path(__file__).resolve().parent.parent / "data" / "history" / "comments"
# --stance-only asks the archive for comments containing a word the stance rules can act on. That is about
# a quarter of all comments, so a day downloads in minutes. Saved under comments_stance/ to keep it apart.
STANCE_WORDS = ("call OR put OR buy OR bought OR sell OR sold OR hold OR share OR leaps OR long OR short OR loaded "
                "OR yolo OR position OR dca OR dumped OR trimmed OR profit OR bag OR added OR grabbed OR exited OR "
                "bearish OR hodl OR opened OR entered OR scooped OR averaged OR inverse OR cashed OR closed OR stopped")
PACE = {"delay": 1.0}  # seconds between requests; grows when the archive says "slow down", shrinks slowly


def get(params, deadline=None):
    """One page. Waits as long as it takes (the archive answers 422 "slow down" when busy), until `deadline`."""
    url = f"{API}?{urllib.parse.urlencode(params)}"
    wait = 4
    while True:
        err = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-research/0.1"})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = json.load(r)
            if body.get("data") is not None:
                PACE["delay"] = max(1.0, PACE["delay"] * 0.98)
                return body["data"]
            err = body.get("error")
        except Exception as e:  # 422 "slow down", 429, timeouts
            err = getattr(e, "code", None) or type(e).__name__
        if deadline and time.time() > deadline:
            raise RuntimeError(f"archive unavailable: {err}")
        PACE["delay"] = min(4.0, PACE["delay"] * 1.2)
        time.sleep(wait)
        wait = min(wait * 2, 120)


def fetch_day(sub, day, part, deadline=None, body=None):
    """All comments of one UTC day. Pages are appended to `part` as they arrive, so a restart resumes."""
    lo = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    hi, seen, n = lo + 86400, set(), 0
    if part.exists():
        for line in part.open():
            c = json.loads(line)
            seen.add(c["id"])
            lo = max(lo, int(c["created_utc"]))
        n = len(seen)
    with part.open("a") as g:
        while True:
            q = {"subreddit": sub, "after": lo, "before": hi, "limit": 100, "sort": "asc", "fields": FIELDS}
            batch = get({**q, "body": body} if body else q, deadline)
            for c in batch:
                if c["id"] not in seen:
                    seen.add(c["id"])
                    g.write(json.dumps(c) + "\n")
                    n += 1
            g.flush()
            if len(batch) < 100:
                return n
            last = int(batch[-1]["created_utc"])
            lo = last if last > lo else lo + 1
            time.sleep(PACE["delay"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True, help="exclusive")
    ap.add_argument("--sub", default="wallstreetbets")
    ap.add_argument("--newest-first", action="store_true")
    ap.add_argument("--stance-only", action="store_true", help="only comments with a buy/sell/hold word (much faster)")
    ap.add_argument("--weekdays", action="store_true", help="skip Saturdays and Sundays")
    ap.add_argument("--hours", type=float, default=0, help="stop starting new days after this many hours")
    a = ap.parse_args()
    t0 = time.time()
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days)]
    out = (OUT.with_name("comments_stance") if a.stance_only else OUT) / a.sub
    out.mkdir(parents=True, exist_ok=True)
    if a.weekdays:
        days = [d for d in days if d.weekday() < 5]
    stop = t0 + a.hours * 3600 if a.hours else None
    for day in (reversed(days) if a.newest_first else days):
        f = out / f"{day}.jsonl.gz"
        lock = out / f"{day}.lock"  # lets several copies of this script share one list of days
        if stop and time.time() > stop:
            break
        if f.exists():
            continue
        try:
            lock.open("x").close()
        except FileExistsError:
            continue
        try:
            t = time.time()
            part = out / f"{day}.part.jsonl"
            n = fetch_day(a.sub, day, part, stop + 1800 if stop else None, STANCE_WORDS if a.stance_only else None)
            with part.open() as src, gzip.open(f.with_suffix(".tmp"), "wt") as g:
                for line in src:
                    g.write(line)
            f.with_suffix(".tmp").rename(f)
            part.unlink()
            print(f"{day} {n} comments in {time.time() - t:.0f}s, pace {PACE['delay']:.1f}s", flush=True)
        except RuntimeError as e:
            print(f"{day} stopped: {e}", flush=True)
            break
        finally:
            lock.unlink(missing_ok=True)
