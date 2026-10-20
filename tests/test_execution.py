from datetime import datetime, timedelta

import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from cta_lab.runner import Contract, run_backtest
from cta_lab.strategy import DonchianAtrStrategy


class KnownRoundTrip(DonchianAtrStrategy):
    def on_bar(self, bar):
        self.cancel_all()
        self._last_bar = bar
        if bar.datetime.day == 1:
            self._target(2, bar, "known_entry")
        elif bar.datetime.day == 3:
            self._target(0, bar, "known_exit")


def known_bars():
    return [
        BarData(
            symbol="DEMO",
            exchange=Exchange.SHFE,
            datetime=datetime(2020, 1, 1) + timedelta(days=i),
            interval=Interval.DAILY,
            open_price=o,
            high_price=max(o, c) + 1,
            low_price=min(o, c) - 1,
            close_price=c,
            volume=1000,
            gateway_name="TEST",
        )
        for i, (o, c) in enumerate([(100, 100), (101, 103), (103, 104), (99, 100)])
    ]


def test_exact_next_bar_open_fills_and_two_sided_costs():
    result = run_backtest(known_bars(), Contract(), strategy_class=KnownRoundTrip)
    assert result.trades["price"].tolist() == [101, 99]
    assert result.trades["datetime"].tolist() == ["2020-01-02T00:00:00", "2020-01-04T00:00:00"]
    assert result.statistics["commission"] == pytest.approx(0.4)
    assert result.statistics["slippage"] == pytest.approx(40)
    assert result.statistics["net_pnl"] == pytest.approx(-80.4)


def test_unfilled_final_exit_cannot_be_success():
    bars = known_bars()
    bars[-1].open_price = 50
    bars[-1].high_price = 51
    bars[-1].low_price = 49
    bars[-1].close_price = 50
    with pytest.raises(RuntimeError, match="open position"):
        run_backtest(bars, Contract(), strategy_class=KnownRoundTrip)


def test_upstream_swallowed_exception_is_surfaced():
    class Broken(DonchianAtrStrategy):
        def on_bar(self, bar):
            raise RuntimeError("intentional failure")

    with pytest.raises(RuntimeError, match="replay interrupted"):
        run_backtest(known_bars(), Contract(), strategy_class=Broken)


def test_last_bar_exception_after_callback_marker_cannot_pass():
    class BrokenOnLast(KnownRoundTrip):
        def on_bar(self, bar):
            super().on_bar(bar)
            if bar.datetime.day == 4:
                raise RuntimeError("failure after final callback marker")

    with pytest.raises(RuntimeError, match="replay interrupted"):
        run_backtest(known_bars(), Contract(), strategy_class=BrokenOnLast)
