"""Five scoring layers — pure functions of closed-candle data."""
from __future__ import annotations
import pandas as pd
from .utils import ema, rsi, macd_hist, zscore, sigmoid

def _empty():
    return {"bullish_score": 0.0, "bearish_score": 0.0, "details": {"error": "not_enough_data"}}

def _clamp(x):
    return round(max(0.0, min(100.0, x)), 2)

W_EMA, W_MACD, W_RSI, W_VOL, W_SR = 30, 25, 20, 15, 10

def technical(ohlcv):
    if len(ohlcv) < 60:
        return _empty()
    close = ohlcv["close"]
    volume = ohlcv["volume"]
    bull = bear = 0.0
    details = {}

    ef = ema(close, 9); es = ema(close, 21)
    cross_up = ef.iloc[-1] > es.iloc[-1] and ef.iloc[-2] <= es.iloc[-2]
    cross_dn = ef.iloc[-1] < es.iloc[-1] and ef.iloc[-2] >= es.iloc[-2]
    above = ef.iloc[-1] > es.iloc[-1]
    slope_up = ef.iloc[-1] > ef.iloc[-3]
    if cross_up:
        bull += W_EMA; details["ema"] = "fresh_bull_cross"
    elif above and slope_up:
        bull += W_EMA * 0.6; details["ema"] = "bull_trend"
    elif cross_dn:
        bear += W_EMA; details["ema"] = "fresh_bear_cross"
    elif not above and not slope_up:
        bear += W_EMA * 0.6; details["ema"] = "bear_trend"
    else:
        details["ema"] = "neutral"

    hist = macd_hist(close)
    h_now, h_prev = hist.iloc[-1], hist.iloc[-2]
    rising = h_now > h_prev
    if h_now > 0 and rising:
        bull += W_MACD; details["macd"] = "bull_momentum"
    elif h_now > 0:
        bull += W_MACD * 0.5; details["macd"] = "bull_fading"
    elif h_now < 0 and not rising:
        bear += W_MACD; details["macd"] = "bear_momentum"
    elif h_now < 0:
        bear += W_MACD * 0.5; details["macd"] = "bear_fading"
    else:
        details["macd"] = "neutral"

    r = rsi(close).iloc[-1]
    if r < 30:
        bull += W_RSI; details["rsi"] = f"oversold({r:.1f})"
    elif r > 70:
        bear += W_RSI; details["rsi"] = f"overbought({r:.1f})"
    elif r > 50:
        bull += W_RSI * 0.4; details["rsi"] = f"mild_bull({r:.1f})"
    elif r < 50:
        bear += W_RSI * 0.4; details["rsi"] = f"mild_bear({r:.1f})"
    else:
        details["rsi"] = f"neutral({r:.1f})"

    avg_v = volume.iloc[-21:-1].mean()
    ratio = volume.iloc[-1] / avg_v if avg_v > 0 else 1.0
    candle_bull = close.iloc[-1] > ohlcv["open"].iloc[-1]
    if ratio > 1.5:
        if candle_bull:
            bull += W_VOL; details["volume"] = f"high_vol_bull({ratio:.2f}x)"
        else:
            bear += W_VOL; details["volume"] = f"high_vol_bear({ratio:.2f}x)"
    else:
        details["volume"] = f"normal({ratio:.2f}x)"

    recent = ohlcv.iloc[-50:]
    hi, lo = recent["high"].max(), recent["low"].min()
    price = close.iloc[-1]
    rng = hi - lo if hi > lo else 1.0
    if (price - lo) / rng < 0.1:
        bull += W_SR; details["sr"] = "near_support"
    elif (hi - price) / rng < 0.1:
        bear += W_SR; details["sr"] = "near_resistance"
    else:
        details["sr"] = "mid_range"

    return {"bullish_score": _clamp(bull), "bearish_score": _clamp(bear), "details": details}

