"""StockTwits public JSON (no key), with Camoufox fallback if httpx is blocked."""
import time

import httpx

from ..browser import browser_fetch_json
from ..db import cache_get, cache_put, cached_get_json, trending, upsert_author_meta
from ..ingest import store_item
from ..util import breathe, log, parse_iso

BASE = "https://api.stocktwits.com/api/2"


def _get_json(con, url, ttl=1800):
    hit = cache_get(con, [url, None], ttl)
    if hit is not None:
        return hit
    try:
        return cached_get_json(con, url, ttl=ttl)
    except httpx.HTTPError as e:
        log(f"stocktwits httpx fallback ({type(e).__name__}): using Camoufox")
        data = browser_fetch_json(url)
        cache_put(con, [url, None], data)
        return data


def trending_symbols(con, valid):
    try:
        data = _get_json(con, f"{BASE}/trending/symbols.json", ttl=1800)
    except Exception as e:
        log(f"stocktwits trending: {type(e).__name__}: {e}")
        return []
    return [s["symbol"] for s in data.get("symbols", []) if s.get("symbol") in valid]


def collect(con, cfg, valid):
    n = 0
    now = time.time()
    top_n = cfg["stocktwits"]["top_n"]
    tickers = list(dict.fromkeys(trending(con, top_n) + trending_symbols(con, valid)))[: top_n * 2]
    for t in tickers:
        try:
            data = _get_json(con, f"{BASE}/streams/symbol/{t}.json", ttl=1800)
        except Exception as e:
            log(f"stocktwits {t}: {type(e).__name__}: {e}")
            continue
        for m in data.get("messages", []):
            u = m.get("user") or {}
            tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            syms = {s.get("symbol") for s in m.get("symbols", [])} & valid or {t}
            item = {
                "id": f"stocktwits:{m['id']}", "source": "stocktwits", "community": t,
                "author": u.get("username"), "created": parse_iso(m.get("created_at")),
                "body": m.get("body", ""), "score": (m.get("likes") or {}).get("total", 0),
                "url": f"https://stocktwits.com/message/{m['id']}", "tag": tag,
            }
            n += store_item(con, item, valid, tickers=syms)
            if u.get("username"):
                joined = u.get("join_date")
                age = (now - parse_iso(joined)) / 86400 if joined else None
                upsert_author_meta(con, "stocktwits", u["username"], age, None, u.get("followers"))
        con.commit()
        breathe()
    return n
