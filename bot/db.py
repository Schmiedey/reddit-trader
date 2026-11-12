import hashlib
import json
import sqlite3
import time

import httpx

from .config import DATA

DB_PATH = DATA / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, fetched_at REAL, body TEXT);
CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY, source TEXT, community TEXT, author TEXT, created REAL,
  title TEXT, body TEXT, score INTEGER, flair TEXT, url TEXT, tag TEXT);
CREATE TABLE IF NOT EXISTS mentions(
  post_id TEXT, ticker TEXT, source TEXT, author TEXT, created REAL, sentiment REAL,
  PRIMARY KEY(post_id, ticker));
CREATE INDEX IF NOT EXISTS idx_mentions_t ON mentions(ticker, created);
CREATE INDEX IF NOT EXISTS idx_mentions_c ON mentions(created);
CREATE TABLE IF NOT EXISTS authors(
  source TEXT, author TEXT, age_days REAL, karma INTEGER, followers INTEGER, meta_at REAL,
  hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0, PRIMARY KEY(source, author));
CREATE TABLE IF NOT EXISTS calls(
  source TEXT, author TEXT, ticker TEXT, day TEXT, direction INTEGER,
  scored INTEGER DEFAULT 0, excess REAL, PRIMARY KEY(source, author, ticker, day));
CREATE TABLE IF NOT EXISTS news(
  id TEXT PRIMARY KEY, provider TEXT, created REAL, headline TEXT, summary TEXT,
  url TEXT, tickers TEXT, sentiment REAL);
CREATE TABLE IF NOT EXISTS llm_reviews(
  item_id TEXT, ticker TEXT, created REAL, stance TEXT, confidence REAL, thesis TEXT,
  PRIMARY KEY(item_id, ticker));
CREATE TABLE IF NOT EXISTS signals(day TEXT, ticker TEXT, score REAL, detail TEXT, PRIMARY KEY(day, ticker));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, action TEXT, qty INTEGER,
  price REAL, score REAL, reasons TEXT, dry_run INTEGER);
CREATE TABLE IF NOT EXISTS blocked(day TEXT, ticker TEXT, reason TEXT, PRIMARY KEY(day, ticker));
CREATE TABLE IF NOT EXISTS equity(day TEXT PRIMARY KEY, equity REAL, spy REAL);
CREATE TABLE IF NOT EXISTS signal_grades(day TEXT, ticker TEXT, score REAL, excess REAL, PRIMARY KEY(day, ticker));
CREATE TABLE IF NOT EXISTS paper_account(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL);
CREATE TABLE IF NOT EXISTS paper_positions(
  sym TEXT PRIMARY KEY, qty INTEGER, entry_price REAL, entry_day TEXT, stop REAL, target REAL, checked_day TEXT);
CREATE TABLE IF NOT EXISTS stances(
  post_id TEXT, ticker TEXT, community TEXT, author TEXT, created REAL, stance TEXT,
  PRIMARY KEY(post_id, ticker));
CREATE INDEX IF NOT EXISTS idx_stances_c ON stances(created);
CREATE TABLE IF NOT EXISTS paper_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, sym TEXT, side TEXT, qty INTEGER, reason TEXT,
  status TEXT DEFAULT 'pending', fill_day TEXT, fill_price REAL);
"""


def connect(path=None, timeout=30):
    con = sqlite3.connect(path or DB_PATH, timeout=timeout)
    con.row_factory = sqlite3.Row
    if path != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    for table, col in MIGRATIONS:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already there
    return con


MIGRATIONS = [("paper_orders", "stop_pct REAL"), ("paper_orders", "take_pct REAL")]


_client = None
_last = {}
MIN_GAP_S = 1.0  # politeness: at most one request per second per host


def http():
    global _client
    if _client is None:
        _client = httpx.Client(timeout=20, follow_redirects=True,
                               headers={"User-Agent": "reddit-trading-bot/0.2 (personal research)"})
    return _client


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def cache_get(con, key, ttl):
    row = con.execute("SELECT fetched_at, body FROM cache WHERE key=?", (_key(key),)).fetchone()
    if row and time.time() - row["fetched_at"] < ttl:
        return json.loads(row["body"])
    return None


def cache_put(con, key, value):
    con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (_key(key), time.time(), json.dumps(value)))
    con.commit()


def _fetch(url, params=None, headers=None):
    host = httpx.URL(url).host
    wait = MIN_GAP_S - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    r = http().get(url, params=params, headers=headers)
    r.raise_for_status()
    return r


def cached_get_json(con, url, params=None, headers=None, ttl=900):
    """GET JSON, cached in SQLite so reruns never refetch. Headers are never cached."""
    hit = cache_get(con, [url, params], ttl)
    if hit is not None:
        return hit
    data = _fetch(url, params, headers).json()
    cache_put(con, [url, params], data)
    return data


def cached_get_text(con, url, params=None, ttl=900):
    hit = cache_get(con, [url, params, "text"], ttl)
    if hit is not None:
        return hit
    text = _fetch(url, params).text
    cache_put(con, [url, params, "text"], text)
    return text


def trending(con, n, hours=24, now=None):
    """Top tickers by distinct authors across Reddit, StockTwits and X in the last `hours`."""
    cutoff = (now or time.time()) - hours * 3600
    rows = con.execute(
        """SELECT ticker, COUNT(DISTINCT source || ':' || author) c FROM mentions
           WHERE created>=? GROUP BY ticker ORDER BY c DESC LIMIT ?""",
        (cutoff, n),
    ).fetchall()
    return [r["ticker"] for r in rows]


def upsert_author_meta(con, source, author, age_days=None, karma=None, followers=None):
    con.execute(
        """INSERT INTO authors(source, author, age_days, karma, followers, meta_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(source, author) DO UPDATE SET age_days=excluded.age_days, karma=excluded.karma,
           followers=excluded.followers, meta_at=excluded.meta_at""",
        (source, author, age_days, karma, followers, time.time()),
    )
