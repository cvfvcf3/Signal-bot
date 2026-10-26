"""Performance metrics."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd

def _daily_returns(equity_df):
    if len(equity_df) < 2:
        return pd.Series(dtype=float)
    df = equity_df.copy()
    df["day"] = (df["ts"] // 86_400_000).astype(int)
    daily = df.groupby("day")["balance"].last()
    return daily.pct_change().dropna()

def sharpe_daily(equity_df):
    r = _daily_returns(equity_df)
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(365))

def max_drawdown_pct(equity_df):
    if len(equity_df) < 2:
        return 0.0
    eq = equity_df["balance"].values.astype(float)
    peak = np.maximum.accumulate(eq)
    return float(-((eq - peak) / peak).min() * 100)

def calmar(equity_df):
    mdd = max_drawdown_pct(equity_df)
    if mdd <= 0 or len(equity_df) < 2:
        return 0.0
    years = (equity_df["ts"].iloc[-1] - equity_df["ts"].iloc[0]) / (365.25 * 86_400_000)
    if years <= 0:
        return 0.0
    s, e = equity_df["balance"].iloc[0], equity_df["balance"].iloc[-1]
    if s <= 0:
        return 0.0
    cagr_pct = ((e / s) ** (1 / years) - 1) * 100
    return float(cagr_pct / mdd)

def compute_metrics(trades, equity_df):
    n = len(trades)
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] < 0]
    flat = n - len(wins) - len(losses)
    gw = sum(t["net_pnl_usd"] for t in wins)
    gl = -sum(t["net_pnl_usd"] for t in losses)
    pf = gw / gl if gl > 0 else None
    net_usd = sum(t["net_pnl_usd"] for t in trades)
    net_bps = sum(t["net_pnl_bps"] for t in trades)
    sb = equity_df["balance"].iloc[0] if len(equity_df) else 0
    eb = equity_df["balance"].iloc[-1] if len(equity_df) else 0
    net_pct = (net_usd / sb * 100) if sb > 0 else 0.0

    by_reason, by_mode, by_symbol = {}, {}, {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
        for k, src in (("mode", by_mode), ("symbol", by_symbol)):
            key = t[k]
            if key not in src:
                src[key] = {"count": 0, "wins": 0, "net_usd": 0.0}
            src[key]["count"] += 1
            if t["net_pnl_usd"] > 0:
                src[key]["wins"] += 1
            src[key]["net_usd"] += t["net_pnl_usd"]

    avg_hold = sum(t["holding_candles"] for t in trades) / n if n else 0.0
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "flat": flat,
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else None,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "avg_win_usd": round(gw / len(wins), 2) if wins else None,
        "avg_loss_usd": round(-gl / len(losses), 2) if losses else None,
        "expectancy_usd": round(net_usd / n, 2) if n else None,
        "net_pnl_usd": round(net_usd, 2), "net_pnl_bps": round(net_bps, 1),
        "net_pnl_pct": round(net_pct, 3),
        "starting_balance": round(sb, 2), "ending_balance": round(eb, 2),
        "sharpe_daily": round(sharpe_daily(equity_df), 3),
        "max_drawdown_pct": round(max_drawdown_pct(equity_df), 3),
        "calmar": round(calmar(equity_df), 3),
        "avg_holding_candles": round(avg_hold, 1),
        "exit_reasons": by_reason, "by_mode": by_mode, "by_symbol": by_symbol,
    }
