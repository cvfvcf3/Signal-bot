"""
Data layer — Termux version. Uses CSV cache instead of parquet
(pyarrow does not build on Termux/ARM).

Providers: bybit_linear (default), binance_futures, local.
All read-only public REST. No API keys, no orders.
"""
from __future__ import annotations
import os
import time
from abc import ABC, abstractmethod
import pandas as pd
import requests
from .utils import tf_to_ms

class DataProvider(ABC):
    name = "abstract"
    @abstractmethod
    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms): ...
    def fetch_funding(self, symbol, start_ms, end_ms):
        return pd.DataFrame(columns=["ts", "rate"])

class BybitLinearProvider(DataProvider):
    name = "bybit_linear"
    BASE = "https://api.bybit.com"
    INTERVAL_MAP = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                    "1h": "60", "2h": "120", "4h": "240", "6h": "360",
                    "12h": "720", "1d": "D"}

    def _get(self, path, params):
        for attempt in range(3):
            try:
                r = requests.get(self.BASE + path, params=params, timeout=20)
                r.raise_for_status()
                data = r.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(f"Bybit: {data}")
                return data
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.0 * (attempt + 1))

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        interval = self.INTERVAL_MAP[timeframe]
        sym = symbol.replace("/", "")
        rows = []
        cursor_end = end_ms
        while cursor_end > start_ms:
            data = self._get("/v5/market/kline", {
                "category": "linear", "symbol": sym,
                "interval": interval, "limit": 1000, "end": cursor_end,
            })
            batch = data["result"]["list"]
            if not batch:
                break
            for row in sorted(batch, key=lambda r: int(r[0])):
                ts = int(row[0])
                if start_ms <= ts <= end_ms:
                    rows.append([ts, float(row[1]), float(row[2]),
                                 float(row[3]), float(row[4]), float(row[5])])
            oldest = min(int(r[0]) for r in batch)
            if oldest <= start_ms or len(batch) < 1000:
                break
            cursor_end = oldest - 1
            time.sleep(0.05)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    def fetch_funding(self, symbol, start_ms, end_ms):
        sym = symbol.replace("/", "")
        rows = []
        cursor_end = end_ms
        while cursor_end > start_ms:
            data = self._get("/v5/market/funding/history", {
                "category": "linear", "symbol": sym,
                "limit": 200, "endTime": cursor_end,
            })
            batch = data["result"]["list"]
            if not batch:
                break
            oldest = cursor_end
            for row in batch:
                ts = int(row["fundingRateTimestamp"])
                rate = float(row["fundingRate"])
                if start_ms <= ts <= end_ms:
                    rows.append([ts, rate])
                oldest = min(oldest, ts)
            if oldest <= start_ms or len(batch) < 200:
                break
            cursor_end = oldest - 1
            time.sleep(0.05)
        df = pd.DataFrame(rows, columns=["ts", "rate"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

class BinanceFuturesProvider(DataProvider):
    name = "binance_futures"
    BASE = "https://fapi.binance.com"

    def _get(self, path, params):
        for attempt in range(3):
            try:
                r = requests.get(self.BASE + path, params=params, timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.0 * (attempt + 1))

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        sym = symbol.replace("/", "")
        rows = []
        cursor = start_ms
        while cursor < end_ms:
            data = self._get("/fapi/v1/klines", {
                "symbol": sym, "interval": timeframe,
                "startTime": cursor, "limit": 1500,
            })
            if not data:
                break
            for row in data:
                ts = int(row[0])
                if start_ms <= ts <= end_ms:
                    rows.append([ts, float(row[1]), float(row[2]),
                                 float(row[3]), float(row[4]), float(row[5])])
            last = int(data[-1][0])
            if last >= end_ms or len(data) < 1500:
                break
            cursor = last + 1
            time.sleep(0.1)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    def fetch_funding(self, symbol, start_ms, end_ms):
        sym = symbol.replace("/", "")
        rows = []
        cursor = start_ms
        while cursor < end_ms:
            data = self._get("/fapi/v1/fundingRate", {
                "symbol": sym, "startTime": cursor, "limit": 1000,
            })
            if not data:
                break
            for row in data:
                ts = int(row["fundingTime"])
                if start_ms <= ts <= end_ms:
                    rows.append([ts, float(row["fundingRate"])])
            last = int(data[-1]["fundingTime"])
            if last >= end_ms or len(data) < 1000:
                break
            cursor = last + 1
            time.sleep(0.1)
        df = pd.DataFrame(rows, columns=["ts", "rate"])
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

class LocalProvider(DataProvider):
    name = "local"
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
    def _path(self, symbol, timeframe):
        sym = symbol.replace("/", "_")
        return os.path.join(self.cache_dir, "local", sym, f"{timeframe}.csv")
    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        p = self._path(symbol, timeframe)
        if not os.path.exists(p):
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.read_csv(p)
        return df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].reset_index(drop=True)
    def fetch_funding(self, symbol, start_ms, end_ms):
        p = self._path(symbol, "funding")
        if not os.path.exists(p):
            return pd.DataFrame(columns=["ts", "rate"])
        df = pd.read_csv(p)
        return df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].reset_index(drop=True)

