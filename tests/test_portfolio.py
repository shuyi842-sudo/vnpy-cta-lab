import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from cta_lab.portfolio import MultiProductCtaStrategy, run_portfolio
from cta_lab.portfolio_model import CausalRollSelector


def config(products=("rb",)):
    cfg = json.loads((Path(__file__).parents[1] / "config/portfolio_protocol.json").read_text())
    cfg["products"] = {p: cfg["products"][p] for p in products}
    cfg["entry_priority"] = list(products)
    cfg["atr_window"], cfg["exit_window"] = 2, 2
    return cfg


def bar(symbol, day, price=100, oi=1000, volume=1000):
    return BarData(
        symbol=symbol,
        exchange=Exchange.SHFE,
        interval=Interval.DAILY,
        datetime=datetime(2025, 1, 1) + timedelta(days=day),
        open_price=price,
        high_price=price + 1,
        low_price=price - 1,
        close_price=price,
        volume=volume,
        open_interest=oi,
        gateway_name="SYNTHETIC",
    )


class SeedPosition(MultiProductCtaStrategy):
    """Known initial intent; subsequent rolls/exits use the actual production strategy."""

    def on_bars(self, bars):
        super().on_bars(bars)
        if self.strategy_engine.datetime == self.trade_start:
            for vt in sorted(bars):
                if vt.split(".")[0].endswith("2505"):
                    self.submit(vt, 1, True, bars[vt], "known_entry", 1)


def rolled_fixture():
    return [
        b
        for day in range(8)
        for b in (
            bar("rb2505", day, 100, 1000),
            bar("rb2510", day, 200, 2000 if day >= 3 else 500),
        )
    ]


