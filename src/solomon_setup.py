"""Solomon's two setups from his annotated GBPJPY charts (Aug-Sep 2026). Rules fixed before testing.

Daily zone  : horizontal level with >= 3 daily swing touches in the past year; zone = level +/- 0.3 daily ATR
              (about 40 pips each side on GBPJPY, matching his 210.64-211.54 zone).
Signals are read only after the daily candle has CLOSED.

A) Rejection (bounce)
   Buy : the day came from above, traded below the level, and CLOSED back above it with a lower wick
         of at least 1/3 of the day's range (price rejected).
   15m : wait for a higher high (a 15m close above the latest confirmed 15m swing high, 2 bars each side).
         The broken swing high becomes support: buy limit there.
   Sell: mirror image at resistance (upper wick, lower low on 15m, sell limit at the broken swing low).

B) Break and retest
   Sell: a daily CLOSE below the bottom of the zone after the previous close was inside/above it.
   15m : sell limit back at the level (the middle of the zone) for the retest.
   Buy : mirror image for a close above the top of the zone.

Both: stop 0.41 daily ATR beyond entry (55 pips on GBPJPY at its typical 133-pip daily range).
      Target = nearest daily swing level beyond the zone that gives >= 3R AND is no more than 4 ATR away
      ("achievable": ~300 pips yes, 700 pips no). Otherwise skip.
      Orders wait up to 5 trading days; cancelled if the target is reached first.
      After the fill: one breakeven move at +1.2R, 20-day time stop, stop wins same-bar ties.
"""
import numpy as np
import pandas as pd

from strategy import SPEC, Params, load, pivots, resample, simulate_trade

ZONE = 0.3
STOP_ATR = 0.41
MAX_TGT_ATR = 4.0
MIN_RR = 3.0
WAIT_DAYS = 5


def daily_atr(d):
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False).mean()


def first_structure_break(o, h, l, c, side, k=2):
    """Index of the first 15m close beyond the latest confirmed swing (higher high for buys),
    and that swing's price. Swings are confirmed k bars after they print."""
    n = len(c)
    last_swing = None
    for j in range(n):
        p = j - k
        if p - k >= 0:
            if side == 1 and h[p] == max(h[p - k:p + k + 1]):
                last_swing = h[p]
            if side == -1 and l[p] == min(l[p - k:p + k + 1]):
                last_swing = l[p]
        if last_swing is not None:
            if (side == 1 and c[j] > last_swing) or (side == -1 and c[j] < last_swing):
                return j, last_swing
    return None, None


