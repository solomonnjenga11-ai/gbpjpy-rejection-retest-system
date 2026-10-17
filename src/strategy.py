"""Shared tools: market specs (spreads, pip sizes), bar loading and resampling, swing-point detection and the
trade simulator used by solomon_setup.py. The bounce strategy further down was the first, rejected version
(run 1 in the report) and is kept for reference.

Original notes:
Solomon's support/resistance trend setup, codified.

Rules (fixed before testing):
  Trend (daily, previous completed day): long only if close > SMA50 > SMA200, short only if close < SMA50 < SMA200.
  Choppy filter: daily ADX(14) > 20.
  Levels: confirmed swing highs/lows on daily, 4h and 1h bars (5 bars each side). A zone's strength is the
          sum of weights (daily 3, 4h 2, 1h 1) of active swing points within +/-0.15 daily ATR of each other.
          Strong zone = strength >= 3.
  Entry (15m): in an uptrend, a 15m bar dips into a strong support zone, closes back above the level and
          closes bullish -> buy at the next 15m open. Mirror for shorts at resistance.
  Stop (hard): below the lower of the previous day's low and the support level, minus a buffer of
          0.2 x daily ATR (about 20-30 pips on GBPJPY). Mirror for shorts.
  Target (hard): just before the nearest strong opposing zone (0.05 ATR in front of it).
  Filter: reward:risk >= 3, otherwise skip.
  Management: stop moves once to breakeven (+costs) when price reaches +1.2R. Never trailed.
              Time stop after N trading days. Optional Friday close.
All indicators use only information available at decision time (no lookahead).
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import os
BARS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bars")

# Typical OANDA spreads (price units) and pip sizes. Costs are deliberately on the conservative side.
SPEC = {
    "EUR_USD": dict(spread=0.00012, pip=0.0001, theme="USD"),
    "GBP_USD": dict(spread=0.00018, pip=0.0001, theme="USD"),
    "AUD_USD": dict(spread=0.00014, pip=0.0001, theme="USD"),
    "USD_CAD": dict(spread=0.00020, pip=0.0001, theme="USD"),
    "USD_JPY": dict(spread=0.012, pip=0.01, theme="JPY"),
    "EUR_JPY": dict(spread=0.018, pip=0.01, theme="JPY"),
    "AUD_JPY": dict(spread=0.020, pip=0.01, theme="JPY"),
    "GBP_JPY": dict(spread=0.030, pip=0.01, theme="JPY"),
    "GBP_JPY_REAL": dict(spread=0.030, pip=0.01, theme="JPY"),
    "GBP_JPY_2026": dict(spread=0.030, pip=0.01, theme="JPY"),
    "GBP_JPY_OANDA": dict(spread=0.030, pip=0.01, theme="JPY"),
    "XAU_USD": dict(spread=0.40, pip=0.1, theme="GOLD"),
    "WTICO_USD": dict(spread=0.04, pip=0.01, theme="OIL"),
    "SPX500_USD": dict(spread=0.6, pip=1.0, theme="US_EQ"),
    "NAS100_USD": dict(spread=1.5, pip=1.0, theme="US_EQ"),
    "JP225_USD": dict(spread=10.0, pip=1.0, theme="INTL_EQ"),
    "UK100_GBP": dict(spread=2.0, pip=1.0, theme="INTL_EQ"),
}


@dataclass
class Params:
    min_rr: float = 3.0
    stop_buffer_atr: float = 0.2
    zone_atr: float = 0.15
    min_strength: int = 3
    adx_min: float = 20.0
    pivot_k: int = 5
    be_trigger_r: float = 1.2
    time_stop_days: int = 10
    friday_close: bool = False
    slip_frac: float = 0.25  # extra slippage on stop exits, as a fraction of spread
    lookback_days: dict = field(default_factory=lambda: {"D": 250, "4h": 60, "1h": 20})


# ---------- bars and indicators ----------

def load(inst: str) -> pd.DataFrame:
    return pd.read_parquet(f"{BARS}/{inst}.parquet")


def resample(b15: pd.DataFrame, rule: str) -> pd.DataFrame:
    # Shift +2h so daily bars roll at 22:00 UTC (around the New York 5pm close).
    shifted = b15.copy()
    shifted.index = shifted.index + pd.Timedelta(hours=2)
    out = shifted.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    out.index = out.index - pd.Timedelta(hours=2)
    return out


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def daily_context(d: pd.DataFrame) -> pd.DataFrame:
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    ctx = pd.DataFrame(index=d.index)
    ctx["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    ctx["sma50"] = d["close"].rolling(50).mean()
    ctx["sma200"] = d["close"].rolling(200).mean()
    ctx["adx"] = adx(d)
    ctx["close"] = d["close"]
    ctx["prev_low"] = d["low"]
    ctx["prev_high"] = d["high"]
    # Everything above describes day i; it becomes usable once day i has closed,
    # i.e. at the start of day i+1.
    ctx["avail"] = d.index + pd.Timedelta(days=1)
    return ctx.dropna()


def pivots(df: pd.DataFrame, k: int, bar_len: pd.Timedelta, weight: int):
    """Confirmed swing points: returns arrays (price, pivot_time, known_time, weight, is_high)."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    win = 2 * k + 1
    hmax = pd.Series(h).rolling(win, center=True).max().to_numpy()
    lmin = pd.Series(l).rolling(win, center=True).min().to_numpy()
    idx = np.arange(n)
    ph = idx[(h == hmax)]
    pl = idx[(l == lmin)]
    ph = ph[(ph >= k) & (ph < n - k)]
    pl = pl[(pl >= k) & (pl < n - k)]
    t = df.index.to_numpy()
    rows = []
    for arr, prices, is_high in ((ph, h, True), (pl, l, False)):
        known = t[arr + k] + np.timedelta64(int(bar_len.total_seconds()), "s")
        rows.append(pd.DataFrame({"price": prices[arr], "ptime": t[arr], "known": known,
                                  "w": weight, "high": is_high}))
    return pd.concat(rows, ignore_index=True)


