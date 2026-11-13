import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot.cloud.engine import ACCOUNTS, Engine, rule_hits

ET = ZoneInfo("America/New_York")


def _days(n, start=date(2025, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _series(days, closes):
    return [{"d": d, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 5e6} for d, c in zip(days, closes)]


def _ts(day, hour=12):
    return datetime.fromisoformat(day).replace(hour=hour, minute=40, tzinfo=ET).timestamp()


def test_rule_hits():
    f = {"buyers5": 4, "buyers_prev5": 1, "sellers5": 0, "warm5": True}
    assert rule_hits(f, 0.02) == ["buildup"]
    assert rule_hits(f, 0.15) == []                                   # price already ran
    assert rule_hits({**f, "buyers_prev5": 3}, 0.02) == []            # not doubled
    assert rule_hits({**f, "buyers_prev5": None}, 0.02) == []         # not enough history yet
    assert rule_hits({**f, "buyers5": 6, "buyers_prev5": 5}, 0.3) == ["mostbought"]
    assert rule_hits({**f, "buyers5": 6, "buyers_prev5": 5, "sellers5": 6}, 0.0) == []
    assert rule_hits({**f, "buyers5": 6}, 0.0) == ["buildup", "mostbought"]


def test_engine_buys_fills_exits_and_survives_a_restart(tmp_path):
    days = _days(90)
    d0 = 70
    bars = {"NVDA": _series(days, [100 + 0.2 * i for i in range(90)]), "SPY": _series(days, [500 + 0.1 * i for i in range(90)])}
    cur = {"day": None}
    fake = lambda _con, syms, since: {s: [b for b in bars[s] if b["d"] <= cur["day"]] for s in syms if s in bars}
    eng = Engine(tmp_path / "a", park=False, bars_fn=fake)
    rows = [("old", "GME", "wallstreetbets", "x", _ts(days[d0 - 12]), "HOLD")]            # history older than two weeks
    rows += [(f"p{i}", "NVDA", "wallstreetbets", f"u{i}", _ts(days[d0]), "BUY") for i in range(6)]
    rows += [("pump", "NVDA", "pennystocks", "z", _ts(days[d0]), "BUY")]
    eng.con.executemany("INSERT INTO stances VALUES(?,?,?,?,?,?)", rows)
    eng.con.commit()
    f = eng.features(_ts(days[d0], 16))["NVDA"]
    assert f["buyers5"] == 6 and f["buyers_prev5"] == 0 and f["sellers5"] == 0       # pump-watch subreddit ignored

    def evening(k):
        cur["day"] = days[k]
        return eng.daily(_ts(days[k], 16))

    assert evening(d0)["ok"]
    for aid in ACCOUNTS:                                                              # both rules fire: every account buys
        o = eng.acct(aid).execute("SELECT * FROM paper_orders").fetchall()
        assert len(o) == 1 and o[0]["sym"] == "NVDA" and o[0]["side"] == "BUY" and o[0]["status"] == "pending"
    cur["day"] = days[d0 + 1]
    assert len(eng.morning_fills(_ts(days[d0 + 1], 9))["fills"]) == 3                 # filled at the next open
    pos = eng.acct("both").execute("SELECT * FROM paper_positions").fetchone()
    assert pos["entry_day"] == days[d0 + 1] and 9_000 < pos["qty"] * pos["entry_price"] <= 10_000   # 10% of $100k
    st = eng.state(_ts(days[d0 + 1], 12))
    assert st["mode"] == "paper" and st["accounts"][0]["positions"][0]["sym"] == "NVDA"

    led = eng.export()                                                                # restart on an empty disk
    eng = Engine(tmp_path / "b", park=False, bars_fn=fake)
    eng.load(led)
    assert eng.acct("both").execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
    assert eng.get("last_daily") == days[d0]
    for k in range(d0 + 1, d0 + 12):
        assert evening(k)["ok"]
    for aid in ACCOUNTS:
        c = eng.acct(aid)
        assert c.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0   # sold after 10 trading days
        sell = c.execute("SELECT * FROM paper_orders WHERE side='SELL' AND status='filled'").fetchone()
        assert sell["fill_day"] == days[d0 + 11]
        assert c.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY'").fetchone()[0] == 1   # cooldown: no rebuy
        assert c.execute("SELECT cash FROM paper_account").fetchone()[0] > 100_000
    a = eng.state(_ts(days[d0 + 11], 17))["accounts"][0]
    assert a["stats"]["closed"] == 1 and a["trades"][0]["ret"] > 0 and len(a["curve"]) == 12


def test_evening_run_waits_when_market_data_is_missing(tmp_path):
    eng = Engine(tmp_path, park=False, bars_fn=lambda *_: {"SPY": _series(["2025-01-06"], [500])})
    res = eng.daily(_ts("2025-01-07", 16))
    assert not res["ok"] and eng.get("last_daily") is None


def test_removed_posts_lose_their_text(tmp_path):
    eng = Engine(tmp_path, park=False)
    valid, now = {"NVDA", "AMD"}, time.time()
    item = {"id": "reddit:r1", "source": "reddit", "community": "stocks", "author": "u1", "created": now,
            "title": "Thoughts on the market", "body": "Bought 100 shares of NVDA today", "score": 3, "flair": ""}
    assert eng._store(dict(item), valid) == 1
    assert eng._store({**item, "body": ""}, valid) == 0                                # moderators removed it
    assert eng.con.execute("SELECT COUNT(*) FROM mentions WHERE post_id='reddit:r1'").fetchone()[0] == 0


def test_due_work_schedule():
    from bot.cloud.due import due

    at = lambda day, h, m=0: datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=ET).timestamp()
    done = {"last_daily": "2026-11-12", "fills_day": None}
    assert due(done, True, at("2026-11-13", 9, 0)) | {"evening_ts": 0} == {"evening_day": None, "evening_ts": 0, "same_day": False, "fills": False}
    assert due(done, True, at("2026-11-13", 9, 45))["fills"]                       # Friday after the open
    assert not due({**done, "fills_day": "2026-11-13"}, True, at("2026-11-13", 10))["fills"]
    assert not due(done, False, at("2026-11-13", 10))["fills"]                     # nothing to fill
    assert due(done, False, at("2026-11-13", 16, 30))["evening_day"] is None       # before the close
    d = due(done, False, at("2026-11-13", 16, 40))
    assert d["evening_day"] == "2026-11-13" and d["same_day"]
    assert due({"last_daily": "2026-11-13"}, False, at("2026-11-14", 12))["evening_day"] is None   # Saturday: Friday done
    d = due(done, True, at("2026-11-16", 9, 50))                                   # Monday: Friday's run was missed
    assert d["evening_day"] == "2026-11-13" and not d["same_day"] and d["fills"]
    assert due({**done, "defer": "2026-11-13"}, False, at("2026-11-13", 19))["evening_day"] is None
    assert due({**done, "defer": "2026-11-13"}, False, at("2026-11-14", 9))["evening_day"] == "2026-11-13"
    assert due({**done, "skip_day": "2026-11-13"}, False, at("2026-11-14", 9))["evening_day"] is None
