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
from .crossasset import CrossAssetMonitor
from .evaluator import AlertEvaluator
from .events import EventProcessor
from .historical import HistoricalEngine
from .indicators import ChartContext, compute as compute_indicators
from .market_data import MarketData
from .notifier import Notifier
from .sentiment import SentimentEngine
from .storage import Storage
from .watchdog import SourceHealth, Watchdog

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
        self.health = SourceHealth()
        self.evaluator = AlertEvaluator(cfg, self.storage, self.market)
        self.crossasset = CrossAssetMonitor(
            cfg, self.storage, cfg.primary_instrument["symbol"])

        self.symbols = [i["symbol"] for i in cfg.instruments]
        self.primary = cfg.primary_instrument["symbol"]
        self.intervals = self.market.intervals
        self.analysis_tf = self.market.analysis_tf
        # cache[symbol][timeframe] -> ChartContext
        self._chart_cache: dict[str, dict[str, ChartContext]] = {}
        self._running = True
        self.watchdog = Watchdog(cfg, self.health, self.market, self.notifier,
                                 self.storage, self.primary)

        self.min_notify_score = cfg.get("notifications.min_notify_score", 2.5)
        self.config_min_conviction = cfg.get("notifications.min_conviction", 40)
        self.always_notify_substantial = cfg.get(
            "notifications.always_notify_substantial", True)
        self.max_news_age_min = cfg.get("news.max_age_minutes", 360)
        self.cluster_sim = cfg.get("news.cluster_similarity", 0.4)
        self.collector_timeout = cfg.get("news.collector_timeout_seconds", 25)

        base = cfg.get("general.loop_interval_seconds", 30)
        self.stream_enabled = cfg.get("news.stream_enabled", True)
        self._engine = None
        # Periodic (non-news) tasks, looked up by name.
        self.tasks = [
            Task("daily", self._task_daily, 86400),
            Task("market", self._task_market,
                 cfg.get("market_data.refresh_seconds", 120)),
            Task("mature", self._task_mature, 3600),
        ]
        if not self.stream_enabled:
            self.tasks.append(Task("news", self._task_news,
                                   cfg.get("news.poll_seconds", 60)))
        hb = cfg.get("notifications.heartbeat_hours", 12)
        if hb and hb > 0:
            self.tasks.append(Task("heartbeat", self._task_heartbeat, hb * 3600))
        self.pulse_hours = cfg.get("notifications.pulse_hours", 3)
        if self.pulse_hours and self.pulse_hours > 0:
            self.tasks.append(Task("pulse", self._task_pulse,
                                   self.pulse_hours * 3600))
        if cfg.get("watchdog.enabled", True):
            self.tasks.append(Task("watchdog", self._task_watchdog,
                                   cfg.get("watchdog.check_seconds", 300)))
        if cfg.get("cross_asset.enabled", False):
            self.tasks.append(Task("crossasset", self._task_crossasset,
                                   cfg.get("cross_asset.refresh_seconds", 900)))
        if cfg.get("alert_eval.enabled", True):
            self.tasks.append(Task("score", self._task_score,
                                   cfg.get("alert_eval.score_seconds", 900)))
            sc_h = cfg.get("alert_eval.scorecard_hours", 24)
            if sc_h and sc_h > 0:
                self.tasks.append(Task("scorecard", self._task_scorecard,
                                       sc_h * 3600))
        self._tasks_by_name = {t.name: t for t in self.tasks}
        self._base_interval = base

    def _task(self, name: str) -> "Task":
        return self._tasks_by_name[name]

    @property
    def min_conviction(self) -> int:
        """Effective threshold: an auto-tuned value overrides the config one."""
        tuned = self.storage.get_meta("tuned_min_conviction")
        try:
            return int(tuned) if tuned is not None else self.config_min_conviction
        except (TypeError, ValueError):
            return self.config_min_conviction

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        self._install_signals()
        log.info("Oljan daemon starting. Symbols=%s primary=%s collectors=%s "
                 "stream=%s", self.symbols, self.primary,
                 [c.name for c in self.collectors], self.stream_enabled)
        tfs = ", ".join(self.intervals)
        try:
            self.notifier.send_text(
                f"🟢 Oljan startad och bevakar {', '.join(self.symbols)}.\n"
                f"Tidsramar: {tfs} · analys-TF: {self.analysis_tf} · "
                f"källor: {len(self.collectors)}"
                + (" · strömmande" if self.stream_enabled else "") + ".\n"
                f"_Du får en notis när något relevant händer._")
        except Exception as e:
            log.warning("startup ping failed: %s", e)

        # Warm up so charts/analysis have data available.
        self._safe(self._task_daily, self._task("daily"))
        self._safe(self._task_market, self._task("market"))

        if self.stream_enabled:
            from .stream import NewsStreamEngine
            self._engine = NewsStreamEngine(
                self.cfg, self.collectors, self._handle_story,
                self.storage.seen, self.storage.mark_seen,
                self.events.source_weight, self.primary, health=self.health)
            self._safe(self._engine.prime, None)
            self._engine.start()

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

        if self._engine:
            self._engine.stop_all()
        log.info("Oljan daemon stopped.")

    def run_once(self) -> None:
        """One synchronous batch pass – for testing / cron mode."""
        log.info("Running a single pass (run_once).")
        for name in ("daily", "market"):
            self._safe(self._task(name).fn, self._task(name), reschedule=False)
        self._safe(self._task_news, None, reschedule=False)
        self._safe(self._task_mature, None, reschedule=False)

    def _handle_story(self, story) -> None:
        """Streaming callback: finalise one clustered story into an alert.

        The engine primes the backlog silently, so any story reaching here is
        genuinely new and worth evaluating.
        """
        chart = self._primary_chart(self.primary)
        mtf = self._mtf_trends(self.primary)
        event = self.events.process_story(story, chart)
        if event is None:
            return
        self.events.persist(event)
        self._handle_event(event, chart, mtf)

    # ------------------------------------------------------------------ tasks
    def _task_market(self) -> None:
        for sym in self.symbols:
            self.market.refresh_all(sym)
            tf_charts: dict[str, ChartContext] = {}
            for interval in self.intervals:
                df = self.market.get_candles(sym, interval)
                if df is None or df.empty or len(df) < 30:
                    continue
                ctx = compute_indicators(df, sym, self.cfg, timeframe=interval)
                ctx.source = self.market.source_of(sym, interval)
                tf_charts[interval] = ctx
            if not tf_charts:
                # No intraday feed -> fall back to REAL daily levels so we
                # still show correct numbers (clearly labelled as daily).
                ddf = self.market.get_candles(sym, "1d")
                if ddf is None or ddf.empty or len(ddf) < 30:
                    ddf = self.market.refresh_daily(sym)
                if ddf is not None and len(ddf) >= 30:
                    dctx = compute_indicators(ddf, sym, self.cfg, timeframe="1d")
                    dctx.source = self.market.source_of(sym, "1d")
                    tf_charts["1d"] = dctx
                    log.info("%s: using daily levels (intraday unavailable)", sym)
            if tf_charts:
                self._chart_cache[sym] = tf_charts
            else:
                log.warning("No price data for %s (intraday or daily)", sym)

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
        from .clustering import cluster_items
        now = datetime.now(timezone.utc)
        chart = self._primary_chart(self.primary)
        mtf = self._mtf_trends(self.primary)
        primed = self.storage.get_meta("news_primed") == "1"

        # 1) Gather NEW items across collectors CONCURRENTLY so a slow source
        #    (e.g. Nitter) never delays fast ones (RSS/GDELT).
        from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                        TimeoutError as FTimeout)
        fresh: list = []
        collected: list = []
        with ThreadPoolExecutor(max_workers=min(8, len(self.collectors) or 1)) as ex:
            futs = {ex.submit(c.collect): c.name for c in self.collectors}
            try:
                for fut in as_completed(futs, timeout=self.collector_timeout):
                    try:
                        items = list(fut.result())
                        collected.extend(items)
                        self.health.record_ok(futs[fut], len(items))
                    except Exception as e:
                        log.warning("collector %s failed: %s",
                                    futs[fut], str(e)[:80])
                        self.health.record_err(futs[fut], str(e))
            except FTimeout:
                log.warning("collectors exceeded %ss; using partial results",
                            self.collector_timeout)
        for item in collected:
            if self.storage.seen(item.hash):
                continue
            self.storage.mark_seen(item.hash, item.source)
            if item.symbol is None:
                item.symbol = self.primary
            ts = item.ts if item.ts.tzinfo else item.ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() / 60.0 > self.max_news_age_min:
                continue
            fresh.append(item)

        # 2) Cluster into cross-source stories (real corroboration + dedup).
        stories = cluster_items(fresh, self.events.source_weight,
                                sim=self.cluster_sim)

        # 3) One event per story.
        processed = 0
        for story in stories:
            event = self.events.process_story(story, chart, now)
            if event is None:
                continue
            self.events.persist(event)
            processed += 1
            if primed:
                self._handle_event(event, chart, mtf)

        if not primed:
            self.storage.set_meta("news_primed", "1")
            log.info("news primed: seeded %d items in %d stories silently (%d "
                     "relevant); future items will push.", len(fresh),
                     len(stories), processed)
        elif fresh:
            log.info("news pass: %d new items -> %d stories, %d relevant/pushed",
                     len(fresh), len(stories), processed)

    def _task_mature(self) -> None:
        self.historical.mature_events()

    def _task_watchdog(self) -> None:
        self.watchdog.evaluate()

    def _task_crossasset(self) -> None:
        self.crossasset.refresh()

    def _task_score(self) -> None:
        self.evaluator.score_due()
        new_min = self.evaluator.maybe_tune(self.min_conviction)
        if new_min is not None:
            self.notifier.send_text(
                f"⚙️ Oljan justerade konviktionströskeln till *{new_min}* "
                f"utifrån uppmätt marginalträffsäkerhet. "
                f"_Färre men mer träffsäkra notiser._")

    def _task_scorecard(self) -> None:
        msg = self.evaluator.scorecard(
            self.cfg.get("alert_eval.scorecard_lookback_days", 14))
        if msg:
            self.notifier.send_text(msg)

    def _task_pulse(self) -> None:
        from .pulse import build_pulse
        chart = self._primary_chart(self.primary)
        name = self.cfg.primary_instrument.get("name", self.primary)
        msg = build_pulse(self.storage, self.pulse_hours,
                          self.market.last_price(self.primary),
                          chart.trend if chart else None, name)
        if msg:
            self.notifier.send_text(msg)

    def _task_heartbeat(self) -> None:
        price = self.market.last_price(self.primary)
        px = f"{price:.2f}" if price else "n/a"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        wd = self.storage.get_meta("watchdog_status", "ok") or "ok"
        health = "✅ alla källor svarar" if wd == "ok" else "⚠️ degraderad insamling"
        self.notifier.heartbeat(
            f"💓 Oljan lever. {self.primary} {px}. {ts}. "
            f"Bevakar {len(self.collectors)} källor · {health}.\n"
            f"_{self.watchdog.status_line()}_")
        self.storage.set_meta("last_heartbeat", ts)

    # ---------------------------------------------------------------- helpers
    def _key_levels(self, chart):
        if chart is None:
            return None
        from .levels import compute_levels
        sym = chart.symbol
        intr = self.storage.get_candles(sym, chart.timeframe or self.analysis_tf)
        daily = self.storage.get_candles(sym, "1d")
        if intr.empty:
            return None
        try:
            return compute_levels(intr, daily if not daily.empty else None,
                                  chart.price, self.cfg)
        except Exception as e:
            log.warning("level computation failed: %s", e)
            return None

    def _handle_event(self, event, chart, mtf=None) -> None:
        levels = self._key_levels(chart)
        cross = self.crossasset.snapshot()
        analysis = self.analyzer.build(event, chart, mtf, levels, cross)

        # Gate on CONVICTION (not raw relevance) so low-signal, neutral or
        # single-source noise is suppressed; substantial + corroborated events
        # always get through.
        should_notify = (
            analysis.conviction >= self.min_conviction
            or (self.always_notify_substantial and event.is_substantial
                and event.n_sources >= 2)
        )
        log.info("EVENT [%s/%s] conv=%d rel=%.1f sub=%.2f manip=%.2f n=%d notify=%s | %s",
                 event.category, event.direction, analysis.conviction,
                 event.relevance, event.substance, event.manipulation,
                 event.n_sources, should_notify, event.item.title[:80])

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
        # Log the pushed alert so its directional claim can be scored later.
        ref_price = chart.price if chart is not None else None
        self.evaluator.record(analysis, ref_price)

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
