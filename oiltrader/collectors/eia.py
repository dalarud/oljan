"""EIA weekly petroleum inventory collector (official, free key).

The EIA Weekly Petroleum Status Report is one of the most reliably
market-moving scheduled events for crude (released Wednesdays ~15:30 UTC).
We fetch weekly crude ending stocks and, when a new week appears, emit an
item describing the week-over-week change. We do not have a free consensus
estimate, so we frame the change against the prior week and the trailing
5-year-ish average (build vs. draw), which the directional lexicon then reads.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import requests

from .base import Collector, NewsItem

log = logging.getLogger("oljan.collectors.eia")

# Weekly Ending Stocks of Crude Oil (excl. SPR), thousand barrels.
EIA_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"


class EiaCollector(Collector):
    name = "eia"

    def __init__(self, api_key: str, storage):
        self.api_key = api_key
        self.storage = storage

    def collect(self) -> Iterable[NewsItem]:
        if not self.api_key:
            return []
        try:
            resp = requests.get(EIA_URL, params={
                "api_key": self.api_key,
                "frequency": "weekly",
                "data[0]": "value",
                # WCESTUS1 = Weekly U.S. Ending Stocks of Crude Oil
                "facets[series][]": "WCESTUS1",
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                "length": 6,
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("response", {}).get("data", [])
        except Exception as e:
            log.warning("EIA fetch failed: %s", e)
            return []

        if len(data) < 2:
            return []

        latest = data[0]
        period = latest.get("period")
        # Only emit once per new report.
        if self.storage.get_meta("eia_last_period") == period:
            return []

        try:
            latest_val = float(latest["value"])
            prev_val = float(data[1]["value"])
        except (KeyError, TypeError, ValueError):
            return []

        change = latest_val - prev_val  # thousand barrels
        avg_recent = sum(float(d["value"]) for d in data[1:]) / (len(data) - 1)
        vs_avg = latest_val - avg_recent

        direction_word = "build" if change > 0 else "drawdown"
        mb = change / 1000.0  # million barrels
        title = (f"EIA weekly crude inventories: {direction_word} of "
                 f"{abs(mb):.1f} million barrels")
        content = (
            f"U.S. commercial crude stocks (excl. SPR) {'rose' if change > 0 else 'fell'} "
            f"by {abs(mb):.1f} million barrels week-over-week to {latest_val/1000:.1f} "
            f"million barrels (period {period}). That is a {direction_word} versus the "
            f"prior week and {'above' if vs_avg > 0 else 'below'} the recent average. "
            f"A larger-than-expected build is typically bearish; an unexpected draw is "
            f"typically bullish."
        )

        self.storage.set_meta("eia_last_period", period)
        ts = datetime.now(timezone.utc)
        return [NewsItem(
            source="eia.gov",
            title=title,
            content=content,
            url="https://www.eia.gov/petroleum/supply/weekly/",
            ts=ts,
            extra={"eia_change_mb": round(mb, 2),
                   "eia_level_mb": round(latest_val / 1000, 1),
                   "eia_period": period},
        )]
