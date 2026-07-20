"""Market data retrieval (multi-timeframe, intraday-first).

Fetches candles for each configured timeframe (e.g. 1m/5m/15m/1h) via a
pluggable provider (see providers.py) and persists them so history accrues
over time. Data is tz-aware (UTC) with lowercase columns:
open, high, low, close, volume.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from .providers import get_provider

log = logging.getLogger("oljan.market_data")


class MarketData:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.provider = get_provider(cfg)

        tfs = cfg.get("market_data.timeframes", None)
        if tfs:
            self.timeframes = [(t["interval"], t.get("lookback", "5d"))
                               for t in tfs]
        else:  # backward-compat with the single-interval config
            iv = cfg.get("market_data.intraday_interval", "15m")
            lb = cfg.get("market_data.intraday_lookback", "30d")
            self.timeframes = [(iv, lb)]

        self.intervals = [iv for iv, _ in self.timeframes]
        self.analysis_tf = cfg.get(
            "market_data.analysis_timeframe",
            cfg.get("market_data.intraday_interval", self.intervals[0]))
        if self.analysis_tf not in self.intervals:
            self.analysis_tf = self.intervals[0]
        self.daily_lookback = cfg.get("market_data.daily_lookback", "5y")
        # polite spacing between provider calls to avoid rate limits
        self.request_spacing = float(cfg.get("market_data.request_spacing", 0.8))

    # ------------------------------------------------------------------ public
    def refresh_all(self, symbol: str) -> dict[str, pd.DataFrame]:
        """Fetch + persist every configured timeframe for a symbol."""
        out: dict[str, pd.DataFrame] = {}
        for interval, lookback in self.timeframes:
            df = self._fetch(symbol, interval, lookback)
            if not df.empty:
                self.storage.upsert_candles(symbol, interval, df)
            out[interval] = df
            if self.request_spacing:
                time.sleep(self.request_spacing)
        return out

    def refresh_daily(self, symbol: str) -> pd.DataFrame:
        df = self._fetch(symbol, "1d", self.daily_lookback)
        if not df.empty:
            self.storage.upsert_candles(symbol, "1d", df)
        return df

    def get_candles(self, symbol: str, interval: str,
                    min_rows: int = 40) -> pd.DataFrame:
        """Stored candles for a timeframe, refreshing if too few."""
        df = self.storage.get_candles(symbol, interval)
        if len(df) < min_rows:
            lookback = dict(self.timeframes).get(interval, "5d")
            fresh = self._fetch(symbol, interval, lookback)
            if not fresh.empty:
                self.storage.upsert_candles(symbol, interval, fresh)
                df = self.storage.get_candles(symbol, interval)
        return df

    def last_price(self, symbol: str) -> Optional[float]:
        df = self.storage.get_candles(symbol, self.analysis_tf)
        if df.empty:
            # fall back to any available timeframe
            for iv in self.intervals:
                df = self.storage.get_candles(symbol, iv)
                if not df.empty:
                    break
        return float(df["close"].iloc[-1]) if not df.empty else None

    # ---------------------------------------------------------------- internal
    def _fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        try:
            return self.provider.fetch(symbol, interval, lookback)
        except Exception as e:
            log.error("Provider %s failed for %s %s: %s",
                      getattr(self.provider, "name", "?"), symbol, interval, e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
