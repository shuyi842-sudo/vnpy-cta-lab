"""Plot published frozen-study aggregates from a local historical reproduction."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

root = Path(__file__).resolve().parents[1]
summary = json.loads((root / "docs/results/portfolio_holdout.summary.json").read_text())
account_path = root / "artifacts/portfolio/reproduction/account.csv"
if not account_path.exists():
    account_path = root / "artifacts/portfolio/holdout/account.csv"
account = pd.read_csv(account_path, parse_dates=["date"])
if abs(account.equity.iloc[-1] - summary["statistics"]["end_equity"]) > 1e-6:
    raise ValueError("Local account does not match the published final equity")
plt.rcParams.update(
    {"font.family": "DejaVu Sans", "font.size": 10, "svg.hashsalt": "cta-portfolio"}
)
fig, axes = plt.subplots(1, 3, figsize=(14, 4.6), layout="constrained")
fig.suptitle("CTA Lab / Frozen 2026 holdout", x=0.02, ha="left", fontsize=20, weight="bold")
returns = (account.equity / summary["protocol"]["capital"] - 1) * 100
axes[0].plot(account.date, returns, color="#17648e", lw=2)
axes[0].set_title("Single-pool net return (%)", loc="left")
axes[1].fill_between(account.date, account.drawdown * 100, color="#bc681b", alpha=0.5)
axes[1].set_title("Close-to-close drawdown (%)", loc="left")
contrib = summary["product_contributions"]
values = [p["net_pnl"] for p in contrib]
axes[2].bar(
    [p["product"] for p in contrib],
    values,
    color=["#bd493f" if v < 0 else "#17648e" for v in values],
)
axes[2].set_title("Net contribution (CNY)", loc="left")
for i, item in enumerate(contrib):
    axes[2].annotate(
        f"{item['net_pnl']:,.0f}\n{int(item['fills'])} fills",
        (i, item["net_pnl"]),
        ha="center",
        va="bottom" if item["net_pnl"] >= 0 else "top",
        xytext=(0, 4 if item["net_pnl"] >= 0 else -4),
        textcoords="offset points",
    )
axes[2].set_ylim(-7000, 6000)
for axis in axes:
    axis.axhline(0, color="#8f9aa8", ls="--", lw=0.8)
    axis.grid(axis="y", alpha=0.2)
    axis.spines[["top", "right"]].set_visible(False)
for axis in axes[:2]:
    axis.tick_params(axis="x", labelrotation=30)
fig.supxlabel(
    "181 trading days | 22 fills | Gross PnL: CNY 55 | Fees + slippage: CNY 1,267.79\nFixed rb/cu/al universe; no final-period cu fills. These results do not establish a persistent edge.",
    fontsize=10,
    color="#526070",
)
out = root / "docs/results"
fig.savefig(out / "portfolio_holdout.png", dpi=170, facecolor="white")
fig.savefig(out / "portfolio_holdout.svg", facecolor="white", metadata={"Date": None})
print(out / "portfolio_holdout.png")
