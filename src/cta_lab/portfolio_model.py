"""Causal contract selection, indicators and a single portfolio equity ledger."""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta

from vnpy.trader.constant import Direction


def product_of(vt_symbol: str) -> str:
    match = re.fullmatch(r"([a-z]+)(\d{4})\.SHFE", vt_symbol)
    if not match:
        raise ValueError(f"expected an actual SHFE maturity: {vt_symbol}")
    return match.group(1)


def delivery_month(vt_symbol: str) -> datetime:
    product_of(vt_symbol)
    code = vt_symbol.split(".")[0][-4:]
    return datetime(2000 + int(code[:2]), int(code[2:]), 1)


def validate_protocol(value: dict) -> dict:
    cfg = deepcopy(value)
    if not cfg["products"] or set(cfg["products"]) != set(cfg["entry_priority"]):
        raise ValueError("entry priority must enumerate each product exactly once")
    if len(cfg["entry_priority"]) != len(set(cfg["entry_priority"])):
        raise ValueError("duplicate entry priority")
    for name in (
        "capital",
        "annual_days",
        "max_contracts",
        "min_previous_volume",
        "delivery_buffer_days",
    ):
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise ValueError(f"invalid {name}")
    for name in (
        "risk_fraction",
        "portfolio_risk_cap",
        "product_notional_cap",
        "gross_notional_cap",
        "margin_cap",
        "daily_loss_limit",
        "drawdown_limit",
    ):
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise ValueError(f"invalid {name}")
    for name in ("commission_rate", "slippage_ticks"):
        if not math.isfinite(cfg[name]) or cfg[name] < 0:
            raise ValueError(f"invalid {name}")
    if cfg["roll_oi_ratio"] < 1:
        raise ValueError("roll hysteresis must be >= 1")
    for spec in cfg["products"].values():
        if any(
            not math.isfinite(spec[k]) or spec[k] <= 0
            for k in ("multiplier", "tick", "margin_rate")
        ):
            raise ValueError("invalid product economics")
    return cfg


class CausalRollSelector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.previous: dict[str, str] = {}

    def choose(self, product: str, bars: dict, decision_time: datetime) -> str | None:
        incumbent = self.previous.get(product)
        cutoff = decision_time + timedelta(days=self.cfg["delivery_buffer_days"])
        candidates = [
            key
            for key, bar in bars.items()
            if product_of(key) == product
            and bar.datetime == decision_time
            and bar.volume >= self.cfg["min_previous_volume"]
            and bar.open_interest > 0
            and delivery_month(key) > cutoff
            and (incumbent is None or delivery_month(key) >= delivery_month(incumbent))
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda key: (-bars[key].open_interest, -bars[key].volume, delivery_month(key), key)
        )
        best = candidates[0]
        if incumbent in candidates:
            if (
                delivery_month(best) <= delivery_month(incumbent)
                or bars[best].open_interest
                < bars[incumbent].open_interest * self.cfg["roll_oi_ratio"]
            ):
                best = incumbent
        self.previous[product] = best
        return best


class ContractWindow:
    def __init__(self, entry: int, exit_window: int, atr: int):
        if any(not isinstance(x, int) or x < 2 for x in (entry, exit_window, atr)):
            raise ValueError("indicator windows must be integers >= 2")
        self.entry, self.exit_window, self.atr_window = entry, exit_window, atr
        self.bars = deque(maxlen=max(entry, exit_window))
        self.ranges = deque(maxlen=atr)

    def observe(self, bar) -> dict:
        previous = self.bars[-1].close_price if self.bars else bar.open_price
        self.ranges.append(
            max(
                bar.high_price - bar.low_price,
                abs(bar.high_price - previous),
                abs(bar.low_price - previous),
            )
        )
        prior = list(self.bars)
        ready = (
            len(prior) >= max(self.entry, self.exit_window) and len(self.ranges) == self.atr_window
        )
        view = {"ready": ready, "atr": sum(self.ranges) / len(self.ranges)}
        if ready:
            view.update(
                {
                    "upper": max(b.high_price for b in prior[-self.entry :]),
                    "lower": min(b.low_price for b in prior[-self.entry :]),
                    "exit_low": min(b.low_price for b in prior[-self.exit_window :]),
                    "exit_high": max(b.high_price for b in prior[-self.exit_window :]),
                }
            )
        self.bars.append(bar)
        return view


