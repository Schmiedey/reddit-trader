import time
from datetime import date, timedelta

from bot.backtest.sim import Bars, rescore, signal_study, simulate
from bot.db import connect
from bot.ingest import store_item
from bot.signals.score import compute_scores

VALID = {"AAPL", "TSLA"}


def test_scores_ignore_posts_after_replay_time():
    con = connect(":memory:")
    now = time.time()
    for i in range(6):
        store_item(con, {"id": f"reddit:f{i}", "source": "reddit", "community": "stocks", "author": f"u{i}",
                         "created": now + 3600, "title": "AAPL to the moon, bullish", "score": 10}, VALID)
    assert "AAPL" not in compute_scores(con, {**__import__("bot.config", fromlist=["CFG"]).CFG}, now)


def _days(n, start=date(2025, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _series(days, closes, vol=5e6):
    return [{"d": d, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": vol} for d, c in zip(days, closes)]


def _sig(score, voices=10):
    return {"score": score, "crowd": score * 2, "news": 0.0, "buzz": 0.0, "parts": {"reddit": score * 2},
            "agree": ["reddit"] if score > 0 else [], "agreement": 1.0, "eff_voices": voices, "voices": voices,
            "spike_ratio": 1.0, "cred_share": 0.5, "lowcred_share": 0.0, "pump_sub_share": 0.0}


def test_sim_buys_next_open_and_hits_target():
    days = _days(80)
    spy = _series(days, [100 + i * 0.1 for i in range(80)])
    up = _series(days, [50 + i * 0.05 for i in range(60)] + [53 + i * 1.0 for i in range(20)])
    bars = {"SPY": spy, "AAPL": up}
    signals = {d: ({"AAPL": {**_sig(0.5), "ticker": "AAPL"}} if d == days[60] else {}) for d in days}
    m = simulate(signals, bars, {"trading.max_ret5": 9, "trading.atr_stop_mult": 2.0, "trading.reward_risk": 2.0,
                                 "scoring.buy_threshold": 0.25}, mode="momentum", use_regime=False, use_veto=True)
    assert m["trades"] == 1 and m["exits"]["target"] == 1 and m["return"] > 0


def test_sim_inverse_and_random_modes_run():
    days = _days(70)
    bars = {"SPY": _series(days, [100] * 70), "AAPL": _series(days, [50 + i * 0.01 for i in range(70)]),
            "TSLA": _series(days, [80 - i * 0.01 for i in range(70)])}
    signals = {d: {"AAPL": {**_sig(0.4), "ticker": "AAPL"}, "TSLA": {**_sig(-0.4), "ticker": "TSLA"}} for d in days}
    assert rescore(signals[days[0]]["TSLA"], None, "inverse")["score"] == 0.4
    for mode in ("score", "inverse", "random", "attention"):
        m = simulate(signals, bars, {"trading.max_ret5": 9}, mode=mode, use_regime=False, use_veto=False)
        assert m["days"] == 70


def test_signal_study_quintiles():
    days = _days(40)
    bars = {"SPY": _series(days, [100] * 40)}
    signals = {d: {} for d in days}
    for k in range(10):  # higher score -> stronger uptrend
        t = f"T{k}"
        bars[t] = _series(days, [20 * (1 + 0.002 * k) ** i for i in range(40)])
        for d in days:
            signals[d][t] = {**_sig(k / 10), "ticker": t}
    s = signal_study(signals, Bars(bars), horizons=(5,))
    assert s["h5"][-1]["mean_excess"] > s["h5"][0]["mean_excess"]
    assert s["ic5"]["mean"] > 0.9


def test_stance_parser():
    from bot.signals.stance import classify, stance_for

    cases = [
        ("Bought 500 shares of NVDA at open", "", "BUY"),
        ("Loaded up on PLTR calls for earnings", "", "BUY"),
        ("$TSLA 40k position, all in", "YOLO", "BUY"),
        ("TSLA puts printing", "YOLO", "SELL"),
        ("Still holding GME, not selling", "", "HOLD"),
        ("Sold all my AMD today, took profits", "", "SELL"),
        ("Shorting CVNA here", "", "SELL"),
        ("Bought puts on SPY", "", "SELL"),
        ("NVDA paid for my car", "Gain", "WON"),
        ("Down 80% on INTC", "Loss", "LOST"),
        ("Made 30k on SPY puts", "Gain", "WON_BEAR"),
        ("Should I buy NVDA here? Thinking about buying", "", None),
        ("NVDA earnings are next week", "", None),
    ]
    for text, flair, want in cases:
        assert classify(text, flair, 0.0)[0] == want, text
    assert classify("Undervalued, strong cash flow thesis", "DD", 0.6)[0] == "DD"
    text = "Sold my AMD yesterday. Bought NVDA with the cash."
    assert stance_for(text, "AMD", 2)[0] == "SELL" and stance_for(text, "NVDA", 2)[0] == "BUY"


def test_crowd_swing_signals():
    from bot.config import CFG
    from bot.signals.crowd import crowd_features, crowd_kind, crowd_signals, finalize

    con = connect(":memory:")
    now = time.time()
    post = lambda i, title, flair="", sub="wallstreetbets", age=3600: store_item(con, {
        "id": f"reddit:c{i}", "source": "reddit", "community": sub, "author": f"u{i}", "created": now - age,
        "title": title, "flair": flair, "score": 5}, VALID)
    post(99, "Still holding AAPL", age=30 * 86400)  # old post: the database has more than two weeks of history
    for i in range(3):
        post(i, "Down 60% on TSLA, I'm done", "Loss")
    post(3, "TSLA paid my rent", "Gain")
    for i in range(4, 8):
        post(i, "Bought 200 shares of AAPL today")
    post(8, "Bought AAPL", sub="pennystocks")                                  # pump-watch sub: ignored
    store_item(con, {"id": "reddit:cm", "source": "reddit", "community": "wallstreetbets", "author": "u9",
                     "created": now - 60, "body": "Bought AAPL too", "flair": "Loss"}, VALID)  # comment: ignored
    f = crowd_features(con, CFG, now)
    assert f["TSLA"]["loss5"] == 3 and f["TSLA"]["gain5"] == 1 and f["AAPL"]["buyers5"] == 4
    cap = {"loss_rule": "capitulation", "loss_size": 2.0}
    assert crowd_kind(f["TSLA"], -0.05)[0] == "loss bounce" and crowd_kind(f["TSLA"], -0.05)[3] == 1.0
    assert crowd_kind(f["TSLA"], -0.12, cap)[0] == "capitulation" and crowd_kind(f["TSLA"], -0.12, cap)[3] == 2.0
    assert crowd_kind(f["TSLA"], -0.05, cap)[0] is None  # capitulation needs a sharp drop
    assert crowd_kind({**f["TSLA"], "loss5": 2, "gain5": 0}, -0.16, cap)[0] == "capitulation"
    assert crowd_kind({**f["TSLA"], "loss5": 2, "gain5": 0}, -0.16)[0] is None  # bounce needs 3 loss posts
    assert crowd_kind(f["AAPL"], 0.02)[0] == "early build-up" and crowd_kind(f["AAPL"], 0.2)[0] is None
    assert crowd_kind(f["AAPL"], 0.02, {"early_build_up": False})[0] is None
    sig = crowd_signals(con, CFG, {}, now)
    assert not sig["TSLA"]["thin"] and sig["TSLA"]["score"] == 0  # candidate until its price is known
    finalize(sig["TSLA"], {"ret5": -0.2}, {"loss_rule": "bounce"})
    assert sig["TSLA"]["score"] >= 0.9 and sig["TSLA"]["swing_kind"] == "loss bounce" and sig["TSLA"]["agree"] == ["reddit"]
    finalize(sig["AAPL"], {"ret5": 0.3})
    assert sig["AAPL"]["score"] == 0


def test_live_job_end_to_end_capitulation_trade(tmp_path, monkeypatch):
    """The real daily job, replayed: loss posts + a sharp drop -> order -> fill next open -> sold after 10 days."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bot.config import CFG
    from bot.sources import prices
    from bot.trader import run_daily

    for k, v in {"strategy": "crowd_swing", "buy_threshold": 0.5, "min_agreeing_sources": 1, "exit_threshold": -9,
                 "early_build_up": False, "loss_rule": "capitulation", "loss_size": 2.0}.items():
        monkeypatch.setitem(CFG["scoring"], k, v)
    for k, v in {"dry_run": False, "max_hold_days": 9, "use_entry_vetoes": False, "atr_stop_mult": 4.0,
                 "max_stop_pct": 0.25, "reward_risk": 50, "max_ret5": 9, "position_pct": 0.10,
                 "risk_per_trade_pct": 0.02, "risk_off_max_new": 3, "risk_off_threshold_bump": 0.0}.items():
        monkeypatch.setitem(CFG["trading"], k, v)
    days = _days(100, date(2025, 1, 6))
    d0 = 80                                                  # the day the loss posts appear
    px = [100.0] * (d0 - 4) + [97, 94, 91, 88, 85] + [85 + 1.5 * k for k in range(1, 100 - d0)]
    bars = {"TSLA": _series(days, px), "SPY": _series(days, [500 + 0.1 * i for i in range(100)])}
    fake = lambda _con, syms, since: {s: bars[s] for s in syms if s in bars}
    monkeypatch.setattr(run_daily, "BARS_FN", fake)
    monkeypatch.setattr(prices, "daily_bars", fake)
    monkeypatch.setattr(run_daily, "log", lambda *_: None)
    db = str(tmp_path / "e2e.db")
    con = connect(db)
    noon = datetime.fromisoformat(days[d0]).replace(hour=12, tzinfo=ZoneInfo("America/New_York")).timestamp()
    for i in range(3):
        store_item(con, {"id": f"reddit:e{i}", "source": "reddit", "community": "wallstreetbets", "author": f"bag{i}",
                         "created": noon, "title": "Down 70% on TSLA, lost it all", "flair": "Loss", "score": 9}, VALID)
    con.commit()
    run = lambda k: run_daily.main(["--as-of", days[k], "--db", db])
    run(d0)
    order = con.execute("SELECT * FROM paper_orders").fetchone()
    assert order["sym"] == "TSLA" and order["side"] == "BUY" and order["status"] == "pending"
    assert "capitulation" in order["reason"] and order["qty"] * 85 > 12_000   # double-size position
    run(d0 + 1)
    pos = con.execute("SELECT * FROM paper_positions").fetchone()
    assert pos["sym"] == "TSLA" and pos["entry_day"] == days[d0 + 1]            # filled at the next open
    for k in range(d0 + 2, d0 + 12):
        run(k)
    assert con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0  # sold after 10 trading days
    sell = con.execute("SELECT * FROM paper_orders WHERE side='SELL' AND status='filled'").fetchone()
    assert sell["fill_day"] == days[d0 + 11] and sell["fill_price"] > pos["entry_price"]
    assert con.execute("SELECT cash FROM paper_account").fetchone()[0] > 100_000
    assert con.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 12
