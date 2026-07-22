"""Proactive trade-setup monitor.

News alerts tell you the story; this tells you when YOUR entry condition
actually triggers. For a mean-reversion RSI trader the trigger is not the
extreme itself but the RECLAIM — RSI pushing back through the threshold from
oversold/overbought, right at a key level.

This version is rebuilt on a backtest of stored Brent candles rather than on
intuition. Three findings drove the rules (see oiltrader/backtest.py):

  1. A fixed 30/70 reclaim is *structurally counter-trend*: in a trending
     market RSI only reaches the fixed line during the move against your
     trade. On the sample it had negative edge (~-0.17R). So we use ADAPTIVE
     thresholds (the recent RSI range from the chart) — a reclaim is then a
     genuine pullback-and-resume, not a knife-catch.

  2. A reclaim alone is not confirmation. Requiring a REJECTION candle (price
     snapping back off the level on the trigger bar) flipped expectancy
     positive across every tested exit.

  3. Fading rallies (short reclaims) lost badly (~-0.5R) at every horizon,
     while buying dips (long reclaims) was positive. The user's two losing
     trades were exactly this — shorting into a support level. So shorts are
     OFF by default (setups.allow_shorts) and, when enabled, demand a real
     downtrend plus confirmation and are always flagged low-confidence.

State (previous RSI) is kept per symbol between checks, so a reclaim is
detected as RSI crosses back across the (now adaptive) threshold — this also
works across stateless scheduled runs via storage meta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("oljan.setups")

# Rejection-candle gate: for a long the trigger bar must close in the upper
# part of its range (buyers rejected the low); mirror for a short.
_REJECT_LONG = 0.60      # close_pos >= this
_REJECT_SHORT = 0.40     # close_pos <= this


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
    quality: int = 0          # 0-100 confluence score
    reasons: tuple = ()       # short Swedish bullets explaining the grade

    def dedup_key(self) -> str:
        return f"setup:{self.side}"


class SetupMonitor:
    def __init__(self, cfg, storage=None):
        prof = cfg.get("trader_profile", {}) or {}
        self.style = str(prof.get("style", "")).lower()
        # Fixed thresholds are now only a fallback; adaptive ones come from the
        # chart context. Kept for configs/charts that don't supply them.
        self.ob = float(prof.get("rsi_overbought", 70))
        self.os = float(prof.get("rsi_oversold", 30))
        self.enabled = bool(cfg.get("setups.enabled", True)) \
            and self.style == "mean_reversion"
        self.near_pct = float(cfg.get("setups.level_proximity_pct", 0.35)) / 100.0
        # Shorts backtested to negative edge; keep them off unless explicitly
        # allowed, and then only with-downtrend + confirmation.
        self.allow_shorts = bool(cfg.get("setups.allow_shorts", False))
        # Minimum confluence quality (0-100) before an alert fires.
        self.min_quality = int(cfg.get("setups.min_quality", 55))
        self.require_rejection = bool(cfg.get("setups.require_rejection", True))
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
        """Update state and return a Setup if a *confirmed* entry triggered."""
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
        close_pos = float(getattr(chart, "bar_close_pos", 0.5))
        os_t = float(getattr(chart, "rsi_os_dyn", self.os) or self.os)
        ob_t = float(getattr(chart, "rsi_ob_dyn", self.ob) or self.ob)
        piv = None
        if levels:
            piv = levels.vwap or getattr(levels, "pdc", None)

        # ---- oversold reclaim -> long reversion (the edge side) ----------
        if prev <= os_t < rsi:
            lvl = self._nearest(levels, chart, up=False, price=price)
            if not self._near(price, lvl, self.near_pct * 2):
                return None
            if self.require_rejection and close_pos < _REJECT_LONG:
                return None  # no snap-back off the low -> not confirmed
            with_trend = trend in ("up", "range")
            conflict = news_bias < -0.25
            target = piv if (piv and piv > price) else \
                self._val(self._nearest(levels, chart, up=True, price=price))
            base = self._val(lvl) or price
            stop = round(base - 0.5 * atr, 2) if atr else round(base * 0.997, 2)
            q, reasons = self._quality("long", with_trend, conflict, close_pos,
                                       rsi, os_t, ob_t)
            if q < self.min_quality:
                return None
            return Setup("long", "RSI-reclaim från översålt (bekräftad)", price,
                         self._val(lvl), self._lbl(lvl), target, stop, trend,
                         with_trend, conflict,
                         self._conf(with_trend, conflict, q), prev, rsi,
                         quality=q, reasons=reasons)

        # ---- overbought reclaim -> short reversion (negative-edge side) --
        if prev >= ob_t > rsi:
            if not self.allow_shorts:
                return None  # backtest: shorting reclaims loses; off by default
            # only a genuine downtrend, never a range, and confirmed
            if trend != "down":
                return None
            lvl = self._nearest(levels, chart, up=True, price=price)
            if not self._near(price, lvl, self.near_pct * 2):
                return None
            if self.require_rejection and close_pos > _REJECT_SHORT:
                return None
            conflict = news_bias > 0.25
            target = piv if (piv and piv < price) else \
                self._val(self._nearest(levels, chart, up=False, price=price))
            base = self._val(lvl) or price
            stop = round(base + 0.5 * atr, 2) if atr else round(base * 1.003, 2)
            q, reasons = self._quality("short", True, conflict, close_pos,
                                       rsi, os_t, ob_t)
            if q < self.min_quality:
                return None
            return Setup("short", "RSI-reclaim från överköpt (bekräftad)", price,
                         self._val(lvl), self._lbl(lvl), target, stop, trend,
                         True, conflict, "försiktig", prev, rsi,
                         quality=q, reasons=reasons)
        return None

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _quality(side, with_trend, conflict, close_pos, rsi, os_t, ob_t):
        """0-100 confluence score with a short reason list."""
        score = 40
        reasons = []
        if with_trend:
            score += 25
            reasons.append("med trend (köp-dipp/sälj-rip)")
        else:
            reasons.append("MOT trend – snålt mål, mindre storlek")
        # strength of the rejection candle
        rej = close_pos if side == "long" else (1.0 - close_pos)
        if rej >= 0.75:
            score += 20
            reasons.append("stark avvisningsstake")
        elif rej >= 0.60:
            score += 10
            reasons.append("avvisningsstake")
        if conflict:
            score -= 25
            reasons.append("färsk motstridig rubrik – momentum emot")
        else:
            score += 10
        return max(0, min(100, score)), tuple(reasons)

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
    def _conf(with_trend: bool, conflict: bool, quality: int) -> str:
        if conflict or quality < 55:
            return "försiktig"
        return "stark" if (with_trend and quality >= 75) else "måttlig"


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
        f"({wt}) · kvalitet {s.quality}/100",
        f"🎯 Mål {tgt} · 🛑 stopp {stop} · signal: *{s.confidence}*",
    ]
    if s.reasons:
        lines.append("• " + " · ".join(s.reasons))
    if s.side == "short":
        lines.append("⚠️ Short-reversion har historiskt svag edge på detta "
                     "instrument – liten storlek, tajt stopp.")
    if s.news_conflict:
        lines.append("⚠️ Färsk motstridig rubrik – momentum mot traden; vänta "
                     "på att den smälts eller ta mindre.")
    if not s.with_trend:
        lines.append("↩️ Mottrendsreversion – snålt mål, tajt stopp, mindre "
                     "storlek.")
    lines.append("_Villkoret slog in nu. Beslutsstöd, ej rådgivning._")
    return "\n".join(lines)
