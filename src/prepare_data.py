"""Turn the OANDA download (GBP_JPY_M15.csv, from oanda_download.py) into the bar file the backtest reads.

Usage:  python src/prepare_data.py data/GBP_JPY_M15.csv
"""
import os
import sys

import pandas as pd

src = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "GBP_JPY_M15.csv")
out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bars")
os.makedirs(out_dir, exist_ok=True)
d = pd.read_csv(src, parse_dates=["time"]).drop_duplicates("time").set_index("time").sort_index()
d = d[["open", "high", "low", "close"]]
bad = (d.high < d.low) | (d <= 0).any(axis=1)
d = d[~bad]
d.to_parquet(os.path.join(out_dir, "GBP_JPY.parquet"))
print(f"{len(d):,} 15-minute candles, {d.index[0]} to {d.index[-1]}, {int(bad.sum())} bad rows removed")