# ---------- signal generation ----------

def strength_at(prices, weights, level, tol):
    m = np.abs(prices - level) <= tol
    return weights[m].sum()


def generate_candidates(inst: str, p: Params) -> pd.DataFrame:
    b15 = load(inst)
    d = resample(b15, "1D")
    h4 = resample(b15, "4h")
    h1 = resample(b15, "1h")
    ctx = daily_context(d)

    lv = pd.concat([
        pivots(d, p.pivot_k, pd.Timedelta(days=1), 3).assign(lb=p.lookback_days["D"]),
        pivots(h4, p.pivot_k, pd.Timedelta(hours=4), 2).assign(lb=p.lookback_days["4h"]),
        pivots(h1, p.pivot_k, pd.Timedelta(hours=1), 1).assign(lb=p.lookback_days["1h"]),
    ], ignore_index=True).sort_values("known").reset_index(drop=True)
    lv_price = lv["price"].to_numpy()
    lv_known = lv["known"].to_numpy()
    lv_expire = (lv["ptime"] + pd.to_timedelta(lv["lb"], unit="D")).to_numpy()
    lv_w = lv["w"].to_numpy()
    lv_high = lv["high"].to_numpy()

    # attach the latest completed daily context to each 15m bar
    bars = b15.reset_index().rename(columns={"time": "t"})
    bars = pd.merge_asof(bars.sort_values("t"), ctx.reset_index(drop=True).sort_values("avail"),
                         left_on="t", right_on="avail", direction="backward",
                         suffixes=("", "_d")).dropna(subset=["atr", "sma200"])
    up = (bars["close_d"] > bars["sma50"]) & (bars["sma50"] > bars["sma200"])
    dn = (bars["close_d"] < bars["sma50"]) & (bars["sma50"] < bars["sma200"])
    trending = bars["adx"] > p.adx_min
    bull = bars["close"] > bars["open"]
    bear = bars["close"] < bars["open"]
    cand_idx = np.where((trending & ((up & bull) | (dn & bear))).to_numpy())[0]

    t = bars["t"].to_numpy()
    o, hi, lo, cl = (bars[c].to_numpy() for c in ("open", "high", "low", "close"))
    atr = bars["atr"].to_numpy()
    plow, phigh = bars["prev_low"].to_numpy(), bars["prev_high"].to_numpy()
    is_up = up.to_numpy()

    spec = SPEC[inst]
    out = []
    last_signal_t = np.datetime64("1970-01-01")
    for i in cand_idx:
        if i + 1 >= len(bars):
            break
        ti = t[i]
        # don't fire repeatedly off the same touch: at most one signal per 4 hours per market
        if ti - last_signal_t < np.timedelta64(4, "h"):
            continue
        hi_n = np.searchsorted(lv_known, ti, side="right")
        act = slice(0, hi_n)
        alive = lv_expire[act] >= ti
        if not alive.any():
            continue
        pr, w, ish = lv_price[act][alive], lv_w[act][alive], lv_high[act][alive]
        a = atr[i]
        tol = p.zone_atr * a
        long = bool(is_up[i])
        entry_ref = o[i + 1]
        if long:
            # support candidates: levels whose zone the bar dipped into and closed back above
            cands = pr[(pr <= cl[i]) & (lo[i] <= pr + tol) & (lo[i] >= pr - 2 * tol)]
        else:
            cands = pr[(pr >= cl[i]) & (hi[i] >= pr - tol) & (hi[i] <= pr + 2 * tol)]
        if len(cands) == 0:
            continue
        level = cands.max() if long else cands.min()
        if strength_at(pr, w, level, tol) < p.min_strength:
            continue
        # opposing zones: strong levels on the other side of entry
        if long:
            opp = np.unique(pr[pr > entry_ref + 0.5 * a])
        else:
            opp = np.unique(pr[pr < entry_ref - 0.5 * a])[::-1]
        target_level = None
        for lvl in opp:  # nearest first
            if strength_at(pr, w, lvl, tol) >= p.min_strength:
                target_level = lvl
                break
        if target_level is None:
            continue
        half = spec["spread"] / 2
        if long:
            entry = entry_ref + half
            stop = min(plow[i], level) - p.stop_buffer_atr * a
            tp = target_level - 0.05 * a
            risk, reward = entry - stop, tp - entry
        else:
            entry = entry_ref - half
            stop = max(phigh[i], level) + p.stop_buffer_atr * a
            tp = target_level + 0.05 * a
            risk, reward = stop - entry, entry - tp
        if risk <= 0.25 * a or risk > 1.5 * a or reward <= 0:
            continue
        rr = reward / risk
        if rr < p.min_rr:
            continue
        out.append(dict(inst=inst, t_signal=ti, t_entry=t[i + 1], side=1 if long else -1,
                        entry=entry, stop=stop, tp=tp, rr=rr, risk_pips=risk / spec["pip"],
                        atr=a, level=level))
        last_signal_t = ti
    return pd.DataFrame(out)


