"""Cross-source story clustering.

The same market-moving development often arrives from several sources within
minutes (e.g. an OPEC decision hits Reuters, a squawk account and OilPrice).
Treating each as a separate alert is noisy AND throws away the single most
useful signal: independent corroboration.

This module groups incoming items into *stories* by shared salient tokens, so
downstream we emit ONE alert per story that:
  * counts distinct corroborating sources (real substance signal),
  * shows which source was FIRST (lead-time intelligence), and
  * cites all sources instead of spamming one per source.

Deliberately simple and transparent: token-set Jaccard + shared named entities,
greedy single-link clustering. No models, no training.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .collectors.base import NewsItem

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "has",
    "have", "had", "will", "would", "says", "say", "said", "after", "before",
    "amid", "over", "into", "out", "up", "down", "new", "more", "than", "that",
    "this", "it", "its", "their", "his", "her", "they", "you", "we", "he", "she",
    "not", "no", "breaking", "just", "report", "reports", "update", "news",
    "oil", "crude", "prices", "price", "market", "markets", "brent", "wti",
}

# Named entities that strongly define a story (weighted higher when matching).
_ENTITIES = {
    "opec", "opec+", "saudi", "russia", "russian", "iran", "iranian", "israel",
    "israeli", "ukraine", "hormuz", "houthi", "houthis", "venezuela", "libya",
    "nigeria", "iraq", "kuwait", "uae", "gaza", "lebanon", "hezbollah", "us",
    "china", "india", "fed", "eia", "op3c", "strait", "redsea", "tanker",
    "refinery", "pipeline", "nuclear", "sanctions", "embargo", "ceasefire",
    "trump", "putin", "netanyahu", "spr", "kremlin", "tehran", "moscow",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+\+?")


@dataclass
class Story:
    items: list[NewsItem] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)

    def add(self, item: NewsItem, toks: set[str], ents: set[str]) -> None:
        self.items.append(item)
        self.tokens |= toks
        self.entities |= ents

    @property
    def sources(self) -> list[str]:
        seen, out = set(), []
        for it in sorted(self.items, key=lambda i: i.ts):
            if it.source not in seen:
                seen.add(it.source)
                out.append(it.source)
        return out

    @property
    def n_sources(self) -> int:
        return len(set(i.source for i in self.items))

    @property
    def first_item(self) -> NewsItem:
        return min(self.items, key=lambda i: i.ts)

    @property
    def first_ts(self) -> datetime:
        return self.first_item.ts

    def representative(self, source_weight: Callable[[str], float]) -> NewsItem:
        # Most credible source, tie-break on the most descriptive (longest) title.
        return max(self.items,
                   key=lambda i: (source_weight(i.source), len(i.title)))

    def key(self) -> str:
        """Stable signature for cross-poll dedup (top salient tokens)."""
        salient = sorted(self.entities) or sorted(self.tokens)
        return "|".join(salient[:4])


def tokenize(text: str) -> tuple[set[str], set[str]]:
    toks = {t for t in _TOKEN_RE.findall((text or "").lower())
            if len(t) >= 3 and t not in _STOPWORDS}
    ents = toks & _ENTITIES
    return toks, ents


def _similar(a: Story, at: set[str], ae: set[str], sim: float) -> bool:
    inter = len(a.tokens & at)
    union = len(a.tokens | at) or 1
    jacc = inter / union
    shared_ents = len(a.entities & ae)
    # Same story if token-overlap is high, OR they share a named entity with
    # at least modest token overlap (handles paraphrases across sources).
    return jacc >= sim or (shared_ents >= 1 and jacc >= 0.25) or \
        (shared_ents >= 2 and jacc >= 0.15)


def cluster_items(items: list[NewsItem], source_weight: Callable[[str], float],
                  sim: float = 0.4) -> list[Story]:
    stories: list[Story] = []
    for it in items:
        toks, ents = tokenize(f"{it.title} {it.title}")  # weight title terms
        placed = False
        for st in stories:
            if _similar(st, toks, ents, sim):
                st.add(it, toks, ents)
                placed = True
                break
        if not placed:
            s = Story()
            s.add(it, toks, ents)
            stories.append(s)
    return stories
