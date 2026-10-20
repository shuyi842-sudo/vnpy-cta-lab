"""Deterministic artificial contract panel for offline integration tests only."""

import math
import random
from datetime import datetime, timedelta

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


def synthetic_panel(cfg, count=450, seed=7719):
    rng = random.Random(seed)
    prices = {"rb": 3500.0, "cu": 70000.0, "al": 20000.0}
    date = datetime(2023, 1, 2)
    bars = []
    for i in range(count):
        while date.weekday() >= 5:
            date += timedelta(days=1)
        for product in cfg["entry_priority"]:
            prior = prices[product]
            opening = prior * math.exp(rng.gauss(0, 0.001))
            drift = (0.0012, -0.0015, 0.0001)[(i // 90) % 3]
            close = opening * math.exp(drift + rng.gauss(0, 0.004))
            prices[product] = close
            tick = cfg["products"][product]["tick"]
            for month_offset in range(1, 13):
                month_index = date.year * 12 + date.month - 1 + month_offset
                year, month = divmod(month_index, 12)
                symbol = f"{product}{year % 100:02}{month + 1:02}"
                basis = 1 + 0.002 * month_offset
                o, c = [
                    max(tick * 2, round(value * basis / tick) * tick) for value in (opening, close)
                ]
                high, low = max(o, c) + tick * 2, min(o, c) - tick * 2
                oi = max(100, 10000 - abs(month_offset - 3) * 1200)
                bars.append(
                    BarData(
                        symbol=symbol,
                        exchange=Exchange.SHFE,
                        datetime=date,
                        interval=Interval.DAILY,
                        open_price=o,
                        high_price=high,
                        low_price=low,
                        close_price=c,
                        volume=2000,
                        open_interest=oi,
                        gateway_name="SYNTHETIC",
                    )
                )
        date += timedelta(days=1)
    return bars
