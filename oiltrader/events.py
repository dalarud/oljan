"""Event processing: relevance, categorisation, and the core
substance-vs-manipulation assessment.

All scoring is rule-based and transparent. Each Event carries the factors
that produced its scores so the notification can explain itself.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from .collectors.base import NewsItem
from .indicators import ChartContext
from .sentiment import SentimentEngine, SentimentResult

log = logging.getLogger("oljan.events")

# category -> keywords that place an item in it (checked in order).
CATEGORIES = {
    "inventory": ["inventory", "stockpile", "stocks", "drawdown", "draw",
                  "build", "eia", "api ", "barrels"],
    "opec": ["opec", "opec+", "saudi", "quota", "production cut", "output cut"],
    "geopolitical": ["war", "attack", "sanction", "iran", "russia", "hormuz",
                     "strike", "escalation", "ceasefire", "embargo", "israel",
                     "ukraine", "pipeline", "houthi"],
    "supply": ["refinery", "outage", "hurricane", "pipeline", "production",
               "output", "rig", "spr"],
    "macro": ["fed", "rate", "recession", "dollar", "inflation", "gdp",
              "demand", "china"],
}

_NUMBER_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:%|percent|million|barrels|bpd|kb/d|mb/d|"
    r"thousand|dollars?|\$)", re.IGNORECASE)


@dataclass
class Event:
    item: NewsItem
    relevance: float
    category: str
    sentiment: SentimentResult
    direction: str                     # bullish | bearish | neutral
    magnitude: float
    substance: float                   # 0..1
    manipulation: float                # 0..1
    is_substantial: bool
    manipulation_flag: bool
    confidence: str                    # low | medium | high
    factors: dict[str, Any] = field(default_factory=dict)
    event_id: Optional[int] = None

    @property
    def symbol(self) -> Optional[str]:
        return self.item.symbol


class EventProcessor:
    def __init__(self, cfg, storage, sentiment: SentimentEngine):
        self.cfg = cfg
        self.storage = storage
        self.sentiment = sentiment
        self.keywords = {str(k).lower(): float(v)
                         for k, v in (cfg.get("relevance.keywords", {}) or {}).items()}
        self.min_score = cfg.get("relevance.min_score", 2.0)
        self.source_weights = {str(k).lower(): float(v) for k, v in
                               (cfg.get("classification.source_weights", {}) or {}).items()}
        self.corr_window = cfg.get("classification.corroboration_window_minutes", 90)
        self.confirm_candles = cfg.get("classification.confirmation_candles", 4)
        self.substance_threshold = cfg.get("classification.substance_threshold", 0.5)
        self.manip_threshold = cfg.get("classification.manipulation_threshold", 0.55)

    # --------------------------------------------------------------- relevance
    def relevance(self, item: NewsItem) -> float:
        text = item.text.lower()
        score = 0.0
        for kw, w in self.keywords.items():
            if kw in text:
                score += w
        return round(score, 2)

    def categorise(self, item: NewsItem) -> str:
        text = item.text.lower()
        best, best_hits = "other", 0
        for cat, kws in CATEGORIES.items():
            hits = sum(1 for kw in kws if kw in text)
            if hits > best_hits:
                best, best_hits = cat, hits
        return best

    def source_weight(self, source: str) -> float:
        # Most-specific (longest) matching key wins, so "x/@deitaone" is scored
        # by its own weight rather than a generic "x/" fallback.
        s = (source or "").lower()
        best_w, best_len = None, -1
        for key, w in self.source_weights.items():
            k = key.lower()
            if k == "unknown":
                continue
            if k in s and len(k) > best_len:
                best_w, best_len = w, len(k)
        return best_w if best_w is not None else self.source_weights.get("unknown", 0.4)

    # -------------------------------------------------------------- assessment
    def process(self, item: NewsItem, chart: Optional[ChartContext]) -> Optional[Event]:
        rel = self.relevance(item)
        if rel < self.min_score:
            return None

        sent = self.sentiment.analyze(item.text)
        category = self.categorise(item)
        direction = sent.bias
        magnitude = sent.magnitude

        factors: dict[str, Any] = {}

        # -- source credibility
        src_w = self.source_weight(item.source)
        factors["source_weight"] = src_w

        # -- corroboration: distinct sources reporting same category recently
        since = item.ts - timedelta(minutes=self.corr_window)
        recent = self.storage.recent_events(since, category=category)
        distinct_sources = {e["source"] for e in recent
                            if e.get("direction") == direction}
        corroboration = len(distinct_sources)
        corr_norm = min(corroboration / 2.0, 1.0)   # 2+ corroborating = full
        factors["corroboration_sources"] = corroboration

        # -- specificity: does it cite hard numbers/units?
        specific = bool(_NUMBER_RE.search(item.text))
        factors["specific_numbers"] = specific

        # -- price/volume confirmation: is the tape already moving the same way
        #    on elevated volume? (real news tends to leave a footprint)
        confirmation = self._confirmation(chart, direction)
        factors["price_confirmation"] = round(confirmation, 2)

        # ---- substance score (0..1): weighted, transparent
        substance = (
            0.40 * src_w
            + 0.25 * corr_norm
            + 0.15 * (1.0 if specific else 0.0)
            + 0.20 * confirmation
        )
        substance = _clamp01(substance)

        # ---- manipulation / noise risk (0..1): a big claim from a weak,
        #      uncorroborated, unconfirmed source is the classic red flag.
        mag_norm = min(magnitude / 2.5, 1.0)
        manipulation = mag_norm * (
            0.5 * (1 - src_w)
            + 0.3 * (1 - corr_norm)
            + 0.2 * (1 - confirmation)
        )
        manipulation = _clamp01(manipulation)

        is_substantial = substance >= self.substance_threshold
        manip_flag = manipulation >= self.manip_threshold

        confidence = self._confidence(substance, corroboration, src_w)

        return Event(
            item=item,
            relevance=rel,
            category=category,
            sentiment=sent,
            direction=direction,
            magnitude=magnitude,
            substance=round(substance, 3),
            manipulation=round(manipulation, 3),
            is_substantial=is_substantial,
            manipulation_flag=manip_flag,
            confidence=confidence,
            factors=factors,
        )

    def _confirmation(self, chart: Optional[ChartContext], direction: str) -> float:
        """0..1 – does recent price/volume action agree with the news bias?"""
        if chart is None or direction == "neutral":
            return 0.0
        # Use trend + elevated volume as a lightweight confirmation proxy.
        agree = ((direction == "bullish" and chart.trend == "up") or
                 (direction == "bearish" and chart.trend == "down"))
        vol_boost = min(max(chart.rel_volume - 1.0, 0.0), 1.0)  # >avg volume
        base = 0.6 if agree else 0.0
        return _clamp01(base + 0.4 * vol_boost)

    @staticmethod
    def _confidence(substance: float, corroboration: int, src_w: float) -> str:
        if substance >= 0.65 and (corroboration >= 1 or src_w >= 0.9):
            return "high"
        if substance >= 0.45:
            return "medium"
        return "low"

    # ------------------------------------------------------------- persistence
    def persist(self, event: Event) -> int:
        eid = self.storage.insert_event({
            "ts": event.item.ts,
            "symbol": event.symbol or self.cfg.primary_instrument.get("symbol"),
            "source": event.item.source,
            "title": event.item.title,
            "url": event.item.url,
            "content": event.item.content,
            "category": event.category,
            "direction": event.direction,
            "magnitude": event.magnitude,
            "relevance": event.relevance,
            "substance": event.substance,
            "manipulation": event.manipulation,
            "confidence": event.confidence,
            "extra": {**event.factors, **event.item.extra,
                      "matched": event.sentiment.matched},
        })
        event.event_id = eid
        return eid


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
