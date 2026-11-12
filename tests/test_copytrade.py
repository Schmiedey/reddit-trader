import pytest

from bot.copytrade import core

COST = core.FEE + core.SLIP


def test_adopts_wallet_leverage_and_pays_costs():
    a = core.new_account()
    trades = core.step(a, {"BTC": 2.0}, 200_000.0, {"BTC": 50_000.0})  # wallet is 0.5x long
    assert a["pos"]["BTC"]["size"] == pytest.approx(0.1)               # 0.5x of $10k
    assert a["equity"] == pytest.approx(10_000 - 5_000 * COST)
    assert len(trades) == 1


def test_marks_to_market_without_trading_when_wallet_unchanged():
    a = core.new_account()
    core.step(a, {"BTC": 2.0}, 200_000.0, {"BTC": 50_000.0})
    before = a["equity"]
    trades = core.step(a, {"BTC": 2.0}, 220_000.0, {"BTC": 55_000.0})
    assert trades == []
    assert a["equity"] == pytest.approx(before + 0.1 * 5_000)


def test_short_gains_when_price_falls_and_closes_with_wallet():
    a = core.new_account()
    core.step(a, {"ETH": -10.0}, 30_000.0, {"ETH": 3_000.0})           # 1x short
    size = a["pos"]["ETH"]["size"]
    assert size == pytest.approx(-10_000 / 3_000)
    trades = core.step(a, {}, 31_000.0, {"ETH": 2_700.0})
    assert "ETH" not in a["pos"] and len(trades) == 1
    assert a["equity"] > 10_000 * 1.09


def test_total_leverage_is_capped():
    a = core.new_account()
    core.step(a, {"BTC": 4.0, "ETH": -100.0}, 10_000.0, {"BTC": 50_000.0, "ETH": 3_000.0})  # 50x gross
    assert core.gross_leverage(a) == pytest.approx(core.MAX_LEV, rel=0.01)
    assert a["pos"]["BTC"]["size"] > 0 > a["pos"]["ETH"]["size"]


def test_account_blows_up_and_stops():
    a = core.new_account()
    core.step(a, {"BTC": 10.0}, 100_000.0, {"BTC": 50_000.0})          # 5x long
    core.step(a, {"BTC": 10.0}, 1.0, {"BTC": 39_000.0})                 # -22% move wipes 5x
    assert a["blown"] and a["equity"] == 0 and a["pos"] == {}
    assert core.step(a, {"BTC": 10.0}, 100_000.0, {"BTC": 60_000.0}) == []


def test_failed_fetch_only_marks():
    a = core.new_account()
    core.step(a, {"BTC": 2.0}, 200_000.0, {"BTC": 50_000.0})
    assert core.step(a, None, 0.0, {"BTC": 51_000.0}) == []
    assert a["pos"]["BTC"]["px"] == 51_000.0
