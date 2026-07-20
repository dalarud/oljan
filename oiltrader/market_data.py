"""Market data retrieval.

Primary provider is yfinance (free, no API key). Data is normalised to a
tz-aware (UTC) DataFrame with lowercase columns: open, high, low, close,
volume. Fetched candles are persisted via Storage so history accrues over
time (see storage.py).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

log = logging.getLogger("oljan.market_data")


class MarketData:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.interval = cfg.get("market_data.intraday_interval", "15m")
        self.intraday_lookback = cfg.get("market_data.intraday_lookback", "30d")
        self.daily_lookback = cfg.get("market_data.daily_lookback", "5y")

    # ------------------------------------------------------------------ public
    def refresh(self, symbol: str) -> pd.DataFrame:
        """Fetch latest intraday candles and persist them."""
        df = self._download(symbol, self.intraday_lookback, self.interval)
        if not df.empty:
            self.storage.upsert_candles(symbol, self.interval, df)
        return df

    def refresh_daily(self, symbol: str) -> pd.DataFrame:
        df = self._download(symbol, self.daily_lookback, "1d")
        if not df.empty:
            self.storage.upsert_candles(symbol, "1d", df)
        return df

    def get_intraday(self, symbol: str, min_rows: int = 50) -> pd.DataFrame:
        """Return intraday candles, preferring stored history, refreshing if
        stale/insufficient."""
        df = self.storage.get_candles(symbol, self.interval)
        if len(df) < min_rows:
            fresh = self.refresh(symbol)
            if not fresh.empty:
                df = self.storage.get_candles(symbol, self.interval)
        return df

    def last_price(self, symbol: str) -> Optional[float]:
        df = self.storage.get_candles(symbol, self.interval)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    # ---------------------------------------------------------------- internal
    def _download(self, symbol: str, period: str, interval: str,
                  retries: int = 3) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so the package imports cheaply

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                df = yf.download(
                    symbol, period=period, interval=interval,
                    auto_adjust=False, progress=False, threads=False,
                )
                return self._normalise(df)
            except Exception as e:  # network / parsing hiccups
                last_err = e
                wait = 2 ** attempt
                log.warning("yfinance download failed (%s) attempt %d/%d: %s; "
                            "retrying in %ds", symbol, attempt + 1, retries,
                            e, wait)
                time.sleep(wait)
        log.error("yfinance download giving up for %s: %s", symbol, last_err)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"])
        # yfinance sometimes returns MultiIndex columns even for one ticker.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        keep = [c for c in ["open", "high", "low", "close", "volume"]
                if c in df.columns]
        df = df[keep].copy()
        # Ensure a tz-aware UTC index.
        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        df.index = idx
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["close"])
        return df
