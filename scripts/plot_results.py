"""Optional static research chart: pip install -e '.[viz]' then run this script."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

root = Path(__file__).resolve().parents[1]
plt.rcParams["svg.hashsalt"] = "vnpy-cta-lab"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)
fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8), layout="constrained")
fig.suptitle("CTA Lab / Out-of-sample evidence", fontsize=19, fontweight="bold", x=0.02, ha="left")
for symbol, color in [("rb2505", "#17648e"), ("rb2510", "#bc681b")]:
    frame = pd.read_csv(root / "artifacts" / symbol / "oos_daily.csv")
    equity = np.r_[1_000_000, 1_000_000 + frame.net_pnl.cumsum().to_numpy()]
    returns = (equity / equity[0] - 1) * 100
    drawdown = (equity / np.maximum.accumulate(equity) - 1) * 100
    axes[0].plot(returns, color=color, lw=2.2, label=f"{symbol} ({returns[-1]:+.3f}%)")
    axes[1].plot(drawdown, color=color, lw=2.2, label=symbol)
for axis, title in zip(
    axes, ["Net return after costs (%)", "Drawdown from equity peak (%)"], strict=True
):
    axis.set_title(title, loc="left", pad=15)
    axis.axhline(0, color="#9da7b2", lw=0.9, linestyle="--")
    axis.set_xlabel("OOS evaluation day (different calendar periods)")
    axis.grid(axis="y", alpha=0.2)
    axis.legend(frameon=False, loc="lower left")
axes[0].legend(frameon=False, loc="upper left")
fig.supxlabel(
    "80 test days per actual contract | fixed sizing budget | flat fold boundaries | cash baseline = 0%\nSmall, overlapping single-product samples: these results do not establish a persistent edge.",
    fontsize=10,
    color="#526070",
)
output = root / "docs" / "results"
output.mkdir(parents=True, exist_ok=True)
fig.savefig(output / "oos_equity.png", dpi=170, facecolor="white")
fig.savefig(output / "oos_equity.svg", facecolor="white", metadata={"Date": None})
svg = output / "oos_equity.svg"
svg.write_text(
    "\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n",
    encoding="utf-8",
    newline="\n",
)
print(output / "oos_equity.png")
