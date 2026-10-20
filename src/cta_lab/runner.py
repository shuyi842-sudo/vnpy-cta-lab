"""Use the upstream matcher and daily accounting; fail on interrupted replay."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData
from vnpy_ctastrategy.backtesting import BacktestingEngine

from .metrics import summarize
from .strategy import DonchianAtrStrategy


class VerifiedBacktestingEngine(BacktestingEngine):
    """Count only bars fully processed by upstream matching, callback and daily marking."""

    def __init__(self):
        super().__init__()
        self.processed_bars = 0

    def new_bar(self, bar):
        super().new_bar(bar)
        self.processed_bars += 1


@dataclass(frozen=True)
class Contract:
    vt_symbol: str = "DEMO.SHFE"
    capital: int = 1_000_000
    multiplier: float = 10.0
    tick: float = 1.0
    commission_rate: float = 0.0001
    slippage_ticks: float = 1.0
    annual_days: int = 240

    def __post_init__(self):
        for name in ("capital", "multiplier", "tick", "annual_days"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("commission_rate", "slippage_ticks"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.commission_rate >= 1:
            raise ValueError("commission_rate must be a fraction below 1")


@dataclass
class Run:
    daily: pd.DataFrame
    statistics: dict
    trades: pd.DataFrame
    audit: list[dict]
    settings: dict
    contract: dict


def run_backtest(
    bars: list[BarData],
    contract: Contract,
    settings: dict | None = None,
    trade_start: datetime | None = None,
    strategy_class: type[DonchianAtrStrategy] = DonchianAtrStrategy,
) -> Run:
    if len(bars) < 3:
        raise ValueError("at least three bars required")
    if any(a.datetime >= b.datetime for a, b in zip(bars, bars[1:], strict=False)):
        raise ValueError("bars must be strictly increasing")
    if any(bar.vt_symbol != contract.vt_symbol for bar in bars):
        raise ValueError("mixed contracts are not supported; run each contract separately")
    start = trade_start or bars[0].datetime
    if start not in {bar.datetime for bar in bars[:-2]}:
        raise ValueError("trade_start must identify an input bar before the final two bars")
    allowed = {
        "entry_window",
        "exit_window",
        "atr_window",
        "atr_multiple",
        "risk_fraction",
        "max_contracts",
        "max_notional_fraction",
        "daily_loss_limit",
        "drawdown_limit",
    }
    settings = dict(settings or {})
    if set(settings) - allowed:
        raise ValueError(f"unknown or reserved settings: {set(settings) - allowed}")
    settings.update(
        {
            "capital": contract.capital,
            "multiplier": contract.multiplier,
            "price_tick": contract.tick,
            "trade_start": start.strftime("%Y-%m-%d"),
            "commission_rate": contract.commission_rate,
            "slippage_ticks": contract.slippage_ticks,
            "flatten_signal_date": bars[-2].datetime.strftime("%Y-%m-%d"),
            "final_date": bars[-1].datetime.strftime("%Y-%m-%d"),
        }
    )
    engine = VerifiedBacktestingEngine()
    messages: list[str] = []
    engine.output = messages.append
    engine.set_parameters(
        vt_symbol=contract.vt_symbol,
        interval=Interval.DAILY,
        start=bars[0].datetime,
        end=bars[-1].datetime,
        rate=contract.commission_rate,
        slippage=contract.tick * contract.slippage_ticks,
        size=contract.multiplier,
        pricetick=contract.tick,
        capital=contract.capital,
        annual_days=contract.annual_days,
    )
    engine.add_strategy(strategy_class, settings)
    engine.history_data = bars
    engine.run_backtesting()
    # Upstream catches strategy errors internally; never report a partial run as success.
    if engine.processed_bars != len(bars):
        raise RuntimeError("replay interrupted: " + "\n".join(messages[-5:]))
    if engine.strategy.pos or engine.active_limit_orders:
        raise RuntimeError("window ended with open position/orders; liquidation not filled")
    daily = engine.calculate_result().copy()
    daily = daily.loc[daily.index >= start.date()]
    trades = pd.DataFrame(
        [
            {
                "datetime": t.datetime.isoformat(),
                "tradeid": t.tradeid,
                "orderid": t.orderid,
                "direction": t.direction.value,
                "offset": t.offset.value,
                "price": t.price,
                "volume": t.volume,
            }
            for t in engine.trades.values()
        ],
        columns=["datetime", "tradeid", "orderid", "direction", "offset", "price", "volume"],
    )
    stats = summarize(daily, contract.capital, contract.annual_days)
    return Run(daily, stats, trades, engine.strategy.audit, settings, asdict(contract))
