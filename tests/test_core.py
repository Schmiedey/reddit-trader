import time

import pytest

from bot.config import CFG
from bot.db import connect, upsert_author_meta
from bot.filter.credibility import author_weight, credibility, fwd_return, prior
from bot.filter.pump_guard import check
from bot.filter.tickers import extract_tickers
from bot.ingest import store_item, store_news
from bot.signals.score import compute_scores
from bot.signals.sentiment import score_text
from bot.trader.risk import plan_entries, plan_exits, position_size

VALID = {"AAPL", "TSLA", "GME", "IT", "ALL", "ON", "NVDA", "A"}


def _rules():
    """Fixed rule values for logic tests, so retuning config.yaml doesn't break them."""
    import copy
    tc, sc = copy.deepcopy(CFG["trading"]), copy.deepcopy(CFG["scoring"])
    sc.update(buy_threshold=0.25, exit_threshold=-0.15, min_agreeing_sources=2)
    tc.update(max_ret5=0.25, risk_off_threshold_bump=0.1, risk_off_max_new=1, max_new_per_day=3, max_hold_days=10,
              atr_stop_mult=2.0, min_stop_pct=0.04, max_stop_pct=0.12, reward_risk=2.0, max_positions=10)
    return tc, sc


def test_tickers_cashtags_and_blocklist():
    assert extract_tickers("Loading $tsla and NVDA calls", VALID) == {"TSLA", "NVDA"}
    assert extract_tickers("IT is ALL ON the line, buy AAPL", VALID) == {"AAPL"}
    assert extract_tickers("I bought $A today", VALID) == {"A"}
    assert extract_tickers("GME TO THE MOON NOW", VALID) == set()  # shouting: cashtags only
    assert extract_tickers("$GME TO THE MOON NOW", VALID) == {"GME"}


def test_sentiment_slang():
    assert score_text("TSLA calls 🚀🚀 going to the moon, tendies incoming") > 0.3
    assert score_text("buying puts, this is a rugpull, bagholders getting rekt") < -0.3


def test_credibility_math():
    assert prior(age_days=5) < prior() < prior(age_days=1000, karma=10000)
    p0 = prior()
    assert credibility(0, 0, p0) == pytest.approx(p0)
    assert credibility(8, 2, p0) > 0.6 > credibility(2, 8, p0) + 0.2
    fc = CFG["filter"]
    assert author_weight("[deleted]", None, fc) == 0
    assert author_weight("newbie", {"age_days": 3, "karma": 900, "followers": None, "hits": 0, "misses": 0}, fc) == 0
    good = {"age_days": 900, "karma": 9000, "followers": None, "hits": 12, "misses": 3}
    assert author_weight("pro", good, fc) > 1.5


def test_fwd_return():
    bars = [{"d": f"2026-01-{i:02d}", "c": 100 + i, "v": 1} for i in range(1, 20)]
    assert fwd_return(bars, "2026-01-01", 10) == pytest.approx(111 / 101 - 1)
    assert fwd_return(bars, "2026-01-15", 10) is None


def test_risk():
    tc, sc = _rules()
    assert position_size(100_000, 50, 0.05) == 100
    assert position_size(100_000, 6000, 0.05) == 0
    sigs = {
        "AAPL": {"ticker": "AAPL", "score": 0.5, "agree": ["reddit", "news"], "thin": False},
        "TSLA": {"ticker": "TSLA", "score": 0.6, "agree": ["reddit"], "thin": False},
        "GME": {"ticker": "GME", "score": 0.9, "agree": ["reddit", "x"], "thin": False, "blocked": "pump"},
        "NVDA": {"ticker": "NVDA", "score": -0.4, "agree": ["reddit", "news"], "thin": False},
    }
    assert [s["ticker"] for s in plan_entries(sigs, {}, tc, sc)] == ["AAPL"]
    exits = dict(plan_exits({"NVDA": 2, "AAPL": 12, "TSLA": 1}, sigs, tc, sc))
    assert set(exits) == {"NVDA", "AAPL"}


def test_pump_guard():
    fc = CFG["filter"]
    good = [{"d": "x", "c": 50, "v": 5_000_000}] * 20
    assert check({}, good, fc) is None
    assert "under" in check({}, [{"d": "x", "c": 2, "v": 5_000_000}], fc)
    assert "volume" in check({}, [{"d": "x", "c": 50, "v": 1000}] * 20, fc)
    assert "spike" in check({"spike_ratio": 9, "lowcred_share": 0.8}, good, fc)


