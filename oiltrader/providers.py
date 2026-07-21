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


class TwelveDataProvider:
    """Twelve Data (free tier). Real OHLC intraday.

    The free plan does NOT expose Brent/WTI futures intraday (paywalled), but
    it does serve the oil ETFs (BNO≈Brent, USO≈WTI) intraday. Those track the
    benchmark in % but not in absolute $/barrel, so with scale_to_benchmark we
    anchor the ETF's intraday series to the benchmark's real latest daily close
    (from an anchor provider, e.g. Alpha Vantage) — giving a correct absolute
    level plus real intraday structure. It is an ESTIMATE (ETF tracking error),
    labelled as such in the notification.
    """
    name = "twelvedata"
    URL = "https://api.twelvedata.com/time_series"
    _IV = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
           "1h": "1h", "1d": "1day"}
    _IV_SECS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800,
                "1h": 3600, "1day": 86400}

    def __init__(self, api_key: str, symbol_map: dict | None = None,
                 scale_to_benchmark: bool = False, anchor=None, timeout: int = 20,
                 scale_override: dict | None = None):
        self.api_key = api_key
        self.symbol_map = symbol_map or {"BZ=F": "BNO", "CL=F": "USO"}
        self.scale = scale_to_benchmark
        self.anchor = anchor
        self.timeout = timeout
        # Manual per-symbol ETF->benchmark factor. When set it OVERRIDES the
        # Alpha Vantage anchor — used when the user calibrates to a real feed
        # (e.g. their TradingView UKOIL), which beats a stale free daily close.
        self.scale_override = {k: float(v) for k, v in
                               (scale_override or {}).items()}
        self._anchor_cache: dict = {}
        self.last_source = self.name

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        td = self.symbol_map.get(symbol)
        iv = self._IV.get(interval)
        if not td or not iv or not self.api_key:
            return pd.DataFrame(columns=_EMPTY)
        # As a SCALED intraday proxy the ETF's absolute daily level is wrong
        # (e.g. BNO ≈ $49 vs Brent ≈ $86). Decline daily requests so the chain
        # falls through to a real daily source (Alpha Vantage). Intraday scaling
        # still works — it uses _raw_fetch("1day") internally, not this path.
        if interval == "1d" and self.scale:
            return pd.DataFrame(columns=_EMPTY)
        n = min(int(parse_lookback(lookback) / self._IV_SECS[iv]) + 5, 5000)
        df = self._raw_fetch(td, iv, n)
        if df.empty:
            return df
        self.last_source = self.name
        # Scale intraday ETF -> benchmark level using a SMOOTHED daily ratio
        # (median of benchmark_close/etf_close over recent days), which is far
        # more robust than dividing by a single noisy intraday tick.
        if self.scale and self.anchor and interval != "1d":
            f = self._scale_factor(symbol)
            if f:
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] * f
                self.last_source = f"twelvedata-scaled({td}→benchmark)"
        return df

    def _raw_fetch(self, td: str, iv: str, n: int) -> pd.DataFrame:
        try:
            resp = requests.get(self.URL, params={
                "symbol": td, "interval": iv, "outputsize": n,
                "timezone": "UTC", "apikey": self.api_key}, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log.warning("TwelveData fetch %s (%s) failed: %s", td, iv, e)
            return pd.DataFrame(columns=_EMPTY)
        vals = payload.get("values")
        if not vals:
            log.warning("TwelveData %s: %s", td,
                        str(payload.get("message") or payload)[:120])
            return pd.DataFrame(columns=_EMPTY)
        rows = []
        for v in vals:
            try:
                rows.append((pd.Timestamp(v["datetime"], tz="UTC"),
                             float(v["open"]), float(v["high"]),
                             float(v["low"]), float(v["close"]),
                             float(v.get("volume", 0) or 0)))
            except (KeyError, ValueError, TypeError):
                continue
        if not rows:
            return pd.DataFrame(columns=_EMPTY)
        rows.sort(key=lambda x: x[0])
        return pd.DataFrame(
            {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
             "low": [r[3] for r in rows], "close": [r[4] for r in rows],
             "volume": [r[5] for r in rows]},
            index=pd.DatetimeIndex([r[0] for r in rows]))

    def _scale_factor(self, symbol: str):
        """ETF->benchmark factor anchored to the LATEST aligned daily close.

        The daily close is authoritative (not a noisy tick), so anchoring to the
        most recent aligned benchmark/ETF close ratio keeps the scaled intraday
        level correct in a trending week. A single bad print is guarded against
        by rejecting a >5% deviation from the recent 3-day median in its favour.
        (A plain 7-day median was smooth but LAGGED the level — e.g. anchoring
        intraday to ~86 while Brent had already fallen to ~82.) Cached per day.
        """
        import datetime as _dt
        import statistics
        # A calibrated manual factor wins outright (real user anchor > stale feed).
        if symbol in self.scale_override:
            return self.scale_override[symbol]
        key = (symbol, _dt.date.today().isoformat())
        if key in self._anchor_cache:
            return self._anchor_cache[key]
        factor = None
        try:
            td = self.symbol_map[symbol]
            etf_daily = self._raw_fetch(td, "1day", 30)
            bench_daily = self.anchor.fetch(symbol, "1d", "45d")
            if not etf_daily.empty and not bench_daily.empty:
                etf_by_date = {ts.date(): c for ts, c in
                               zip(etf_daily.index, etf_daily["close"])}
                ratios = []
                for ts, c in zip(bench_daily.index, bench_daily["close"]):
                    e = etf_by_date.get(ts.date())
                    if e and e > 0 and c and c > 0:
                        ratios.append(c / e)
                ratios = ratios[-7:]
                if ratios:
                    latest = ratios[-1]
                    ref = statistics.median(ratios[-3:])
                    # track the latest close, but reject an outlier print
                    factor = ref if (ref and abs(latest / ref - 1) > 0.05) \
                        else latest
                    if not (0.1 < factor < 20):   # sanity guard
                        factor = None
        except Exception as e:
            log.warning("scale-factor computation failed: %s", e)
        self._anchor_cache[key] = factor
        return factor


class ChainProvider:
    """Try providers in order; return the first non-empty result."""
    name = "chain"

    def __init__(self, providers: list):
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.last_source = None

    def fetch(self, symbol: str, interval: str, lookback: str) -> pd.DataFrame:
        for p in self.providers:
            df = p.fetch(symbol, interval, lookback)
            if df is not None and not df.empty:
                self.last_source = getattr(p, "last_source", None) or p.name
                return df
        self.last_source = None
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
    if name == "twelvedata":
        scale = bool(cfg.get("market_data.twelvedata_scale_to_benchmark", True))
        anchor = (AlphaVantageProvider(cfg.secret("ALPHAVANTAGE_API_KEY"))
                  if scale else None)
        return TwelveDataProvider(
            cfg.secret("TWELVEDATA_API_KEY"),
            symbol_map=cfg.get("market_data.twelvedata_symbols",
                               {"BZ=F": "BNO", "CL=F": "USO"}),
            scale_to_benchmark=scale, anchor=anchor,
            scale_override=cfg.get("market_data.scale_override", {}) or {})
    if name == "yahoo":
        return YahooChartProvider(
            retries=int(cfg.get("market_data.max_retries", 4)),
            timeout=int(cfg.get("market_data.request_timeout", 20)))
    return None


def get_provider(cfg):
    names = [cfg.get("market_data.provider", "yahoo")]
    fb = cfg.get("market_data.fallback_provider", None)
    if isinstance(fb, list):
        names += fb
    elif fb:
        names.append(fb)
    providers = [p for p in (_build_one(n, cfg) for n in names) if p]
    if not providers:
        providers = [YahooChartProvider()]
    return providers[0] if len(providers) == 1 else ChainProvider(providers)