def test_roll_gap_is_not_profit_and_each_leg_pays_cost():
    result = run_portfolio(
        rolled_fixture(),
        config(),
        {"entry_window": 2, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    fills = [e for e in result.events if e["event"] == "fill"]
    assert len(fills) == 4
    assert [e["symbol"] for e in fills] == [
        "rb2505.SHFE",
        "rb2505.SHFE",
        "rb2510.SHFE",
        "rb2510.SHFE",
    ]
    assert [e["price"] for e in fills] == [100, 100, 200, 200]
    assert fills[1]["reason"] == "roll_close" and fills[2]["reason"] == "roll_open"
    assert result.statistics["net_pnl"] == pytest.approx(-224.2)
    assert result.statistics["roll_cost"] == pytest.approx(112.1)
    assert result.statistics["end_equity"] == pytest.approx(1_000_000 - 224.2)
    assert sum(result.contract_daily.net_pnl) == pytest.approx(result.statistics["net_pnl"])
    assert all(e["datetime"] > e["signal_datetime"] for e in fills)


def test_one_shared_pool_rejects_second_product_when_first_uses_budget():
    cfg = config(("rb", "al"))
    cfg.update({"gross_notional_cap": 0.0012, "commission_rate": 0, "slippage_ticks": 0})
    bars = [bar(product + "2505", day) for day in range(4) for product in ("rb", "al")]
    result = run_portfolio(
        bars, cfg, {"entry_window": 40, "atr_multiple": 2}, "2025-01-01", "2025-01-04", SeedPosition
    )
    fills = [e for e in result.events if e["event"] == "fill"]
    assert {e["symbol"] for e in fills} == {"rb2505.SHFE"}
    assert any(e["event"] == "reject" and e["reason"] == "shared_gross_cap" for e in result.events)
    assert result.statistics["end_equity"] == 1_000_000
    assert max(result.snapshots.gross) == 1000


def test_gap_open_is_checked_against_actual_shared_budget():
    cfg = config()
    cfg.update({"gross_notional_cap": 0.00105, "commission_rate": 0, "slippage_ticks": 0})
    bars = [bar("rb2505", day, 108 if day == 1 else 100) for day in range(4)]
    result = run_portfolio(
        bars, cfg, {"entry_window": 40, "atr_multiple": 2}, "2025-01-01", "2025-01-04", SeedPosition
    )
    assert not [e for e in result.events if e["event"] == "fill"]
    assert any(e["event"] == "reject" and e["reason"] == "shared_gross_cap" for e in result.events)


def test_intraday_touch_cannot_replace_an_unmarketable_open():
    bars = [bar("rb2505", day) for day in range(4)]
    bars[1].open_price, bars[1].high_price = 120, 121
    result = run_portfolio(
        bars,
        config(),
        {"entry_window": 40, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-04",
        SeedPosition,
    )
    assert not [e for e in result.events if e["event"] == "fill"]
    assert any(
        e["event"] == "reject" and e["reason"] == "not_marketable_at_open" for e in result.events
    )


def test_old_close_releases_shared_budget_before_new_leg():
    cfg = config()
    cfg.update(
        {
            "max_contracts": 1,
            "gross_notional_cap": 0.0025,
            "commission_rate": 0,
            "slippage_ticks": 0,
        }
    )
    result = run_portfolio(
        rolled_fixture(),
        cfg,
        {"entry_window": 2, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    roll_fills = [
        e for e in result.events if e["event"] == "fill" and e["reason"].startswith("roll_")
    ]
    assert [e["reason"] for e in roll_fills] == ["roll_close", "roll_open"]
    assert roll_fills[0]["datetime"] == roll_fills[1]["datetime"]
    assert result.statistics["net_pnl"] == 0


def test_unfilled_old_leg_blocks_new_roll_leg_until_old_closes():
    bars = rolled_fixture()
    old = next(b for b in bars if b.symbol == "rb2505" and b.datetime.day == 5)
    old.open_price, old.low_price = 50, 49
    result = run_portfolio(
        bars,
        config(),
        {"entry_window": 2, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    assert any(
        e["event"] == "reject" and e["reason"] == "existing_product_leg_not_flat"
        for e in result.events
    )
    rolls = [e for e in result.events if e["event"] == "fill" and e["reason"] == "roll_open"]
    assert rolls[0]["datetime"] == "2025-01-06T00:00:00"


def test_missing_bar_cannot_fill_using_upstream_cached_quote():
    bars = [b for b in rolled_fixture() if not (b.symbol == "rb2505" and b.datetime.day == 2)]
    result = run_portfolio(
        bars,
        config(),
        {"entry_window": 40, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    assert not [e for e in result.events if e["event"] == "fill"]
    assert any(
        e["event"] == "reject" and e["reason"] == "missing_execution_bar" for e in result.events
    )


def test_missing_held_contract_mark_fails_whole_replay():
    bars = [b for b in rolled_fixture() if not (b.symbol == "rb2505" and b.datetime.day == 3)]
    with pytest.raises(RuntimeError, match="missing real bar for held"):
        run_portfolio(
            bars,
            config(),
            {"entry_window": 40, "atr_multiple": 2},
            "2025-01-01",
            "2025-01-08",
            SeedPosition,
        )


def test_future_prices_and_oi_cannot_change_earlier_actions():
    bars = rolled_fixture()
    original = run_portfolio(
        bars,
        config(),
        {"entry_window": 2, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    changed = deepcopy(bars)
    for b in changed:
        if b.datetime.day >= 6:
            b.open_interest *= 20
            for name in ("open_price", "high_price", "low_price", "close_price"):
                setattr(b, name, getattr(b, name) * 1.01)
    other = run_portfolio(
        changed,
        config(),
        {"entry_window": 2, "atr_multiple": 2},
        "2025-01-01",
        "2025-01-08",
        SeedPosition,
    )
    cutoff = "2025-01-06"
    assert [e for e in original.events if e["datetime"] < cutoff] == [
        e for e in other.events if e["datetime"] < cutoff
    ]


def test_selector_hysteresis_forward_only_and_delivery_buffer():
    selector = CausalRollSelector(config())
    bars = {b.vt_symbol: b for b in (bar("rb2505", 0, oi=1000), bar("rb2510", 0, oi=1050))}
    assert selector.choose("rb", bars, datetime(2025, 1, 1)) == "rb2510.SHFE"
    bars["rb2505.SHFE"].open_interest = 1_000_000
    assert selector.choose("rb", bars, datetime(2025, 1, 1)) == "rb2510.SHFE"
    selector = CausalRollSelector(config())
    bars["rb2505.SHFE"].open_interest = 1000
    bars["rb2510.SHFE"].open_interest = 900
    assert selector.choose("rb", bars, datetime(2025, 1, 1)) == "rb2505.SHFE"
    bars["rb2510.SHFE"].open_interest = 1050
    assert selector.choose("rb", bars, datetime(2025, 1, 1)) == "rb2505.SHFE"
    bars["rb2510.SHFE"].open_interest = 1101
    assert selector.choose("rb", bars, datetime(2025, 1, 1)) == "rb2510.SHFE"
    selector = CausalRollSelector(config())
    for b in bars.values():
        b.datetime = datetime(2025, 4, 15)
    bars["rb2505.SHFE"].open_interest = 1_000_000
    assert selector.choose("rb", bars, datetime(2025, 4, 15)) == "rb2510.SHFE"
