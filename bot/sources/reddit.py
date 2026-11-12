import re
import time

from ..browser import browser_goto
from ..config import env
from ..db import upsert_author_meta
from ..filter.tickers import extract_tickers
from ..ingest import store_item
from ..util import breathe, log, parse_iso

_POST_JS = """limit => [...document.querySelectorAll('shreddit-post')].slice(0, limit).map(el => ({
    id: (el.getAttribute('id') || '').replace(/^t3_/, ''),
    author: el.getAttribute('author') || null,
    created: el.getAttribute('created-timestamp') || '',
    title: el.getAttribute('post-title') || '',
    body: el.querySelector('[slot="text-body"]')?.innerText?.trim() || '',
    score: parseInt(el.getAttribute('score') || '0', 10) || 0,
    flair: el.querySelector('shreddit-post-flair')?.innerText?.trim() || '',
    permalink: el.getAttribute('permalink') || '',
    stickied: el.hasAttribute('stickied'),
}))"""

_COMMENT_JS = """limit => [...document.querySelectorAll('shreddit-comment')].slice(0, limit).map(el => ({
    id: (el.getAttribute('thingid') || el.getAttribute('id') || '').replace(/^t1_/, ''),
    author: el.getAttribute('author') || null,
    created: el.getAttribute('created') || '',
    body: el.querySelector('[slot="comment"]')?.innerText?.trim() || '',
    score: parseInt(el.getAttribute('score') || '0', 10) || 0,
    permalink: el.getAttribute('permalink') || '',
}))"""

_USER_JS = """() => {
    const body = document.body?.innerText || '';
    const times = [...document.querySelectorAll('time')]
        .filter(t => !t.closest('shreddit-post, shreddit-comment'))
        .map(t => ({dt: t.getAttribute('datetime') || '', txt: t.innerText.trim()}));
    return {body: body.slice(0, 2000), times};
}"""


def _client():
    import praw

    cid, sec = env("REDDIT_CLIENT_ID"), env("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in .env")
    return praw.Reddit(
        client_id=cid, client_secret=sec,
        user_agent=env("REDDIT_USER_AGENT", "reddit-trading-bot/0.1"), check_for_async=False,
    )


def _post(p, sub):
    return {
        "id": f"reddit:{p.id}", "source": "reddit", "community": sub,
        "author": p.author.name if p.author else None, "created": p.created_utc,
        "title": p.title or "", "body": p.selftext or "", "score": p.score,
        "flair": p.link_flair_text or "", "url": f"https://reddit.com{p.permalink}",
    }


def _comment(c, sub, flair):
    return {
        "id": f"reddit:{c.id}", "source": "reddit", "community": sub,
        "author": c.author.name if c.author else None, "created": c.created_utc,
        "body": c.body or "", "score": c.score, "flair": flair,
        "url": f"https://reddit.com{c.permalink}",
    }


def _scrape_posts(sub, sort, limit):
    p, _ = browser_goto(f"https://www.reddit.com/r/{sub}/{sort}/")
    p.wait_for_selector("shreddit-post", timeout=12000)
    if limit > 20:
        for _ in range(2):
            if p.locator("shreddit-post").count() >= limit:
                break
            p.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            p.wait_for_timeout(1200)
    return [r for r in p.evaluate(_POST_JS, limit) if r.get("id")]


def _scrape_comments(permalink, limit):
    p, _ = browser_goto(f"https://www.reddit.com{permalink}")
    try:
        p.wait_for_selector("shreddit-comment", timeout=6000)
    except Exception:
        return []
    return [r for r in p.evaluate(_COMMENT_JS, limit) if r.get("id")]


def _post_from_dom(row, sub):
    return {
        "id": f"reddit:{row['id']}", "source": "reddit", "community": sub,
        "author": row["author"], "created": parse_iso(row["created"]),
        "title": row["title"], "body": row["body"], "score": row["score"],
        "flair": row["flair"], "url": f"https://reddit.com{row['permalink']}",
    }


