"""RSS/Atom news collector (keyless, robust).

RSS is the most reliable free news source: no API key, no rate-limit games,
and most oil-relevant outlets publish feeds. Feeds are configurable.

Uses `feedparser` when available (best coverage), otherwise falls back to a
dependency-free stdlib parser (requests + xml.etree) so the system keeps
working even if feedparser can't be installed.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse

import requests

from .base import Collector, NewsItem, now_utc

log = logging.getLogger("oljan.collectors.rss")

try:
    import feedparser  # optional; nicer parsing when present
    _HAVE_FEEDPARSER = True
except Exception:  # pragma: no cover
    feedparser = None
    _HAVE_FEEDPARSER = False


def _source_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host or "unknown"
    except Exception:
        return "unknown"


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


class RssCollector(Collector):
    name = "rss"

    def __init__(self, feeds: list[str]):
        self.feeds = feeds or []

    def collect(self) -> Iterable[NewsItem]:
        items: list[NewsItem] = []
        for feed_url in self.feeds:
            try:
                if _HAVE_FEEDPARSER:
                    items.extend(self._collect_feedparser(feed_url))
                else:
                    items.extend(self._collect_stdlib(feed_url))
            except Exception as e:
                log.warning("RSS collect failed for %s: %s", feed_url, e)
        return items

    # ------------------------------------------------------------ feedparser
    def _collect_feedparser(self, feed_url: str) -> list[NewsItem]:
        parsed = feedparser.parse(feed_url)
        out: list[NewsItem] = []
        for entry in getattr(parsed, "entries", [])[:40]:
            link = getattr(entry, "link", "") or ""
            summary = (getattr(entry, "summary", "")
                       or getattr(entry, "description", "") or "")
            ts = now_utc()
            for key in ("published_parsed", "updated_parsed"):
                val = getattr(entry, key, None)
                if val:
                    ts = datetime.fromtimestamp(calendar.timegm(val),
                                                tz=timezone.utc)
                    break
            out.append(NewsItem(
                source=_source_from_url(link) or _source_from_url(feed_url),
                title=(getattr(entry, "title", "") or "").strip(),
                content=_strip_html(summary),
                url=link,
                ts=ts,
            ))
        return out

    # ---------------------------------------------------------------- stdlib
    def _collect_stdlib(self, feed_url: str) -> list[NewsItem]:
        import xml.etree.ElementTree as ET

        resp = requests.get(feed_url, timeout=20,
                            headers={"User-Agent": "oljan-oil-monitor/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        def _tag(el):  # strip namespace
            return el.tag.rsplit("}", 1)[-1]

        out: list[NewsItem] = []
        # RSS 2.0: channel/item ; Atom: entry
        items = [e for e in root.iter() if _tag(e) in ("item", "entry")]
        for it in items[:40]:
            title, link, desc, ts = "", "", "", now_utc()
            for child in it:
                t = _tag(child)
                if t == "title":
                    title = (child.text or "").strip()
                elif t == "link":
                    link = (child.text or child.get("href") or "").strip()
                elif t in ("description", "summary", "content"):
                    desc = child.text or desc
                elif t in ("pubDate", "published", "updated"):
                    ts = _parse_date(child.text) or ts
            out.append(NewsItem(
                source=_source_from_url(link) or _source_from_url(feed_url),
                title=title,
                content=_strip_html(desc),
                url=link,
                ts=ts,
            ))
        return out


def _parse_date(text: str | None):
    if not text:
        return None
    try:  # RFC 822 (RSS)
        dt = parsedate_to_datetime(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None
