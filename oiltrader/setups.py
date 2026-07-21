"""Proactive trade-setup monitor.

News alerts tell you the story; this tells you when YOUR entry condition
actually triggers. For a mean-reversion RSI trader the trigger is not the
extreme itself but the RECLAIM — RSI pushing back through the threshold from
oversold/overbought, ideally right at a key level. When that happens on the
live chart the daemon fires a concise, actionable SETUP alert.

It is regime/trend-aware (a with-trend reclaim is a stronger signal than a
counter-trend one) and news-aware (a reclaim fighting a fresh, opposite-
direction headline is flagged, because that is momentum pushing against your
mean-reversion). State (previous RSI) is kept per symbol between checks, so a
reclaim is detected as RSI crosses back across the threshold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("oljan.setups")


@dataclass
class Setup:
    side: str                 # "long" | "short"
    kind: str
    price: float
    level: Optional[float]
    level_label: str
    target: Optional[float]
    stop: Optional[float]
    trend: str
    with_trend: bool
    news_conflict: bool
    confidence: str           # stark | måttlig | försiktig
    rsi_prev: float
    rsi_now: float

    def dedup_key(self) -> str:
        return f"setup:{self.side}"


class SetupMonitor:
    def __init__(self, cfg, storage=None):
        prof = cfg.get("trader_profile", {}) or {}
        self.style = str(prof.get("style", "")).lower()
        self.ob = float(prof.get("rsi_overbought", 70))
        self.os = float(prof.get("rsi_oversold", 30))
        self.enabled = bool(cfg.get("setups.enabled", True)) \
            and self.style == "mean_reversion"
        self.near_pct = float(cfg.get("setups.level_proximity_pct", 0.35)) / 100.0
        # Previous RSI persists in storage when available, so reclaim detection
        # also works across stateless runs (e.g. a scheduled GitHub Action).
        self.storage = storage
        self._last_rsi: dict[str, float] = {}

    def _get_last(self, symbol: str) -> Optional[float]:
        if self.storage is not None:
            v = self.storage.get_meta(f"setup_rsi:{symbol}")
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return self._last_rsi.get(symbol)

    def _set_last(self, symbol: str, rsi: float) -> None:
        if self.storage is not None:
            self.storage.set_meta(f"setup_rsi:{symbol}", f"{rsi:.2f}")
        else:
            self._last_rsi[symbol] = rsi

    def update_and_detect(self, symbol: str, chart, levels, trend: str,
                          news_bias: float = 0.0) -> Optional[Setup]:
        """Update state and return a Setup if an entry just triggered."""
        if not self.enabled or chart is None:
            return None
        rsi = getattr(chart, "rsi", None)
        prev = self._get_last(symbol)
        if rsi is not None:
            self._set_last(symbol, rsi)
        if prev is None or rsi is None:
            return None

        price = float(chart.price)
        atr = float(getattr(chart, "atr", 0) or 0)
        piv = None
        if levels:
            piv = levels.vwap or getattr(levels, "pdc", None)

        # ---- oversold reclaim -> long reversion --------------------------
        if prev <= self.os < rsi:
            lvl = self._nearest(levels, chart, up=False, price=price)
            if not self._near(price, lvl, self.near_pct * 2):
                return None
            target = piv if (piv and piv > price) else \
                self._val(self._nearest(levels, chart, up=True, price=price))
            base = self._val(lvl) or price
            stop = round(base - 0.5 * atr, 2) if atr else round(base * 0.997, 2)
            with_trend = trend in ("up", "range")
            conflict = news_bias < -0.25       # fresh bearish momentum vs a buy
            return Setup("long", "RSI-reclaim från översålt", price,
                         self._val(lvl), self._lbl(lvl), target, stop, trend,
                         with_trend, conflict,
                         self._conf(with_trend, conflict), prev, rsi)

        # ---- overbought reclaim -> short reversion -----------------------
        if prev >= self.ob > rsi:
            lvl = self._nearest(levels, chart, up=True, price=price)
            if not self._near(price, lvl, self.near_pct * 2):
                return None
            target = piv if (piv and piv < price) else \
                self._val(self._nearest(levels, chart, up=False, price=price))
            base = self._val(lvl) or price
            stop = round(base + 0.5 * atr, 2) if atr else round(base * 1.003, 2)
            with_trend = trend in ("down", "range")
            conflict = news_bias > 0.25        # fresh bullish momentum vs a sell
            return Setup("short", "RSI-reclaim från överköpt", price,
                         self._val(lvl), self._lbl(lvl), target, stop, trend,
                         with_trend, conflict,
                         self._conf(with_trend, conflict), prev, rsi)
        return None

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _nearest(levels, chart, up: bool, price: float):
        if levels:
            seq = levels.resistances_above() if up else levels.supports_below()
            if seq:
                return seq[0]
        v = (getattr(chart, "nearest_resistance", None) if up
             else getattr(chart, "nearest_support", None))
        return ("nivå", v) if v else None

    @staticmethod
    def _val(lvl):
        return lvl[1] if lvl else None

    @staticmethod
    def _lbl(lvl):
        return lvl[0] if lvl else "nivå"

    @staticmethod
    def _near(price, lvl, band) -> bool:
        v = lvl[1] if lvl else None
        return v is not None and abs(price - v) / price <= band

    @staticmethod
    def _conf(with_trend: bool, conflict: bool) -> str:
        if conflict:
            return "försiktig"
        return "stark" if with_trend else "måttlig"


def format_setup(s: Setup, name: str, disp=lambda v: v) -> str:
    """Concise, actionable Swedish alert for a triggered setup."""
    side = "KÖP" if s.side == "long" else "SÄLJ"
    wt = "med trenden" if s.with_trend else "MOT trenden (motvind)"
    lvl = f"{s.level_label} {disp(s.level):.2f}" if s.level is not None else "nivå"
    tgt = f"{disp(s.target):.2f}" if s.target is not None else "medel/VWAP"
    stop = f"{disp(s.stop):.2f}" if s.stop is not None else "n/a"
    lines = [
        f"⚡ *SETUP {side} · {s.kind}* – {name} {disp(s.price):.2f}",
        f"RSI {s.rsi_prev:.0f}→{s.rsi_now:.0f} vid {lvl} · trend {s.trend} "
        f"({wt})",
        f"🎯 Mål {tgt} · 🛑 stopp {stop} · signal: *{s.confidence}*",
    ]
    if s.news_conflict:
        lines.append("⚠️ Färsk motstridig rubrik – momentum mot traden; vänta "
                     "på att den smälts eller ta mindre.")
    if not s.with_trend:
        lines.append("↩️ Mottrendsreversion – snålt mål, tajt stopp, mindre "
                     "storlek.")
    lines.append("_Villkoret slog in nu. Beslutsstöd, ej rådgivning._")
    return "\n".join(lines)
