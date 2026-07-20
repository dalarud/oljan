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


class AlphaVantageProvider:
    """Real Brent/WTI prices from Alpha Vantage (EIA-sourced).

    IMPORTANT: the commodity endpoints (BRENT/WTI) are DAILY/weekly/monthly
    only — there is no intraday for the oil benchmarks — and the free key is
    limited to ~25 calls/day. So this serves the daily series (real, correct
    numbers) and returns empty for intraday intervals WITHOUT making a call
    (to avoid wasting quota). Use it as the daily source / fallback; keep an
    intraday-capable provider (Yahoo) primary for 1m/5m/15m.
    """
    name = "alphavantage"
    URL = "https://www.alphavantage.co/query"
    _FUNc = {"BZ=F": "BRENT", "CL=F": "WTI"}

    def __init__(self, api_key: str, timeout: int = 25):
        self.api_key = api_key
        self.timeout = timeout

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        func = self._FUNc.get(symbol)
        av_interval = {"1d": "daily", "1day": "daily", "daily": "daily",
                       "1wk": "weekly", "1mo": "monthly"}.get(interval)
        # No intraday for oil benchmarks + don't burn quota on unsupported calls.
        if not func or not av_interval or not self.api_key:
            return pd.DataFrame(columns=_EMPTY)
        try:
            resp = requests.get(self.URL, params={
                "function": func, "interval": av_interval,
                "apikey": self.api_key}, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log.warning("AlphaVantage fetch %s failed: %s", symbol, e)
            return pd.DataFrame(columns=_EMPTY)
        data = payload.get("data")
        if not data:
            note = payload.get("Information") or payload.get("Note") or ""
            if note:
                log.warning("AlphaVantage limited/blocked: %s", str(note)[:120])
            return pd.DataFrame(columns=_EMPTY)
        rows = []
        for r in data:
            try:
                v = float(r["value"])
            except (KeyError, ValueError, TypeError):
                continue
            rows.append((pd.Timestamp(r["date"], tz="UTC"), v))
        if not rows:
            return pd.DataFrame(columns=_EMPTY)
        rows.sort(key=lambda x: x[0])
        idx = [t for t, _ in rows]
        vals = [v for _, v in rows]
        # Commodity series is close-only; OHLC collapse to the close.
        df = pd.DataFrame({"open": vals, "high": vals, "low": vals,
                           "close": vals, "volume": [0.0] * len(vals)},
                          index=pd.DatetimeIndex(idx))
        secs = parse_lookback(lookback)
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=secs)
        return df[df.index >= cutoff] if len(df) > 30 else df


class ChainProvider:
    """Try providers in order; return the first non-empty result."""
    name = "chain"

    def __init__(self, providers: list):
        self.providers = providers
        self.name = "+".join(p.name for p in providers)

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        for p in self.providers:
            df = p.fetch(symbol, interval, lookback)
            if df is not None and not df.empty:
                return df
        return pd.DataFrame(columns=_EMPTY)


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


def _build_one(name: str, cfg):
    name = (name or "").lower()
    if name == "yfinance":
        return YFinanceProvider()
    if name == "alphavantage":
        return AlphaVantageProvider(cfg.secret("ALPHAVANTAGE_API_KEY"))
    if name == "yahoo":
        return YahooChartProvider(
            retries=int(cfg.get("market_data.max_retries", 4)),
            timeout=int(cfg.get("market_data.request_timeout", 20)))
    return None


def get_provider(cfg):
    names = [cfg.get("market_data.provider", "yahoo")]
    fb = cfg.get("market_data.fallback_provider", None)
    if fb:
        names.append(fb)
    providers = [p for p in (_build_one(n, cfg) for n in names) if p]
    if not providers:
        providers = [YahooChartProvider()]
    return providers[0] if len(providers) == 1 else ChainProvider(providers)