class SharedEquityBook:
    """Trade cash-flow identity, not a broker cash/settlement account."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cash_ledger = float(cfg["capital"])
        self.positions = defaultdict(int)
        self.risk_distances: dict[str, float] = {}
        self.commission = 0.0
        self.slippage = 0.0
        self.roll_cost = 0.0

    def costs(self, vt: str, price: float, volume: int) -> tuple[float, float]:
        spec = self.cfg["products"][product_of(vt)]
        return (
            price * volume * spec["multiplier"] * self.cfg["commission_rate"],
            volume * spec["multiplier"] * spec["tick"] * self.cfg["slippage_ticks"],
        )

    def exposure(self, prices: dict, positions: dict | None = None) -> dict:
        positions = self.positions if positions is None else positions
        gross = margin = risk = 0.0
        products = defaultdict(float)
        for vt, quantity in positions.items():
            if not quantity:
                continue
            spec = self.cfg["products"][product_of(vt)]
            notional = abs(quantity) * prices[vt] * spec["multiplier"]
            gross += notional
            margin += notional * spec["margin_rate"]
            products[product_of(vt)] += notional
            risk += abs(quantity) * self.risk_distances.get(vt, 0) * spec["multiplier"]
        return {
            "gross": gross,
            "margin": margin,
            "initial_stop_risk": risk,
            "products": dict(products),
        }

    def equity(self, prices: dict) -> float:
        return self.cash_ledger + sum(
            quantity * prices[vt] * self.cfg["products"][product_of(vt)]["multiplier"]
            for vt, quantity in self.positions.items()
            if quantity
        )

    def opening_size(self, vt: str, price: float, distance: float, equity: float) -> int:
        if not all(math.isfinite(x) and x > 0 for x in (price, distance, equity)):
            return 0
        size = self.cfg["products"][product_of(vt)]["multiplier"]
        return max(
            0,
            min(
                int(equity * self.cfg["risk_fraction"] / (distance * size)),
                int(equity * self.cfg["product_notional_cap"] / (price * size)),
                self.cfg["max_contracts"],
            ),
        )

    def can_open(
        self, vt: str, signed_quantity: int, price: float, distance: float, marks: dict
    ) -> tuple[bool, str, dict]:
        product = product_of(vt)
        if any(q and product_of(key) == product for key, q in self.positions.items()):
            return False, "existing_product_leg_not_flat", {}
        if not distance > 0 or abs(signed_quantity) > self.cfg["max_contracts"]:
            return False, "invalid_risk_or_quantity", {}
        fees, slip = self.costs(vt, price, abs(signed_quantity))
        equity = self.equity(marks) - fees - slip
        proposed = dict(self.positions)
        proposed[vt] = signed_quantity
        prices = {**marks, vt: price}
        exposure = self.exposure(prices, proposed)
        exposure["initial_stop_risk"] += (
            abs(signed_quantity) * distance * self.cfg["products"][product]["multiplier"]
        )
        evidence = {
            "equity_after_costs": equity,
            **exposure,
            "available_after_margin": equity - exposure["margin"],
        }
        checks = [
            (equity > 0, "insolvent"),
            (exposure["gross"] <= equity * self.cfg["gross_notional_cap"], "shared_gross_cap"),
            (exposure["margin"] <= equity * self.cfg["margin_cap"], "shared_margin_cap"),
            (
                exposure["products"][product] <= equity * self.cfg["product_notional_cap"],
                "product_notional_cap",
            ),
            (
                exposure["initial_stop_risk"] <= equity * self.cfg["portfolio_risk_cap"],
                "shared_initial_risk_cap",
            ),
            (
                abs(signed_quantity) * distance * self.cfg["products"][product]["multiplier"]
                <= equity * self.cfg["risk_fraction"],
                "product_initial_risk_cap",
            ),
        ]
        for allowed, reason in checks:
            if not allowed:
                return False, reason, evidence
        return True, "admitted", evidence

    def fill(self, trade, distance: float, reason: str) -> dict:
        quantity = int(trade.volume) * (1 if trade.direction == Direction.LONG else -1)
        vt = trade.vt_symbol
        fees, slip = self.costs(vt, trade.price, abs(quantity))
        self.cash_ledger -= (
            quantity * trade.price * self.cfg["products"][product_of(vt)]["multiplier"]
            + fees
            + slip
        )
        self.positions[vt] += quantity
        if self.positions[vt]:
            self.risk_distances[vt] = distance
        else:
            self.risk_distances.pop(vt, None)
        self.commission += fees
        self.slippage += slip
        if reason.startswith("roll_"):
            self.roll_cost += fees + slip
        return {"commission": fees, "slippage": slip, "signed_quantity": quantity}
