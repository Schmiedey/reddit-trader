"""Load recent archived Reddit posts into the LIVE database (data/bot.db), so the crowd-swing
method has its two weeks of history from day one instead of waiting for the collector.
Also tries to fetch the days between the archive files and today. Safe to rerun (posts dedupe by id).

    uv run python scripts/seed_reddit_history.py [--since 2026-10]
"""
import argparse
import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_reddit as bf  # noqa: E402
from bot.config import CFG  # noqa: E402
from bot.db import connect  # noqa: E402
from bot.ingest import store_item  # noqa: E402
from bot.signals.crowd import ensure_stances  # noqa: E402
from bot.sources.prices import load_valid_symbols  # noqa: E402

DEAD = ("", "[removed]", "[deleted]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=(date.today() - timedelta(days=45)).strftime("%Y-%m"))
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    subs = CFG["reddit"]["subreddits"] + CFG["reddit"].get("pump_watch", [])
    if not a.no_fetch:  # top up: from the end of the newest archive file to now
        for sub in subs:
            files = sorted((bf.OUT / sub).glob("*.json.gz"))
            if not files:
                continue
            end = json.load(gzip.open(files[-1])).get("end")
            if not end or end >= date.today().isoformat():
                continue
            utc = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            try:
                bf.get.__defaults__ = (4,)  # few retries: the newest days may not be archived yet
                rows = bf.fetch_range(sub, utc(date.fromisoformat(end)), utc(date.today() + timedelta(days=1)))
            except RuntimeError as e:
                print(f"{sub}: newest days not available ({str(e)[-40:]})")
                continue
            path = bf.OUT / sub / f"{end}-topup.json.gz"
            with gzip.open(path, "wt") as f:
                json.dump({"start": end, "end": date.today().isoformat(), "posts": rows}, f)
            print(f"{sub}: fetched {len(rows)} posts since {end}")
    con = connect()
    valid = load_valid_symbols(con)
    new = posts = 0
    for sub in subs:
        for f in sorted((bf.OUT / sub).glob("*.json.gz")):
            if f.name[:7] < a.since:
                continue
            data = json.load(gzip.open(f))
            for p in data["posts"] if isinstance(data, dict) else data:
                body = p.get("selftext") or ""
                posts += 1
                new += store_item(con, {
                    "id": f"reddit:{p['id']}", "source": "reddit", "community": p.get("subreddit") or sub,
                    "author": p.get("author"), "created": float(p["created_utc"]), "title": p.get("title") or "(no title)",
                    "body": "" if body in DEAD else body, "score": p.get("score") or 0,
                    "flair": p.get("link_flair_text") or ""}, valid)
        con.commit()
    print(f"seeded {posts} posts, {new} new mentions, {ensure_stances(con)} stances classified")


if __name__ == "__main__":
    main()
