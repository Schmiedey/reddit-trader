import re

from .filter.tickers import CASHTAG, extract_tickers

SPAM_SOURCES = {"x", "stocktwits"}
SPAM_CASHTAGS = 5
from .signals.sentiment import blend_tag, score_text
from .util import utc_day

CALL_MIN = 0.3  # |sentiment| needed for a post to count as a bullish/bearish "call"
SENTENCES = re.compile(r"(?<=[.!?\n])\s+")


def ticker_sentiment(text, ticker, n_tickers):
    """Sentiment toward one ticker. When a post names several tickers ("selling AMD, buying NVDA"),
    score only the sentences that mention this one; if none do, use the whole text at half strength."""
    if n_tickers <= 1:
        return score_text(text)
    pat = re.compile(rf"(?<![A-Za-z])\$?{re.escape(ticker)}(?![A-Za-z])")
    hits = [s for s in SENTENCES.split(text) if pat.search(s)]
    return score_text(" ".join(hits)) if hits else 0.5 * score_text(text)
DEFAULTS = {"community": "", "author": None, "title": "", "body": "", "score": 0, "flair": "", "url": "", "tag": None}


def store_item(con, item, valid, tickers=None, fallback=None):
    """Store a post/comment/tweet, its ticker mentions, and any calls. Returns new mentions."""
    item = {**DEFAULTS, **item}
    if con.execute("SELECT 1 FROM posts WHERE id=?", (item["id"],)).fetchone():
        con.execute("UPDATE posts SET score=? WHERE id=?", (item["score"], item["id"]))
        return 0
    con.execute(
        """INSERT INTO posts(id,source,community,author,created,title,body,score,flair,url,tag)
           VALUES(:id,:source,:community,:author,:created,:title,:body,:score,:flair,:url,:tag)""",
        item,
    )
    text = f"{item['title']}\n{item['body']}".strip()
    if item["source"] in SPAM_SOURCES and len(set(CASHTAG.findall(text.upper()))) >= SPAM_CASHTAGS:
        return 0  # "$AAA $BBB $CCC $DDD $EEE 🚀" lists are bots and pump channels, not opinions
    tks = set(tickers) if tickers is not None else extract_tickers(text, valid)
    if not tks and fallback:
        tks = set(fallback)
    if not tks:
        return 0
    day = utc_day(item["created"])
    new = 0
    for t in tks:
        sent = blend_tag(ticker_sentiment(text, t, len(tks)), item["tag"])
        new += con.execute(
            "INSERT OR IGNORE INTO mentions VALUES(?,?,?,?,?,?)",
            (item["id"], t, item["source"], item["author"], item["created"], sent),
        ).rowcount
        if item["author"] and abs(sent) >= CALL_MIN:
            con.execute(
                "INSERT OR IGNORE INTO calls(source,author,ticker,day,direction) VALUES(?,?,?,?,?)",
                (item["source"], item["author"], t, day, 1 if sent > 0 else -1),
            )
    return new


def store_news(con, nid, provider, created, headline, summary, url, tickers):
    if not tickers:
        return 0
    sent = score_text(f"{headline}. {summary or ''}")
    return con.execute(
        "INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?,?)",
        (nid, provider, created, headline, summary, url, "," + ",".join(sorted(tickers)) + ",", sent),
    ).rowcount
