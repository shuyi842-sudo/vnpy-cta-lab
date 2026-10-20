from dataclasses import replace

import pandas as pd
import pytest

from cta_lab.data import read_bars, write_fixture
from cta_lab.metrics import summarize
from cta_lab.risk import RiskLimits
from cta_lab.runner import Contract, run_backtest


def fixture_bars(tmp_path, count=180):
    path = tmp_path / "fixture.csv"
    write_fixture(path, count)
    return read_bars(path, "DEMO.SHFE")


@pytest.mark.parametrize(
    "bad_row",
    [
        "2020-01-02,10,9,8,10,1",
        "2020-01-02,nan,11,8,10,1",
        "2020-01-02,10,11,8,10,-1",
        "2020-01-01,10,11,8,10,1",
    ],
)
def test_data_rejects_invalid_ohlc_or_order(tmp_path, bad_row):
    path = tmp_path / "bad.csv"
    path.write_text(
        "datetime,open,high,low,close,volume\n2020-01-01,10,11,8,10,1\n"
        + bad_row
        + "\n2020-01-03,10,11,8,10,1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        read_bars(path, "DEMO.SHFE")


def test_risk_caps_and_does_not_round_up():
    assert RiskLimits(capital=1000, risk_fraction=0.01, max_contracts=5).size(10, 2, 10) == 0
    assert RiskLimits().size(3500, 100, 10) == 5
    assert RiskLimits(max_notional_fraction=0.01).size(3500, 100, 10) == 0
    assert RiskLimits().size(float("nan"), 100, 10) == 0


def test_initial_loss_is_in_drawdown():
    daily = pd.DataFrame(
        {"net_pnl": [-10.0, 5.0], "commission": [0, 0], "slippage": [0, 0], "trade_count": [0, 0]}
    )
    assert summarize(daily, 100)["max_drawdown"] == pytest.approx(-0.1)


def test_vnpy_pnl_reconciles_to_independent_flat_trade_ledger(tmp_path):
    bars = fixture_bars(tmp_path)
    contract = Contract()
    result = run_backtest(bars, contract, {"entry_window": 20})
    assert len(result.trades) > 0
    signed_cash = 0
    commission = 0
    volume = 0
    position = 0
    for event in result.audit:
        if event["event"] != "fill":
            continue
        # Direction values are localized upstream; position delta is authoritative.
        signed_volume = event["position"] - position
        position = event["position"]
        signed_cash -= signed_volume * event["price"] * contract.multiplier
        commission += (
            event["price"] * event["volume"] * contract.multiplier * contract.commission_rate
        )
        volume += event["volume"]
        assert event["datetime"] > event["signal_datetime"]
        assert abs(event["position"]) <= 5
    assert position == 0
    slippage = volume * contract.multiplier * contract.tick * contract.slippage_ticks
    assert result.statistics["net_pnl"] == pytest.approx(signed_cash - commission - slippage)
    assert result.statistics["commission"] == pytest.approx(commission)
    assert result.statistics["slippage"] == pytest.approx(slippage)


def test_future_changes_do_not_change_prior_events(tmp_path):
    bars = fixture_bars(tmp_path)
    original = run_backtest(bars, Contract())
    altered = fixture_bars(tmp_path)
    for bar in altered[130:]:
        for field in ("open_price", "high_price", "low_price", "close_price"):
            setattr(bar, field, getattr(bar, field) * 1.01)
    other = run_backtest(altered, Contract())
    cutoff = bars[130].datetime.isoformat()
    assert [x for x in original.audit if x["datetime"] < cutoff] == [
        x for x in other.audit if x["datetime"] < cutoff
    ]


def test_costs_do_not_change_fills_and_reduce_pnl(tmp_path):
    bars = fixture_bars(tmp_path)
    zero = run_backtest(bars, replace(Contract(), commission_rate=0, slippage_ticks=0))
    paid = run_backtest(bars, Contract())
    pd.testing.assert_frame_equal(zero.trades, paid.trades)
    assert paid.statistics["net_pnl"] == pytest.approx(
        zero.statistics["net_pnl"] - paid.statistics["commission"] - paid.statistics["slippage"]
    )


def test_warmup_cannot_trade(tmp_path):
    bars = fixture_bars(tmp_path)
    start = bars[70].datetime
    result = run_backtest(bars, Contract(), trade_start=start)
    assert min(result.daily.index) == start.date()
    assert all(x["datetime"] >= start.isoformat() for x in result.audit)