def _collect_camoufox(con, rc, valid):
    n = 0
    for sub in rc["subreddits"] + rc.get("pump_watch", []):
        try:
            for row in _scrape_posts(sub, "new", rc["posts_per_sub"]):
                n += store_item(con, _post_from_dom(row, sub), valid)
            breathe()
            hot = [r for r in _scrape_posts(sub, "hot", rc["hot_posts_for_comments"] + 4) if not r["stickied"]]
            for row in hot[: rc["hot_posts_for_comments"]]:
                item = _post_from_dom(row, sub)
                n += store_item(con, item, valid)
                parent = extract_tickers(f"{item['title']}\n{item['body']}", valid)
                if row["permalink"]:
                    for c in _scrape_comments(row["permalink"], rc["comments_per_hot_post"]):
                        c_item = {
                            "id": f"reddit:{c['id']}", "source": "reddit", "community": sub,
                            "author": c["author"], "created": parse_iso(c["created"]),
                            "body": c["body"], "score": c["score"], "flair": item["flair"],
                            "url": f"https://reddit.com{c['permalink']}",
                        }
                        n += store_item(con, c_item, valid, fallback=parent)
                breathe()
            con.commit()
        except Exception as e:
            log(f"reddit r/{sub} (camoufox) failed: {type(e).__name__}: {e}")
    _fetch_author_meta_camoufox(con, rc["new_authors_per_run"])
    con.commit()
    return n


def _parse_karma(text):
    m = re.search(r"([\d,]+)\s+Karma\b", text, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    nums = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+)\s+(?:Post|Comment)\s+karma", text, re.I)]
    return sum(nums) if nums else None


def _fetch_author_meta_camoufox(con, limit):
    names = _pending_authors(con, limit)
    now = time.time()
    for name in names:
        try:
            p, resp = browser_goto(f"https://www.reddit.com/user/{name}/", timeout=15000)
            if resp and resp.status == 404:
                upsert_author_meta(con, "reddit", name, None, 0)
                continue
            p.wait_for_timeout(800)
            info = p.evaluate(_USER_JS)
            body = info["body"]
            if "nobody on Reddit goes by that name" in body or "account has been suspended" in body:
                upsert_author_meta(con, "reddit", name, None, 0)
                continue
            dt = next((t["dt"] for t in info["times"] if t["dt"] and "ago" not in t["txt"].lower()), None)
            age = (now - parse_iso(dt)) / 86400 if dt else None
            upsert_author_meta(con, "reddit", name, age, _parse_karma(body))
        except Exception:
            pass
        breathe()


def _pending_authors(con, limit):
    return [
        row["author"] for row in con.execute(
            """SELECT m.author, MAX(m.created) last FROM mentions m
               LEFT JOIN authors a ON a.source='reddit' AND a.author=m.author
               WHERE m.source='reddit' AND m.author IS NOT NULL AND a.meta_at IS NULL
               GROUP BY m.author ORDER BY last DESC LIMIT ?""", (limit,),
        )
    ]


def collect(con, cfg, valid):
    rc = cfg["reddit"]
    if not env("REDDIT_CLIENT_ID") or not env("REDDIT_CLIENT_SECRET"):
        return _collect_camoufox(con, rc, valid)
    r = _client()
    n = 0
    for sub in rc["subreddits"] + rc.get("pump_watch", []):
        try:
            sr = r.subreddit(sub)
            for p in sr.new(limit=rc["posts_per_sub"]):
                n += store_item(con, _post(p, sub), valid)
            breathe()
            for p in sr.hot(limit=rc["hot_posts_for_comments"]):
                if p.stickied:
                    continue
                item = _post(p, sub)
                n += store_item(con, item, valid)
                parent = extract_tickers(f"{item['title']}\n{item['body']}", valid)
                p.comment_sort = "top"
                p.comments.replace_more(limit=0)
                for c in p.comments.list()[: rc["comments_per_hot_post"]]:
                    n += store_item(con, _comment(c, sub, item["flair"]), valid, fallback=parent)
                breathe()
            con.commit()
        except Exception as e:
            log(f"reddit r/{sub} failed: {type(e).__name__}: {e}")
    _fetch_author_meta(con, r, rc["new_authors_per_run"])
    con.commit()
    return n


def _fetch_author_meta(con, r, limit):
    """Look up account age and karma for authors who mentioned tickers, newest first."""
    names = _pending_authors(con, limit)
    now = time.time()
    for name in names:
        try:
            u = r.redditor(name)
            upsert_author_meta(con, "reddit", name, (now - u.created_utc) / 86400, u.link_karma + u.comment_karma)
        except Exception:
            upsert_author_meta(con, "reddit", name, None, 0)  # suspended/deleted: weight 0
        breathe()
