"""Alert self-evaluation loop.

Every pushed alert makes an implicit directional claim ("bullish", "bearish").
This module records the price at alert time, waits a fixed horizon, then scores
whether price actually moved that way — turning the engine's own output into a
measured, honest track record instead of an unfalsifiable stream of opinions.

It reports a rolling precision scorecard (overall and by conviction bucket) and,
optionally, nudges the live conviction threshold when the band just above it is
demonstrably no better than a coin flip. All tuning is bounded, logged and
announced, never silent.

Anti-lookahead: an alert is scored only once its horizon has fully elapsed in
recorded candles, using price_at(..., "after") on data timestamped after the
alert — the same discipline as the historical event study.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("oljan.evaluator")

# Conviction buckets for the scorecard / tuning.
_BUCKETS = [(0, 39, "0-39"), (40, 59, "40-59"), (60, 79, "60-79"),
            (80, 100, "80-100")]


class AlertEvaluator:
    def __init__(self, cfg, storage, market):
        self.cfg = cfg
        self.storage = storage
        self.market = market
        self.enabled = cfg.get("alert_eval.enabled", True)
        self.horizon_h = float(cfg.get("alert_eval.horizon_hours", 1.0))
        # a move smaller than this (fractional) counts as "no move" -> a miss
        self.deadband = float(cfg.get("alert_eval.deadband_pct", 0.15)) / 100.0
        self.interval = cfg.get(
            "market_data.analysis_timeframe",
            cfg.get("market_data.intraday_interval", "5m"))
        # auto-tuning (opt-in): bounded self-adjustment of min_conviction
        self.auto_tune = cfg.get("alert_eval.auto_tune", False)
        self.tune_min_sample = cfg.get("alert_eval.tune_min_sample", 20)
        self.tune_floor = cfg.get("alert_eval.tune_floor", 30)
        self.tune_ceiling = cfg.get("alert_eval.tune_ceiling", 70)
        self.tune_step = cfg.get("alert_eval.tune_step", 5)
        self.precision_floor = cfg.get("alert_eval.precision_floor", 0.45)

    # -------------------------------------------------------------- recording
    def record(self, analysis, ref_price: Optional[float]) -> None:
        """Log a freshly-pushed alert for later scoring."""
        if not self.enabled:
            return
        ev = analysis.event
        symbol = ev.symbol or self.cfg.primary_instrument.get("symbol")
        try:
            self.storage.insert_alert(
                ts=datetime.now(timezone.utc), event_id=ev.event_id,
                symbol=symbol, category=ev.category, direction=ev.direction,
                conviction=analysis.conviction, ref_price=ref_price,
                horizon_h=self.horizon_h)
        except Exception as e:
            log.warning("failed to record alert for scoring: %s", e)

    # ----------------------------------------------------------------- scoring
    def score_due(self) -> int:
        """Score every pushed alert whose horizon has fully elapsed."""
        if not self.enabled:
            return 0
        now = datetime.now(timezone.utc)
        scored = 0
        for a in self.storage.unscored_alerts():
            ts = datetime.fromtimestamp(a["ts"], tz=timezone.utc)
            target = ts + timedelta(hours=a["horizon_h"])
            if now < target:
                continue  # horizon not reached yet
            symbol = a["symbol"]
            ref = a["ref_price"]
            if ref is None or ref <= 0:
                ref_row = self.storage.price_at(symbol, self.interval, ts, "before")
                ref = ref_row[1] if ref_row else None
            fwd = self.storage.price_at(symbol, self.interval, target, "after")
            if ref is None or ref <= 0 or fwd is None or fwd[1] is None:
                # give up on stale alerts we can never price; otherwise wait
                if now - target > timedelta(hours=self.horizon_h + 12):
                    self.storage.score_alert(a["id"], 0, 0.0)
                    scored += 1
                continue
            fwd_return = (fwd[1] - ref) / ref
            correct = self._is_correct(a["direction"], fwd_return)
            self.storage.score_alert(a["id"], int(correct), fwd_return)
            scored += 1
        if scored:
            log.info("scored %d matured alert(s)", scored)
        return scored

    def _is_correct(self, direction: str, fwd_return: float) -> bool:
        if abs(fwd_return) < self.deadband:
            return False  # essentially flat within the horizon -> not a hit
        if direction == "bullish":
            return fwd_return > 0
        if direction == "bearish":
            return fwd_return < 0
        return False

    # -------------------------------------------------------------- scorecard
    def scorecard(self, lookback_days: int = 14) -> Optional[str]:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = self.storage.alert_stats(since)
        if not rows:
            return None
        overall = self._precision(rows)
        lines = [f"🧪 *Oljan – träffsäkerhet ({lookback_days}d)*",
                 f"Totalt: {overall[1]}/{overall[2]} rätt "
                 f"({overall[0]*100:.0f}%) · horisont {self.horizon_h:g}h"]
        for lo, hi, label in _BUCKETS:
            b = [r for r in rows if lo <= (r["conviction"] or 0) <= hi]
            if not b:
                continue
            p, c, n = self._precision(b)
            avg = sum(r["fwd_return"] or 0.0 for r in b) / n
            lines.append(f"konv {label}: {c}/{n} ({p*100:.0f}%) · "
                         f"snittavkastning {avg*100:+.2f}%")
        lines.append("_Riktningsträff mot faktisk prisrörelse efter alarmet. "
                     "Beslutsstöd, ej facit._")
        return "\n".join(lines)

    @staticmethod
    def _precision(rows) -> tuple[float, int, int]:
        n = len(rows)
        c = sum(1 for r in rows if r["correct"])
        return (c / n if n else 0.0, c, n)

    # -------------------------------------------------------------- auto-tune
    def maybe_tune(self, current_min: int) -> Optional[int]:
        """Return a new min_conviction if evidence warrants, else None."""
        if not (self.enabled and self.auto_tune):
            return None
        since = datetime.now(timezone.utc) - timedelta(days=30)
        rows = self.storage.alert_stats(since)
        # the band just above the current threshold is what admits marginal
        # alerts; if that band is no better than a coin flip, raise the bar.
        band = [r for r in rows
                if current_min <= (r["conviction"] or 0) < current_min + 20]
        if len(band) < self.tune_min_sample:
            return None
        p, _, _ = self._precision(band)
        new = current_min
        if p < self.precision_floor and current_min + self.tune_step <= self.tune_ceiling:
            new = current_min + self.tune_step
        elif p > 0.65 and current_min - self.tune_step >= self.tune_floor:
            # consistently strong at the margin -> we can admit a bit more
            new = current_min - self.tune_step
        if new != current_min:
            self.storage.set_meta("tuned_min_conviction", str(new))
            log.info("auto-tuned min_conviction %d -> %d (marginal precision "
                     "%.0f%% over n=%d)", current_min, new, p * 100, len(band))
            return new
        return None
