"""Backtest engine with Break-Even and Chop Filter (ADX)."""
from __future__ import annotations
import uuid
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from .strategy import Strategy
from .utils import tf_to_ms, atr_pct, frac_to_bps, adx

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
        
        if not pos.break_even_active:
            profit_bps = ((high - pos.entry_price) / pos.entry_price) * 10000
            if profit_bps >= be_trigger:
                pos.break_even_active = True
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

def precompute_btc_regimes(btc_df, config):
    from . import layers
    threshold = config["btc_macro"]["strong_threshold"]
    out = []
    if len(btc_df) < 60: return out
    for i in range(59, len(btc_df)):
        reading = layers.technical(btc_df.iloc[:i + 1])
        b, s = reading["bullish_score"], reading["bearish_score"]
        regime = "bullish" if b >= threshold else ("bearish" if s >= threshold else "neutral")
        out.append((int(btc_df.iloc[i]["ts"]), regime))
    return out

def regime_at(regimes, ts):
    import bisect
    if not regimes: return "neutral"
    keys = [r[0] for r in regimes]
    idx = bisect.bisect_right(keys, ts) - 1
    return regimes[idx][1] if idx >= 0 else "neutral"

def run_mode_backtest(config, mode, symbols, data, funding, btc_regimes, start_ms, end_ms, verbose=False):
    mode_cfg = config["modes"][mode]
    primary_tf = mode_cfg["primary_timeframe"]
    strategy = Strategy(config)
    cost_bps = _cost_bps(config)
    balance = config["risk"]["starting_balance_usd"]
    min_atr = mode_cfg.get("min_atr_bps", 0)
    min_adx = mode_cfg.get("min_adx", 0)

    primary_maps = {}
    for sym in symbols:
        df = data[sym].get(primary_tf)
        if df is None or len(df) == 0: continue
        df = df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].reset_index(drop=True)
        if len(df) < 100: continue
        primary_maps[sym] = df
    symbols = [s for s in symbols if s in primary_maps]
    if not symbols:
        return {"mode": mode, "trades": [], "equity": pd.DataFrame(columns=["ts", "balance"])}

    all_ts = sorted({int(t) for sym in symbols for t in primary_maps[sym]["ts"].tolist()})
    sym_ts_idx = {sym: {int(t): i for i, t in enumerate(primary_maps[sym]["ts"].tolist())} for sym in symbols}

    open_positions = {}
    last_signal_ts = {}
    trades = []
    equity_curve = []
    cooldown_ms = mode_cfg.get("cooldown_minutes", 0) * 60_000
    max_concurrent = mode_cfg.get("max_concurrent_positions", 1)
    timeout_candles = mode_cfg["evaluation_candles"]
    inval_age = mode_cfg.get("min_candles_before_invalidation", 2)
    is_btc_map = {sym: sym == config["btc_symbol"] for sym in symbols}

    for ti, ts in enumerate(all_ts):
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            idx = sym_ts_idx[sym].get(ts)
            if idx is None: continue
            candle = primary_maps[sym].iloc[idx]
            held = idx - pos.entry_index

            res = resolve_exit(pos, candle, mode_cfg, cost_bps)
            if res is not None:
                trade = make_trade(pos, ts, res["price"], res["reason"], cost_bps)
                trade.holding_candles = held
                trades.append(trade); balance += trade.net_pnl_usd
                del open_positions[sym]
                continue

            if held >= inval_age:
                reading = _eval_invalidation(strategy, config, mode, sym, ts, data, funding, btc_regimes, is_btc_map[sym])
                if reading is not None and _is_reversed(pos.action, reading, mode_cfg):
                    trade = make_trade(pos, ts, float(candle["close"]), "INVALIDATED", cost_bps)
                    trade.holding_candles = held
                    trades.append(trade); balance += trade.net_pnl_usd
                    del open_positions[sym]
                    continue

            if held >= timeout_candles:
                trade = make_trade(pos, ts, float(candle["close"]), "TIMEOUT", cost_bps)
                trade.holding_candles = held
                trades.append(trade); balance += trade.net_pnl_usd
                del open_positions[sym]

        if len(open_positions) < max_concurrent:
            for sym in symbols:
                if sym in open_positions: continue
                idx = sym_ts_idx[sym].get(ts)
                if idx is None: continue
                last = last_signal_ts.get(sym)
                if last is not None and (ts - last) < cooldown_ms: continue
                
                hist = primary_maps[sym].iloc[:idx + 1]
                if min_atr > 0:
                    current_atr = frac_to_bps(atr_pct(hist, period=mode_cfg["atr_period"]))
                    if current_atr < min_atr:
                        continue 
                if min_adx > 0:
                    current_adx = adx(hist, period=14)
                    if current_adx < min_adx:
                        continue

                ctx = _build_context(config, mode, sym, ts, primary_maps[sym], idx, data, funding, btc_regimes, is_btc_map[sym])
                if ctx is None: continue
                decision = strategy.evaluate(ctx)
                if decision["action"] not in ("BUY", "SELL"): continue
                entry_price = float(primary_maps[sym].iloc[idx]["close"])
                sizing = strategy.compute_tp_sl(decision["action"], entry_price, ctx["primary_ohlcv"], mode)
                if sizing is None: continue
                size_usd = position_size_usd(config, balance, entry_price, sizing["sl_price"])
                if size_usd <= 0: continue
                import json
                pos = Position(
                    signal_id=str(uuid.uuid4()), symbol=sym, mode=mode,
                    action=decision["action"], entry_ts=ts, entry_index=idx,
                    entry_price=entry_price, tp_price=sizing["tp_price"],
                    sl_price=sizing["sl_price"], size_usd=size_usd,
                    target_bps=sizing["target_bps"], stop_bps=sizing["stop_bps"],
                    peak=entry_price, armed=False, confidence=decision["confidence"],
                    layers_json=json.dumps({k: {"b": v["bullish_score"], "s": v["bearish_score"]} for k, v in decision["layers"].items()}),
                )
                open_positions[sym] = pos
                last_signal_ts[sym] = ts

        unrealized = 0.0
        for sym, pos in open_positions.items():
            idx = sym_ts_idx[sym].get(ts)
            if idx is not None:
                cp = float(primary_maps[sym].iloc[idx]["close"])
                unrealized += pos.size_usd * _pnl_pct(pos.action, pos.entry_price, cp) / 100
        equity_curve.append((ts, balance + unrealized))

    for sym, pos in list(open_positions.items()):
        df = primary_maps[sym]
        last_row = df.iloc[-1]
        trade = make_trade(pos, int(last_row["ts"]), float(last_row["close"]), "END_OF_DATA", cost_bps)
        trade.holding_candles = len(df) - 1 - pos.entry_index
        trades.append(trade); balance += trade.net_pnl_usd

    equity_df = pd.DataFrame(equity_curve, columns=["ts", "balance"])
    return {"mode": mode, "trades": [t.__dict__.copy() for t in trades], "equity": equity_df}