def run_inst(inst, p: Params, stop_atr=STOP_ATR, setups=("bounce", "break"), body_levels=False, replace_pending=False,
            target_rank=0, cooldown_days=0,
            trend_filter=False, max_vol_ratio=None, min_wick=None):
    spec = SPEC[inst]
    half = spec["spread"] / 2
    b15 = load(inst)
    d = resample(b15, "1D")
    d = d[d.index.dayofweek != 5]
    d["atr"] = daily_atr(d)
    if body_levels:   # Solomon reads levels from where daily candles CLOSE, not from wicks
        body = pd.DataFrame({"high": d[["open", "close"]].max(axis=1), "low": d[["open", "close"]].min(axis=1)}, index=d.index)
        piv = pivots(body, 3, pd.Timedelta(days=1), 1)
    else:
        piv = pivots(d, 3, pd.Timedelta(days=1), 1)
    pv_price, pv_time, pv_known = piv["price"].to_numpy(), piv["ptime"].to_numpy(), piv["known"].to_numpy()
    days = d.index.to_numpy()
    O, H, L, C, A = (d[x].to_numpy() for x in ("open", "high", "low", "close", "atr"))
    t15 = b15.index
    o15, h15, l15, c15 = (b15[x].to_numpy() for x in ("open", "high", "low", "close"))
    SMA50 = d["close"].rolling(50).mean().to_numpy()
    SMA200 = d["close"].rolling(200).mean().to_numpy()
    ATR_MED = d["atr"].rolling(250, min_periods=60).median().to_numpy()
    last_stop = []   # (time, level) of recent stopped-out trades, for the optional cooldown

    def signal_at(i):
        a = A[i]
        now = days[i] + np.timedelta64(1, "D")
        m = (pv_known <= now) & (pv_time >= days[i] - np.timedelta64(365, "D"))
        if m.sum() < 3:
            return None, None
        pr = pv_price[m]
        rng = H[i] - L[i]
        best = None
        for lvl in np.unique(np.round(pr / (0.05 * a)) * (0.05 * a)):
            touches = int((np.abs(pr - lvl) <= ZONE * a).sum())
            if touches < 3:
                continue
            zt, zb = lvl + ZONE * a, lvl - ZONE * a
            sig = None
            if "bounce" in setups and rng > 0:
                if C[i - 1] > lvl and L[i] < lvl and C[i] > lvl and (min(O[i], C[i]) - L[i]) >= rng / 3:
                    sig = ("bounce", 1)
                elif C[i - 1] < lvl and H[i] > lvl and C[i] < lvl and (H[i] - max(O[i], C[i])) >= rng / 3:
                    sig = ("bounce", -1)
            if sig is None and "break" in setups:
                if C[i - 1] >= zb and C[i] < zb:
                    sig = ("break", -1)
                elif C[i - 1] <= zt and C[i] > zt:
                    sig = ("break", 1)
            if sig and (best is None or touches > best[1]):
                best = (lvl, touches, sig)
        return best, pr

    sig_cache = {}

    def cached(i):
        if i not in sig_cache:
            sig_cache[i] = signal_at(i)
        return sig_cache[i]

    out = []
    busy_until = np.datetime64("1970-01-01")
    for i in range(30, len(d) - 1):
        a = A[i]
        now = days[i] + np.timedelta64(1, "D")
        if now < busy_until:
            continue
        best, pr = cached(i)
        if best is None:
            continue
        lvl, touches, (kind, side) = best

        # optional quality filters, all known when the daily candle closes
        if trend_filter and not np.isnan(SMA50[i]) and np.sign(C[i] - SMA50[i]) != side:
            continue
        if max_vol_ratio and not np.isnan(ATR_MED[i]) and a / ATR_MED[i] > max_vol_ratio:
            continue
        if min_wick and kind == "bounce":
            rg = H[i] - L[i]
            wk = ((min(O[i], C[i]) - L[i]) if side == 1 else (H[i] - max(O[i], C[i]))) / rg if rg > 0 else 0
            if wk < min_wick:
                continue
        s = t15.searchsorted(now)
        end_t = now + np.timedelta64(int(WAIT_DAYS * 7 / 5 * 24), "h")
        if replace_pending:   # a newer daily setup cancels this one if it is still unfilled
            for i2 in range(i + 1, min(i + 8, len(d) - 1)):
                nxt = days[i2] + np.timedelta64(1, "D")
                if nxt >= end_t:
                    break
                if cached(i2)[0] is not None:
                    end_t = nxt
                    break
        e = t15.searchsorted(end_t)
        if e - s < 10:
            continue
        if kind == "bounce":
            j, swing = first_structure_break(o15[s:e], h15[s:e], l15[s:e], c15[s:e], side)
            if j is None:
                if not replace_pending:
                    busy_until = t15[e - 1].to_datetime64()
                continue
            # the 15m trigger must happen at the zone: the broken swing may sit at most one zone-width beyond it
            if (side == 1 and swing > lvl + 2 * ZONE * a) or (side == -1 and swing < lvl - 2 * ZONE * a):
                if not replace_pending:
                    busy_until = t15[min(s + j, len(t15) - 1)].to_datetime64()
                continue
            entry, arm = swing, s + j + 1
        else:
            entry, arm = lvl, s
        stop = entry - side * stop_atr * a
        risk = stop_atr * a
        # target: nearest daily swing beyond the zone with >= 3R and within 4 ATR
        beyond = pr[(pr > lvl + ZONE * a)] if side == 1 else pr[(pr < lvl - ZONE * a)]
        dist = (beyond - entry) * side
        ok = (dist >= MIN_RR * risk) & (dist <= MAX_TGT_ATR * a)
        if not ok.any():
            continue
        cand = np.unique(beyond[ok])
        cand = cand[np.argsort((cand - entry) * side)]
        if target_rank >= len(cand):
            continue
        tgt = cand[target_rank]
        if cooldown_days and any((np.datetime64(now) - ts) < np.timedelta64(cooldown_days, "D") and abs(lv - lvl) <= 2 * ZONE * a
                                 for ts, lv in last_stop):
            continue
        tp = tgt - side * 0.05 * a
        rr = (tp - entry) * side / risk
        if rr < MIN_RR:
            continue
        hh, ll = h15[arm:e], l15[arm:e]
        if side == 1:
            fill = np.nonzero(ll + half <= entry)[0]
            gone = np.nonzero(hh - half >= tp)[0]
        else:
            fill = np.nonzero(hh - half >= entry)[0]
            gone = np.nonzero(ll + half <= tp)[0]
        if len(fill) == 0 or (len(gone) and gone[0] < fill[0]):
            if not replace_pending:
                busy_until = t15[min(e, len(t15) - 1)].to_datetime64()
            continue
        k = arm + fill[0]
        rng_i = H[i] - L[i]
        wick = ((min(O[i], C[i]) - L[i]) if side == 1 else (H[i] - max(O[i], C[i]))) / rng_i if rng_i > 0 else 0
        trend = np.sign(C[i] - SMA50[i]) if not np.isnan(SMA50[i]) else 0
        trend_long = np.sign(SMA50[i] - SMA200[i]) if not np.isnan(SMA200[i]) else 0
        c = dict(inst=inst, setup=kind, t_signal=pd.Timestamp(now), t_entry=t15[k], side=side, entry=entry,
                 stop=stop, tp=tp, rr=rr, risk_pips=risk / spec["pip"], atr=a, level=lvl, touches=touches,
                 wick=wick, with_trend=int(trend == side), with_long_trend=int(trend_long == side),
                 vol_ratio=a / ATR_MED[i] if not np.isnan(ATR_MED[i]) else 1.0,
                 tgt_atr=abs(tp - entry) / a, body_frac=abs(C[i] - O[i]) / rng_i if rng_i > 0 else 0,
                 dow=pd.Timestamp(now).dayofweek, hours_to_fill=(t15[k] - pd.Timestamp(now)).total_seconds() / 3600)
        t_exit, R, reason = simulate_trade(b15, c, p, spec)
        c.update(t_exit=t_exit, R=R, reason=reason)
        if reason == "stop":
            last_stop.append((np.datetime64(t_exit), lvl))
        out.append(c)
        busy_until = np.datetime64(t_exit)
    return pd.DataFrame(out)
