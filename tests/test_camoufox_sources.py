from unittest.mock import patch

import httpx

from bot.config import CFG
from bot.db import connect
from bot.sources import reddit, stocktwits, x_source


class _FakePage:
    def __init__(self, eval_seq=None, text=""):
        self._evals = list(eval_seq or [])
        self._text = text

    def wait_for_selector(self, *a, **kw):
        pass

    def wait_for_timeout(self, *a, **kw):
        pass

    def locator(self, *a, **kw):
        class _Loc:
            def count(self):
                return 0
        return _Loc()

    def evaluate(self, *a, **kw):
        return self._evals.pop(0) if self._evals else []

    def inner_text(self, *a, **kw):
        return self._text


def test_stocktwits_httpx_403_falls_back_to_camoufox():
    con = connect(":memory:")
    req = httpx.Request("GET", "https://api.stocktwits.com/api/2/trending/symbols.json")
    err = httpx.HTTPStatusError("forbidden", request=req, response=httpx.Response(403, request=req))
    payload = {"symbols": [{"symbol": "AAPL"}, {"symbol": "FAKE"}]}
    with patch("bot.sources.stocktwits.cached_get_json", side_effect=err), \
         patch("bot.sources.stocktwits.browser_fetch_json", return_value=payload) as mock_fb:
        syms = stocktwits.trending_symbols(con, {"AAPL", "NVDA"})
    assert syms == ["AAPL"]
    assert mock_fb.call_count == 1
    # second call hits SQLite cache without hitting httpx or browser again
    assert stocktwits.trending_symbols(con, {"AAPL", "NVDA"}) == ["AAPL"]
    assert mock_fb.call_count == 1


def test_reddit_keyless_camoufox_collects_posts_comments_and_author():
    con = connect(":memory:")
    cfg = {
        **CFG,
        "reddit": {
            "subreddits": ["wallstreetbets"], "pump_watch": [],
            "posts_per_sub": 5, "hot_posts_for_comments": 1,
            "comments_per_hot_post": 5, "new_authors_per_run": 1,
        },
    }
    posts = [{
        "id": "abc123", "author": "trader1", "created": "2026-11-12T10:00:00+0000",
        "title": "$NVDA earnings beat", "body": "Very bullish on NVDA", "score": 42,
        "flair": "DD", "permalink": "/r/wallstreetbets/comments/abc123/nvda/", "stickied": False,
    }]
    comments = [{
        "id": "c999", "author": "trader2", "created": "2026-11-12T09:55:00+0000",
        "body": "Calls printing", "score": 7, "permalink": "/r/wallstreetbets/comments/abc123/comment/c999/",
    }]
    user_info = {
        "body": "trader1\n1,250 Karma\n",
        "times": [{"dt": "2022-01-01T00:00:00+0000", "txt": "January 2022"}],
    }
    pages = [
        (_FakePage([posts]), None),
        (_FakePage([posts]), None),
        (_FakePage([comments]), None),
        (_FakePage([user_info]), None),
    ]
    with patch("bot.sources.reddit.env", return_value=None), \
         patch("bot.sources.reddit.browser_goto", side_effect=pages):
        n = reddit.collect(con, cfg, {"NVDA", "AAPL"})
    assert n == 2
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    row = con.execute("SELECT age_days, karma FROM authors WHERE author='trader1'").fetchone()
    assert row["age_days"] > 365 and row["karma"] == 1250


def test_x_camoufox_source_parses_articles_and_snowflake():
    con = connect(":memory:")
    src = x_source.get_source(con, {"provider": "camoufox", "cache_minutes": 60})
    assert isinstance(src, x_source.CamoufoxXSource)

    arts = [{
        "href": "/unusual_whales/status/2120893377944424538",
        "dt": "",
        "text": "unusual_whales\n@unusual_whales\n16m\nBig $NVDA call sweep\n12\n34\n560\n25K",
    }]
    page = _FakePage([arts], text="unusual_whales\nJoined November 2019\n5.3M Followers")
    with patch("bot.sources.x_source.browser_goto", return_value=(page, None)):
        tw = src.user_tweets("unusual_whales")
    assert len(tw) == 1
    assert tw[0]["id"] == "2120893377944424538"
    assert tw[0]["author"] == "unusual_whales"
    assert tw[0]["text"] == "Big $NVDA call sweep"
    assert tw[0]["likes"] == 560
    assert tw[0]["followers"] == 5_300_000
    assert tw[0]["created"] > 1_790_000_000