def _build_context(config, mode, sym, ts, primary_df, idx, data, funding, btc_regimes, is_btc):
    mode_cfg = config["modes"][mode]
    primary_hist = primary_df.iloc[:idx + 1]
    if len(primary_hist) < 60: return None
    confirm = {}
    for tf in mode_cfg["confirm_timeframes"]:
        df = data[sym].get(tf)
        confirm[tf] = df[df["ts"] <= ts] if df is not None else pd.DataFrame()
    f_df = funding.get(sym)
    f_hist = f_df[f_df["ts"] <= ts] if f_df is not None and len(f_df) else pd.DataFrame(columns=["ts", "rate"])
    return {"mode": mode, "primary_ohlcv": primary_hist, "confirm_ohlcv": confirm, "funding": f_hist, "btc_regime": regime_at(btc_regimes, ts), "is_btc": is_btc}

def _eval_invalidation(strategy, config, mode, sym, ts, data, funding, btc_regimes, is_btc):
    mode_cfg = config["modes"][mode]
    df = data[sym].get(mode_cfg["primary_timeframe"])
    if df is None or len(df) == 0: return None
    try: idx = df["ts"].tolist().index(ts)
    except ValueError: return None
    ctx = _build_context(config, mode, sym, ts, df.reset_index(drop=True), idx, data, funding, btc_regimes, is_btc)
    return strategy.evaluate(ctx) if ctx else None

def _is_reversed(action, reading, mode_cfg):
    gap = mode_cfg["min_score_gap"]
    if action == "BUY": return (reading["bearish_score"] - reading["bullish_score"]) >= gap
    return (reading["bullish_score"] - reading["bearish_score"]) >= gap

def load_data_for_mode(config, mode, symbols, store, start_ms, end_ms):
    mode_cfg = config["modes"][mode]
    needed_tfs = {mode_cfg["primary_timeframe"], *mode_cfg["confirm_timeframes"]}
    needed_tfs.add(config["btc_macro"]["htf_timeframe"])
    data, funding = {}, {}
    all_syms = list(symbols)
    if config["btc_symbol"] not in all_syms: all_syms.append(config["btc_symbol"])
    for sym in all_syms:
        data[sym] = {}
        for tf in needed_tfs:
            try: df = store.get(sym, tf, start_ms - 30 * tf_to_ms(tf), end_ms)
            except Exception: df = pd.DataFrame()
            data[sym][tf] = df
        try: funding[sym] = store.get_funding(sym, start_ms - 30 * 86_400_000, end_ms)
        except Exception: funding[sym] = pd.DataFrame(columns=["ts", "rate"])
    btc_4h = data[config["btc_symbol"]].get(config["btc_macro"]["htf_timeframe"], pd.DataFrame())
    btc_regimes = precompute_btc_regimes(btc_4h, config)
    return data, funding, btc_regimes

def run_single_backtest(config, mode, symbols, store, start_ms, end_ms, verbose=False):
    data, funding, btc_regimes = load_data_for_mode(config, mode, symbols, store, start_ms, end_ms)
    result = run_mode_backtest(config, mode, symbols, data, funding, btc_regimes, start_ms, end_ms, verbose=verbose)
    from .metrics import compute_metrics
    result["metrics"] = compute_metrics(result["trades"], result["equity"])
    result["start_ms"] = start_ms
    result["end_ms"] = end_ms
    return result
