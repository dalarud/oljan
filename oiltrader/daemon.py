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
from .setups import SetupMonitor, format_setup
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
        self.setups = SetupMonitor(cfg, storage=self.storage)
        self.setup_cooldown_min = cfg.get("setups.cooldown_minutes", 30)
        self.fast_skip_min = cfg.get("market_data.fast_skip_minutes", 20)
        # Dedicated Yahoo provider for the fast poll: real ~24h Brent futures,
        # ONE polite request per cycle (keeps the primary series on a single
        # real basis and, unlike the ETF, has data during the EU morning).
        from .providers import YahooChartProvider
        self._fast_provider = YahooChartProvider(
            cooldown=cfg.get("market_data.yahoo_cooldown_seconds", 45))

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
        # Collapse a running story: don't re-alert the same category+direction
        # within this window unless conviction jumps by the escalation delta.
        self.topic_cooldown_s = cfg.get(
            "notifications.topic_cooldown_minutes", 45) * 60
        self.topic_escalation_delta = cfg.get(
            "notifications.topic_escalation_delta", 15)
        self.topic_price_esc_pct = cfg.get(
            "notifications.topic_price_escalation_pct", 0.7)
        self.max_news_age_min = cfg.get("news.max_age_minutes", 360)
        self.cluster_sim = cfg.get("news.cluster_similarity", 0.4)
        self.collector_timeout = cfg.get("news.collector_timeout_seconds", 25)

        base = cfg.get("general.loop_interval_seconds", 30)
        # Default to the simpler concurrent batch poll: it already collects all
        # sources in parallel (a slow source never blocks fast ones), so at a
        # short poll interval its latency is on par with streaming but with far
        # fewer moving parts. Streaming remains available via stream_enabled.
        self.stream_enabled = cfg.get("news.stream_enabled", False)
        self._engine = None
        # Periodic (non-news) tasks, looked up by name.
        self.tasks = [
            Task("daily", self._task_daily, 86400),
            Task("market", self._task_market,
                 cfg.get("market_data.refresh_seconds", 120)),
            Task("mature", self._task_mature, 3600),
        ]
        if self.setups.enabled:
            self.tasks.append(Task("market_fast", self._task_market_fast,
                                   cfg.get("market_data.fast_refresh_seconds", 60)))
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
        # Morning report: a single briefing at a fixed local time instead of
        # overnight pings. Checked on a short cadence; fires once per day.
        self.morning_enabled = cfg.get("notifications.morning_report.enabled", True)
        if self.morning_enabled:
            self.tasks.append(Task("morning", self._task_morning, 300))
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

    def run_cron(self) -> None:
        """One full pass for a SCHEDULED runner (e.g. GitHub Actions) that fires
        every N minutes. State (seen items, dedup, last-RSI, morning/pulse
        timers) lives in the persisted sqlite DB, so across stateless runs this
        behaves like the continuous daemon: it primes silently on the very first
        run, then only new events/setups/reports fire. Time-gated tasks
        (morning, pulse) fire when due."""
        log.info("Running a scheduled pass (run_cron).")
        # Daily data only needs refreshing once/day (Alpha Vantage free = 25
        # calls/day), so gate it to avoid burning the quota every 15 min.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.storage.get_meta("daily_done_date") != today:
            self._safe(self._task_daily, None, reschedule=False)
            self.storage.set_meta("daily_done_date", today)
        self._safe(self._task_market, None, reschedule=False)
        self._safe(self._task_market_fast, None, reschedule=False)  # fresh 5m + setups
        self._safe(self._check_momentum, None, reschedule=False)  # move w/o headline
        self._safe(self._task_news, None, reschedule=False)
        self._safe(self._task_mature, None, reschedule=False)
        if self.morning_enabled:
            self._safe(self._task_morning, None, reschedule=False)
        self._safe(self._cron_pulse_if_due, None, reschedule=False)
        if self.cfg.get("watchdog.enabled", True):
            self._safe(self._task_watchdog, None, reschedule=False)
        # Publish a snapshot for the web dashboard (best-effort).
        path = self.cfg.get("web.state_path", "state.json")
        try:
            self.export_state(path)
        except Exception as e:
            log.warning("state export failed: %s", e)

    def export_state(self, path: str) -> dict:
        """Write a compact JSON snapshot of the current picture for the web
        control panel (price, levels, regime, plan, events, pulse, scorecard)."""
        import json
        from datetime import timedelta
        from .playbook import classify_intel, compact_plan
        from .pulse import build_pulse

        off = self.analyzer.broker_offset
        chart = self._primary_chart(self.primary)
        levels = self._key_levels(chart)
        name = self.cfg.primary_instrument.get("name", self.primary)
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        evs = [e for e in self.storage.recent_events(since)
               if e.get("source") not in ("seed", None)]
        intel = classify_intel(evs)

        def sc(e):
            return float(e.get("relevance") or 0) * max(
                float(e.get("substance") or 0), 0.15)

        top = sorted(evs, key=sc, reverse=True)[:12]
        events = [{
            "ts": e.get("ts"), "dir": e.get("direction"),
            "title": (e.get("title") or "").strip()[:180],
            "url": e.get("url"), "cat": e.get("category"),
            "rel": e.get("relevance"),
            "sub": round(float(e.get("substance") or 0), 2),
        } for e in top]

        def disp(v):
            return round(v + off, 2) if isinstance(v, (int, float)) else None

        res = [{"label": l, "v": disp(v)}
               for l, v in (levels.resistances_above() if levels else [])][:3]
        sup = [{"label": l, "v": disp(v)}
               for l, v in (levels.supports_below() if levels else [])][:3]
        piv = (levels.vwap or getattr(levels, "pdc", None)) if levels else None
        # Scored alert history for the dashboard's precision visualisation.
        alert_rows = self.storage.alert_stats(
            datetime.now(timezone.utc) - timedelta(days=14))
        alerts = [{"ts": a.get("ts"), "conv": a.get("conviction"),
                   "correct": a.get("correct"), "dir": a.get("direction"),
                   "ret": round(float(a.get("fwd_return") or 0.0), 4)}
                  for a in alert_rows[:100]]
        scale_factor = (self.cfg.get("market_data.scale_override", {})
                        or {}).get(self.primary)
        bias = ("bullish" if intel["bias"] > 0.1 else
                "bearish" if intel["bias"] < -0.1 else "neutral")
        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instrument": name, "tv_symbol": self.cfg.get("web.tv_symbol", "TVC:UKOIL"),
            "price": disp(chart.price) if chart is not None else None,
            "price_source": (chart.source if chart is not None else None),
            "price_stale_min": round(chart.last_candle_age_min) if chart else None,
            "scale_factor": scale_factor,
            "rsi": round(chart.rsi) if chart is not None else None,
            "trend": self._mtf_trends(self.primary),
            "bias": bias, "regime": intel["regime"],
            "supply_corroboration": intel.get("supply_corroboration", 0),
            "levels": {
                "resistance": res, "support": sup,
                "pivot": disp(piv),
                "pdh": disp(levels.pdh) if levels else None,
                "pdl": disp(levels.pdl) if levels else None,
            },
            "plan": compact_plan(evs, chart, levels,
                                 self.crossasset.snapshot(),
                                 self.cfg.get("trader_profile")),
            "events": events,
            "pulse": build_pulse(self.storage, self.pulse_hours,
                                 self.market.last_price(self.primary),
                                 chart.trend if chart else None, name),
            "scorecard": self.evaluator.scorecard(14),
            "alerts": alerts,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info("dashboard state written to %s", path)
        return state

    def _cron_pulse_if_due(self) -> None:
        """Fire the market pulse if pulse_hours have elapsed since the last one
        (meta-gated, since a scheduled run has no long-lived scheduler)."""
        if not (self.pulse_hours and self.pulse_hours > 0):
            return
        last = self.storage.get_meta("last_pulse_ts")
        now = time.time()
        try:
            if last and (now - float(last)) < self.pulse_hours * 3600:
                return
        except (TypeError, ValueError):
            pass
        self._task_pulse()
        self.storage.set_meta("last_pulse_ts", str(now))

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

    def _task_market_fast(self) -> None:
        """High-cadence Yahoo-only poll of the primary analysis timeframe for
        real ~24h Brent (incl. the EU morning) and prompt setup detection.

        Yahoo-only on purpose: it keeps the primary series on ONE real basis
        (no ETF-scaled candles mixed in) and avoids burning the Twelve Data
        quota while Yahoo is briefly cooling down. If Yahoo is unavailable this
        cycle we simply keep the last real candle and retry next minute."""
        if self.notifier.in_quiet_hours():
            return
        sym, iv = self.primary, self.analysis_tf
        lookback = dict(self.market.timeframes).get(iv, "5d")
        df = self._fast_provider.fetch(sym, iv, lookback)
        if df is None or df.empty or len(df) < 30:
            return  # Yahoo cooling/unavailable; keep last real candle
        self.storage.upsert_candles(sym, iv, df)
        self.storage.set_meta(f"src:{sym}:{iv}", "yahoo")
        ctx = compute_indicators(df, sym, self.cfg, timeframe=iv)
        ctx.source = "yahoo"
        self._chart_cache.setdefault(sym, {})[iv] = ctx
        self._check_setups()
        self._check_momentum()

    def _check_momentum(self) -> None:
        """Alert when price makes a sustained move regardless of headlines.

        Fills the gap where the tape runs for an hour on an old story: the
        engine only alerted on NEW headlines, so a news-quiet grind produced
        silence. A material move over the window is information in itself —
        especially for a mean-reversion trader who must NOT fade it."""
        if not self.cfg.get("momentum.enabled", True):
            return
        if self.notifier.in_quiet_hours():
            return
        df = self.storage.get_candles(self.primary, self.analysis_tf)
        if df is None or df.empty or len(df) < 20:
            return
        import pandas as pd
        last_ts = df.index[-1]
        if getattr(last_ts, "tzinfo", None) is None:
            last_ts = last_ts.tz_localize("UTC")
        if (datetime.now(timezone.utc) - last_ts).total_seconds() > 20 * 60:
            return  # feed resting; a stale "move" is old news
        win = self.cfg.get("momentum.window_minutes", 45)
        past = df[df.index <= df.index[-1] - pd.Timedelta(minutes=win)]
        base = float(past["close"].iloc[-1]) if not past.empty \
            else float(df["close"].iloc[0])
        now_p = float(df["close"].iloc[-1])
        if base <= 0:
            return
        pct = (now_p - base) / base * 100.0
        chart = self._primary_chart(self.primary)
        atr_pct = (chart.atr / chart.price * 100.0) if chart and chart.price else 0.0
        thr = max(self.cfg.get("momentum.min_pct", 0.6),
                  self.cfg.get("momentum.atr_mult", 1.5) * atr_pct)
        if abs(pct) < thr:
            return
        cooldown = self.cfg.get("momentum.cooldown_minutes", 60) * 60
        prev = self.storage.get_meta("momentum_last_ts")
        try:
            if prev and time.time() - float(prev) < cooldown:
                return
        except (TypeError, ValueError):
            pass
        from datetime import timedelta
        recent = [e for e in self.storage.recent_events(
            datetime.now(timezone.utc) - timedelta(minutes=win))
            if e.get("source") not in ("seed", None)]
        name = self.cfg.primary_instrument.get("name", self.primary)
        off = self.analyzer.broker_offset
        arrow, word = ("🚀", "UPP") if pct > 0 else ("🔻", "NED")
        driver = (f"{len(recent)} färska rubriker i fönstret."
                  if recent else
                  "INGEN ny rubrik i fönstret – flödesdriven rörelse "
                  "(premie/positionering).")
        style = ""
        if str((self.cfg.get("trader_profile", {}) or {}).get("style", "")
               ).lower() == "mean_reversion":
            style = ("\n🔁 Din stil: fadea INTE detta läge – vänta på "
                     "RSI-reclaim + avvisning innan mottrend, eller handla "
                     "reversion MED rörelsens riktning på nästa rekyl.")
        msg = (f"{arrow} *MOMENTUM {word}* {pct:+.1f}% på {win} min – "
               f"{name} {now_p + off:.2f}\n{driver}{style}")
        if self.notifier.send_ambient(msg):
            self.storage.set_meta("momentum_last_ts", str(time.time()))
            log.info("MOMENTUM alert %s %.2f%% (%d headlines in window)",
                     word, pct, len(recent))

    def _recent_news_bias(self, minutes: int = 20) -> float:
        """Net directional bias of substantial events in the last `minutes`."""
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        evs = [e for e in self.storage.recent_events(since)
               if e.get("source") not in ("seed", None)]
        strong = [e for e in evs if float(e.get("substance") or 0) >= 0.4]
        if not strong:
            return 0.0
        b = sum(1 for e in strong if e.get("direction") == "bullish")
        s = sum(1 for e in strong if e.get("direction") == "bearish")
        return (b - s) / max(b + s, 1)

    def _check_setups(self) -> None:
        """Fire a proactive alert when a trade entry condition triggers."""
        if not self.setups.enabled:
            return
        chart = self._primary_chart(self.primary)
        if chart is None or not getattr(chart, "price_sane", True):
            return
        if chart.last_candle_age_min > self.analyzer.stale_after_min:
            return  # not live (e.g. ETF proxy resting) -> don't signal
        trend = self.analyzer._trend_hint(self._mtf_trends(self.primary))
        levels = self._key_levels(chart)
        setup = self.setups.update_and_detect(
            self.primary, chart, levels, trend, self._recent_news_bias())
        if setup is None:
            return
        if self.notifier.in_quiet_hours():
            return
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(
            minutes=self.setup_cooldown_min)
        if self.storage.notified_since(setup.dedup_key(), since):
            return  # already flagged this side recently
        name = self.cfg.primary_instrument.get("name", self.primary)
        msg = format_setup(setup, name, disp=lambda v: v + self.analyzer.broker_offset)
        # Enrich the push with the fused technical+fundamental synthesis so the
        # alert is a complete decision aid, not just the raw trigger.
        try:
            from datetime import timedelta as _td
            from .playbook import classify_intel
            from .synthesis import build_synthesis, format_synthesis
            evs = [e for e in self.storage.recent_events(
                       datetime.now(timezone.utc) - _td(hours=24))
                   if e.get("source") not in ("seed", None)]
            syn = build_synthesis(classify_intel(evs), chart,
                                  self._mtf_trends(self.primary), levels, evs)
            block = format_synthesis(syn)
            if block:
                msg = msg + "\n\n" + block
        except Exception:
            log.debug("synthesis enrich failed", exc_info=True)
        if self.notifier.send_text(msg):
            self.storage.record_notification(None, "setup", setup.dedup_key())
            log.info("SETUP %s fired (%s) rsi %.0f->%.0f conf=%s",
                     setup.side, setup.kind, setup.rsi_prev, setup.rsi_now,
                     setup.confidence)

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
        # Overnight the intraday ETF proxy is expected to be stale; don't churn
        # the degraded/recovered state (and stay silent) during quiet hours.
        if self.notifier.in_quiet_hours():
            return
        self.watchdog.evaluate()

    def _task_crossasset(self) -> None:
        self.crossasset.refresh()

    def _task_score(self) -> None:
        self.evaluator.score_due()
        new_min = self.evaluator.maybe_tune(self.min_conviction)
        if new_min is not None:
            self.notifier.send_ambient(
                f"⚙️ Oljan justerade konviktionströskeln till *{new_min}* "
                f"utifrån uppmätt marginalträffsäkerhet. "
                f"_Färre men mer träffsäkra notiser._")

    def _task_scorecard(self) -> None:
        msg = self.evaluator.scorecard(
            self.cfg.get("alert_eval.scorecard_lookback_days", 14))
        if msg:
            self.notifier.send_ambient(msg)

    def _task_morning(self) -> None:
        """Send the morning briefing once per day at/after the configured local
        time (default 06:00), replacing overnight pings."""
        from .morning import build_morning_report
        tz = self.notifier.tz
        now_local = datetime.now(timezone.utc).astimezone(tz)
        target = str(self.cfg.get("notifications.morning_report.time", "06:00"))
        try:
            th, tm = (int(x) for x in target.split(":"))
        except Exception:
            th, tm = 6, 0
        target_min = th * 60 + tm
        now_min = now_local.hour * 60 + now_local.minute
        today = now_local.strftime("%Y-%m-%d")
        # Fire once per day on the FIRST run at/after the target time, within a
        # generous window. GitHub's scheduled cron is sparse/irregular (can skip
        # 1-3 h), so a narrow window is missed entirely — hence a wide default
        # (6 h): the briefing still goes out on the first run of the morning,
        # just a little late rather than never.
        window = self.cfg.get("notifications.morning_report.window_minutes", 360)
        if self.storage.get_meta("morning_report_date") == today:
            return
        if not (target_min <= now_min < target_min + window):
            return
        chart = self._primary_chart(self.primary)
        report = build_morning_report(
            self.cfg, self.storage,
            name=self.cfg.primary_instrument.get("name", self.primary),
            symbol=self.primary, chart=chart, levels=self._key_levels(chart),
            mtf_trends=self._mtf_trends(self.primary),
            cross=self.crossasset.snapshot(),
            night_hours=self.cfg.get("notifications.morning_report.night_hours", 9),
            tz=tz)
        # The morning report is the point of the quiet window — send it even
        # though we're technically still at the edge of quiet hours.
        self.notifier.send_text(report)
        self.storage.set_meta("morning_report_date", today)
        log.info("morning report sent for %s", today)

    def _task_pulse(self) -> None:
        from .pulse import build_pulse
        chart = self._primary_chart(self.primary)
        name = self.cfg.primary_instrument.get("name", self.primary)
        msg = build_pulse(self.storage, self.pulse_hours,
                          self.market.last_price(self.primary),
                          chart.trend if chart else None, name)
        # Append the fused synthesis read so the periodic pulse also carries an
        # actionable "where's the edge" line.
        try:
            from datetime import timedelta as _td
            from .playbook import classify_intel
            from .synthesis import build_synthesis, format_synthesis
            evs = [e for e in self.storage.recent_events(
                       datetime.now(timezone.utc) - _td(hours=24))
                   if e.get("source") not in ("seed", None)]
            levels = self._key_levels(chart) if chart is not None else None
            syn = build_synthesis(classify_intel(evs), chart,
                                  self._mtf_trends(self.primary), levels, evs)
            block = format_synthesis(syn)
            if msg and block:
                msg = msg + "\n\n" + block
        except Exception:
            log.debug("pulse synthesis enrich failed", exc_info=True)
        if msg:
            self.notifier.send_ambient(msg)

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
        if intr.empty:
            return None
        # Keep every level on ONE current basis: derive prior-day H/L/C from
        # the intraday series itself whenever intraday is the trusted source
        # (scaled ETF estimate OR real Yahoo intraday). The stored daily can be
        # stale/different-basis (free Alpha Vantage), which would make PD levels
        # incoherent with the price.
        src = chart.source or ""
        if "scaled" in src or "yahoo" in src:
            daily = self._resample_daily(intr)
        else:
            daily = self.storage.get_candles(sym, "1d")
        try:
            return compute_levels(intr, daily if daily is not None and
                                  not daily.empty else None, chart.price, self.cfg)
        except Exception as e:
            log.warning("level computation failed: %s", e)
            return None

    @staticmethod
    def _resample_daily(intr):
        """Daily OHLC from an intraday series (keeps one consistent basis)."""
        try:
            d = intr.resample("1D").agg(
                {"open": "first", "high": "max", "low": "min",
                 "close": "last", "volume": "sum"}).dropna(how="any")
            return d
        except Exception:
            return None

    def _topic_suppressed(self, event, conviction: int,
                          price: float | None = None) -> bool:
        """True if we've alerted this (category, direction) recently without a
        meaningful escalation — collapses a running story into one alert instead
        of re-firing every time it re-clusters. Two escalations break through:
        materially higher conviction, OR the PRICE having moved materially since
        the last alert (a story that keeps driving the tape is new information
        even if the headline wording is the same)."""
        if event.direction == "neutral":
            return False
        key = f"topic_last:{event.category}:{event.direction}"
        now = time.time()
        prev = self.storage.get_meta(key)
        if prev:
            try:
                parts = prev.split(":")
                pts, pconv = float(parts[0]), float(parts[1])
                pprice = float(parts[2]) if len(parts) > 2 and parts[2] else None
                price_moved = (price is not None and pprice
                               and abs(price - pprice) / pprice * 100
                               >= self.topic_price_esc_pct)
                if (now - pts < self.topic_cooldown_s
                        and conviction < pconv + self.topic_escalation_delta
                        and not price_moved):
                    return True
            except (ValueError, TypeError, IndexError):
                pass
        tail = f":{price:.4f}" if isinstance(price, (int, float)) else ""
        self.storage.set_meta(key, f"{now}:{conviction}{tail}")
        return False

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

        if should_notify and self._topic_suppressed(
                event, analysis.conviction,
                chart.price if chart is not None else None):
            log.info("  -> topic-cooldown: same %s/%s recently alerted; suppressing",
                     event.category, event.direction)
            should_notify = False

        if not should_notify:
            return

        chart_path = None
        if chart is not None and self.cfg.get("notifications.send_charts", True):
            df = self.storage.get_candles(
                event.symbol or self.primary, self.analysis_tf)
            if not df.empty:
                chart_path = render_chart(df, chart, self.cfg,
                                          tag=event.category)
        pushed = self.notifier.notify_event(analysis, chart_path)
        # Only score alerts that were actually delivered (not deduped or
        # suppressed during quiet hours), so the track record reflects the
        # notifications you really received.
        if pushed:
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
