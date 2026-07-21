"""Technical indicators and support/resistance detection.

Deliberately simple and interpretable (per the design brief): standard,
well-understood indicators computed transparently, plus a swing-pivot
support/resistance detector with level clustering. No fitted/black-box models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class ChartContext:
    symbol: str
    price: float
    trend: str                    # up | down | sideways
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    atr: float
    atr_pct: float                # ATR as % of price
    rel_volume: float             # last volume / average volume
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    dist_to_support_pct: Optional[float] = None
    dist_to_resistance_pct: Optional[float] = None
    n_candles: int = 0
    timeframe: str = ""           # interval the context was computed from
    source: str = ""              # data provider that served the candles
    last_candle_age_min: float = 0.0
    price_sane: bool = True        # price within a plausible oil band

    def rsi_state(self) -> str:
        if self.rsi >= 70:
            return "overbought"
        if self.rsi <= 30:
            return "oversold"
        return "neutral"


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    # No losses in the window => pure uptrend => RSI 100 (not 50).
    out = out.mask(avg_loss == 0, 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return out.fillna(50.0)  # warmup period defaults to neutral


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    return mid + num_std * std, mid, mid - num_std * std


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _swing_pivots(df: pd.DataFrame, width: int) -> tuple[list[float], list[float]]:
    """Return (resistance_pivots, support_pivots) using local swing highs/lows."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    res, sup = [], []
    for i in range(width, n - width):
        window_h = highs[i - width:i + width + 1]
        window_l = lows[i - width:i + width + 1]
        if highs[i] == window_h.max():
            res.append(float(highs[i]))
        if lows[i] == window_l.min():
            sup.append(float(lows[i]))
    return res, sup


def _cluster(levels: list[float], cluster_pct: float) -> list[float]:
    """Merge levels within cluster_pct of each other into their mean."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters: list[list[float]] = [[levels[0]]]
    for lvl in levels[1:]:
        anchor = clusters[-1][0]
        if abs(lvl - anchor) / anchor * 100.0 <= cluster_pct:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return [float(np.mean(c)) for c in clusters]


def support_resistance(df: pd.DataFrame, price: float, lookback: int = 300,
                       width: int = 3, cluster_pct: float = 0.5):
    sub = df.tail(lookback)
    res_pivots, sup_pivots = _swing_pivots(sub, width)
    res_levels = _cluster(res_pivots, cluster_pct)
    sup_levels = _cluster(sup_pivots, cluster_pct)
    supports = sorted([l for l in sup_levels if l < price], reverse=True)
    resistances = sorted([l for l in res_levels if l > price])
    return supports, resistances


def compute(df: pd.DataFrame, symbol: str, cfg,
            timeframe: str = "") -> ChartContext:
    """Compute the full chart context for the latest candle."""
    i = cfg.get
    ema_fast_p = i("indicators.ema_fast", 12)
    ema_slow_p = i("indicators.ema_slow", 26)
    rsi_p = i("indicators.rsi_period", 14)
    atr_p = i("indicators.atr_period", 14)
    bb_p = i("indicators.bb_period", 20)
    bb_std = i("indicators.bb_std", 2.0)
    vol_p = i("indicators.volume_avg_period", 20)
    sr_lookback = i("indicators.sr_lookback", 300)
    sr_width = i("indicators.sr_pivot_width", 3)
    sr_cluster = i("indicators.sr_cluster_pct", 0.5)

    close = df["close"]
    price = float(close.iloc[-1])

    # Data validation: candle freshness + price sanity (oil ~ $10-300/bbl).
    try:
        last_ts = df.index[-1]
        if getattr(last_ts, "tzinfo", None) is None:
            last_ts = last_ts.tz_localize("UTC")
        age_min = max((datetime.now(timezone.utc) - last_ts.to_pydatetime())
                      .total_seconds() / 60.0, 0.0)
    except Exception:
        age_min = 0.0
    price_sane = 10.0 < price < 300.0

    ef = ema(close, ema_fast_p)
    es = ema(close, ema_slow_p)
    r = rsi(close, rsi_p)
    macd_line, signal_line, hist = macd(close, ema_fast_p, ema_slow_p, 9)
    bb_u, bb_m, bb_l = bollinger(close, bb_p, bb_std)
    a = atr(df, atr_p)

    atr_val = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else 0.0
    vol = df["volume"].fillna(0.0)
    avg_vol = float(vol.tail(vol_p).mean()) or 0.0
    rel_vol = float(vol.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    # trend: EMA relationship + slope of the fast EMA
    ef_last, es_last = float(ef.iloc[-1]), float(es.iloc[-1])
    slope = float(ef.iloc[-1] - ef.iloc[min(len(ef) - 1, max(0, len(ef) - 5))])
    if ef_last > es_last and slope > 0:
        trend = "up"
    elif ef_last < es_last and slope < 0:
        trend = "down"
    else:
        trend = "sideways"

    supports, resistances = support_resistance(
        df, price, sr_lookback, sr_width, sr_cluster)
    nearest_sup = supports[0] if supports else None
    nearest_res = resistances[0] if resistances else None
    dist_sup = ((price - nearest_sup) / price * 100.0
                if nearest_sup else None)
    dist_res = ((nearest_res - price) / price * 100.0
                if nearest_res else None)

    return ChartContext(
        symbol=symbol,
        price=price,
        trend=trend,
        ema_fast=ef_last,
        ema_slow=es_last,
        rsi=float(r.iloc[-1]),
        macd=float(macd_line.iloc[-1]),
        macd_signal=float(signal_line.iloc[-1]),
        macd_hist=float(hist.iloc[-1]),
        bb_upper=float(bb_u.iloc[-1]) if not np.isnan(bb_u.iloc[-1]) else price,
        bb_mid=float(bb_m.iloc[-1]) if not np.isnan(bb_m.iloc[-1]) else price,
        bb_lower=float(bb_l.iloc[-1]) if not np.isnan(bb_l.iloc[-1]) else price,
        atr=atr_val,
        atr_pct=(atr_val / price * 100.0) if price else 0.0,
        rel_volume=rel_vol,
        supports=supports[:5],
        resistances=resistances[:5],
        nearest_support=nearest_sup,
        nearest_resistance=nearest_res,
        dist_to_support_pct=dist_sup,
        dist_to_resistance_pct=dist_res,
        n_candles=len(df),
        timeframe=timeframe,
        last_candle_age_min=round(age_min, 1),
        price_sane=price_sane,
    )
