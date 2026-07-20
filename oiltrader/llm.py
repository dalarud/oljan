"""Optional LLM interpretation of the highest-relevance items.

The keyword lexicon is a fast, transparent pre-filter but it can't read context
("OPEC *considers* a cut" vs "OPEC *cuts*"), conditionals, or novel phrasing.
When enabled, this module sends only the top items (by relevance) to a small,
cheap Claude model for a structured read: direction, whether it's a concrete
ACTION vs. mere talk/threat, magnitude, confidence, and a one-line Swedish
rationale. It degrades gracefully to the lexicon when disabled or on any error,
and caches by item hash to avoid re-classifying.

Requires the `anthropic` package and ANTHROPIC_API_KEY (both optional).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("oljan.llm")

_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "event_type": {"type": "string"},
        "is_action": {"type": "boolean"},
        "magnitude": {"type": "number"},
        "confidence": {"type": "number"},
        "rationale_sv": {"type": "string"},
    },
    "required": ["direction", "event_type", "is_action", "magnitude",
                 "confidence", "rationale_sv"],
    "additionalProperties": False,
}

_SYSTEM = (
    "Du är en analytiker för råoljemarknaden (Brent/WTI). Bedöm en enskild "
    "nyhetsrubrik utifrån dess sannolika påverkan på OLJEPRISET (inte allmän "
    "sentiment). Notera: krig/sanktioner/attacker är prispositiva (bullish) "
    "för olja; eldupphör/fredsavtal/ökat utbud är prisnegativa (bearish). "
    "Skilj konkret HÄNDELSE (is_action=true, t.ex. 'OPEC sänker produktionen') "
    "från enbart prat/hot/övervägande (is_action=false, t.ex. 'Iran hotar att "
    "stänga Hormuz'). magnitude och confidence är 0..1. rationale_sv: EN kort "
    "mening på svenska om prisimplikationen."
)


@dataclass
class LlmResult:
    direction: str
    event_type: str
    is_action: bool
    magnitude: float          # 0..1
    confidence: float         # 0..1
    rationale_sv: str


class LlmClassifier:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("llm.enabled", False))
        self.model = cfg.get("llm.model", "claude-haiku-4-5")
        self.min_relevance = cfg.get("llm.min_relevance", 3.0)
        self.timeout = cfg.get("llm.timeout_seconds", 20)
        self.api_key = ""
        self._client = None
        self._cache: dict[str, Optional[LlmResult]] = {}
        if self.enabled:
            # Only touch secrets when actually enabled (some callers pass a
            # minimal cfg without a .secret() accessor).
            self.api_key = getattr(cfg, "secret", lambda *_: "")("ANTHROPIC_API_KEY")
            if not self.api_key:
                log.warning("llm.enabled but ANTHROPIC_API_KEY missing; "
                            "disabling LLM.")
                self.enabled = False

    def _client_lazy(self):
        if self._client is None:
            import anthropic  # optional dependency, imported lazily
            self._client = anthropic.Anthropic(api_key=self.api_key,
                                               timeout=self.timeout)
        return self._client

    def classify(self, text: str, key: str = "") -> Optional[LlmResult]:
        if not self.enabled or not text.strip():
            return None
        if key and key in self._cache:
            return self._cache[key]
        try:
            client = self._client_lazy()
            resp = client.messages.create(
                model=self.model,
                max_tokens=512,
                system=_SYSTEM,
                messages=[{"role": "user", "content": text[:1500]}],
                output_config={"format": {"type": "json_schema",
                                          "schema": _SCHEMA}},
            )
            raw = next((b.text for b in resp.content if b.type == "text"), "")
            data = json.loads(raw)
            result = LlmResult(
                direction=str(data["direction"]),
                event_type=str(data["event_type"])[:40],
                is_action=bool(data["is_action"]),
                magnitude=max(0.0, min(float(data["magnitude"]), 1.0)),
                confidence=max(0.0, min(float(data["confidence"]), 1.0)),
                rationale_sv=str(data["rationale_sv"])[:200],
            )
        except Exception as e:
            log.warning("LLM classify failed (%s); falling back to lexicon: %s",
                        self.model, str(e)[:120])
            result = None
        if key:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = result
        return result
