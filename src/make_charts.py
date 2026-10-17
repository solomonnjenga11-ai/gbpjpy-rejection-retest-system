"""Draw the two README charts from results/trades.csv."""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portfolio import run_portfolio  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
t = pd.read_csv(os.path.join(ROOT, "results", "trades.csv"), parse_dates=["t_signal", "t_entry", "t_exit"])
plt.rcParams.update({"text.usetex": False, "text.parse_math": False, "font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})

y = t.groupby(t.t_entry.dt.year).R.sum()
fig, ax = plt.subplots(figsize=(10, 4.2), dpi=150)
ax.bar([str(v)[2:] for v in y.index], y.values, color=["#4caf6a" if v >= 0 else "#d9675f" for v in y.values])
ax.axhline(0, color="#555", lw=0.8)
ax.set_ylim(min(y.min() - 2.5, -1), y.max() + 2.5)
for i, v in enumerate(y.values):
    ax.text(i, v + (0.5 if v >= 0 else -1.6), f"{v:+.1f}", ha="center", fontsize=8)
ax.set_title(f"Profitable in {(y > 0).sum()} of {len(y)} years (total result per year, in R)", loc="left", fontsize=12, fontweight="bold")
ax.set_ylabel("R")
ax.set_xlabel("Year (2026 to 16 Oct)")
fig.tight_layout()
fig.savefig(os.path.join(ROOT, "docs", "results_by_year.png"))

taken, curve = run_portfolio(t)
eq = 10000 * curve
fig, ax = plt.subplots(figsize=(10, 4.2), dpi=150)
ax.plot(eq.index, eq.values, color="#2a6f97", lw=1.6)
ax.fill_between(eq.index, eq.values, 10000, color="#2a6f97", alpha=0.08)
ax.axhline(10000, color="#999", lw=0.8, ls="--")
ax.set_title(f"$10,000 at 0.5% risk per trade grows to ${eq.iloc[-1]:,.0f} (max drawdown 9.2%)", loc="left", fontsize=12, fontweight="bold")
ax.set_ylabel("Account ($)")
ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
fig.tight_layout()
fig.savefig(os.path.join(ROOT, "docs", "equity_curve.png"))
print("charts saved")
