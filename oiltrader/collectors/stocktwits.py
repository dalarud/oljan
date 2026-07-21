"""Stocktwits collector (keyless, best-effort).

Stocktwits exposes a public JSON stream per symbol. It may rate-limit or
change; failures are swallowed. Treated as low-credibility social signal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import requests

from .base import Collector, NewsItem

log = logging.getLogger("oljan.collectors.stocktwits")

URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"


class StocktwitsCollector(Collector):
    name = "stocktwits"

    def __init__(self, symbols: list[str]):
        self.symbols = symbols or []

    def collect(self) -> Iterable[NewsItem]:
        items: list[NewsItem] = []
        for sym in self.symbols:
            try:
                resp = requests.get(
                    URL.format(sym=sym), timeout=15,
                    headers={"User-Agent": "oljan-oil-monitor/1.0"})
                if resp.status_code != 200:
                    log.debug("Stocktwits %s returned %s", sym, resp.status_code)
                    continue
                messages = resp.json().get("messages", [])
            except Exception as e:
                log.debug("Stocktwits fetch failed for %s: %s", sym, e)
                continue
            for m in messages[:30]:
                body = m.get("body", "") or ""
                created = m.get("created_at", "")
                try:
                    ts = datetime.strptime(
                        created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
                mid = m.get("id", "")
                items.append(NewsItem(
                    source="stocktwits",
                    title=body[:120],
                    content=body,
                    url=f"https://stocktwits.com/message/{mid}",
                    ts=ts,
                    extra={"symbol_tag": sym},
                ))
        return items