class CandleStore:
    """CSV-cached OHLCV + funding. Only fetches missing ranges."""

    def __init__(self, provider, cache_dir):
        self.provider = provider
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _ohlcv_path(self, symbol, timeframe):
        sym = symbol.replace("/", "_")
        d = os.path.join(self.cache_dir, self.provider.name, sym)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{timeframe}.csv")

    def _funding_path(self, symbol):
        sym = symbol.replace("/", "_")
        d = os.path.join(self.cache_dir, self.provider.name, sym)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "_funding.csv")

    def _read(self, path, columns):
        if not os.path.exists(path):
            return pd.DataFrame(columns=columns)
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame(columns=columns)

    def get(self, symbol, timeframe, start_ms, end_ms):
        path = self._ohlcv_path(symbol, timeframe)
        cached = self._read(path, ["ts", "open", "high", "low", "close", "volume"])
        if len(cached):
            have_start, have_end = int(cached["ts"].min()), int(cached["ts"].max())
        else:
            have_start, have_end = None, None

        frames = [cached] if len(cached) else []
        tf_ms = tf_to_ms(timeframe)

        if have_start is None or start_ms < have_start - tf_ms:
            fetch_end = (have_start - 1) if have_start else end_ms
            if fetch_end > start_ms:
                try:
                    df = self.provider.fetch_ohlcv(symbol, timeframe, start_ms, fetch_end)
                    if len(df):
                        frames.append(df)
                except Exception as e:
                    print(f"[data] fetch {symbol} {timeframe} failed: {e}")

        if have_end is None or end_ms > have_end + tf_ms:
            fetch_start = (have_end + 1) if have_end else start_ms
            if end_ms > fetch_start:
                try:
                    df = self.provider.fetch_ohlcv(symbol, timeframe, fetch_start, end_ms)
                    if len(df):
                        frames.append(df)
                except Exception as e:
                    print(f"[data] fetch {symbol} {timeframe} failed: {e}")

        combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume"])
        combined = combined.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        if len(combined) and (have_start is None or len(frames) > 1):
            combined.to_csv(path, index=False)
        return combined[(combined["ts"] >= start_ms) & (combined["ts"] <= end_ms)].reset_index(drop=True)

    def get_funding(self, symbol, start_ms, end_ms):
        path = self._funding_path(symbol)
        cached = self._read(path, ["ts", "rate"])
        try:
            fresh = self.provider.fetch_funding(symbol, start_ms, end_ms)
        except Exception as e:
            print(f"[data] funding {symbol} failed: {e}")
            fresh = pd.DataFrame(columns=["ts", "rate"])
        combined = pd.concat([cached, fresh], ignore_index=True)
        combined = combined.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        if len(combined):
            combined.to_csv(path, index=False)
        return combined[(combined["ts"] >= start_ms) & (combined["ts"] <= end_ms)].reset_index(drop=True)

def make_store(config):
    name = config["data"]["exchange"]
    cache_dir = config["data"]["cache_dir"]
    if name == "bybit_linear":
        provider = BybitLinearProvider()
    elif name == "binance_futures":
        provider = BinanceFuturesProvider()
    elif name == "local":
        provider = LocalProvider(cache_dir)
    else:
        raise ValueError(f"Unknown exchange: {name!r}")
    return CandleStore(provider, cache_dir)
