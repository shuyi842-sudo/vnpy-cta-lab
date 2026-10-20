"""Pre-trade contract limits. These are research assumptions, not broker checks."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    capital: float = 1_000_000
    risk_fraction: float = 0.005
    max_contracts: int = 5
    max_notional_fraction: float = 0.5

    def __post_init__(self) -> None:
        if not math.isfinite(self.capital) or self.capital <= 0:
            raise ValueError("capital must be positive and finite")
        if not 0 < self.risk_fraction <= 0.1:
            raise ValueError("risk_fraction must be in (0, 0.1]")
        if not isinstance(self.max_contracts, int) or self.max_contracts < 1:
            raise ValueError("max_contracts must be a positive integer")
        if not 0 < self.max_notional_fraction <= 1:
            raise ValueError("max_notional_fraction must be in (0, 1]")

    def size(self, price: float, stop_distance: float, multiplier: float) -> int:
        if not all(math.isfinite(x) and x > 0 for x in (price, stop_distance, multiplier)):
            return 0
        risk_size = int(self.capital * self.risk_fraction / (stop_distance * multiplier))
        exposure_size = int(self.capital * self.max_notional_fraction / (price * multiplier))
        return max(0, min(risk_size, exposure_size, self.max_contracts))
