"""Oil-directional sentiment.

Generic sentiment is the wrong tool for commodities: "war" and "sanctions"
are negative in tone but *bullish* for oil, while "ceasefire" is positive in
tone but *bearish*. So the primary signal here is a domain lexicon mapping
phrases to their expected impact on the oil price (bullish +, bearish -).
VADER is kept only as a weak secondary tie-breaker.

Everything is transparent: we return the matched phrases so a human can see
exactly why a bias was assigned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _VADER = SentimentIntensityAnalyzer()
except Exception:  # pragma: no cover
    _VADER = None


@dataclass
class SentimentResult:
    bias: str                       # bullish | bearish | neutral
    directional_score: float        # signed, oil-price impact
    magnitude: float                # abs(directional_score), roughly 0..3+
    vader_compound: float
    matched: list[tuple[str, float]] = field(default_factory=list)

    def explain(self) -> str:
        if not self.matched:
            return "inga riktade nyckelord matchade (neutral)"
        parts = [f"'{k}' ({v:+.1f})" for k, v in self.matched]
        return ", ".join(parts)


def _negated(text: str, start: int) -> bool:
    """True if a negator appears within ~4 words before position `start`."""
    prefix = text[:start]
    prev_words = re.findall(r"[a-z']+", prefix)[-4:]
    return any(w in _NEGATORS for w in prev_words)


def _normalise_lexicon(cfg) -> dict[str, float]:
    """Accepts either a flat mapping or a {bullish:{}, bearish:{}} shape."""
    lex = cfg.get("directional_lexicon", {}) or {}
    flat: dict[str, float] = {}
    if "bullish" in lex or "bearish" in lex:
        for group in ("bullish", "bearish"):
            for k, v in (lex.get(group) or {}).items():
                flat[str(k).lower()] = float(v)
    else:
        for k, v in lex.items():
            flat[str(k).lower()] = float(v)
    return flat


# Negators that, appearing just before a phrase, flip its price impact.
_NEGATORS = {"no", "not", "without", "denies", "denied", "deny", "denying",
             "avoids", "avoided", "avoid", "unlikely", "fails", "failed",
             "rules", "ruled", "halts", "halted", "rejects", "rejected",
             "cancels", "cancelled", "canceled", "postpones", "postponed"}


class SentimentEngine:
    def __init__(self, cfg):
        self.lexicon = _normalise_lexicon(cfg)
        # Longer phrases first so "larger than expected build" wins over "build".
        self._phrases = sorted(self.lexicon.keys(), key=len, reverse=True)
        # Precompile word-boundary patterns (so "build" != "building").
        self._patterns = {
            p: re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])")
            for p in self._phrases
        }

    def analyze(self, text: str) -> SentimentResult:
        low = (text or "").lower()
        matched: list[tuple[str, float]] = []
        consumed_spans: list[tuple[int, int]] = []
        score = 0.0
        for phrase in self._phrases:
            for m in self._patterns[phrase].finditer(low):
                span = (m.start(), m.end())
                # Skip if fully inside an already-matched (longer) phrase.
                if any(span[0] >= s and span[1] <= e
                       for s, e in consumed_spans):
                    continue
                consumed_spans.append(span)
                w = self.lexicon[phrase]
                # Negation: a negator within ~4 words before flips the impact.
                if _negated(low, m.start()):
                    w = -w
                    matched.append((f"NOT {phrase}", w))
                else:
                    matched.append((phrase, w))
                score += w
                break  # count each phrase once

        vader = 0.0
        if _VADER is not None:
            try:
                vader = _VADER.polarity_scores(text or "")["compound"]
            except Exception:
                vader = 0.0

        # Directional lexicon dominates. If it's silent, fall back to a weak
        # VADER read (note: tone, not oil-direction, so weighted lightly).
        directional = score
        if abs(directional) < 1e-9 and abs(vader) >= 0.5:
            directional = 0.4 * vader  # small, low-confidence nudge

        if directional > 0.3:
            bias = "bullish"
        elif directional < -0.3:
            bias = "bearish"
        else:
            bias = "neutral"

        return SentimentResult(
            bias=bias,
            directional_score=round(directional, 3),
            magnitude=round(abs(directional), 3),
            vader_compound=round(vader, 3),
            matched=matched,
        )
