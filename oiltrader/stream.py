"""Streaming news engine.

Instead of one synchronous poll cycle, each collector runs on its own thread at
its own cadence and pushes items into a shared queue the moment they're fetched.
A single worker thread ingests items continuously, clusters them into pending
"stories", and finalises each story after a short GATHER WINDOW — just long
enough to let corroborating sources arrive, but no longer. High-credibility
sources (official / terminal-speed relays) use a shorter window so they fire
almost immediately.

Net effect: a headline is delivered within ~(collector interval + gather window)
rather than waiting for a whole batch cycle, while still merging the same story
from multiple sources into one corroborated alert.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Callable

from .clustering import Story, tokenize, _similar

log = logging.getLogger("oljan.stream")


class _CollectorThread(threading.Thread):
    def __init__(self, collector, interval: float, queue: "Queue",
                 stop: threading.Event, jitter: float = 0.0, health=None):
        super().__init__(daemon=True, name=f"collect-{collector.name}")
        self.collector = collector
        self.interval = max(interval, 5.0)
        self.queue = queue
        self.stop = stop
        self.jitter = jitter
        self.health = health

    def run(self) -> None:
        if self.jitter:
            self.stop.wait(self.jitter)
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                n = 0
                for item in self.collector.collect():
                    self.queue.put(item)
                    n += 1
                if self.health:
                    self.health.record_ok(self.collector.name, n)
            except Exception as e:
                log.warning("collector %s error: %s", self.collector.name,
                            str(e)[:80])
                if self.health:
                    self.health.record_err(self.collector.name, str(e))
            elapsed = time.monotonic() - start
            self.stop.wait(max(1.0, self.interval - elapsed))


class NewsStreamEngine:
    def __init__(self, cfg, collectors, on_story: Callable, seen, mark_seen,
                 source_weight, primary_symbol: str, health=None):
        self.cfg = cfg
        self.collectors = collectors
        self.on_story = on_story
        self.seen = seen
        self.mark_seen = mark_seen
        self.source_weight = source_weight
        self.primary = primary_symbol
        self.health = health
        self.stop = threading.Event()
        self.queue: "Queue" = Queue()
        self.pending: list[list] = []   # [Story, deadline_monotonic]
        self.sim = cfg.get("news.cluster_similarity", 0.4)
        self.gather = cfg.get("news.stream_gather_seconds", 25)
        self.fast_gather = cfg.get("news.stream_priority_gather_seconds", 8)
        self.max_age_min = cfg.get("news.max_age_minutes", 240)
        self._threads: list[_CollectorThread] = []
        self._worker: threading.Thread | None = None

    def _interval_for(self, name: str) -> float:
        g = self.cfg.get
        return {
            "rss": g("news.poll_seconds", 60),
            "gdelt": max(g("news.poll_seconds", 60), 60),
            "x": g("social.x_poll_seconds", 90),
            "stocktwits": g("social.stocktwits_poll_seconds", 300),
            "reddit": g("social.reddit_poll_seconds", 300),
            "newsapi": 900,
            "eia": g("eia.poll_seconds", 3600),
        }.get(name, g("news.poll_seconds", 60))

    def prime(self) -> int:
        """Seed the backlog silently so streaming only alerts on NEW items."""
        n = 0
        for c in self.collectors:
            try:
                for item in c.collect():
                    if not self.seen(item.hash):
                        self.mark_seen(item.hash, item.source)
                        n += 1
            except Exception as e:
                log.warning("prime %s failed: %s", c.name, str(e)[:80])
        log.info("stream primed: %d backlog items seeded silently", n)
        return n

    def start(self) -> None:
        for i, c in enumerate(self.collectors):
            t = _CollectorThread(c, self._interval_for(c.name), self.queue,
                                 self.stop, jitter=min(i * 1.5, 10),
                                 health=self.health)
            t.start()
            self._threads.append(t)
        self._worker = threading.Thread(target=self._run_worker, daemon=True,
                                        name="stream-worker")
        self._worker.start()
        log.info("stream started: %d collectors, gather=%ss (fast=%ss)",
                 len(self._threads), self.gather, self.fast_gather)

    def stop_all(self) -> None:
        self.stop.set()

    # ---------------------------------------------------------------- worker
    def _run_worker(self) -> None:
        while not self.stop.is_set():
            try:
                item = self.queue.get(timeout=1.0)
                self._ingest(item)
                # drain any burst quickly
                for _ in range(200):
                    try:
                        self._ingest(self.queue.get_nowait())
                    except Empty:
                        break
            except Empty:
                pass
            except Exception as e:
                log.warning("stream worker ingest error: %s", str(e)[:80])
            self._flush()

    def _ingest(self, item) -> None:
        if self.seen(item.hash):
            return
        self.mark_seen(item.hash, item.source)
        if item.symbol is None:
            item.symbol = self.primary
        ts = item.ts if item.ts.tzinfo else item.ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        if age_min > self.max_age_min:
            return
        toks, ents = tokenize(f"{item.title} {item.title}")
        for entry in self.pending:
            if _similar(entry[0], toks, ents, self.sim):
                entry[0].add(item, toks, ents)
                return
        story = Story()
        story.add(item, toks, ents)
        # high-credibility sources fire fast; others wait for corroboration
        window = (self.fast_gather if self.source_weight(item.source) >= 0.7
                  else self.gather)
        self.pending.append([story, time.monotonic() + window])

    def _flush(self) -> None:
        if not self.pending:
            return
        nowm = time.monotonic()
        due = [e for e in self.pending if nowm >= e[1]]
        for entry in due:
            self.pending.remove(entry)
            try:
                self.on_story(entry[0])
            except Exception as e:
                log.error("on_story failed: %s", str(e)[:120])
