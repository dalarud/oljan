"""Market-data providers.

Primary provider talks directly to Yahoo Finance's public, keyless chart JSON
endpoint using `requests`. This is deliberately dependency-light and robust:
it needs no API key, respects standard proxy/CA environment variables, and
avoids the fragile curl_cffi stack that newer yfinance versions rely on.

An optional `yfinance` provider is kept as a fallback if that package is
installed and you prefer it.

All providers return a tz-aware (UTC) DataFrame with lowercase columns:
open, high, low, close, volume.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

log = logging.getLogger("oljan.providers")

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# Yahoo intraday range caps (approx) – used to clamp requested lookbacks.
_MAX_LOOKBACK_SECONDS = {
    "1m": 7 * 86400,
    "2m": 60 * 86400,
    "5m": 60 * 86400,
    "15m": 60 * 86400,
    "30m": 60 * 86400,
    "60m": 730 * 86400,
    "1h": 730 * 86400,
    "1d": 3650 * 86400,
}

_EMPTY = ["open", "high", "low", "close", "volume"]


def parse_lookback(s: str) -> int:
    """'7d' / '60d' / '180d' / '5y' / '12h' -> seconds."""
    s = str(s).strip().lower()
    num = "".join(c for c in s if c.isdigit() or c == ".")
    unit = "".join(c for c in s if c.isalpha()) or "d"
    val = float(num) if num else 30
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800,
            "y": 31536000, "mo": 2592000}.get(unit, 86400)
    return int(val * mult)


class YahooChartProvider:
    name = "yahoo"
    HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    PATH = "/v8/finance/chart/{symbol}"

    def __init__(self, retries: int = 4, timeout: int = 20):
        self.retries = retries
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _UA,
                                      "Accept": "application/json"})

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        yint = "60m" if interval == "1h" else interval
        secs = min(parse_lookback(lookback),
                   _MAX_LOOKBACK_SECONDS.get(yint, 60 * 86400))
        now = int(datetime.now(timezone.utc).timestamp())
        params = {
            "period1": now - secs,
            "period2": now,
            "interval": yint,
            "includePrePost": "false",
            "events": "div,splits",
        }

        last_err: Exception | None = None
        for attempt in range(self.retries):
            # Rotate hosts across attempts (helps with transient per-host 429s).
            host = self.HOSTS[attempt % len(self.HOSTS)]
            url = f"https://{host}" + self.PATH.format(symbol=symbol)
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    raise RuntimeError("Yahoo rate-limited (429)")
                resp.raise_for_status()
                return self._parse(resp.json())
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("Yahoo fetch %s %s via %s failed (attempt %d/%d): "
                            "%s; retry in %ds", symbol, yint, host, attempt + 1,
                            self.retries, str(e)[:120], wait)
                time.sleep(wait)
        log.error("Yahoo fetch giving up for %s %s: %s", symbol, yint, last_err)
        return pd.DataFrame(columns=_EMPTY)

    @staticmethod
    def _parse(payload: dict) -> pd.DataFrame:
        chart = (payload or {}).get("chart", {})
        if chart.get("error"):
            raise RuntimeError(str(chart["error"]))
        results = chart.get("result") or []
        if not results:
            return pd.DataFrame(columns=_EMPTY)
        res = results[0]
        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        if not ts or not quote:
            return pd.DataFrame(columns=_EMPTY)
        df = pd.DataFrame({
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        })
        df.index = pd.to_datetime(ts, unit="s", utc=True)
        df = df.dropna(subset=["close"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df


class YFinanceProvider:
    """Optional fallback using the yfinance package (if installed)."""
    name = "yfinance"

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        import yfinance as yf
        yint = "60m" if interval == "1h" else interval
        secs = min(parse_lookback(lookback),
                   _MAX_LOOKBACK_SECONDS.get(yint, 60 * 86400))
        days = max(1, secs // 86400)
        period = f"{days}d"
        df = yf.download(symbol, period=period, interval=yint,
                         auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty:
            return pd.DataFrame(columns=_EMPTY)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        df = df[[c for c in _EMPTY if c in df.columns]].copy()
        idx = pd.to_datetime(df.index)
        df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return df.dropna(subset=["close"]).sort_index()


def get_provider(cfg):
    name = str(cfg.get("market_data.provider", "yahoo")).lower()
    if name == "yfinance":
        return YFinanceProvider()
    return YahooChartProvider(
        retries=int(cfg.get("market_data.max_retries", 4)),
        timeout=int(cfg.get("market_data.request_timeout", 20)),
    )
