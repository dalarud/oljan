"""Resilient 24/7 orchestration loop.

A tiny scheduler runs tasks at independent cadences. Every task is wrapped so
a failure is logged (with backoff) but never kills the loop. Process-level
auto-restart is handled by systemd/Docker (see deploy/); this class handles
in-process resilience, graceful shutdown and a periodic heartbeat.
"""
from __future__ import annotations

import logging
import signal
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .analysis import Analyzer
from .charting import render_chart
from .collectors import build_collectors
from .events import EventProcessor
from .historical import HistoricalEngine
from .indicators import ChartContext, compute as compute_indicators
from .market_data import MarketData
from .notifier import Notifier
from .sentiment import SentimentEngine
from .storage import Storage

log = logging.getLogger("oljan.daemon")


@dataclass
class Task:
    name: str
    fn: Callable[[], None]
    interval: float
    next_run: float = 0.0
    fail_streak: int = 0

    def due(self, now: float) -> bool:
        return now >= self.next_run

    def schedule_next(self, now: float, backoff: bool = False) -> None:
        if backoff and self.fail_streak > 0:
            delay = min(self.interval * (2 ** self.fail_streak), 3600)
        else:
            delay = self.interval
        self.next_run = now + delay


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.storage = Storage(cfg.data_dir / "oljan.db")
        self.market = MarketData(cfg, self.storage)
        self.sentiment = SentimentEngine(cfg)
        self.events = EventProcessor(cfg, self.storage, self.sentiment)
        self.historical = HistoricalEngine(cfg, self.storage)
        self.analyzer = Analyzer(cfg, self.historical)
        self.notifier = Notifier(cfg, self.storage)
        self.collectors = build_collectors(cfg, self.storage)

        self.symbols = [i["symbol"] for i in cfg.instruments]
        self.primary = cfg.primary_instrument["symbol"]
        self.intervals = self.market.intervals
        self.analysis_tf = self.market.analysis_tf
        # cache[symbol][timeframe] -> ChartContext
        self._chart_cache: dict[str, dict[str, ChartContext]] = {}
        self._running = True

        self.min_notify_score = cfg.get("notifications.min_notify_score", 2.5)
        self.always_notify_substantial = cfg.get(
            "notifications.always_notify_substantial", True)

        base = cfg.get("general.loop_interval_seconds", 30)
        self.tasks = [
            Task("market", self._task_market,
                 cfg.get("market_data.refresh_seconds", 120)),
            Task("news", self._task_news,
                 cfg.get("news.poll_seconds", 180)),
            Task("mature", self._task_mature, 3600),
            Task("daily", self._task_daily, 86400),
        ]
        hb = cfg.get("notifications.heartbeat_hours", 12)
        if hb and hb > 0:
            self.tasks.append(Task("heartbeat", self._task_heartbeat,
                                   hb * 3600))
        self._base_interval = base

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        self._install_signals()
        log.info("Oljan daemon starting. Symbols=%s primary=%s collectors=%s",
                 self.symbols, self.primary, [c.name for c in self.collectors])
        # Warm up so charts/analysis have data immediately.
        self._safe(self._task_daily, self.tasks[3])
        self._safe(self._task_market, self.tasks[0])

        # Immediate liveness ping so the user knows the daemon is up.
        price = self.market.last_price(self.primary)
        px = f"{price:.2f}" if price else "n/a"
        tfs = ", ".join(self.intervals)
        try:
            self.notifier.send_text(
                f"🟢 Oljan startad och bevakar {self.primary} ({px}).\n"
                f"Tidsramar: {tfs} · källor: {len(self.collectors)} · "
                f"analys-TF: {self.analysis_tf}.\n"
                f"_Du får en notis när något relevant händer._")
        except Exception as e:
            log.warning("startup ping failed: %s", e)

        now = time.time()
        for t in self.tasks:
            t.schedule_next(now)

        while self._running:
            now = time.time()
            for task in self.tasks:
                if not self._running:
                    break
                if task.due(now):
                    self._safe(task.fn, task)
            time.sleep(self._base_interval)
        log.info("Oljan daemon stopped.")

    def run_once(self) -> None:
        """One full pass of every task – useful for testing / cron mode."""
        log.info("Running a single pass (run_once).")
        for task in [self.tasks[3], self.tasks[0], self.tasks[1],
                     self.tasks[2]]:
            self._safe(task.fn, task, reschedule=False)

    # ------------------------------------------------------------------ tasks
    def _task_market(self) -> None:
        for sym in self.symbols:
            self.market.refresh_all(sym)
            tf_charts: dict[str, ChartContext] = {}
            for interval in self.intervals:
                df = self.market.get_candles(sym, interval)
                if df is None or df.empty or len(df) < 30:
                    continue
                tf_charts[interval] = compute_indicators(df, sym, self.cfg)
            if tf_charts:
                self._chart_cache[sym] = tf_charts
            else:
                log.warning("Insufficient candles for %s across timeframes", sym)

    def _task_daily(self) -> None:
        for sym in self.symbols:
            self.market.refresh_daily(sym)

    def _primary_chart(self, symbol: str) -> ChartContext | None:
        tf_charts = self._chart_cache.get(symbol, {})
        return tf_charts.get(self.analysis_tf) or (
            next(iter(tf_charts.values()), None))

    def _mtf_trends(self, symbol: str) -> dict[str, str]:
        tf_charts = self._chart_cache.get(symbol, {})
        return {tf: tf_charts[tf].trend for tf in self.intervals
                if tf in tf_charts}

    def _task_news(self) -> None:
        chart = self._primary_chart(self.primary)
        mtf = self._mtf_trends(self.primary)
        # On the very first run against a fresh DB the whole backlog looks
        # "new"; seed it silently so we don't flood with dozens of alerts at
        # once. Only genuinely new items after startup are pushed.
        primed = self.storage.get_meta("news_primed") == "1"
        new_items = 0
        processed = 0
        for collector in self.collectors:
            try:
                items = list(collector.collect())
            except Exception as e:
                log.warning("Collector %s failed: %s", collector.name, e)
                continue
            for item in items:
                if self.storage.seen(item.hash):
                    continue
                self.storage.mark_seen(item.hash, item.source)
                new_items += 1
                if item.symbol is None:
                    item.symbol = self.primary
                event = self.events.process(item, chart)
                if event is None:
                    continue
                self.events.persist(event)
                processed += 1
                if primed:
                    self._handle_event(event, chart, mtf)
        if not primed:
            self.storage.set_meta("news_primed", "1")
            log.info("news primed: seeded %d backlog items silently (%d "
                     "relevant); future items will push.", new_items, processed)
        elif new_items:
            log.info("news pass: %d new items, %d relevant events",
                     new_items, processed)

    def _task_mature(self) -> None:
        self.historical.mature_events()

    def _task_heartbeat(self) -> None:
        price = self.market.last_price(self.primary)
        px = f"{price:.2f}" if price else "n/a"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.notifier.heartbeat(
            f"💓 Oljan lever. {self.primary} {px}. {ts}. "
            f"Bevakar {len(self.collectors)} källor.")
        self.storage.set_meta("last_heartbeat", ts)

    # ---------------------------------------------------------------- helpers
    def _handle_event(self, event, chart, mtf=None) -> None:
        analysis = self.analyzer.build(event, chart, mtf)

        should_notify = (
            event.relevance >= self.min_notify_score
            or (self.always_notify_substantial and event.is_substantial)
        )
        log.info("EVENT [%s/%s] rel=%.1f sub=%.2f manip=%.2f conf=%s notify=%s | %s",
                 event.category, event.direction, event.relevance,
                 event.substance, event.manipulation, event.confidence,
                 should_notify, event.item.title[:80])

        if not should_notify:
            return

        chart_path = None
        if chart is not None and self.cfg.get("notifications.send_charts", True):
            df = self.storage.get_candles(
                event.symbol or self.primary, self.analysis_tf)
            if not df.empty:
                chart_path = render_chart(df, chart, self.cfg,
                                          tag=event.category)
        self.notifier.notify_event(analysis, chart_path)

    def _safe(self, fn: Callable[[], None], task: Optional[Task],
              reschedule: bool = True) -> None:
        try:
            fn()
            if task:
                task.fail_streak = 0
                if reschedule:
                    task.schedule_next(time.time())
        except Exception as e:
            if task:
                task.fail_streak += 1
                if reschedule:
                    task.schedule_next(time.time(), backoff=True)
            log.error("Task %s failed (streak=%s): %s\n%s",
                      task.name if task else "?",
                      task.fail_streak if task else "-", e,
                      traceback.format_exc())

    def _install_signals(self) -> None:
        def handler(signum, frame):
            log.info("Received signal %s; shutting down gracefully.", signum)
            self._running = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # e.g. non-main thread
                pass
