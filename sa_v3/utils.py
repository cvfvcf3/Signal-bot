"""Timeframe parsing, bps helpers, technical indicators."""
from __future__ import annotations
import math
from typing import Iterable
import numpy as np
import pandas as pd

TIMEFRAME_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000,
    "1d": 86_400_000,
}

def tf_to_ms(tf: str) -> int:
    if tf not in TIMEFRAME_MS:
        raise ValueError(f"Unknown timeframe: {tf!r}")
    return TIMEFRAME_MS[tf]

def bps_to_frac(bps: float) -> float:
    return bps / 10_000.0

def frac_to_bps(frac: float) -> float:
    return frac * 10_000.0

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)

def macd_hist(series: pd.Series, fast=12, slow=26, signal=9) -> pd.Series:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line - signal_line

def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 2:
        return 0.0
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_value = tr.rolling(period).mean().iloc[-1]
    last_close = close.iloc[-1]
    if pd.isna(atr_value) or last_close <= 0:
        return 0.0
    return float(atr_value / last_close)

def sigmoid(x: float) -> float:
    x = max(min(x, 20.0), -20.0)
    return 1.0 / (1.0 + math.exp(-x))

def zscore(history: Iterable[float], current: float, min_len: int = 10) -> float:
    arr = np.asarray(list(history), dtype=float)
    if len(arr) < min_len:
        return 0.0
    mean = arr.mean()
    std = arr.std()
    if std == 0:
        return 0.0
    return float((current - mean) / std)
