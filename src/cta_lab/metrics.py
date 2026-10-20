"""Performance from daily marked-to-market PnL, including initial equity in drawdown."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def summarize(daily: pd.DataFrame, capital: float, annual_days: int = 240) -> dict:
    if daily.empty:
        raise ValueError("no evaluation days")
    pnl = daily["net_pnl"].astype(float)
    equity = capital + pnl.cumsum()
    if not np.isfinite(equity).all():
        raise ValueError("non-finite equity")
    previous = equity.shift(1, fill_value=capital)
    returns = equity / previous - 1
    peak = equity.cummax().clip(lower=capital)
    drawdown = equity / peak - 1
    solvent = bool((equity > 0).all())
    volatility = float(returns.std(ddof=1))
    sharpe = (
        float(returns.mean() / volatility * math.sqrt(annual_days))
        if solvent and volatility > 0
        else None
    )
    total_return = float(equity.iloc[-1] / capital - 1)
    cagr = float((equity.iloc[-1] / capital) ** (annual_days / len(pnl)) - 1) if solvent else None
    return {
        "days": len(pnl),
        "net_pnl": float(pnl.sum()),
        "end_equity": float(equity.iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "solvent": solvent,
        "commission": float(daily["commission"].sum()),
        "slippage": float(daily["slippage"].sum()),
        "fills": int(daily["trade_count"].sum()),
        "annual_days": annual_days,
    }
