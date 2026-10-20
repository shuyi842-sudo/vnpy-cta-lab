"""Strict single-contract daily OHLCV ingestion and deterministic test fixtures."""

from __future__ import annotations

import csv
import hashlib
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

FIELDS = ("datetime", "open", "high", "low", "close", "volume")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bars(path: Path, vt_symbol: str) -> list[BarData]:
    """Dates are exchange trading dates, not wall-clock timestamps or night sessions."""
    symbol, exchange = vt_symbol.rsplit(".", 1)
    if not symbol:
        raise ValueError("symbol must not be empty")
    market = Exchange(exchange)
    bars = []
    previous = None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(FIELDS):
            raise ValueError(f"CSV header must be exactly {','.join(FIELDS)}")
        for line, row in enumerate(reader, 2):
            try:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d")
                values = [float(row[name]) for name in FIELDS[1:]]
                o, h, low, c, v = values
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("non-finite value")
                if min(o, h, low, c) <= 0 or v < 0:
                    raise ValueError("prices must be positive and volume nonnegative")
                if not low <= min(o, c) <= max(o, c) <= h:
                    raise ValueError("invalid OHLC bounds")
                if previous is not None and dt <= previous:
                    raise ValueError("duplicate or non-increasing trading date")
                previous = dt
                bars.append(
                    BarData(
                        symbol=symbol,
                        exchange=market,
                        datetime=dt,
                        interval=Interval.DAILY,
                        volume=v,
                        open_price=o,
                        high_price=h,
                        low_price=low,
                        close_price=c,
                        gateway_name="CSV",
                    )
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{path.name}:{line}: {exc}") from exc
    if len(bars) < 3:
        raise ValueError("at least three daily bars required")
    return bars


def write_fixture(path: Path, count: int = 900, seed: int = 20261020) -> None:
    """Artificial regimes for integration tests, never market performance evidence."""
    if count < 3:
        raise ValueError("count must be at least three")
    rng = random.Random(seed)
    dt, previous = datetime(2020, 1, 2), 3500.0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for i in range(count):
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
            drift = (0.0015, -0.001, 0.0)[(i // 100) % 3]
            o = previous * math.exp(rng.gauss(0, 0.002))
            c = o * math.exp(drift + rng.gauss(0, 0.009))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
            low = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
            writer.writerow(
                [
                    dt.strftime("%Y-%m-%d"),
                    *[round(x, 2) for x in (o, h, low, c)],
                    rng.randint(1000, 9000),
                ]
            )
            previous = c
            dt += timedelta(days=1)
