"""Expanding training windows with non-overlapping, flat-to-flat test folds."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from .metrics import summarize
from .runner import Contract, run_backtest


def walk_forward(
    bars, contract: Contract, train: int = 300, test: int = 100, base_settings: dict | None = None
) -> tuple[pd.DataFrame, dict]:
    if train < 100 or test < 10 or len(bars) < train + test:
        raise ValueError("need train >= 100, test >= 10, and at least one full test fold")
    candidates = [
        {**(base_settings or {}), "entry_window": window, "atr_multiple": multiple}
        for window in (20, 40, 60)
        for multiple in (2.0, 3.0)
    ]
    max_lookback = max(
        max(setting["entry_window"], setting.get("exit_window", 15), setting.get("atr_window", 20))
        for setting in candidates
    )
    if train <= max_lookback + 2:
        raise ValueError(
            "training window must exceed the largest indicator lookback plus two exit bars"
        )
    folds, frames = [], []
    for end in range(train, len(bars) - test + 1, test):
        scores = []
        for setting in candidates:
            result = run_backtest(bars[:end], contract, setting)
            # Fixed grid, deterministic tie-break, no use of test bars in ranking.
            score = result.statistics["sharpe"]
            scores.append(
                score if score is not None and result.statistics["fills"] >= 4 else float("-inf")
            )
        if max(scores) == float("-inf"):
            chosen = 0  # Predeclared fallback; report insufficient training trades.
        else:
            chosen = max(range(len(candidates)), key=scores.__getitem__)
        # Warm every configured indicator, including non-default ATR/exit windows.
        # Retain the original 61-bar default warmup; these bars cannot submit orders.
        warmup = max(
            61,
            candidates[chosen]["entry_window"] + 1,
            candidates[chosen].get("exit_window", 15) + 1,
            candidates[chosen].get("atr_window", 20) + 1,
        )
        result = run_backtest(
            bars[max(0, end - warmup) : end + test],
            contract,
            candidates[chosen],
            bars[end].datetime,
        )
        frames.append(result.daily)
        folds.append(
            {
                "train_start": bars[0].datetime.isoformat(),
                "train_end": bars[end - 1].datetime.isoformat(),
                "test_start": bars[end].datetime.isoformat(),
                "test_end": bars[end + test - 1].datetime.isoformat(),
                "chosen": candidates[chosen],
                "training_scores": [s if s != float("-inf") else None for s in scores],
                "candidates": candidates,
                "statistics": result.statistics,
                "trades": result.trades.to_dict("records"),
                "events": result.audit,
            }
        )
    daily = pd.concat(frames)
    if daily.index.duplicated().any():
        raise RuntimeError("overlapping test days")
    return daily, {
        "method": "expanding training; fixed initial capital for sizing in every fold; flat fold boundaries",
        "folds": folds,
        "unused_tail_bars": (len(bars) - train) % test,
        "statistics": summarize(daily, contract.capital, contract.annual_days),
    }


def cost_sensitivity(bars, contract: Contract, settings: dict) -> list[dict]:
    return [
        {
            "cost_multiple": multiple,
            **run_backtest(
                bars,
                replace(
                    contract,
                    commission_rate=contract.commission_rate * multiple,
                    slippage_ticks=contract.slippage_ticks * multiple,
                ),
                settings,
            ).statistics,
        }
        for multiple in (0, 1, 2, 3)
    ]
