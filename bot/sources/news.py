"""Keyless news: Yahoo Finance and Google News RSS for trending tickers, parsed with feedparser."""
import calendar
import re
import time
from urllib.parse import quote

import feedparser
import httpx

from ..db import cached_get_text, trending
from ..ingest import store_news
from ..util import breathe, log

FEEDS = {
    "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=US&lang=en-US",
    "google": "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en",
}
TAG = re.compile(r"<[^>]+>")


def _query(provider, t):
    return t if provider == "yahoo" else quote(f"${t} stock")


def collect(con, cfg, valid):
    n = 0
    for t in trending(con, cfg["news"]["top_n"]):
        for prov, url in FEEDS.items():
            try:
                text = cached_get_text(con, url.format(_query(prov, t)), ttl=1800)
            except httpx.HTTPError as e:
                log(f"news {prov} {t}: {type(e).__name__}")
                continue
            for e in feedparser.parse(text).entries[: cfg["news"]["per_feed"]]:
                ts = e.get("published_parsed")
                created = calendar.timegm(ts) if ts else time.time()
                n += store_news(
                    con, f"{prov}:{e.get('id') or e.get('link')}", prov, created,
                    e.get("title", ""), TAG.sub(" ", e.get("summary", ""))[:500], e.get("link", ""), [t],
                )
            breathe()
        con.commit()
    return n
