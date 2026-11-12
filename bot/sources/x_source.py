"""X (Twitter) data through a pluggable provider.

Supports keyless scraping via Camoufox (`provider: camoufox`, or default when no API key is set)
and paid API access via `twitterapi_io` (`X_PROVIDER_KEY`).
"""
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..browser import browser_goto
from ..config import env
from ..db import cache_get, cache_put, cached_get_json, trending, upsert_author_meta
from ..ingest import store_item
from ..util import breathe, log, parse_iso, parse_twitter_date

_SNOWFLAKE_EPOCH_MS = 1288834974657
_STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)")
_METRIC_RE = re.compile(r"^\d+(?:\.\d+)?[KMB]?$")
_TIME_RE = re.compile(r"^(?:\d+[smhd]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s+\d{4})?)$")

_X_ARTICLES_JS = """() => [...document.querySelectorAll('article')].map(a => ({
    href: a.querySelector('a[href*="/status/"]')?.getAttribute('href') || '',
    dt: a.querySelector('time')?.getAttribute('datetime') || '',
    text: a.innerText || '',
}))"""


class XSource(ABC):
    """Returns normalized tweets: {id, author, created, text, likes, followers, author_created}."""

    @abstractmethod
    def user_tweets(self, handle): ...

    @abstractmethod
    def search(self, query): ...


class TwitterApiIo(XSource):
    BASE = "https://api.twitterapi.io"

    def __init__(self, con, key, ttl):
        self.con, self.key, self.ttl = con, key, ttl

    def _get(self, path, params):
        data = cached_get_json(self.con, self.BASE + path, params, headers={"X-API-Key": self.key}, ttl=self.ttl)
        tweets = data.get("tweets") or (data.get("data") or {}).get("tweets") or []
        return [self._norm(t) for t in tweets if t.get("id")]

    def user_tweets(self, handle):
        return self._get("/twitter/user/last_tweets", {"userName": handle})

    def search(self, query):
        return self._get("/twitter/tweet/advanced_search", {"query": query, "queryType": "Latest"})

    @staticmethod
    def _norm(t):
        a = t.get("author") or {}
        return {
            "id": str(t["id"]), "author": a.get("userName"), "created": parse_twitter_date(t.get("createdAt")),
            "text": t.get("text", ""), "likes": t.get("likeCount", 0), "followers": a.get("followers"),
            "author_created": parse_twitter_date(a.get("createdAt")),
        }


def _snowflake_ts(tweet_id):
    try:
        return ((int(tweet_id) >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return None


def _parse_compact_num(s):
    if not s:
        return 0
    s = s.strip().replace(",", "")
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(s[-1:].upper(), 1)
    num = s[:-1] if mult > 1 else s
    try:
        return int(float(num) * mult)
    except ValueError:
        return 0


def _clean_article_text(raw):
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.startswith("@") and i + 1 < len(lines):
            lines = lines[i + 2 :] if _TIME_RE.match(lines[i + 1]) else lines[i + 1 :]
            break
    metrics = []
    while lines and len(metrics) < 4 and _METRIC_RE.match(lines[-1]):
        metrics.append(_parse_compact_num(lines.pop()))
    likes = metrics[1] if len(metrics) >= 2 else (metrics[0] if metrics else 0)
    return "\n".join(lines), likes


class CamoufoxXSource(XSource):
    def __init__(self, con, ttl):
        self.con, self.ttl = con, ttl

    def _parse_articles(self, page, default_followers=None, default_created=None):
        out = []
        for art in page.evaluate(_X_ARTICLES_JS):
            m = _STATUS_RE.match(art["href"])
            if not m:
                continue
            author, tid = m.group(1), m.group(2)
            created = parse_iso(art["dt"]) if art["dt"] else _snowflake_ts(tid)
            text, likes = _clean_article_text(art["text"])
            if not text:
                continue
            out.append({
                "id": tid, "author": author, "created": created,
                "text": text, "likes": likes,
                "followers": default_followers, "author_created": default_created,
            })
        return out

    def user_tweets(self, handle):
        key = ["camoufox_x_user", handle]
        hit = cache_get(self.con, key, self.ttl)
        if hit is not None:
            return hit
        p, _ = browser_goto(f"https://x.com/{handle}")
        try:
            p.wait_for_selector("article", timeout=15000)
            p.wait_for_timeout(800)
        except Exception:
            cache_put(self.con, key, [])
            return []
        header = p.inner_text("body")[:800]
        fm = re.search(r"([\d,.]+[KMB]?)\s+Followers", header)
        followers = _parse_compact_num(fm.group(1)) if fm else None
        jm = re.search(r"Joined\s+([A-Za-z]+\s+\d{4})", header)
        joined = None
        if jm:
            try:
                joined = datetime.strptime(jm.group(1), "%B %Y").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        tweets = self._parse_articles(p, followers, joined)
        cache_put(self.con, key, tweets)
        return tweets

    def search(self, query):
        key = ["camoufox_x_search", query]
        hit = cache_get(self.con, key, self.ttl)
        if hit is not None:
            return hit
        p, _ = browser_goto(f"https://x.com/search?q={quote(query)}&f=live")
        try:
            p.wait_for_selector("article", timeout=6000)
            p.wait_for_timeout(800)
            tweets = self._parse_articles(p)
        except Exception:
            tweets = []
        cache_put(self.con, key, tweets)
        return tweets


PROVIDERS = {"twitterapi_io": TwitterApiIo, "camoufox": CamoufoxXSource}


def get_source(con, xc):
    provider = xc.get("provider")
    if provider == "disabled":
        return None
    ttl = xc["cache_minutes"] * 60
    key = env("X_PROVIDER_KEY")
    if provider == "twitterapi_io" and key:
        return TwitterApiIo(con, key, ttl=ttl)
    if provider in (None, "none", "camoufox"):
        return CamoufoxXSource(con, ttl=ttl)
    return None


def _store(con, tw, valid, fallback=None):
    if tw["created"] is None:
        return 0
    if tw["author"]:
        age = (tw["created"] - tw["author_created"]) / 86400 if tw["author_created"] else None
        upsert_author_meta(con, "x", tw["author"], age, None, tw["followers"])
    item = {
        "id": f"x:{tw['id']}", "source": "x", "community": "x", "author": tw["author"],
        "created": tw["created"], "body": tw["text"], "score": tw["likes"],
        "url": f"https://x.com/{tw['author']}/status/{tw['id']}",
    }
    return store_item(con, item, valid, fallback=fallback)


def collect(con, cfg, valid):
    xc = cfg["x"]
    src = get_source(con, xc)
    if src is None:
        log("x: skipped (set X_PROVIDER_KEY and x.provider)")
        return 0
    n = 0
    jobs = [(src.user_tweets, h, None) for h in xc["accounts"]]
    jobs += [(src.search, f"${t} lang:en -filter:retweets", {t}) for t in trending(con, xc["cashtag_search_top_n"])]
    for fn, arg, fallback in jobs:
        try:
            for tw in fn(arg)[: xc["max_tweets_per_query"]]:
                n += _store(con, tw, valid, fallback)
        except httpx.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", "")
            log(f"x {arg}: {type(e).__name__} {code}")
            if code in (401, 402, 403):
                break  # bad key or out of credits
        except Exception as e:
            log(f"x {arg}: {type(e).__name__}: {e}")
        breathe()
    con.commit()
    return n
