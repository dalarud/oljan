"""Collectors: pluggable news/social/official-report sources.

build_collectors() wires up the enabled collectors from config + secrets.
Add a new source by implementing Collector.collect() and registering it here.
"""
from __future__ import annotations

import logging

from .base import Collector, NewsItem  # noqa: F401
from .rss import RssCollector
from .eia import EiaCollector
from .reddit import RedditCollector
from .stocktwits import StocktwitsCollector
from .newsapi import NewsApiCollector
from .x import XCollector
from .gdelt import GdeltCollector

log = logging.getLogger("oljan.collectors")


def build_collectors(cfg, storage) -> list[Collector]:
    collectors: list[Collector] = []

    feeds = cfg.get("news.rss_feeds", []) or []
    if feeds:
        collectors.append(RssCollector(feeds))

    if cfg.get("news.gdelt_enabled", True):
        collectors.append(GdeltCollector(cfg.get("news.gdelt_query", "")))

    if cfg.get("news.newsapi_enabled", False):
        key = cfg.secret("NEWSAPI_KEY")
        if key:
            collectors.append(NewsApiCollector(key, cfg.get("news.newsapi_query", "")))
        else:
            log.warning("newsapi_enabled but NEWSAPI_KEY missing; skipping")

    if cfg.get("eia.enabled", False):
        key = cfg.secret("EIA_API_KEY")
        if key:
            collectors.append(EiaCollector(key, storage))
        else:
            log.warning("eia.enabled but EIA_API_KEY missing; skipping")

    if cfg.get("social.reddit_enabled", False):
        cid = cfg.secret("REDDIT_CLIENT_ID")
        csec = cfg.secret("REDDIT_CLIENT_SECRET")
        if cid and csec:
            collectors.append(RedditCollector(
                cid, csec, cfg.secret("REDDIT_USER_AGENT"),
                cfg.get("social.reddit_subreddits", [])))
        else:
            log.warning("reddit_enabled but credentials missing; skipping")

    if cfg.get("social.stocktwits_enabled", False):
        collectors.append(StocktwitsCollector(
            cfg.get("social.stocktwits_symbols", [])))

    if cfg.get("social.x_enabled", False):
        accounts = cfg.get("social.x_accounts", []) or []
        instances = cfg.get("social.x_nitter_instances", []) or []
        if accounts and (instances or cfg.secret("X_BEARER_TOKEN")):
            collectors.append(XCollector(
                accounts, instances, cfg.secret("X_BEARER_TOKEN")))
        else:
            log.warning("x_enabled but no accounts / nitter instances / token; "
                        "skipping X collector")

    log.info("Active collectors: %s", [c.name for c in collectors])
    return collectors
