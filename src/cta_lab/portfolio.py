"""Actual vn.py portfolio matching with causal rolls and one funded risk ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from vnpy.trader.constant import Direction, Interval, Offset
from vnpy_portfoliostrategy.backtesting import BacktestingEngine
from vnpy_portfoliostrategy.template import StrategyTemplate

from .metrics import summarize
from .portfolio_model import (
    CausalRollSelector,
    ContractWindow,
    SharedEquityBook,
    product_of,
    validate_protocol,
)


class FundedPortfolioEngine(BacktestingEngine):
    """Keep upstream fill/PnL math; gate each open fill against shared resources."""

    def __init__(self):
        super().__init__()
        self.processed_dates = 0

    def new_bars(self, dt):
        for vt, quantity in self.strategy.book.positions.items():
            if quantity and (dt, vt) not in self.history_data:
                raise RuntimeError(f"missing real bar for held contract {vt} at {dt.date()}")
        super().new_bars(dt)
        self.processed_dates += 1

    def cross_limit_order(self):
        strategy = self.strategy
        priority = strategy.cfg["entry_priority"]
        orders = sorted(
            list(self.active_limit_orders.values()),
            key=lambda order: (
                order.offset == Offset.OPEN,
                priority.index(product_of(order.vt_symbol)),
                int(order.orderid),
            ),
        )
        for order in orders:
            if order.vt_orderid not in self.active_limit_orders:
                continue
            real_bar = self.history_data.get((self.datetime, order.vt_symbol))
            context = strategy.contexts[order.vt_orderid]
            if real_bar is None:
                strategy.event(
                    "reject",
                    orderid=order.vt_orderid,
                    symbol=order.vt_symbol,
                    reason="missing_execution_bar",
                )
                self.cancel_order(strategy, order.vt_orderid)
                continue
            price = real_bar.open_price
            marketable = (
                order.price >= price if order.direction == Direction.LONG else order.price <= price
            )
            if not marketable:
                strategy.event(
                    "reject",
                    orderid=order.vt_orderid,
                    symbol=order.vt_symbol,
                    reason="not_marketable_at_open",
                )
                self.cancel_order(strategy, order.vt_orderid)
                continue
            signed = int(order.volume) * (1 if order.direction == Direction.LONG else -1)
            if order.offset == Offset.OPEN:
                marks = {vt: bar.open_price for vt, bar in self.bars.items()}
                allowed, reason, evidence = strategy.book.can_open(
                    order.vt_symbol, signed, price, context["distance"], marks
                )
            else:
                position = strategy.book.positions[order.vt_symbol]
                allowed = position * signed < 0 and abs(signed) <= abs(position)
                reason, evidence = "invalid_close" if not allowed else "close_admitted", {}
            if not allowed:
                strategy.event(
                    "reject",
                    orderid=order.vt_orderid,
                    symbol=order.vt_symbol,
                    reason=reason,
                    **evidence,
                )
                self.cancel_order(strategy, order.vt_orderid)
                continue
            strategy.event(
                "admission",
                orderid=order.vt_orderid,
                symbol=order.vt_symbol,
                reason=reason,
                **evidence,
            )
            # Feed one admissible order to the unmodified upstream matcher. Each
            # fill updates the common ledger before the next order is considered.
            all_orders = self.active_limit_orders
            self.active_limit_orders = {order.vt_orderid: order}
            try:
                super().cross_limit_order()
            finally:
                remains = order.vt_orderid in self.active_limit_orders
                self.active_limit_orders = all_orders
                if not remains:
                    all_orders.pop(order.vt_orderid, None)


class MultiProductCtaStrategy(StrategyTemplate):
    parameters = ["cfg", "setting", "trade_start", "flatten_date", "final_date"]
    author = "CTA Lab"

    def __init__(self, strategy_engine, strategy_name, vt_symbols, setting):
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
        self.cfg = validate_protocol(self.cfg)
        self.book = SharedEquityBook(self.cfg)
        self.selector = CausalRollSelector(self.cfg)
        self.windows = {
            vt: ContractWindow(
                self.setting["entry_window"], self.cfg["exit_window"], self.cfg["atr_window"]
            )
            for vt in vt_symbols
        }
        self.contexts: dict[str, dict] = {}
        self.stops: dict[str, float] = {}
        self.audit: list[dict] = []
        self.snapshots: list[dict] = []
        self.active: dict[str, str | None] = {}
        self.peak = self.previous_equity = float(self.cfg["capital"])
        self.halted = False

    def on_init(self):
        pass

    def event(self, kind, **values):
        self.audit.append(
            {"event": kind, "datetime": self.strategy_engine.datetime.isoformat(), **values}
        )

    def update_trade(self, trade):
        super().update_trade(trade)
        context = self.contexts[trade.vt_orderid]
        costs = self.book.fill(trade, context["distance"], context["reason"])
        assert self.get_pos(trade.vt_symbol) == self.book.positions[trade.vt_symbol]
        if trade.offset == Offset.OPEN:
            sign = 1 if trade.direction == Direction.LONG else -1
            self.stops[trade.vt_symbol] = trade.price - sign * context["distance"]
        elif not self.get_pos(trade.vt_symbol):
            self.stops.pop(trade.vt_symbol, None)
        self.event(
            "fill",
            symbol=trade.vt_symbol,
            orderid=trade.vt_orderid,
            tradeid=trade.vt_tradeid,
            signal_datetime=context["signal_datetime"],
            reason=context["reason"],
            price=trade.price,
            volume=trade.volume,
            position=self.get_pos(trade.vt_symbol),
            **costs,
        )

    def submit(self, vt, signed_quantity, opening, bar, reason, distance):
        quantity = abs(int(signed_quantity))
        if not quantity:
            return
        buy = signed_quantity > 0
        price = bar.close_price * (1.1 if buy else 0.9)
        price = max(self.cfg["products"][product_of(vt)]["tick"], price)
        method = (
            self.buy
            if opening and buy
            else self.short
            if opening
            else self.cover
            if buy
            else self.sell
        )
        ids = method(vt, price, quantity)
        for orderid in ids:
            self.contexts[orderid] = {
                "distance": distance,
                "reason": reason,
                "signal_datetime": bar.datetime.isoformat(),
            }
        self.event(
            "signal",
            symbol=vt,
            quantity=signed_quantity,
            opening=opening,
            reason=reason,
            orderids=ids,
            distance=distance,
        )

    def on_bars(self, bars):
        self.cancel_all()
        dt = self.strategy_engine.datetime
        views = {vt: self.windows[vt].observe(bar) for vt, bar in bars.items()}
        marks = {vt: bar.close_price for vt, bar in bars.items()}
        equity = self.book.equity(marks)
        self.peak = max(self.peak, equity)
        daily_return = equity / self.previous_equity - 1 if self.previous_equity > 0 else -1
        self.previous_equity = equity
        drawdown = equity / self.peak - 1
        self.halted = self.halted or drawdown <= -self.cfg["drawdown_limit"]
        exposure = self.book.exposure(marks)
        force_flat = (
            self.halted or daily_return <= -self.cfg["daily_loss_limit"] or dt >= self.flatten_date
        )
        force_flat = (
            force_flat
            or exposure["gross"] > equity * self.cfg["gross_notional_cap"]
            or exposure["margin"] > equity * self.cfg["margin_cap"]
        )
        self.snapshots.append(
            {
                "date": dt.date(),
                "equity": equity,
                "cash_ledger": self.book.cash_ledger,
                "commission_total": self.book.commission,
                "slippage_total": self.book.slippage,
                "positions": dict(self.book.positions),
                "drawdown": drawdown,
                **exposure,
            }
        )
        for product in self.cfg["entry_priority"]:
            selected = self.selector.choose(product, bars, dt)
            previous = self.active.get(product)
            if selected != previous:
                self.event(
                    "selection",
                    product=product,
                    previous=previous,
                    selected=selected,
                    effective="next_open",
                    source_datetime=dt.isoformat(),
                    selected_open_interest=float(bars[selected].open_interest)
                    if selected
                    else None,
                    selected_volume=float(bars[selected].volume) if selected else None,
                    incumbent_open_interest=float(bars[previous].open_interest)
                    if previous in bars
                    else None,
                )
            self.active[product] = selected
        if dt < self.trade_start or dt >= self.final_date:
            return
        if force_flat:
            self.event(
                "portfolio_risk" if dt < self.flatten_date else "window_exit",
                equity=equity,
                daily_return=daily_return,
                drawdown=drawdown,
                halted=self.halted,
            )
            for vt, position in sorted(self.book.positions.items()):
                if position:
                    self.submit(
                        vt,
                        -position,
                        False,
                        bars[vt],
                        "portfolio_exit",
                        self.book.risk_distances[vt],
                    )
            return
        for product in self.cfg["entry_priority"]:
            chosen = self.active[product]
            held = [
                vt
                for vt, position in self.book.positions.items()
                if position and product_of(vt) == product
            ]
            if len(held) > 1:
                raise RuntimeError("multiple maturities held for one product")
            old = held[0] if held else None
            direction = 0
            if old:
                position = self.book.positions[old]
                direction = 1 if position > 0 else -1
                view = views[old]
                close = bars[old].close_price
                distance = view["atr"] * self.setting["atr_multiple"]
                if direction > 0:
                    self.stops[old] = max(self.stops[old], close - distance)
                    exit_signal = close < self.stops[old] or (
                        view["ready"] and close < view["exit_low"]
                    )
                else:
                    self.stops[old] = min(self.stops[old], close + distance)
                    exit_signal = close > self.stops[old] or (
                        view["ready"] and close > view["exit_high"]
                    )
                if old != chosen or exit_signal:
                    reason = "signal_exit" if exit_signal else "roll_close"
                    self.submit(
                        old, -position, False, bars[old], reason, self.book.risk_distances[old]
                    )
                else:
                    continue
                if exit_signal:
                    continue
            if chosen is None or not views[chosen]["ready"]:
                continue
            view, bar = views[chosen], bars[chosen]
            if not old:
                direction = (
                    1
                    if bar.close_price > view["upper"]
                    else -1
                    if bar.close_price < view["lower"]
                    else 0
                )
            if direction:
                distance = view["atr"] * self.setting["atr_multiple"]
                quantity = self.book.opening_size(chosen, bar.close_price, distance, equity)
                self.submit(
                    chosen,
                    direction * quantity,
                    True,
                    bar,
                    "roll_open" if old else "breakout",
                    distance,
                )


@dataclass
class PortfolioRun:
    daily: pd.DataFrame
    statistics: dict
    events: list[dict]
    snapshots: pd.DataFrame
    contract_daily: pd.DataFrame


def run_portfolio(
    bars, cfg, setting, start, end, strategy_class=MultiProductCtaStrategy
) -> PortfolioRun:
    cfg = validate_protocol(cfg)
    start = datetime.fromisoformat(start) if isinstance(start, str) else start
    end = datetime.fromisoformat(end) if isinstance(end, str) else end
    if (
        set(setting) != {"entry_window", "atr_multiple"}
        or not math.isfinite(setting["atr_multiple"])
        or setting["atr_multiple"] <= 0
    ):
        raise ValueError("invalid fixed strategy setting")
    dates = sorted({b.datetime for b in bars if b.datetime <= end})
    first = next((i for i, dt in enumerate(dates) if dt >= start), len(dates))
    if len(dates) - first < 3:
        raise ValueError("evaluation needs at least three dates")
    warmup = max(setting["entry_window"], cfg["exit_window"], cfg["atr_window"]) + 1
    dates = dates[max(0, first - warmup) :]
    allowed_dates = set(dates)
    history = {}
    for bar in bars:
        if bar.datetime not in allowed_dates:
            continue
        if product_of(bar.vt_symbol) not in cfg["products"]:
            raise ValueError("unknown product")
        key = bar.datetime, bar.vt_symbol
        if key in history:
            raise ValueError("duplicate contract/date")
        values = (
            bar.open_price,
            bar.high_price,
            bar.low_price,
            bar.close_price,
            bar.volume,
            bar.open_interest,
        )
        if (
            not all(math.isfinite(x) for x in values)
            or min(values[:4]) <= 0
            or min(values[4:]) < 0
            or not bar.low_price
            <= min(bar.open_price, bar.close_price)
            <= max(bar.open_price, bar.close_price)
            <= bar.high_price
        ):
            raise ValueError("invalid portfolio OHLCV/OI")
        history[key] = bar
    symbols = sorted({key[1] for key in history})
    engine = FundedPortfolioEngine()
    messages = []
    engine.output = messages.append
    engine.set_parameters(
        vt_symbols=symbols,
        interval=Interval.DAILY,
        start=dates[0],
        end=dates[-1],
        rates={vt: cfg["commission_rate"] for vt in symbols},
        slippages={
            vt: cfg["products"][product_of(vt)]["tick"] * cfg["slippage_ticks"] for vt in symbols
        },
        sizes={vt: cfg["products"][product_of(vt)]["multiplier"] for vt in symbols},
        priceticks={vt: cfg["products"][product_of(vt)]["tick"] for vt in symbols},
        capital=cfg["capital"],
        annual_days=cfg["annual_days"],
    )
    engine.add_strategy(
        strategy_class,
        {
            "cfg": cfg,
            "setting": setting,
            "trade_start": start,
            "flatten_date": dates[-2],
            "final_date": dates[-1],
        },
    )
    engine.history_data, engine.dts = history, set(dates)
    # Upstream's zero-day initialization compares day-of-month with its seed
    # timestamp. Seed the preceding date so the first supplied bar is not
    # accidentally consumed while trading=False (e.g. a Jan-1 first bar).
    engine.datetime = dates[0] - timedelta(days=1)
    engine.run_backtesting()
    if engine.processed_dates != len(dates):
        raise RuntimeError("portfolio replay interrupted: " + "\n".join(messages[-4:]))
    engine.strategy.cancel_all()
    if any(engine.strategy.book.positions.values()) or engine.active_limit_orders:
        raise RuntimeError("portfolio window ended with open positions or orders")
    snapshots = pd.DataFrame(engine.strategy.snapshots).set_index("date")
    if snapshots.index.duplicated().any():
        raise RuntimeError("duplicate portfolio date")
    daily = engine.calculate_result()
    if daily is None:
        daily = pd.DataFrame(
            0.0, index=snapshots.index, columns=["net_pnl", "commission", "slippage", "trade_count"]
        )
    ledger_equity = cfg["capital"] + daily.net_pnl.cumsum()
    if not all(abs(a - b) < 1e-6 for a, b in zip(ledger_equity, snapshots.equity, strict=True)):
        raise RuntimeError("shared ledger does not reconcile to vn.py daily accounting")
    evaluation = daily.loc[daily.index >= start.date()].copy()
    contributions = pd.DataFrame(
        [
            {
                "date": day,
                "symbol": vt,
                "product": product_of(vt),
                "net_pnl": result.net_pnl,
                "commission": result.commission,
                "slippage": result.slippage,
                "fills": result.trade_count,
            }
            for day, portfolio_day in engine.daily_results.items()
            if day >= start.date()
            for vt, result in portfolio_day.contract_results.items()
            if result.trade_count or result.start_pos or result.end_pos
        ]
    )
    stats = summarize(evaluation, cfg["capital"], cfg["annual_days"])
    stats.update(
        {
            "roll_cost": engine.strategy.book.roll_cost,
            "roll_selections": sum(
                e["event"] == "selection"
                and e["previous"] is not None
                and e["selected"] is not None
                and e["datetime"] >= start.isoformat()
                for e in engine.strategy.audit
            ),
            "rejections": sum(e["event"] == "reject" for e in engine.strategy.audit),
        }
    )
    return PortfolioRun(
        evaluation,
        stats,
        engine.strategy.audit,
        snapshots.loc[snapshots.index >= start.date()],
        contributions,
    )
