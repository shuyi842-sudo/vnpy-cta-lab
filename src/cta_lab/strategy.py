"""Causal daily Donchian breakout, close-based ATR stop, next-bar limit execution."""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime

from vnpy.trader.constant import Offset
from vnpy.trader.object import BarData, TradeData
from vnpy_ctastrategy import CtaTemplate

from .risk import RiskLimits


class DonchianAtrStrategy(CtaTemplate):
    author = "CTA Lab"
    entry_window = 40
    exit_window = 15
    atr_window = 20
    atr_multiple = 2.5
    capital = 1_000_000
    risk_fraction = 0.005
    max_contracts = 5
    max_notional_fraction = 0.5
    multiplier = 10.0
    price_tick = 1.0
    commission_rate = 0.0001
    slippage_ticks = 1.0
    daily_loss_limit = 0.02
    drawdown_limit = 0.10
    trade_start = "1970-01-01"
    flatten_signal_date = "2099-01-01"
    final_date = "2099-01-02"
    parameters = [
        "entry_window",
        "exit_window",
        "atr_window",
        "atr_multiple",
        "capital",
        "risk_fraction",
        "max_contracts",
        "max_notional_fraction",
        "multiplier",
        "price_tick",
        "trade_start",
        "flatten_signal_date",
        "final_date",
        "commission_rate",
        "slippage_ticks",
        "daily_loss_limit",
        "drawdown_limit",
    ]
    variables = ["atr", "stop_price"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        if any(
            not isinstance(n, int) or n < 2
            for n in (self.entry_window, self.exit_window, self.atr_window)
        ):
            raise ValueError("lookback windows must be integers >= 2")
        if any(
            not math.isfinite(x) or x <= 0
            for x in (self.atr_multiple, self.multiplier, self.price_tick)
        ):
            raise ValueError("ATR multiple, multiplier and tick must be positive")
        if not 0 < self.daily_loss_limit <= 1 or not 0 < self.drawdown_limit <= 1:
            raise ValueError("loss and drawdown limits must be in (0, 1]")
        self.limits = RiskLimits(
            self.capital, self.risk_fraction, self.max_contracts, self.max_notional_fraction
        )
        self.bars = deque(maxlen=max(self.entry_window, self.exit_window) + 1)
        self.ranges = deque(maxlen=self.atr_window)
        self.atr = 0.0
        self.stop_price = 0.0
        self.audit: list[dict] = []
        self._last_bar: BarData | None = None
        self._signal_time: datetime | None = None
        self._cash = float(self.capital)
        self._tracked_pos = 0
        self._previous_equity = float(self.capital)
        self._peak_equity = float(self.capital)
        self.halted = False

    def on_init(self):
        self.write_log("CSV runner supplies warmup bars; no database or gateway is used")

    def on_start(self):
        pass

    def on_stop(self):
        self.cancel_all()

    def on_bar(self, bar: BarData):
        self.cancel_all()  # Each daily limit lives for at most one subsequent bar.
        previous_close = self.bars[-1].close_price if self.bars else bar.open_price
        self.ranges.append(
            max(
                bar.high_price - bar.low_price,
                abs(bar.high_price - previous_close),
                abs(bar.low_price - previous_close),
            )
        )
        self.atr = sum(self.ranges) / len(self.ranges)
        self._last_bar = bar
        equity = self._cash + self.pos * bar.close_price * self.multiplier
        self._peak_equity = max(self._peak_equity, equity)
        daily_loss = equity / self._previous_equity - 1 if self._previous_equity > 0 else -1
        drawdown = equity / self._peak_equity - 1
        self._previous_equity = equity
        risk_exit = daily_loss <= -self.daily_loss_limit or drawdown <= -self.drawdown_limit
        if drawdown <= -self.drawdown_limit:
            self.halted = True
        day = bar.datetime.strftime("%Y-%m-%d")
        ready = (
            len(self.bars) >= max(self.entry_window, self.exit_window)
            and len(self.ranges) == self.atr_window
        )
        if day >= self.final_date:
            self.bars.append(bar)
            return
        if risk_exit or self.halted:
            self.audit.append(
                {
                    "event": "risk",
                    "datetime": bar.datetime.isoformat(),
                    "equity": equity,
                    "daily_return": daily_loss,
                    "drawdown": drawdown,
                    "halted": self.halted,
                }
            )
            self._target(0, bar, "risk_exit")
        elif day >= self.flatten_signal_date:
            self._target(0, bar, "end_of_window")
        elif ready and day >= self.trade_start:
            prior = list(self.bars)
            upper = max(x.high_price for x in prior[-self.entry_window :])
            lower = min(x.low_price for x in prior[-self.entry_window :])
            exit_low = min(x.low_price for x in prior[-self.exit_window :])
            exit_high = max(x.high_price for x in prior[-self.exit_window :])
            if self.pos > 0:
                self.stop_price = max(
                    self.stop_price, bar.close_price - self.atr_multiple * self.atr
                )
                if bar.close_price < max(self.stop_price, exit_low):
                    self._target(0, bar, "long_exit")
            elif self.pos < 0:
                self.stop_price = min(
                    self.stop_price, bar.close_price + self.atr_multiple * self.atr
                )
                if bar.close_price > min(self.stop_price, exit_high):
                    self._target(0, bar, "short_exit")
            else:
                size = self.limits.size(
                    bar.close_price, self.atr_multiple * self.atr, self.multiplier
                )
                if size and bar.close_price > upper:
                    self._target(size, bar, "long_breakout")
                elif size and bar.close_price < lower:
                    self._target(-size, bar, "short_breakout")
        self.bars.append(bar)

    def _target(self, target: int, bar: BarData, reason: str):
        delta = target - int(self.pos)
        if not delta:
            return
        # Finite aggressive limits; a large opening gap can remain unfilled.
        # They are not market orders, exchange limit prices, or intrabar stop orders.
        buy_price = bar.close_price * 1.1
        sell_price = max(self.price_tick, bar.close_price * 0.9)
        self._signal_time = bar.datetime
        if self.pos > 0 and target == 0:
            ids = self.sell(sell_price, abs(delta))
        elif self.pos < 0 and target == 0:
            ids = self.cover(buy_price, abs(delta))
        elif delta > 0:
            ids = self.buy(buy_price, delta)
        else:
            ids = self.short(sell_price, abs(delta))
        self.audit.append(
            {
                "event": "signal",
                "datetime": bar.datetime.isoformat(),
                "reason": reason,
                "position": self.pos,
                "target": target,
                "atr": self.atr,
                "orderids": ids,
            }
        )

    def on_trade(self, trade: TradeData):
        signed_volume = self.pos - self._tracked_pos
        self._tracked_pos = self.pos
        cost = trade.price * trade.volume * self.multiplier * self.commission_rate
        cost += trade.volume * self.multiplier * self.price_tick * self.slippage_ticks
        self._cash -= signed_volume * trade.price * self.multiplier + cost
        if trade.offset == Offset.OPEN:
            self.stop_price = (
                trade.price - self.atr_multiple * self.atr
                if self.pos > 0
                else trade.price + self.atr_multiple * self.atr
            )
        elif not self.pos:
            self.stop_price = 0.0
        self.audit.append(
            {
                "event": "fill",
                "datetime": trade.datetime.isoformat(),
                "signal_datetime": self._signal_time.isoformat() if self._signal_time else None,
                "direction": trade.direction.value,
                "offset": trade.offset.value,
                "price": trade.price,
                "volume": trade.volume,
                "position": self.pos,
            }
        )
