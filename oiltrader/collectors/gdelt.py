"""GDELT 2.0 collector – broad, reliable global news/OSINT firehose.

GDELT indexes worldwide news every ~15 minutes and exposes a keyless DOC API.
It is a strong reliability backbone for OSINT breadth (supply-disrupting events
anywhere on earth) that doesn't depend on flaky Nitter instances. Requires a
User-Agent and asks for <=1 request / 5s, so it's polled once per news cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import requests

from .base import Collector, NewsItem, now_utc

log = logging.getLogger("oljan.collectors.gdelt")

URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERY = ('(crude oil OR Brent OR WTI OR OPEC OR "Strait of Hormuz" OR '
                 'refinery OR pipeline OR sanctions) sourcelang:english')


class GdeltCollector(Collector):
    name = "gdelt"

    def __init__(self, query: str = "", timespan: str = "1h",
                 max_records: int = 40):
        self.query = query or DEFAULT_QUERY
        self.timespan = timespan
        self.max_records = max_records

    def collect(self) -> Iterable[NewsItem]:
        try:
            resp = requests.get(URL, params={
                "query": self.query, "mode": "ArtList",
                "maxrecords": self.max_records, "timespan": self.timespan,
                "sort": "DateDesc", "format": "json",
            }, headers={"User-Agent": "oljan-oil-monitor/1.0"}, timeout=25)
            if resp.status_code != 200 or not resp.text.strip().startswith("{"):
                log.debug("GDELT non-JSON/status %s", resp.status_code)
                return []
            articles = resp.json().get("articles", [])
        except Exception as e:
            log.warning("GDELT fetch failed: %s", e)
            return []
        items: list[NewsItem] = []
        for a in articles:
            ts = _parse_seendate(a.get("seendate"))
            items.append(NewsItem(
                source=a.get("domain", "gdelt") or "gdelt",
                title=(a.get("title") or "").strip(),
                content="",
                url=a.get("url", "") or "",
                ts=ts,
                extra={"via": "gdelt", "country": a.get("sourcecountry")},
            ))
        return items


def _parse_seendate(s: str | None) -> datetime:
    if not s:
        return now_utc()
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return now_utc()
