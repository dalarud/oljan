"""NewsAPI collector (optional, free dev tier).

https://newsapi.org free tier is limited (100 req/day, 24h-delayed articles)
so it's off by default and best used as a supplement to RSS.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import requests

from .base import Collector, NewsItem

log = logging.getLogger("oljan.collectors.newsapi")

URL = "https://newsapi.org/v2/everything"


class NewsApiCollector(Collector):
    name = "newsapi"

    def __init__(self, api_key: str, query: str):
        self.api_key = api_key
        self.query = query or "crude oil OR WTI OR Brent OR OPEC"

    def collect(self) -> Iterable[NewsItem]:
        if not self.api_key:
            return []
        try:
            resp = requests.get(URL, params={
                "q": self.query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 30,
                "apiKey": self.api_key,
            }, timeout=20)
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
        except Exception as e:
            log.warning("NewsAPI fetch failed: %s", e)
            return []
        items: list[NewsItem] = []
        for a in articles:
            try:
                ts = datetime.strptime(
                    a.get("publishedAt", ""), "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)
            items.append(NewsItem(
                source="newsapi",
                title=a.get("title", "") or "",
                content=(a.get("description", "") or a.get("content", "") or "")[:800],
                url=a.get("url", "") or "",
                ts=ts,
                extra={"outlet": (a.get("source") or {}).get("name")},
            ))
        return items
