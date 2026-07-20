"""Collector base types.

A collector polls one source and yields NewsItem objects. Collectors must be
resilient: any network/parse error is caught and logged, never raised into the
main loop.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass
class NewsItem:
    source: str                     # e.g. "oilprice.com", "reddit", "eia.gov"
    title: str
    content: str
    url: str
    ts: datetime                    # UTC, publication time (best available)
    symbol: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}. {self.content}".strip()

    @property
    def hash(self) -> str:
        basis = (self.url or "") + "|" + (self.title or "")
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Collector:
    """Interface for all collectors."""

    name: str = "base"

    def collect(self) -> Iterable[NewsItem]:  # pragma: no cover - interface
        raise NotImplementedError