def test_score_end_to_end():
    con = connect(":memory:")
    now = time.time()
    for i in range(6):
        upsert_author_meta(con, "reddit", f"user{i}", 800, 5000)
        store_item(con, {"id": f"reddit:p{i}", "source": "reddit", "community": "stocks", "author": f"user{i}",
                         "created": now - 3600, "title": "AAPL is undervalued, bullish", "body": "Great buyback, strong beat",
                         "score": 50}, VALID)
    store_item(con, {"id": "reddit:spam", "source": "reddit", "community": "stocks", "author": "fresh",
                     "created": now - 3600, "title": "AAPL crash incoming, puts", "score": 1}, VALID)
    upsert_author_meta(con, "reddit", "fresh", 2, 10)
    store_news(con, "n1", "alpaca", now - 7200, "Apple beats estimates, raises outlook", "Strong growth", "", ["AAPL"])
    sig = compute_scores(con, CFG, now)["AAPL"]
    assert not sig["thin"] and sig["voices"] == 6  # the brand-new account is ignored
    assert sig["score"] >= 0.25, sig  # the momentum-mode buy bar
    assert {"reddit", "news"} <= set(sig["agree"])
    # duplicate ingest is a no-op
    assert store_item(con, {"id": "reddit:p0", "source": "reddit", "created": now, "title": "AAPL"}, VALID) == 0


def _bar(d, o, h, l, c):
    return {"d": d, "o": o, "h": h, "l": l, "c": c, "v": 5e6}


def test_local_broker_fills_next_open_and_stops():
    from bot.trader.broker import LocalPaperBroker

    con = connect(":memory:")
    cfg = {**CFG, "paper": {"starting_cash": 10_000, "slippage_pct": 0}}
    b = LocalPaperBroker(con, cfg)
    b.queue("2026-01-05", "AAPL", "BUY", 10, "test")
    bars = {"AAPL": [_bar("2026-01-05", 99, 101, 98, 100), _bar("2026-01-06", 100, 104, 99, 103)]}
    assert b.process(bars) == ["BOUGHT 10 AAPL @ 100.00"]  # filled at the next day's open, not the signal day
    assert b.cash() == pytest.approx(9_000)
    assert b.equity(bars) == pytest.approx(9_000 + 10 * 103)
    assert b.process(bars) == []  # reprocessing the same bars does nothing
    # gap down through the 7% stop fills at the open, not the stop price
    bars["AAPL"].append(_bar("2026-01-07", 90, 91, 88, 89))
    assert b.process(bars) == ["SOLD 10 AAPL @ 90.00 (stop-loss)"]
    assert b.cash() == pytest.approx(9_900) and b.positions() == {}


def test_local_broker_take_profit_and_cash_limit():
    from bot.trader.broker import LocalPaperBroker

    con = connect(":memory:")
    b = LocalPaperBroker(con, {**CFG, "paper": {"starting_cash": 1_000, "slippage_pct": 0}})
    b.queue("2026-01-05", "NVDA", "BUY", 50, "test")  # can only afford 10
    bars = {"NVDA": [_bar("2026-01-06", 100, 101, 99, 100), _bar("2026-01-07", 110, 120, 109, 118)]}
    ev = b.process(bars)
    assert ev == ["BOUGHT 10 NVDA @ 100.00", "SOLD 10 NVDA @ 115.00 (take-profit)"]


def test_symbol_file_parse():
    from bot.sources.prices import parse_symbol_file

    text = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
            "AAPL|Apple Inc.|Q|N|N|100|N|N\nZXZZT|Test|Q|Y|N|100|N|N\nBRK.B|x|Q|N|N|100|N|N\n"
            "File Creation Time: 1112202608:00|||||||\n")
    assert parse_symbol_file(text, "Symbol") == {"AAPL"}


def test_ticker_sentiment_splits_multi_ticker_posts():
    from bot.ingest import ticker_sentiment

    text = "Dumping my AMD, it's going to crash. Loading NVDA calls, super bullish."
    assert ticker_sentiment(text, "NVDA", 2) > 0.3
    assert ticker_sentiment(text, "AMD", 2) < -0.3


def test_post_quality_and_consensus():
    from bot.signals.score import consensus, post_quality

    assert post_quality("reddit", "DD", 3000) == pytest.approx(2.0)
    assert post_quality("reddit", "YOLO", 100) < 1 < post_quality("reddit", "Discussion", 2000)
    assert post_quality("stocktwits", "", 0) == 1.0
    agreed, _, _ = consensus([5.0, 10, 5.0, 10], 3)  # 10 voices all at +0.5
    split, a, _ = consensus([0.0, 10, 5.0, 10], 3)  # half +0.5, half -0.5
    assert agreed > 0.3 and split == 0 and a == 0
    few, _, n = consensus([1.0, 2, 1.0, 2], 3)  # 2 voices at +0.5
    assert n == pytest.approx(2) and few < agreed