def multi_timeframe(ohlcv_by_tf):
    details = {}
    bull_n = bear_n = usable = 0
    for tf, df in ohlcv_by_tf.items():
        if df is None or len(df) < 30:
            details[tf] = "not_enough_data"
            continue
        ef = ema(df["close"], 9).iloc[-1]
        es = ema(df["close"], 21).iloc[-1]
        usable += 1
        if ef > es:
            bull_n += 1; details[tf] = "bullish"
        elif ef < es:
            bear_n += 1; details[tf] = "bearish"
        else:
            details[tf] = "neutral"
    if usable == 0:
        return {"bullish_score": 0.0, "bearish_score": 0.0, "details": details}
    return {"bullish_score": _clamp(bull_n / usable * 100),
            "bearish_score": _clamp(bear_n / usable * 100), "details": details}

def volume_imbalance(ohlcv, lookback=20, vol_multiplier=1.5):
    if len(ohlcv) < lookback:
        return _empty()
    recent = ohlcv.iloc[-lookback:].copy()
    body = recent["close"] - recent["open"]
    sign = (body > 0).astype(float) - (body < 0).astype(float)
    rng = (recent["high"] - recent["low"]).replace(0, 1e-9)
    weight = recent["volume"] * (1.0 + (rng / rng.mean() - 1.0).abs())
    signed = sign * weight
    total = weight.sum()
    if total <= 0:
        return _empty()
    imbalance = float(signed.sum() / total)
    avg_vol = recent["volume"].mean()
    last_vol = ohlcv["volume"].iloc[-1]
    vol_boost = 1.0
    if avg_vol > 0 and last_vol / avg_vol > vol_multiplier:
        vol_boost = 1.2
    bull = max(imbalance, 0) * 100 * vol_boost
    bear = max(-imbalance, 0) * 100 * vol_boost
    return {"bullish_score": _clamp(bull), "bearish_score": _clamp(bear),
            "details": {"imbalance": round(imbalance, 4),
                        "vol_ratio": round(last_vol / avg_vol, 2) if avg_vol > 0 else 1.0}}

def funding(funding_history, rolling_window=100, min_history=20):
    if funding_history is None or len(funding_history) < min_history:
        return _empty()
    recent = funding_history["rate"].iloc[-rolling_window:].tolist()
    current = recent[-1]
    z = zscore(recent[:-1], current, min_len=min_history)
    p = sigmoid(-z)
    if p > 0.5:
        bull = (p - 0.5) * 2 * 100; bear = 0.0
    elif p < 0.5:
        bear = (0.5 - p) * 2 * 100; bull = 0.0
    else:
        bull = bear = 0.0
    return {"bullish_score": _clamp(bull), "bearish_score": _clamp(bear),
            "details": {"rate": round(current, 6), "z": round(z, 3), "n": len(recent)}}

W_BOS, W_CHOCH, W_OB, W_FVG, W_SWEEP = 40, 40, 10, 30, 20

def _find_fractals(df, left=5, right=5):
    highs = df["high"].values; lows = df["low"].values
    n = len(df); out = []
    for i in range(left, n - right):
        wh = highs[i - left:i + right + 1]
        wl = lows[i - left:i + right + 1]
        if highs[i] == wh.max() and (wh == highs[i]).sum() == 1:
            out.append({"index": i, "price": float(highs[i]), "type": "high"})
        if lows[i] == wl.min() and (wl == lows[i]).sum() == 1:
            out.append({"index": i, "price": float(lows[i]), "type": "low"})
    out.sort(key=lambda f: f["index"])
    return out

