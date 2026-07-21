"""Self-monitoring / watchdog.

A 24/7 engine is only useful if it's actually collecting. Silent degradation —
a dead Nitter instance, a rate-limited data provider, a collector throwing
every cycle — is worse than a crash because nothing tells you. This module
tracks the health of each collector and the market-data feed and raises a
single, de-duplicated alert when things degrade (and a recovery note when they
come back), so you learn about a blind spot instead of trusting a stale screen.

`SourceHealth` is a thread-safe tally the collector threads write to.
`Watchdog` evaluates that tally plus market-data freshness on a slow cadence
and only emits text on a state *transition* (healthy -> degraded, degraded ->
healthy), never every cycle.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("oljan.watchdog")


@dataclass
class _Stat:
    ok_count: int = 0
    err_count: int = 0
    consec_fail: int = 0
    items_total: int = 0
    last_ok: Optional[float] = None       # monotonic seconds
    last_ok_wall: Optional[float] = None  # unix seconds (for reporting)
    last_err: str = ""


class SourceHealth:
    """Thread-safe per-collector success/failure tally."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, _Stat] = {}

    def record_ok(self, name: str, n_items: int = 0) -> None:
        with self._lock:
            s = self._stats.setdefault(name, _Stat())
            s.ok_count += 1
            s.consec_fail = 0
            s.items_total += max(n_items, 0)
            s.last_ok = time.monotonic()
            s.last_ok_wall = time.time()

    def record_err(self, name: str, err: str) -> None:
        with self._lock:
            s = self._stats.setdefault(name, _Stat())
            s.err_count += 1
            s.consec_fail += 1
            s.last_err = (err or "")[:120]

    def snapshot(self) -> dict[str, _Stat]:
        with self._lock:
            # shallow copies so callers can read without holding the lock
            return {k: _Stat(**vars(v)) for k, v in self._stats.items()}


class Watchdog:
    """Evaluates SourceHealth + market freshness; alerts on transitions only."""

    def __init__(self, cfg, health: SourceHealth, market, notifier, storage,
                 primary: str):
        self.cfg = cfg
        self.health = health
        self.market = market
        self.notifier = notifier
        self.storage = storage
        self.primary = primary
        # A collector is "stale" if it hasn't succeeded in this many seconds.
        # Default: 6x the news poll interval, floored at 10 min.
        poll = cfg.get("news.poll_seconds", 60)
        self.stale_after = cfg.get("watchdog.source_stale_seconds",
                                   max(poll * 6, 600))
        self.consec_fail_alert = cfg.get("watchdog.consecutive_fail_alert", 3)
        # Market data considered stale after this many minutes with no fresh
        # candle (intraday). Default 3x the market refresh interval.
        refresh = cfg.get("market_data.refresh_seconds", 120)
        self.market_stale_min = cfg.get(
            "watchdog.market_stale_minutes", max(refresh * 3 / 60.0, 15))
        self.enabled = cfg.get("watchdog.enabled", True)
        # Require this many consecutive problem checks before alerting, so a
        # single transient blip (e.g. one missed Yahoo poll) doesn't flap.
        self.min_degraded_checks = cfg.get("watchdog.min_degraded_checks", 2)
        self._degraded = False           # current alert state
        self._problem_streak = 0
        self._started = time.monotonic()

    def evaluate(self) -> None:
        """Run one health check; alert only when the degraded state flips."""
        if not self.enabled:
            return
        problems = self._collect_problems()
        now_problem = bool(problems)
        self._problem_streak = self._problem_streak + 1 if now_problem else 0

        # Alert only once degradation has PERSISTED, to avoid flapping on a
        # single transient blip; recovery is reported immediately.
        if self._problem_streak >= self.min_degraded_checks and not self._degraded:
            self._degraded = True
            self.notifier.send_ambient(self._format_alert(problems))
        elif not now_problem and self._degraded:
            self._degraded = False
            self.notifier.send_ambient(
                "✅ *Oljan återställd* – alla källor och prisdata svarar igen.")
        self.storage.set_meta("watchdog_status",
                              "degraded" if self._degraded else "ok")

    # ------------------------------------------------------------- evaluation
    def _collect_problems(self) -> list[str]:
        problems: list[str] = []
        snap = self.health.snapshot()

        # Give collectors a grace period after startup before judging silence.
        warming = (time.monotonic() - self._started) < self.stale_after

        for name, s in sorted(snap.items()):
            if s.consec_fail >= self.consec_fail_alert:
                problems.append(
                    f"{name}: {s.consec_fail} fel i rad"
                    + (f" ({s.last_err})" if s.last_err else ""))
                continue
            if warming:
                continue
            if s.last_ok is None:
                problems.append(f"{name}: har aldrig lyckats hämta")
            else:
                idle = time.monotonic() - s.last_ok
                if idle > self.stale_after:
                    problems.append(
                        f"{name}: inget svar på {self._mins(idle)} min")

        # Market data freshness (does the primary have a recent candle?)
        mp = self._market_stale_minutes()
        if mp is not None and mp > self.market_stale_min:
            problems.append(
                f"prisdata: senaste candle {mp:.0f} min gammal ({self.primary})")
        return problems

    def _market_stale_minutes(self) -> Optional[float]:
        try:
            df = self.storage.get_candles(self.primary, self.market.analysis_tf)
            if df is None or df.empty:
                # fall back to any timeframe before declaring a gap
                for iv in getattr(self.market, "intervals", []):
                    df = self.storage.get_candles(self.primary, iv)
                    if df is not None and not df.empty:
                        break
            if df is None or df.empty:
                return None
            last = df.index[-1]
            if getattr(last, "tzinfo", None) is None:
                last = last.tz_localize("UTC")
            return (datetime.now(timezone.utc) - last).total_seconds() / 60.0
        except Exception as e:
            log.debug("market staleness check failed: %s", e)
            return None

    # ---------------------------------------------------------------- format
    def _format_alert(self, problems: list[str]) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        body = "\n".join(f"• {p}" for p in problems)
        return (f"⚠️ *Oljan – degraderad datainsamling* ({ts})\n{body}\n"
                f"_Notiser kan vara ofullständiga tills detta löser sig._")

    @staticmethod
    def _mins(seconds: float) -> str:
        return f"{seconds / 60.0:.0f}"

    # ----------------------------------------------------------- status line
    def status_line(self) -> str:
        snap = self.health.snapshot()
        if not snap:
            return "inga källor rapporterade ännu"
        parts = []
        for name, s in sorted(snap.items()):
            tag = "ok" if s.consec_fail == 0 else f"{s.consec_fail}✗"
            parts.append(f"{name}:{tag}({s.items_total})")
        return " · ".join(parts)
