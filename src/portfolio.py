"""Portfolio layer: Solomon's risk rules applied to the stream of candidate trades.

- Fixed 0.5% of equity at risk per trade (compounding on current equity).
- At most 2 trades open at once (1% total open risk), one per instrument, one per theme.
- Loss limits on realised P&L: -2% day -> no new entries until next day; -5% week -> until next Monday;
  -10% month -> until the 1st of next month.
- Simultaneous candidates are ranked by reward:risk (ML ranking comes in Phase 2).
"""
import pandas as pd

from strategy import SPEC

RISK = 0.005


def run_portfolio(trades: pd.DataFrame, max_open: int = 2):
    trades = trades.sort_values(["t_entry", "rr"], ascending=[True, False]).reset_index(drop=True)
    equity = 1.0
    open_pos = []  # dicts with inst, theme, t_exit, R, stake
    taken = []
    day_pnl, week_pnl, month_pnl = {}, {}, {}
    curve = []

    def realise(upto):
        nonlocal equity
        still = []
        for pos in sorted(open_pos, key=lambda x: x["t_exit"]):
            if pos["t_exit"] <= upto:
                pnl = pos["R"] * pos["stake"]
                start_eq = equity
                equity += pnl
                ts = pos["t_exit"]
                for book, key in ((day_pnl, ts.date()), (week_pnl, (ts.isocalendar()[0], ts.isocalendar()[1])),
                                  (month_pnl, (ts.year, ts.month))):
                    book[key] = book.get(key, 0.0) + pnl / start_eq
                curve.append((ts, equity))
            else:
                still.append(pos)
        open_pos[:] = still

    for c in trades.to_dict("records"):
        t = c["t_entry"]
        realise(t)
        if len(open_pos) >= max_open:
            continue
        theme = SPEC[c["inst"]]["theme"]
        if any(p["inst"] == c["inst"] or p["theme"] == theme for p in open_pos):
            continue
        iso = t.isocalendar()
        if (day_pnl.get(t.date(), 0) <= -0.02 or week_pnl.get((iso[0], iso[1]), 0) <= -0.05
                or month_pnl.get((t.year, t.month), 0) <= -0.10):
            continue
        stake = RISK * equity
        open_pos.append(dict(inst=c["inst"], theme=theme, t_exit=c["t_exit"], R=c["R"], stake=stake))
        taken.append(c)
    realise(pd.Timestamp.max)
    taken = pd.DataFrame(taken)
    curve = pd.Series(dict(curve)).sort_index() if curve else pd.Series(dtype=float)
    return taken, curve


def stats(taken: pd.DataFrame, curve: pd.Series) -> dict:
    if taken.empty:
        return dict(trades=0)
    eq = curve.resample("ME").last().ffill()
    eq = pd.concat([pd.Series([1.0], index=[eq.index[0] - pd.offsets.MonthEnd(1)]), eq])
    mret = eq.pct_change().dropna()
    dd = (curve / curve.cummax() - 1).min()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = curve.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    sharpe = (mret.mean() / mret.std() * 12 ** 0.5) if mret.std() > 0 else 0
    return dict(
        trades=len(taken),
        trades_per_month=round(len(taken) / max(len(mret), 1), 1),
        win_rate=round((taken["reason"] == "target").mean() * 100, 1),
        be_rate=round((taken["reason"] == "be").mean() * 100, 1),
        avg_R=round(taken["R"].mean(), 3),
        total_return_pct=round((curve.iloc[-1] - 1) * 100, 1),
        cagr_pct=round(cagr * 100, 2),
        avg_month_pct=round(mret.mean() * 100, 2),
        pct_months_positive=round((mret > 0).mean() * 100, 1),
        worst_month_pct=round(mret.min() * 100, 2),
        best_month_pct=round(mret.max() * 100, 2),
        max_dd_pct=round(dd * 100, 2),
        sharpe=round(sharpe, 2),
    )
