"""Download OANDA 15-minute candles (bid/ask midpoint) for backtesting. Standard library only.

Run on your Mac in Terminal:
    export OANDA_TOKEN="paste-your-demo-token-here"
    python3 oanda_download.py GBP_JPY
    python3 oanda_download.py GBP_JPY EUR_USD GBP_USD      # several at once

Output: GBP_JPY_M15.csv in the current folder (UTC times). Attach the CSV in the chat.
If it stops partway, just run the same command again: it resumes from the last saved candle.
Your token stays on your Mac: it is read from the OANDA_TOKEN variable and never written to the file.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api-fxpractice.oanda.com/v3"   # demo (practice) server
START = datetime(2005, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(days=50)                     # ~4,800 15m candles per request (limit is 5,000)


def get(path, params, token):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(10):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("Token rejected (401). Check OANDA_TOKEN is a DEMO token copied in full.")
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 5 * (attempt + 1)))   # temporary server problem: wait and retry
                continue
            sys.exit(f"HTTP {e.code}: {e.read().decode()[:300]}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(min(60, 5 * (attempt + 1)))
    sys.exit("OANDA kept failing. Wait a few minutes and run the same command again; it will resume.")


def download(inst, token):
    out = f"{inst}_M15.csv"
    now = datetime.now(timezone.utc)
    t, rows, mode = START, 0, "w"
    if os.path.exists(out) and os.path.getsize(out) > 100:
        with open(out, "rb") as f:   # resume after the last saved candle
            f.seek(-200, os.SEEK_END)
            last = f.read().decode().strip().splitlines()[-1].split(",")[0]
        t = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(minutes=15)
        mode = "a"
        print(f"Resuming {inst} from {t:%Y-%m-%d %H:%M}")
    with open(out, mode, newline="") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(["time", "open", "high", "low", "close", "volume"])
        while t < now:
            end = min(t + STEP, now)
            data = get(f"/instruments/{inst}/candles",
                       {"granularity": "M15", "price": "M", "from": t.isoformat(), "to": end.isoformat()}, token)
            for c in data.get("candles", []):
                if c.get("complete"):
                    m = c["mid"]
                    w.writerow([c["time"][:19].replace("T", " "), m["o"], m["h"], m["l"], m["c"], c["volume"]])
                    rows += 1
            f.flush()
            print(f"\r{inst}: up to {end:%Y-%m-%d}, {rows:,} new candles", end="", flush=True)
            t = end
            time.sleep(0.15)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    token = os.environ.get("OANDA_TOKEN", "").strip()
    if not token:
        sys.exit('Set your token first:  export OANDA_TOKEN="..."')
    for inst in (sys.argv[1:] or ["GBP_JPY"]):
        download(inst, token)
