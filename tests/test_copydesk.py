import pytest

from bot.copydesk import desk

COST = desk.FEE + desk.SLIP


def test_leader_targets_use_exchange_position_and_keep_last_fill_per_minute():
    fills = [
        {"time": 60_000 * 5 + 1, "coin": "BTC", "side": "B", "sz": "1", "startPosition": "0", "tid": 1},
        {"time": 60_000 * 5 + 9, "coin": "BTC", "side": "B", "sz": "2", "startPosition": "1", "tid": 2},
        {"time": 60_000 * 6, "coin": "BTC", "side": "A", "sz": "3", "startPosition": "7", "tid": 3},  # exchange says 7: trust it
    ]
    assert desk.leader_targets(fills) == [(60_000 * 5 + 9, "BTC", 3.0), (60_000 * 6, "BTC", 4.0)]


def test_rebalance_copies_size_and_charges_costs():
    sl = desk.new_slice(10_000.0)
    d, fee = desk.rebalance(sl, "BTC", 0.1, 50_000.0)
    assert d == pytest.approx(0.1) and fee == pytest.approx(5_000 * COST)
    assert sl["eq"] == pytest.approx(10_000 - 5_000 * COST)


def test_leverage_cap_shrinks_the_new_position():
    sl = desk.new_slice(10_000.0)
    desk.rebalance(sl, "BTC", 0.8, 50_000.0)          # 4x
    desk.rebalance(sl, "ETH", -10.0, 3_000.0)          # wants 3x more, only ~1x room left
    assert desk.gross(sl) == pytest.approx(desk.MAX_LEV * sl["eq"], rel=1e-3)
    assert sl["pos"]["ETH"][0] < 0


def test_mark_and_blowup():
    sl = desk.new_slice(10_000.0)
    desk.rebalance(sl, "BTC", 1.0, 50_000.0)           # 5x long
    desk.mark(sl, lambda c: 40_000.0)                   # -20% wipes it out
    assert sl["blown"] and sl["eq"] == 0 and sl["pos"] == {}
    assert desk.rebalance(sl, "BTC", 1.0, 40_000.0) is None


def test_funding_longs_pay_positive_rate_shorts_receive():
    lng, sht = desk.new_slice(10_000.0), desk.new_slice(10_000.0)
    desk.rebalance(lng, "ETH", 1.0, 3_000.0)
    desk.rebalance(sht, "ETH", -1.0, 3_000.0)
    assert desk.pay_funding(lng, "ETH", 0.0001, 3_000.0) == pytest.approx(-0.3)
    assert desk.pay_funding(sht, "ETH", 0.0001, 3_000.0) == pytest.approx(0.3)


def test_closing_removes_position():
    sl = desk.new_slice(10_000.0)
    desk.rebalance(sl, "SOL", 10.0, 150.0)
    desk.rebalance(sl, "SOL", 0.0, 160.0)
    assert "SOL" not in sl["pos"]


def test_interp_and_compact():
    assert desk.interp([0, 10], [0.0, 100.0], 5) == 50.0
    assert desk.interp([0, 10], [0.0, 100.0], 50) == 100.0
    now = 40 * 86400_000
    curve = [[t, 1.0, 1.0] for t in range(0, now + 1, desk.H)]
    kept = desk.compact(curve, now)
    old = [p for p in kept if p[0] < now - 30 * 86400_000]
    assert len(old) == pytest.approx(10 * 4, abs=2) and kept[-1][0] == now