def _structure(fractals):
    highs = [f for f in fractals if f["type"] == "high"]
    lows = [f for f in fractals if f["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return {"bias": "unknown", "last_high": None, "last_low": None}
    lh, ph = highs[-1], highs[-2]; ll, pl = lows[-1], lows[-2]
    hh = lh["price"] > ph["price"]; hl = ll["price"] > pl["price"]
    loh = lh["price"] < ph["price"]; lol = ll["price"] < pl["price"]
    bias = "bullish" if (hh and hl) else ("bearish" if (loh and lol) else "ranging")
    return {"bias": bias, "last_high": lh, "last_low": ll}

def _fvg(df, lookback=20):
    n = len(df); start = max(2, n - lookback)
    bull = bear = None
    for i in range(start, n):
        c1h, c1l = df["high"].iloc[i - 2], df["low"].iloc[i - 2]
        c3h, c3l = df["high"].iloc[i], df["low"].iloc[i]
        if c1h < c3l:
            gl, gh = c1h, c3l
            filled = (df["low"].iloc[i + 1:] <= gh).any() if i + 1 < n else False
            if not filled:
                bull = {"index": i, "gap_low": float(gl), "gap_high": float(gh)}
        if c1l > c3h:
            gl, gh = c3h, c1l
            filled = (df["high"].iloc[i + 1:] >= gl).any() if i + 1 < n else False
            if not filled:
                bear = {"index": i, "gap_low": float(gl), "gap_high": float(gh)}
    return bull, bear

def _order_block(df, break_index, direction, search_back=10):
    start = max(0, break_index - search_back)
    for i in range(break_index - 1, start - 1, -1):
        is_bear = df["close"].iloc[i] < df["open"].iloc[i]
        is_bull = df["close"].iloc[i] > df["open"].iloc[i]
        if direction == "bullish" and is_bear:
            return {"index": i, "low": float(df["low"].iloc[i]), "high": float(df["high"].iloc[i])}
        if direction == "bearish" and is_bull:
            return {"index": i, "low": float(df["low"].iloc[i]), "high": float(df["high"].iloc[i])}
    return None

def _sweep(df, fractals, lookback=10):
    if not fractals:
        return None
    recent = df.iloc[-lookback:]
    lows = [f for f in fractals if f["type"] == "low"]
    highs = [f for f in fractals if f["type"] == "high"]
    if lows:
        level = lows[-1]["price"]
        if ((recent["low"] < level) & (recent["close"] > level)).any():
            return {"type": "bullish_sweep", "level": level}
    if highs:
        level = highs[-1]["price"]
        if ((recent["high"] > level) & (recent["close"] < level)).any():
            return {"type": "bearish_sweep", "level": level}
    return None

def smc(ohlcv, fractal_left=5, fractal_right=5):
    min_candles = fractal_left + fractal_right + 40
    if len(ohlcv) < min_candles:
        return _empty()
    price = float(ohlcv["close"].iloc[-1])
    fractals = _find_fractals(ohlcv, left=fractal_left, right=fractal_right)
    structure = _structure(fractals)
    bull = bear = 0.0
    details = {"bias": structure["bias"], "event": "none"}

    lh, ll = structure["last_high"], structure["last_low"]
    if lh and price > lh["price"]:
        if structure["bias"] == "bullish":
            bull += W_BOS; details["event"] = "BOS_bull"
        else:
            bull += W_CHOCH; details["event"] = "CHoCH_bull"
        ob = _order_block(ohlcv, lh["index"], "bullish")
        if ob:
            bull += W_OB; details["ob"] = ob
    elif ll and price < ll["price"]:
        if structure["bias"] == "bearish":
            bear += W_BOS; details["event"] = "BOS_bear"
        else:
            bear += W_CHOCH; details["event"] = "CHoCH_bear"
        ob = _order_block(ohlcv, ll["index"], "bearish")
        if ob:
            bear += W_OB; details["ob"] = ob

    bull_fvg, bear_fvg = _fvg(ohlcv)
    if bull_fvg and bull_fvg["gap_low"] <= price <= bull_fvg["gap_high"] * 1.02:
        bull += W_FVG; details["fvg"] = "bull"
    elif bear_fvg and bear_fvg["gap_low"] * 0.98 <= price <= bear_fvg["gap_high"]:
        bear += W_FVG; details["fvg"] = "bear"

    sw = _sweep(ohlcv, fractals)
    if sw:
        if sw["type"] == "bullish_sweep":
            bull += W_SWEEP
        else:
            bear += W_SWEEP
        details["sweep"] = sw["type"]

    return {"bullish_score": _clamp(bull), "bearish_score": _clamp(bear), "details": details}
