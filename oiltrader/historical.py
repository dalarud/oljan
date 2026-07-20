"""Historical event study / analog analysis.

Given a new event (category + directional bias), we look up how price behaved
after *analogous past events* and report the distribution of forward returns.
This is what turns a headline into a probabilistic, historically-grounded
statement like "rebounded within 2-4h in 70% of 12 similar cases".

Anti-bias guarantees (see also storage.analog_outcomes):
  * No look-ahead: an event's forward returns are computed only from candles
    recorded strictly AFTER the event timestamp, and only once the horizon has
    fully elapsed (the event is then marked "matured").
  * The current event is excluded from its own analog set.
  * Only matured events contribute to statistics, so partial/incomplete
    windows never leak into the base rate.
  * Purely descriptive statistics (no parameter fitting), so there is nothing
    to overfit; sample size is always reported and drives the confidence label.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("oljan.historical")


@dataclass
class HorizonStat:
    horizon_h: float
    n: int
    hit_rate: float                 # share where price moved in event's bias dir
    median_return: float            # median forward return (fractional)
    p25_return: float
    p75_return: float

    def hit_pct(self) -> int:
        return int(round(self.hit_rate * 100))


@dataclass
class AnalogReport:
    category: str
    direction: str
    stats: list[HorizonStat] = field(default_factory=list)
    total_samples: int = 0
    confidence: str = "low"

    def best_horizon(self) -> Optional[HorizonStat]:
        matured = [s for s in self.stats if s.n > 0]
        if not matured:
            return None
        # favour the horizon with the strongest directional hit rate + samples
        return max(matured, key=lambda s: (s.hit_rate, s.n))


class HistoricalEngine:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.horizons = [float(h) for h in
                         cfg.get("historical.horizons_hours", [1, 2, 4, 24])]
        self.min_sample = cfg.get("historical.min_sample_for_stats", 5)
        self.interval = cfg.get("market_data.intraday_interval", "15m")

    # ------------------------------------------------------------- maturation
    def mature_events(self) -> int:
        """Compute forward returns for events whose horizons have elapsed.

        Returns the number of events fully matured in this pass.
        """
        now = datetime.now(timezone.utc)
        matured_count = 0
        max_h = max(self.horizons)
        for ev in self.storage.unmatured_events():
            ts = datetime.fromtimestamp(ev["ts"], tz=timezone.utc)
            symbol = ev.get("symbol")
            if not symbol:
                continue
            ref = self.storage.price_at(symbol, self.interval, ts, "before")
            if ref is None:
                # No recorded price at/around event yet; try 'after' once data
                # arrives. Skip for now.
                if now - ts > timedelta(hours=max_h + 6):
                    # Give up on very old events with no price data.
                    self.storage.set_matured(ev["id"])
                continue
            _, ref_price = ref
            if ref_price is None or ref_price <= 0:
                continue

            all_done = True
            for h in self.horizons:
                target_time = ts + timedelta(hours=h)
                if now < target_time:
                    all_done = False
                    continue  # horizon not reached yet
                fwd = self.storage.price_at(symbol, self.interval,
                                            target_time, "after")
                if fwd is None:
                    # We should have data by now; if the window has long passed
                    # without data, mark this horizon unavailable (skip it).
                    if now - target_time < timedelta(hours=6):
                        all_done = False
                    continue
                _, fwd_price = fwd
                if fwd_price is None:
                    continue
                fwd_return = (fwd_price - ref_price) / ref_price
                self.storage.insert_outcome(ev["id"], h, ref_price,
                                            fwd_price, fwd_return)
            if all_done:
                self.storage.set_matured(ev["id"])
                matured_count += 1
        if matured_count:
            log.info("Matured %d event(s) in event-study", matured_count)
        return matured_count

    # ---------------------------------------------------------------- analogs
    def analog_report(self, category: str, direction: str,
                      exclude_event_id: Optional[int] = None) -> AnalogReport:
        report = AnalogReport(category=category, direction=direction)
        if direction == "neutral":
            report.confidence = "low"
            return report

        total = 0
        for h in self.horizons:
            returns = self.storage.analog_outcomes(
                category, direction, h, exclude_event_id)
            n = len(returns)
            total = max(total, n)
            if n == 0:
                report.stats.append(HorizonStat(h, 0, 0.0, 0.0, 0.0, 0.0))
                continue
            if direction == "bullish":
                hits = sum(1 for r in returns if r > 0)
            else:  # bearish: a "hit" is price falling
                hits = sum(1 for r in returns if r < 0)
            hit_rate = hits / n
            med = statistics.median(returns)
            p25 = _percentile(returns, 25)
            p75 = _percentile(returns, 75)
            report.stats.append(
                HorizonStat(h, n, hit_rate, med, p25, p75))

        report.total_samples = total
        if total >= self.min_sample:
            report.confidence = "high" if total >= 2 * self.min_sample else "medium"
        else:
            report.confidence = "low"
        return report


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac
