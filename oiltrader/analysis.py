"""The analysis brain.

Combines the chart context, the event assessment and the historical analog
study into a single, transparent, leverage-aware recommendation. Output is a
structured Analysis plus a formatted (Swedish) notification message that always
states confidence, sources and uncertainties, and never hides the reasoning.

This is decision *support*, not automated trading and not financial advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .events import Event
from .historical import AnalogReport, HistoricalEngine
from .indicators import ChartContext


@dataclass
class Analysis:
    event: Event
    chart: Optional[ChartContext]
    analogs: AnalogReport
    headline: str
    assessment: str                 # substance vs manipulation verdict
    recommendation: str
    confidence: str
    uncertainties: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    suggested_stop: Optional[float] = None
    message: str = ""


class Analyzer:
    def __init__(self, cfg, historical: HistoricalEngine):
        self.cfg = cfg
        self.historical = historical
        self.leverage = float(cfg.get("position.leverage", 1) or 1)
        self.side = str(cfg.get("position.side", "flat")).lower()
        self.entry = cfg.get("position.entry_price", None)

    def build(self, event: Event, chart: Optional[ChartContext],
              mtf_trends: Optional[dict[str, str]] = None) -> Analysis:
        analogs = self.historical.analog_report(
            event.category, event.direction, exclude_event_id=event.event_id)

        mtf_trends = mtf_trends or {}
        assessment = self._assessment(event)
        recommendation, stop = self._recommendation(event, chart, analogs,
                                                    mtf_trends)
        uncertainties = self._uncertainties(event, chart, analogs, mtf_trends)
        confidence = self._combined_confidence(event, analogs)

        headline = self._headline(event)
        sources = [event.item.source]

        message = self._format_message(
            event, chart, analogs, headline, assessment, recommendation,
            confidence, uncertainties, stop, mtf_trends)

        return Analysis(
            event=event, chart=chart, analogs=analogs, headline=headline,
            assessment=assessment, recommendation=recommendation,
            confidence=confidence, uncertainties=uncertainties,
            sources=sources, suggested_stop=stop, message=message,
        )

    # ------------------------------------------------------------- components
    def _headline(self, event: Event) -> str:
        dir_word = {"bullish": "hausse", "bearish": "baisse",
                    "neutral": "neutral"}[event.direction]
        return (f"[{event.category.upper()} · {dir_word}] "
                f"{event.item.title.strip()[:200]}")

    def _assessment(self, event: Event) -> str:
        s, m = event.substance, event.manipulation
        f = event.factors
        drivers = []
        drivers.append(f"källvikt {f.get('source_weight', 0):.2f}")
        drivers.append(f"{f.get('corroboration_sources', 0)} bekräftande källa(or)")
        drivers.append("konkreta siffror" if f.get("specific_numbers")
                       else "inga hårda siffror")
        drivers.append(f"pris/volym-bekräftelse {f.get('price_confirmation', 0):.2f}")
        driver_txt = ", ".join(drivers)

        if event.manipulation_flag and not event.is_substantial:
            verdict = ("SANNOLIKT BRUS / MÖJLIG MANIPULATION – stor påstådd "
                       "effekt men svag/obekräftad källa utan tape-stöd.")
        elif event.is_substantial and not event.manipulation_flag:
            verdict = ("SUBSTANSIELL – trovärdig, bekräftad och/eller redan "
                       "synlig i pris/volym.")
        elif event.is_substantial and event.manipulation_flag:
            verdict = ("BLANDAD – har substans men även manipulationsrisk; "
                       "behandla med försiktighet tills fler källor bekräftar.")
        else:
            verdict = ("OKLAR / LÅG SIGNAL – varken tydligt substansiell eller "
                       "tydligt brus; vänta på bekräftelse.")
        return (f"{verdict}\nSubstans={s:.2f}, manipulationsrisk={m:.2f} "
                f"({driver_txt}).")

    def _mtf_alignment(self, event: Event, mtf_trends: dict[str, str]):
        """Return (aligned, conflicting, arrow_line) across timeframes."""
        want = "up" if event.direction == "bullish" else \
               "down" if event.direction == "bearish" else None
        arrows = {"up": "↑", "down": "↓", "sideways": "→"}
        parts, aligned, conflicting = [], 0, 0
        for tf, tr in mtf_trends.items():
            parts.append(f"{tf} {arrows.get(tr, '→')}")
            if want is None:
                continue
            if tr == want:
                aligned += 1
            elif tr != "sideways":
                conflicting += 1
        return aligned, conflicting, " · ".join(parts)

    def _recommendation(self, event: Event, chart: Optional[ChartContext],
                        analogs: AnalogReport, mtf_trends: dict[str, str]):
        lines: list[str] = []
        stop: Optional[float] = None
        best = analogs.best_horizon()

        aligned, conflicting, arrow_line = self._mtf_alignment(event, mtf_trends)
        if arrow_line:
            verdict = ("samsyn över tidsramar" if aligned and not conflicting
                       else "blandad bild över tidsramar" if conflicting
                       else "neutral över tidsramar")
            lines.append(f"MTF-trend: {arrow_line}  ({verdict}).")

        # Historical framing
        if best and best.n > 0:
            move = "fortsatt upp" if event.direction == "bullish" else "fortsatt ned"
            lines.append(
                f"Historik: vid liknande {event.category}-händelser ({event.direction}) "
                f"gick priset {move} inom {best.horizon_h:g}h i {best.hit_pct()}% av "
                f"{best.n} fall (median {best.median_return*100:+.1f}%).")
        else:
            lines.append(
                "Historik: för få mognade analoga fall ännu – basera beslut på "
                "chart + källkvalitet tills databasen byggts upp.")

        # Manipulation-first guardrail
        if event.manipulation_flag and not event.is_substantial:
            lines.append(
                "Åtgärd: agera INTE på enbart denna nyhet. Vänta på bekräftelse "
                "(andra källor och/eller volym). Falska spikar reverserar ofta snabbt.")
            if chart and chart.nearest_support:
                stop = self._stop_for_long(chart)
                lines.append(
                    f"Skydd för x{self.leverage:g} long: håll en tight stop precis "
                    f"under support {chart.nearest_support:.2f} "
                    f"(~{stop:.2f}) för att inte fastna i en falsk rörelse.")
            return "\n".join(lines), stop

        # Substantial (or mixed) – give directional, position-aware advice
        aligns = ((event.direction == "bullish" and self.side == "long") or
                  (event.direction == "bearish" and self.side == "short"))
        against = ((event.direction == "bullish" and self.side == "short") or
                   (event.direction == "bearish" and self.side == "long"))

        if chart:
            stop = (self._stop_for_long(chart) if self.side == "long"
                    else self._stop_for_short(chart) if self.side == "short"
                    else None)

        if aligns:
            lines.append(
                f"Åtgärd: nyheten är i linje med din {self.side}. Överväg att "
                f"hålla/öka gradvis; låt vinnare löpa mot närmaste "
                f"{'motstånd' if self.side=='long' else 'stöd'}.")
        elif against:
            lines.append(
                f"Åtgärd: nyheten går EMOT din {self.side}. Överväg att minska, "
                f"hedga eller dra upp stoppen. Definiera din invalideringsnivå nu.")
        else:
            bias_word = "long" if event.direction == "bullish" else "short"
            lines.append(
                f"Åtgärd: du är flat. Om du vill ta position pekar signalen mot "
                f"{bias_word}; vänta gärna på en retest av nyckelnivå för bättre R/R.")

        # Concrete levels + leverage risk note
        if chart:
            if self.side in ("long", "flat") and chart.nearest_support:
                lines.append(
                    f"Nivåer: stöd {chart.nearest_support:.2f}"
                    + (f", motstånd {chart.nearest_resistance:.2f}"
                       if chart.nearest_resistance else "")
                    + f". ATR {chart.atr:.2f} ({chart.atr_pct:.1f}%).")
            if stop is not None:
                risk_pct = abs(chart.price - stop) / chart.price * 100.0
                lev_pct = risk_pct * self.leverage
                lines.append(
                    f"Förslag stop ~{stop:.2f} = {risk_pct:.1f}% på priset "
                    f"≈ {lev_pct:.0f}% på marginalen vid x{self.leverage:g}.")
            lines.append(self._leverage_risk_note(chart))

        return "\n".join(lines), stop

    def _stop_for_long(self, chart: ChartContext) -> Optional[float]:
        base = chart.nearest_support if chart.nearest_support else chart.price
        return round(base - 0.5 * chart.atr, 2)

    def _stop_for_short(self, chart: ChartContext) -> Optional[float]:
        base = chart.nearest_resistance if chart.nearest_resistance else chart.price
        return round(base + 0.5 * chart.atr, 2)

    def _leverage_risk_note(self, chart: ChartContext) -> str:
        if self.leverage <= 1:
            return "Ingen hävstång angiven."
        liq_move = 100.0 / self.leverage  # approx adverse % to liquidation
        atr_moves = (liq_move / chart.atr_pct) if chart.atr_pct else float("inf")
        return (f"Hävstångsrisk x{self.leverage:g}: en rörelse på ~{liq_move:.1f}% "
                f"mot dig ≈ likvidation (grovt). Det är bara ~{atr_moves:.1f} ATR – "
                f"håll marginal och undvik överexponering runt nyheter.")

    def _uncertainties(self, event: Event, chart, analogs,
                       mtf_trends: dict[str, str]) -> list[str]:
        u: list[str] = []
        if analogs.total_samples < self.historical.min_sample:
            u.append(f"Litet historiskt urval (n={analogs.total_samples}); "
                     f"basraten är osäker.")
        if event.confidence == "low":
            u.append("Låg källtillförlitlighet/bekräftelse för själva nyheten.")
        if chart is None:
            u.append("Ingen chart-kontext tillgänglig vid analystillfället.")
        if event.direction == "neutral":
            u.append("Oklar riktning – lexikonet gav ingen tydlig bias.")
        if event.manipulation_flag:
            u.append("Förhöjd manipulations-/brusrisk.")
        _, conflicting, _ = self._mtf_alignment(event, mtf_trends)
        if conflicting:
            u.append("Tidsramarna pekar åt olika håll – vänta på samsyn eller "
                     "handla mindre.")
        u.append("Korrelation ≠ kausalitet; historiska mönster upprepas inte "
                 "garanterat. Detta är beslutsstöd, inte finansiell rådgivning.")
        return u

    def _combined_confidence(self, event: Event, analogs: AnalogReport) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        rev = {0: "low", 1: "medium", 2: "high"}
        combined = min(order[event.confidence], order[analogs.confidence])
        return rev[combined]

    # ------------------------------------------------------------- formatting
    def _format_message(self, event, chart, analogs, headline, assessment,
                        recommendation, confidence, uncertainties, stop,
                        mtf_trends) -> str:
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}[confidence]
        tf = self.cfg.get("market_data.analysis_timeframe", "")
        parts = [f"🛢️ *OLJAN* {conf_emoji} konfidens: *{confidence.upper()}*",
                 f"*{headline}*"]

        if chart:
            parts.append(
                f"\n📊 *Chart* ({chart.symbol}"
                + (f", {tf}" if tf else "") + f"): pris {chart.price:.2f}, "
                f"trend {chart.trend}, RSI {chart.rsi:.0f} ({chart.rsi_state()}), "
                f"rel.volym {chart.rel_volume:.1f}x"
                + (f", stöd {chart.nearest_support:.2f}"
                   if chart.nearest_support else "")
                + (f", motstånd {chart.nearest_resistance:.2f}"
                   if chart.nearest_resistance else ""))

        parts.append(f"\n🔎 *Bedömning*\n{assessment}")
        parts.append(f"\n🧭 *Rekommendation*\n{recommendation}")

        if uncertainties:
            parts.append("\n⚠️ *Osäkerheter*\n- " + "\n- ".join(uncertainties))

        parts.append(f"\n📰 Källa: {event.item.source}"
                     + (f" · {event.item.url}" if event.item.url else ""))
        if event.sentiment.matched:
            parts.append("🔬 Nyckelord: " + event.sentiment.explain())
        parts.append("\n_Beslutsstöd, ej finansiell rådgivning._")
        return "\n".join(parts)
