"""Event processing: relevance, categorisation, and the core
substance-vs-manipulation assessment.

All scoring is rule-based and transparent. Each Event carries the factors
that produced its scores so the notification can explain itself.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .collectors.base import NewsItem
from .indicators import ChartContext
from .llm import LlmClassifier
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
    # cross-source story context
    sources: list[str] = field(default_factory=list)
    source_times: list = field(default_factory=list)  # [(source, ts)] by ts
    n_sources: int = 1
    first_ts: Optional["Any"] = None   # earliest publication across sources
    age_minutes: float = 0.0
    freshness: float = 1.0             # 0..1, decays with age

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
        self.freshness_halflife = cfg.get("news.freshness_halflife_minutes", 60)
        # word-boundary matchers so "build" != "building", "api" != "capital"
        self._kw_pat = {kw: _wb(kw) for kw in self.keywords}
        self._cat_pat = {cat: [_wb(k) for k in kws]
                         for cat, kws in CATEGORIES.items()}
        # Optional LLM reader for the highest-relevance items (off by default).
        self.llm = LlmClassifier(cfg)

    # --------------------------------------------------------------- relevance
    def relevance(self, item: NewsItem) -> float:
        text = item.text.lower()
        score = 0.0
        for kw, w in self.keywords.items():
            if self._kw_pat[kw].search(text):
                score += w
        return round(score, 2)

    def categorise(self, item: NewsItem) -> str:
        text = item.text.lower()
        best, best_hits = "other", 0
        for cat, pats in self._cat_pat.items():
            hits = sum(1 for p in pats if p.search(text))
            if hits > best_hits:
                best, best_hits = cat, hits
        return best

    _SIZE_RES = [
        (re.compile(r"(\d+(?:\.\d+)?)\s*m(?:illion)?\s*(?:barrels|bbl)", re.I), 5.0),
        (re.compile(r"(\d+(?:\.\d+)?)\s*mb/?d", re.I), 2.0),
        (re.compile(r"(\d+(?:\.\d+)?)\s*(?:kb/?d|thousand\s*(?:barrels|bpd))", re.I), 2000.0),
        (re.compile(r"(\d+(?:\.\d+)?)\s*%", re.I), 5.0),
        (re.compile(r"\$\s*(\d+(?:\.\d+)?)", re.I), 100.0),
    ]

    def _extract_size(self, text: str) -> float:
        """0..1 impact score from any hard magnitude cited in the text."""
        best = 0.0
        for rx, denom in self._SIZE_RES:
            for m in rx.finditer(text or ""):
                try:
                    best = max(best, min(float(m.group(1)) / denom, 1.0))
                except ValueError:
                    continue
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
    def process(self, item: NewsItem, chart: Optional[ChartContext],
                now=None) -> Optional[Event]:
        """Assess a single item (wraps it as a one-item story)."""
        from .clustering import Story
        st = Story()
        toks, ents = set(), set()
        st.add(item, toks, ents)
        return self.process_story(st, chart, now)

    def process_story(self, story, chart: Optional[ChartContext],
                      now=None) -> Optional[Event]:
        """Assess a clustered story (one or more items about the same event)."""
        now = now or datetime.now(timezone.utc)
        rep = story.representative(self.source_weight)
        # relevance = the strongest item in the story
        rel = max(self.relevance(it) for it in story.items)
        if rel < self.min_score:
            return None

        sent = self.sentiment.analyze(rep.text)
        category = self.categorise(rep)

        # -- aggregate NET direction across every item in the story, weighted
        #    by source credibility (robust to one odd headline; detects when
        #    sources genuinely conflict).
        agg, pos, neg = 0.0, 0, 0
        for it in story.items:
            s = self.sentiment.analyze(it.text)
            agg += s.directional_score * (0.5 + 0.5 * self.source_weight(it.source))
            if s.directional_score > 0.3:
                pos += 1
            elif s.directional_score < -0.3:
                neg += 1
        if agg > 0.3:
            direction = "bullish"
        elif agg < -0.3:
            direction = "bearish"
        else:
            direction = "neutral"
        conflict = pos > 0 and neg > 0
        magnitude = max(sent.magnitude, min(abs(agg) / max(len(story.items), 1), 3.0))

        factors: dict[str, Any] = {}
        factors["conflict"] = conflict

        # -- optional LLM read of the strongest item: better at context,
        #    conditionals and novel phrasing than the keyword lexicon. Only
        #    the representative (highest-weight) headline is sent, and only
        #    when relevance clears the LLM threshold, to bound cost. Degrades
        #    silently to the lexicon direction when disabled or on error.
        factors["is_action"] = None
        if self.llm.enabled and rel >= self.llm.min_relevance:
            llm_res = self.llm.classify(rep.text, key=rep.hash)
            if llm_res is not None:
                direction = llm_res.direction
                # LLM magnitude is 0..1; map onto the lexicon's ~0..3 scale
                # and keep the stronger of the two reads.
                magnitude = max(magnitude, llm_res.magnitude * 3.0)
                factors["is_action"] = llm_res.is_action
                factors["llm"] = {
                    "direction": llm_res.direction,
                    "event_type": llm_res.event_type,
                    "is_action": llm_res.is_action,
                    "magnitude": round(llm_res.magnitude, 2),
                    "confidence": round(llm_res.confidence, 2),
                    "rationale_sv": llm_res.rationale_sv,
                }

        # -- event size from hard numbers (bigger => more market impact)
        size = max((self._extract_size(it.text) for it in story.items),
                   default=0.0)
        factors["size"] = round(size, 2)

        # -- source credibility: use the MOST credible corroborating source
        best_src_w = max(self.source_weight(s) for s in story.sources)
        factors["source_weight"] = round(best_src_w, 2)

        # -- corroboration: distinct independent sources on the SAME story
        corroboration = story.n_sources
        corr_norm = min(max(corroboration - 1, 0) / 2.0, 1.0)  # 3+ sources = full
        factors["corroboration_sources"] = corroboration

        # -- specificity: does any item cite hard numbers/units?
        specific = any(bool(_NUMBER_RE.search(it.text)) for it in story.items)
        factors["specific_numbers"] = specific

        # -- price/volume confirmation
        confirmation = self._confirmation(chart, direction)
        factors["price_confirmation"] = round(confirmation, 2)

        # -- recency / freshness (intraday: stale news is far less actionable)
        first_ts = story.first_ts
        if first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)
        first_by_src: dict[str, Any] = {}
        for it in story.items:
            ts = it.ts if it.ts.tzinfo else it.ts.replace(tzinfo=timezone.utc)
            if it.source not in first_by_src or ts < first_by_src[it.source]:
                first_by_src[it.source] = ts
        source_times = sorted(first_by_src.items(), key=lambda kv: kv[1])
        age_min = max((now - first_ts).total_seconds() / 60.0, 0.0)
        freshness = 0.5 ** (age_min / max(self.freshness_halflife, 1))
        factors["freshness"] = round(freshness, 2)
        factors["age_minutes"] = round(age_min, 1)

        # ---- substance (0..1): truthiness of the claim
        substance = _clamp01(
            0.40 * best_src_w
            + 0.30 * corr_norm
            + 0.12 * (1.0 if specific else 0.0)
            + 0.18 * confirmation
        )

        # ---- manipulation / noise risk (0..1)
        mag_norm = min(magnitude / 2.5, 1.0)
        manipulation = _clamp01(mag_norm * (
            0.5 * (1 - best_src_w)
            + 0.3 * (1 - corr_norm)
            + 0.2 * (1 - confirmation)
        ))

        is_substantial = substance >= self.substance_threshold
        manip_flag = manipulation >= self.manip_threshold
        confidence = self._confidence(substance, corroboration, best_src_w)

        return Event(
            item=rep,
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
            sources=story.sources,
            source_times=source_times,
            n_sources=corroboration,
            first_ts=first_ts,
            age_minutes=round(age_min, 1),
            freshness=round(freshness, 3),
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
        # Multiple independent sources on the same story is a strong signal.
        if substance >= 0.6 and (corroboration >= 2 or src_w >= 0.9):
            return "high"
        if substance >= 0.45 or corroboration >= 2:
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
                      "matched": event.sentiment.matched,
                      "sources": event.sources, "n_sources": event.n_sources},
        })
        event.event_id = eid
        return eid


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _wb(term: str) -> "re.Pattern":
    """Word-boundary matcher for a keyword/phrase (alnum-boundary aware)."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")
