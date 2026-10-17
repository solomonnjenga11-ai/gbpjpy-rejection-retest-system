"""Run the final GBPJPY rules (both setups, levels from daily closes) and write results/.

Usage:  python src/run_backtest.py
"""
import json
import os
import sys
from dataclasses import replace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portfolio import run_portfolio, stats  # noqa: E402
from solomon_setup import run_inst  # noqa: E402
from strategy import Params  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results")
os.makedirs(OUT, exist_ok=True)

P = replace(Params(), time_stop_days=20)          # breakeven at +1.2R, 20-day time stop
trades = run_inst("GBP_JPY", P, body_levels=True)  # levels drawn from daily candle closes
trades.to_csv(os.path.join(OUT, "trades.csv"), index=False)

taken, curve = run_portfolio(trades)               # fixed 0.5% risk, loss limits
summary = {"all": stats(taken, curve)}
cuts = [("2005_2014", None, "2015-01-01"), ("2015_Mar2022", "2015-01-01", "2022-03-05"), ("Mar2022_Oct2026", "2022-03-05", None)]
for name, a, b in cuts:
    sub = trades
    if a:
        sub = sub[sub.t_entry >= a]
    if b:
        sub = sub[sub.t_entry < b]
    summary[name] = {"trades": len(sub), "avg_R": round(sub.R.mean(), 3), "hit_target_pct": round((sub.reason == "target").mean() * 100, 1)}
summary["by_setup"] = {k: {"trades": len(g), "avg_R": round(g.R.mean(), 3)} for k, g in trades.groupby("setup")}
json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)
print(json.dumps(summary, indent=1, default=str))