# ---------- trade outcomes ----------

def simulate_trade(bars15: pd.DataFrame, c: dict, p: Params, spec: dict):
    """Walk forward through 15m bars. Conservative: if stop and target fall in the same bar, the stop wins.
    Returns (exit_time, R, reason)."""
    t = bars15.index
    start = t.searchsorted(c["t_entry"])
    end_time = c["t_entry"] + pd.Timedelta(days=p.time_stop_days * 7 / 5)
    stop_idx = t.searchsorted(end_time)
    seg = bars15.iloc[start:stop_idx + 1]
    side, entry, stop, tp = c["side"], c["entry"], c["stop"], c["tp"]
    risk = abs(entry - stop)
    half = spec["spread"] / 2
    slip = spec["spread"] * p.slip_frac
    be_done = False
    o, h, l, cl = (seg[x].to_numpy() for x in ("open", "high", "low", "close"))
    ts = seg.index
    for j in range(len(seg)):
        if p.friday_close and ts[j].weekday() == 4 and ts[j].hour >= 20 and ts[j].minute >= 45:
            px = cl[j] - side * half
            return ts[j], side * (px - entry) / risk, "friday"
        # stop first (conservative)
        if side == 1:
            if l[j] - half <= stop:
                fill = min(o[j] - half, stop) - slip if j > 0 else stop - slip
                return ts[j], (fill - entry) / risk, "be" if be_done else "stop"
            if h[j] - half >= tp:
                return ts[j], (tp - entry) / risk, "target"
            if not be_done and h[j] - half >= entry + p.be_trigger_r * risk:
                stop = entry + spec["spread"]  # breakeven plus costs
                be_done = True
        else:
            if h[j] + half >= stop:
                fill = max(o[j] + half, stop) + slip if j > 0 else stop + slip
                return ts[j], (entry - fill) / risk, "be" if be_done else "stop"
            if l[j] + half <= tp:
                return ts[j], (entry - tp) / risk, "target"
            if not be_done and l[j] + half <= entry - p.be_trigger_r * risk:
                stop = entry - spec["spread"]
                be_done = True
    px = cl[-1] - side * half
    return ts[-1], side * (px - entry) / risk, "time"


def outcomes(inst: str, cands: pd.DataFrame, p: Params) -> pd.DataFrame:
    if cands.empty:
        return cands
    b15 = load(inst)
    spec = SPEC[inst]
    res = [simulate_trade(b15, c, p, spec) for c in cands.to_dict("records")]
    cands = cands.copy()
    cands["t_exit"], cands["R"], cands["reason"] = zip(*res)
    return cands