def _trend(n, start=100, step=1.0, rng=2.0):
    return [{"d": f"d{i:03d}", "o": start + i * step, "h": start + i * step + rng, "l": start + i * step - rng,
             "c": start + i * step, "v": 5e6} for i in range(n)]


def test_technicals_and_regime():
    from bot.signals.technicals import entry_veto, market_regime, technicals

    tc, _ = _rules()
    up = _trend(30)
    assert entry_veto(technicals(up), tc) is None
    assert "downtrend" in entry_veto(technicals(_trend(30, 200, -1)), tc)
    spike = _trend(30) + [{"d": "z", "o": 150, "h": 160, "l": 150, "c": 160, "v": 5e6}]
    assert "chasing" in entry_veto(technicals(spike), tc)
    assert "history" in entry_veto(technicals(_trend(10)), tc)
    assert market_regime(_trend(60)) == "risk-on" and market_regime(_trend(60, 200, -1)) == "risk-off"


def test_risk_sizing_and_regime_limits():
    from bot.trader.risk import stop_and_target

    tc, sc = _rules()
    assert stop_and_target(0.01, tc) == pytest.approx((0.04, 0.08))  # 2 ATR = 2%, clamped up to 4%
    assert stop_and_target(0.05, tc)[0] == pytest.approx(0.10)
    assert stop_and_target(0.2, tc)[0] == pytest.approx(0.12)
    # 1% risk with a 10% stop -> $10k position, under the 5% ($5k) cap -> cap wins
    assert position_size(100_000, 50, 0.05, 0.10, 0.01) == 100
    # 1% risk with a 25% stop -> $4k position
    assert position_size(100_000, 50, 0.05, 0.25, 0.01) == 80
    sigs = {t: {"ticker": t, "score": 0.3 + i / 50, "agree": ["a", "b"], "thin": False} for i, t in enumerate("ABCDE")}
    assert len(plan_entries(sigs, set(), tc, sc, "risk-on")) == tc["max_new_per_day"]
    assert plan_entries(sigs, set(), tc, sc, "risk-off") == [sigs["E"]]  # D (0.36) and E (0.38) clear 0.35, but risk-off allows 1


def test_breakeven_stop():
    from bot.trader.broker import LocalPaperBroker

    con = connect(":memory:")
    b = LocalPaperBroker(con, {**CFG, "paper": {"starting_cash": 10_000, "slippage_pct": 0}})
    b.queue("d0", "X", "BUY", 10, "t", stop_pct=0.05, take_pct=0.5)
    bars = {"X": [_bar("d1", 100, 106, 99, 106)]}  # closes +6%, past 1x risk (5%)
    assert b.process(bars) == ["BOUGHT 10 X @ 100.00"]
    assert b.positions()["X"]["stop"] == pytest.approx(100)
    bars["X"].append(_bar("d2", 103, 104, 99, 100))
    assert b.process(bars) == ["SOLD 10 X @ 100.00 (breakeven stop)"]


def test_scorecard_forward_return():
    from bot.signals.scorecard import fwd_from_next_open

    bars = [{"d": f"2026-01-{i:02d}", "o": 100 + i, "c": 101 + i} for i in range(1, 20)]
    # signal on the 1st -> buy at the 2nd's open (102), sell at the 11th's close (112)
    assert fwd_from_next_open(bars, "2026-01-01") == pytest.approx(112 / 102 - 1)


def test_reddit_x_specifics():
    from bot.signals.score import post_quality

    # DD bonus is for the post; comments in the thread get a smaller one
    assert post_quality("reddit", "DD", 0, is_post=True) > post_quality("reddit", "DD", 0, is_post=False) > 1
    con = connect(":memory:")
    now = time.time()
    # cashtag spam from X is dropped
    spam = {"id": "x:1", "source": "x", "author": "bot", "created": now, "body": "$AAPL $TSLA $GME $NVDA $IT to the moon"}
    assert store_item(con, spam, VALID) == 0
    # a wire account's tweet counts as news, not as a crowd voice
    for i in range(6):
        store_item(con, {"id": f"reddit:w{i}", "source": "reddit", "community": "stocks", "author": f"u{i}",
                         "created": now - 600, "title": "NVDA calls, very bullish", "score": 5}, VALID)
    store_item(con, {"id": "x:2", "source": "x", "author": "DeItaone", "created": now - 60,
                     "body": "NVIDIA BEATS ESTIMATES, RAISES GUIDANCE $NVDA"}, VALID)
    sig = compute_scores(con, CFG, now)["NVDA"]
    assert "x" not in sig["parts"] and "news" in sig["parts"] and sig["voices"] == 6
