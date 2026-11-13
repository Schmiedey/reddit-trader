"""Reddit posts from the keyless Arctic Shift archive (no login, no API key, no browser)."""
import json
import time
import urllib.parse
import urllib.request

API = "https://arctic-shift.photon-reddit.com/api/posts/search"
FIELDS = "id,author,created_utc,title,selftext,score,num_comments,link_flair_text,subreddit"
DEAD = ("", "[removed]", "[deleted]")
WAITS = (3, 8, 15, 30, 60)


def get(params, tries=4):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-research/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.load(r)
            if body.get("data") is not None:
                return body["data"]
            err = body.get("error")
        except Exception as e:  # 422 "slow down", 429, timeouts
            err = getattr(e, "code", None) or type(e).__name__
        time.sleep(WAITS[min(i, len(WAITS) - 1)])
    raise RuntimeError(f"archive unavailable for r/{params.get('subreddit')}: {err}")


def fetch_range(sub, after_ts, before_ts, tries=4):
    """All posts in [after_ts, before_ts), 100 per request, oldest first."""
    lo, rows, seen = int(after_ts), [], set()
    while True:
        batch = get({"subreddit": sub, "after": lo, "before": int(before_ts), "limit": 100, "sort": "asc",
                     "fields": FIELDS}, tries)
        rows += [p for p in batch if p["id"] not in seen]
        seen |= {p["id"] for p in batch}
        if len(batch) < 100:
            return rows
        last = int(batch[-1]["created_utc"])
        lo = last if last > lo else lo + 1
        time.sleep(1)


def to_item(p, sub):
    body = p.get("selftext") or ""
    return {"id": f"reddit:{p['id']}", "source": "reddit", "community": p.get("subreddit") or sub,
            "author": p.get("author"), "created": float(p["created_utc"]), "title": p.get("title") or "(no title)",
            "body": "" if body in DEAD else body, "score": p.get("score") or 0,
            "flair": p.get("link_flair_text") or ""}
