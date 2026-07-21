"""Reddit collector (optional, free app credentials).

Reddit's free API works well with a "script" app. This is social sentiment
and is treated as low-credibility in the scoring (see classification config)
- useful for early signal but weighted for manipulation/noise risk.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from .base import Collector, NewsItem

log = logging.getLogger("oljan.collectors.reddit")


class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, client_id: str, client_secret: str, user_agent: str,
                 subreddits: list[str], limit: int = 25):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent or "oljan-oil-monitor/1.0"
        self.subreddits = subreddits or []
        self.limit = limit
        self._reddit = None

    def _client(self):
        if self._reddit is None:
            import praw  # lazy import; optional dependency
            self._reddit = praw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
                check_for_async=False,
            )
        return self._reddit

    def collect(self) -> Iterable[NewsItem]:
        if not (self.client_id and self.client_secret):
            return []
        items: list[NewsItem] = []
        try:
            reddit = self._client()
        except Exception as e:
            log.warning("Reddit client init failed: %s", e)
            return []
        for sub in self.subreddits:
            try:
                for post in reddit.subreddit(sub).new(limit=self.limit):
                    ts = datetime.fromtimestamp(
                        getattr(post, "created_utc", 0), tz=timezone.utc)
                    items.append(NewsItem(
                        source="reddit",
                        title=getattr(post, "title", "") or "",
                        content=(getattr(post, "selftext", "") or "")[:800],
                        url="https://reddit.com" + getattr(post, "permalink", ""),
                        ts=ts,
                        extra={"subreddit": sub,
                               "score": getattr(post, "score", 0)},
                    ))
            except Exception as e:
                log.warning("Reddit fetch failed for r/%s: %s", sub, e)
        return items
