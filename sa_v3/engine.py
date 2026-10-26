"""Backtest engine with Break-Even and Chop Filter."""
from __future__ import annotations
import uuid
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from .strategy import Strategy
from .utils import tf_to_ms, atr_pct, frac_to_bps

@dataclass
class Position:
    signal_id: str
    symbol: str
    mode: str
    action: str
    entry_ts: int
    entry_index: int
    entry_price: float
    tp_price: float
    sl_price: float
    size_usd: float
    target_bps: float
    stop_bps: float
    peak: float
    armed: bool = False
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    confidence: float = 0.0
    layers_json: str = ""
    break_even_active: bool = False

@dataclass
class Trade:
    signal_id: str
    symbol: str
    mode: str
    action: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    tp_price: float
    sl_price: float
    size_usd: float
    gross_pnl_pct: float
    net_pnl_pct: float
    gross_pnl_bps: float
    net_pnl_bps: float
    gross_pnl_usd: float
    net_pnl_usd: float
    exit_reason: str
    mfe_pct: float
    mae_pct: float
    holding_candles: int
    confidence: float = 0.0
    layers_json: str = ""

def default_window(days):
    import time
    end_ms = int(time.time() * 1000)
    return end_ms - days * 86_400_000, end_ms

def _cost_bps(config):
    return config["costs"]["fee_roundtrip_bps"] + config["costs"]["slippage_roundtrip_bps"]

def _pnl_pct(action, entry, exit_):
    return ((exit_ - entry) if action == "BUY" else (entry - exit_)) / entry * 100

def position_size_usd(config, balance, entry_price, stop_price):
    risk_pct = config["risk"]["risk_pct_per_trade"] / 100.0
    stop_dist = abs(entry_price - stop_price) / entry_price
    if stop_dist <= 0:
        return 0.0
    size = balance * risk_pct / stop_dist
    max_lev = config["risk"].get("max_leverage")
    if max_lev is not None:
        size = min(size, balance * max_lev)
    return size

def resolve_exit(pos, candle, cfg_mode, cost_bps):
    high, low = float(candle["high"]), float(candle["low"])
    tp, sl = pos.tp_price, pos.sl_price
    trail_enabled = cfg_mode["trailing"]["enabled"]
    trail_bps = cfg_mode["trailing"]["trail_distance_bps"]
    be_trigger = cfg_mode.get("break_even_trigger_bps", 9999)

    if pos.action == "BUY":
        pos.mfe_pct = max(pos.mfe_pct, ((high - pos.entry_price) / pos.entry_price) * 100)
        pos.mae_pct = max(pos.mae_pct, ((pos.entry_price - low) / pos.entry_price) * 100)
        
        # Check Break-Even (if not active yet)
        if not pos.break_even_active:
            profit_bps = ((high - pos.entry_price) / pos.entry_price) * 10000
            if profit_bps >= be_trigger:
                pos.break_even_active = True
                # Move SL to entry + fees + a little buffer
                pos.sl_price = pos.entry_price * (1 + (cost_bps + 5) / 10000)
                sl = pos.sl_price

        if low <= sl:
            reason = "BE" if pos.break_even_active else "SL"
            return {"reason": reason, "price": sl}
        if trail_enabled and pos.armed:
            trail = max(pos.peak * (1 - trail_bps / 10_000), tp)
            if low <= trail:
                return {"reason": "TRAIL", "price": trail}
        if trail_enabled:
            if not pos.armed and high >= tp:
                pos.armed = True; pos.peak = high
            elif pos.armed:
                pos.peak = max(pos.peak, high)
        else:
            if high >= tp:
                return {"reason": "TP", "price": tp}
    else:
        pos.mfe_pct = max(pos.mfe_pct, ((pos.entry_price - low) / pos.entry_price) * 100)
        pos.mae_pct = max(pos.mae_pct, ((high - pos.entry_price) / pos.entry_price) * 100)
        
        if not pos.break_even_active:
            profit_bps = ((pos.entry_price - low) / pos.entry_price) * 10000
            if profit_bps >= be_trigger:
                pos.break_even_active = True
                pos.sl_price = pos.entry_price * (1 - (cost_bps + 5) / 10000)
                sl = pos.sl_price

        if high >= sl:
            reason = "BE" if pos.break_even_active else "SL"
            return {"reason": reason, "price": sl}
        if trail_enabled and pos.armed:
            trail = min(pos.peak * (1 + trail_bps / 10_000), tp)
            if high >= trail:
                return {"reason": "TRAIL", "price": trail}
        if trail_enabled:
            if not pos.armed and low <= tp:
                pos.armed = True; pos.peak = low
            elif pos.armed:
                pos.peak = min(pos.peak, low)
        else:
            if low <= tp:
                return {"reason": "TP", "price": tp}
    return None

def make_trade(pos, exit_ts, exit_price, exit_reason, cost_bps):
    gross_pct = _pnl_pct(pos.action, pos.entry_price, exit_price)
    gross_bps = gross_pct * 100
    net_bps = gross_bps - cost_bps
    net_pct = net_bps / 100
    gross_usd = pos.size_usd * gross_pct / 100
    net_usd = pos.size_usd * net_pct / 100
    return Trade(
        signal_id=pos.signal_id, symbol=pos.symbol, mode=pos.mode, action=pos.action,
        entry_ts=pos.entry_ts, exit_ts=exit_ts,
        entry_price=pos.entry_price, exit_price=round(exit_price, 8),
        tp_price=pos.tp_price, sl_price=pos.sl_price,
        size_usd=round(pos.size_usd, 2),
        gross_pnl_pct=round(gross_pct, 4), net_pnl_pct=round(net_pct, 4),
        gross_pnl_bps=round(gross_bps, 1), net_pnl_bps=round(net_bps, 1),
        gross_pnl_usd=round(gross_usd, 4), net_pnl_usd=round(net_usd, 4),
        exit_reason=exit_reason,
        mfe_pct=round(pos.mfe_pct, 4), mae_pct=round(pos.mae_pct, 4),
        holding_candles=0, confidence=pos.confidence, layers_json=pos.layers_json,
    )

def precompute_btc_regimes(btc_df, config
