"""Canonical intraday reference levels.

Intraday traders act on objective, widely-watched levels — not subjective
fractal pivots. This module computes the levels that actually matter and are
unambiguous from the data:

  * PDH / PDL / PDC  – prior-day high / low / close
  * day high / low   – current session extremes
  * VWAP             – volume-weighted average price (session)
  * round numbers    – nearest psychological levels
  * swing S/R        – kept as a secondary, clearly-labelled source

Everything is derived from real candles; nothing is invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .indicators import support_resistance


@dataclass
class KeyLevels:
    price: float
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    pdc: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    vwap: Optional[float] = None
    swing_supports: list[float] = field(default_factory=list)
    swing_resistances: list[float] = field(default_factory=list)

    def _labeled(self, above: bool) -> list[tuple[str, float]]:
        cand: list[tuple[str, float]] = []
        named = [("PDH", self.pdh), ("PDL", self.pdl), ("PDC", self.pdc),
                 ("dagshögsta", self.day_high), ("dagslägsta", self.day_low),
                 ("VWAP", self.vwap)]
        for label, val in named:
            if val is None:
                continue
            if above and val > self.price * 1.0005:
                cand.append((label, val))
            elif not above and val < self.price * 0.9995:
                cand.append((label, val))
        swings = self.swing_resistances if above else self.swing_supports
        for val in swings:
            cand.append(("nivå", val))
        # round numbers
        rn = _round_levels(self.price, above)
        for val in rn:
            cand.append(("rund", val))
        # dedup within 0.1%, keep the most meaningful label (named first)
        cand.sort(key=lambda x: x[1], reverse=not above)
        out: list[tuple[str, float]] = []
        for label, val in cand:
            if any(abs(val - v) / max(v, 1e-9) < 0.001 for _, v in out):
                continue
            out.append((label, val))
        return out[:3]

    def resistances_above(self) -> list[tuple[str, float]]:
        return self._labeled(above=True)

    def supports_below(self) -> list[tuple[str, float]]:
        return self._labeled(above=False)


def _round_levels(price: float, above: bool) -> list[float]:
    """Nearest whole and half-dollar psychological levels."""
    import math
    out = []
    if above:
        out.append(math.ceil(price))
        half = math.floor(price) + 0.5
        if half > price:
            out.append(half)
    else:
        out.append(math.floor(price))
        half = math.ceil(price) - 0.5
        if half < price:
            out.append(half)
    return out


def _prior_day(daily: pd.DataFrame):
    """Return (high, low, close) of the last COMPLETED prior day, or Nones."""
    if daily is None or daily.empty:
        return None, None, None
    today = datetime.now(timezone.utc).date()
    prior = daily[daily.index.date < today]
    if prior.empty:
        prior = daily.iloc[:-1] if len(daily) > 1 else daily
    if prior.empty:
        return None, None, None
    row = prior.iloc[-1]
    hi = float(row["high"]) if "high" in row and not pd.isna(row["high"]) else None
    lo = float(row["low"]) if "low" in row and not pd.isna(row["low"]) else None
    cl = float(row["close"]) if "close" in row and not pd.isna(row["close"]) else None
    # close-only daily series (e.g. Alpha Vantage) has high==low==close.
    if hi is not None and lo is not None and hi == lo:
        hi = lo = None
    return hi, lo, cl


def _session_vwap(intraday: pd.DataFrame):
    today = datetime.now(timezone.utc).date()
    day = intraday[intraday.index.date == today]
    if day.empty or "volume" not in day or day["volume"].fillna(0).sum() <= 0:
        return None, None, None
    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    vwap = float((typical * day["volume"]).sum() / day["volume"].sum())
    return float(day["high"].max()), float(day["low"].min()), vwap


def compute_levels(intraday: pd.DataFrame, daily: Optional[pd.DataFrame],
                   price: float, cfg) -> KeyLevels:
    pdh, pdl, pdc = _prior_day(daily) if daily is not None else (None, None, None)
    day_high, day_low, vwap = _session_vwap(intraday)
    if day_high is None and not intraday.empty:
        # fall back to last-24h extremes if session detection is empty
        recent = intraday.tail(288)  # ~24h of 5m
        day_high, day_low = float(recent["high"].max()), float(recent["low"].min())
    sup, res = support_resistance(
        intraday, price,
        cfg.get("indicators.sr_lookback", 300),
        cfg.get("indicators.sr_pivot_width", 3),
        cfg.get("indicators.sr_cluster_pct", 0.5))
    return KeyLevels(
        price=price, pdh=pdh, pdl=pdl, pdc=pdc,
        day_high=day_high, day_low=day_low, vwap=vwap,
        swing_supports=sup[:3], swing_resistances=res[:3])
