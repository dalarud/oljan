"""The analysis brain.

Combines the chart context, the event assessment and the historical analog
study into a single, transparent, leverage-aware recommendation. Output is a
structured Analysis plus a formatted (Swedish) notification message that always
states confidence, sources and uncertainties, and never hides the reasoning.

This is decision *support*, not automated trading and not financial advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    conviction: int = 0             # 0..100 single triage number
    action_short: str = ""          # crisp action headline
    message: str = ""


class Analyzer:
    def __init__(self, cfg, historical: HistoricalEngine):
        self.cfg = cfg
        self.historical = historical
        self.leverage = float(cfg.get("position.leverage", 1) or 1)
        self.side = str(cfg.get("position.side", "flat")).lower()
        self.entry = cfg.get("position.entry_price", None)
        self.verbosity = str(cfg.get("notifications.verbosity", "compact")).lower()
        self.stale_after_min = float(cfg.get("notifications.stale_after_minutes", 20))
        # symbol -> friendly name (e.g. BZ=F -> "Brent (UKOIL)")
        self.names = {i.get("symbol"): i.get("name", i.get("symbol"))
                      for i in getattr(cfg, "instruments", []) or []}

    def build(self, event: Event, chart: Optional[ChartContext],
              mtf_trends: Optional[dict[str, str]] = None,
              levels=None) -> Analysis:
        analogs = self.historical.analog_report(
            event.category, event.direction, exclude_event_id=event.event_id)

        mtf_trends = mtf_trends or {}
        assessment = self._assessment(event)
        conviction = self._conviction(event, analogs, mtf_trends)
        action_short, recommendation, stop = self._recommendation(
            event, chart, analogs, mtf_trends)
        uncertainties = self._uncertainties(event, chart, analogs, mtf_trends)
        confidence = self._combined_confidence(event, analogs)
        headline = self._headline(event, conviction, action_short)

        if self.verbosity == "full":
            message = self._format_full(
                event, chart, analogs, headline, assessment, recommendation,
                confidence, uncertainties, stop, mtf_trends, conviction,
                action_short, levels)
        else:
            message = self._format_compact(
                event, chart, conviction, action_short, confidence, levels)

        return Analysis(
            event=event, chart=chart, analogs=analogs, headline=headline,
            assessment=assessment, recommendation=recommendation,
            confidence=confidence, uncertainties=uncertainties,
            sources=event.sources or [event.item.source], suggested_stop=stop,
            conviction=conviction, action_short=action_short, message=message,
        )

    # --------------------------------------------------------- plain language
    _MEANING = {
        ("inventory", "bullish"): "Stramare utbud än väntat → normalt prispositivt.",
        ("inventory", "bearish"): "Lageruppbyggnad/gott om utbud → normalt prisnegativt.",
        ("opec", "bullish"): "OPEC drar ned utbudet → normalt prispositivt.",
        ("opec", "bearish"): "OPEC ökar utbudet → normalt prisnegativt.",
        ("geopolitical", "bullish"): "Hot mot utbud/leveranser → riskpremie upp, prispositivt.",
        ("geopolitical", "bearish"): "Nedtrappning/fred → lägre riskpremie, prisnegativt.",
        ("supply", "bullish"): "Utbudsstörning (produktion/export) → prispositivt.",
        ("supply", "bearish"): "Ökat utbud/återstart → prisnegativt.",
        ("macro", "bullish"): "Starkare efterfrågeutsikter/svagare USD → prispositivt.",
        ("macro", "bearish"): "Svagare efterfrågan/recessionsoro → prisnegativt.",
    }

    def _plain_meaning(self, event: Event) -> str:
        # An LLM read (when enabled) captures context the lexicon can't; prefer
        # its one-line Swedish rationale over the category template.
        llm = event.factors.get("llm")
        if llm and llm.get("rationale_sv"):
            note = " (endast prat/hot, ej konkret händelse)" \
                if llm.get("is_action") is False else ""
            return f"{llm['rationale_sv']}{note}"
        base = self._MEANING.get((event.category, event.direction))
        if base:
            return base
        if event.direction == "bullish":
            return "Tolkas som prispositivt för olja."
        if event.direction == "bearish":
            return "Tolkas som prisnegativt för olja."
        return "Oklar prispåverkan – riktning ej fastställd."

    def _verdict_plain(self, event: Event) -> str:
        n = event.n_sources
        if event.manipulation_flag and not event.is_substantial:
            return f"⚠️ Ser ut som brus/rykte ({n} källa) – avvakta bekräftelse."
        if n > 1 and event.is_substantial:
            return f"Bekräftat av {n} oberoende källor – bedöms substansiellt."
        if event.is_substantial:
            return "Trovärdig källa – bedöms substansiellt."
        return f"Endast {n} källa, obekräftat – behandla försiktigt."

    # ------------------------------------------------------------- conviction
    def _conviction(self, event: Event, analogs: AnalogReport,
                    mtf_trends: dict[str, str]) -> int:
        """Single 0..100 triage number combining the transparent factors."""
        best = analogs.best_horizon()
        hist_edge = 0.0
        if best and best.n >= self.historical.min_sample:
            hist_edge = max(0.0, min((best.hit_rate - 0.5) * 2, 1.0))
        aligned, conflicting, _ = self._mtf_alignment(event, mtf_trends)
        n_tf = len(mtf_trends) or 0
        mtf_frac = (aligned / n_tf) if n_tf else 0.5
        src_w = float(event.factors.get("source_weight", 0.4))
        size = float(event.factors.get("size", 0.0))
        corr_norm = min(max(event.n_sources - 1, 0) / 2.0, 1.0)
        conv = 100 * (
            0.28 * event.substance
            + 0.18 * corr_norm
            + 0.14 * event.freshness
            + 0.10 * src_w
            + 0.10 * hist_edge
            + 0.12 * mtf_frac
            + 0.08 * size          # bigger cited magnitude => more impact
        )
        if event.manipulation_flag:
            conv *= 0.6
        if event.direction == "neutral":
            conv *= 0.5
        if event.factors.get("conflict"):
            conv *= 0.8            # sources disagree on direction
        if event.factors.get("is_action") is False:
            conv *= 0.75          # LLM read it as mere talk/threat, not action
        return int(round(max(0.0, min(conv, 100.0))))

    # ------------------------------------------------------------- components
    def _headline(self, event: Event, conviction: int, action: str) -> str:
        dir_word = {"bullish": "HAUSSE", "bearish": "BAISSE",
                    "neutral": "NEUTRAL"}[event.direction]
        src = f" · {event.n_sources} källor" if event.n_sources > 1 else ""
        return (f"[{dir_word} · konv {conviction}{src}] {action} — "
                f"{event.item.title.strip()[:160]}")

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
        """Return (action_short, detail_text, suggested_stop)."""
        lines: list[str] = []
        stop: Optional[float] = None

        # Manipulation-first guardrail
        if event.manipulation_flag and not event.is_substantial:
            action = "AVVAKTA – bekräfta först"
            lines.append(
                "Agera INTE på enbart denna uppgift. Vänta på bekräftelse "
                "(fler källor och/eller volym); falska spikar reverserar ofta snabbt.")
            if chart and chart.nearest_support:
                stop = self._stop_for_long(chart)
                lines.append(
                    f"Om redan x{self.leverage:g} long: tight stop precis under "
                    f"support {chart.nearest_support:.2f} (~{stop:.2f}).")
            return action, "\n".join(lines), stop

        aligns = ((event.direction == "bullish" and self.side == "long") or
                  (event.direction == "bearish" and self.side == "short"))
        against = ((event.direction == "bullish" and self.side == "short") or
                   (event.direction == "bearish" and self.side == "long"))

        if chart:
            stop = (self._stop_for_long(chart) if self.side == "long"
                    else self._stop_for_short(chart) if self.side == "short"
                    else None)

        # Name the concrete target level (the level "to let it run to").
        tgt_up = chart.nearest_resistance if chart else None
        tgt_dn = chart.nearest_support if chart else None

        if aligns:
            action = f"HÅLL/ÖKA {self.side}"
            if self.side == "long" and tgt_up:
                lines.append(f"I linje med din long. Överväg hålla/öka; låt "
                             f"vinnare löpa mot motstånd {tgt_up:.2f}.")
            elif self.side == "short" and tgt_dn:
                lines.append(f"I linje med din short. Överväg hålla/öka; låt "
                             f"vinnare löpa mot stöd {tgt_dn:.2f}.")
            else:
                lines.append(f"I linje med din {self.side}. Överväg hålla/öka "
                             f"gradvis; låt vinnare löpa.")
        elif against:
            action = f"MINSKA/HEDGA {self.side}"
            inval = tgt_dn if self.side == "long" else tgt_up
            inval_txt = f" Invalidering vid {inval:.2f}." if inval else ""
            lines.append(f"EMOT din {self.side}. Överväg minska/hedga eller dra "
                         f"upp stoppen.{inval_txt}")
        else:
            bias_word = "long" if event.direction == "bullish" else \
                        "short" if event.direction == "bearish" else "ingen"
            action = f"BEVAKA ({bias_word}-bias)" if bias_word != "ingen" else "BEVAKA"
            lines.append(
                f"Du är flat. Signalen pekar mot {bias_word}; avvakta gärna en "
                f"retest av nyckelnivå för bättre R/R.")

        return action, "\n".join(lines), stop

    def _levels_block(self, chart: Optional[ChartContext],
                      stop: Optional[float]) -> str:
        """Explicit price ladder (targets / support / stop), side-aware."""
        if chart is None:
            return ""
        lines: list[str] = []
        if self.side in ("long", "flat"):
            if chart.resistances:
                tgt = " → ".join(f"{r:.2f}" for r in chart.resistances[:3])
                lines.append(f"🎯 Mål upp (motstånd): {tgt}")
            else:
                lines.append("🎯 Inget motstånd ovanför (blue sky) – traila stop.")
            if chart.supports:
                sup = " → ".join(f"{s:.2f}" for s in chart.supports[:3])
                lines.append(f"🛡 Stöd nedåt: {sup}")
        else:  # short
            if chart.supports:
                tgt = " → ".join(f"{s:.2f}" for s in chart.supports[:3])
                lines.append(f"🎯 Mål ned (stöd): {tgt}")
            else:
                lines.append("🎯 Inget stöd nedanför – traila stop.")
            if chart.resistances:
                res = " → ".join(f"{r:.2f}" for r in chart.resistances[:3])
                lines.append(f"🛡 Motstånd uppåt: {res}")
        if stop is not None:
            risk = abs(chart.price - stop) / chart.price * 100.0
            lev = risk * self.leverage
            lines.append(f"🛑 Stop ~{stop:.2f}  ({risk:.1f}% på priset ≈ "
                         f"{lev:.0f}% på marginal vid x{self.leverage:g}). "
                         f"ATR {chart.atr:.2f}.")
        lines.append(self._leverage_risk_note(chart))
        return "\n".join(lines)

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

    # -------------------------------------------------------- compact format
    def _dir_emoji(self, event) -> str:
        return {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}[event.direction]

    def _price_line(self, chart, levels) -> str:
        if chart is None:
            return "📊 ⚠️ Ingen prisdata – inga nivåer."
        stale = chart.last_candle_age_min > self.stale_after_min
        if not chart.price_sane or stale:
            why = ("orimligt pris" if not chart.price_sane
                   else f"föråldrad ({chart.last_candle_age_min:.0f}m)")
            return f"📊 ⚠️ Prisdata otillförlitlig ({why}) – nivåer utelämnas."
        disp = self.names.get(chart.symbol, chart.symbol)
        ctf = chart.timeframe or ""
        est = " · est." if "scaled" in (chart.source or "") else \
              " · dagsdata" if ctf == "1d" else ""
        line = f"📊 {disp} {chart.price:.2f} ({ctf}{est})"
        if levels:
            if levels.day_high and levels.day_low:
                line += f" · dag {levels.day_low:.2f}–{levels.day_high:.2f}"
            if levels.pdh and levels.pdl:
                line += f" · igår {levels.pdl:.2f}–{levels.pdh:.2f}"
            if levels.vwap:
                line += f" · VWAP {levels.vwap:.2f}"
        return line

    def _action_line(self, event, chart, levels) -> str:
        if chart is None or not chart.price_sane \
                or chart.last_candle_age_min > self.stale_after_min:
            return f"🎯 {self._bias_word(event)} – agera på nivåer först när "\
                   f"prisdata är tillförlitlig."
        res = levels.resistances_above() if levels else \
            [("nivå", r) for r in (chart.resistances or [])[:3]]
        sup = levels.supports_below() if levels else \
            [("nivå", s) for s in (chart.supports or [])[:3]]
        r_txt = " / ".join(f"{v:.2f}" for _, v in res[:2]) or "inget ovanför"
        s_txt = " / ".join(f"{v:.2f}" for _, v in sup[:2]) or "inget nedanför"
        invalid = sup[0][1] if sup else None
        inval_txt = f" · ogiltig < {invalid:.2f}" if invalid else ""
        return (f"🎯 {self._bias_word(event)} · motstånd {r_txt} · "
                f"stöd {s_txt}{inval_txt}")

    def _bias_word(self, event) -> str:
        return {"bullish": "Long-bias", "bearish": "Short-bias",
                "neutral": "Neutral"}[event.direction]

    def _format_compact(self, event, chart, conviction, action_short,
                        confidence, levels) -> str:
        published = event.first_ts or event.item.ts
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        dir_word = {"bullish": "HAUSSE", "bearish": "BAISSE",
                    "neutral": "NEUTRAL"}[event.direction]
        src = f" · {event.n_sources} källor" if event.n_sources > 1 else \
              f" · {event.item.source}"
        parts = [
            f"{self._dir_emoji(event)} *{dir_word}* · konv {conviction} · "
            f"{_freshness_label(event)}{src}",
            f"*{event.item.title.strip()[:180]}*",
            f"\n💡 {self._plain_meaning(event)} {self._verdict_plain(event)}",
            self._price_line(chart, levels),
            self._action_line(event, chart, levels),
            f"\n🔗 {event.item.url or '(länk saknas)'} · {published:%H:%M UTC}",
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------- full format
    def _format_full(self, event, chart, analogs, headline, assessment,
                     recommendation, confidence, uncertainties, stop,
                     mtf_trends, conviction, action_short, levels=None) -> str:
        bar = _conviction_bar(conviction)
        tf = self.cfg.get("market_data.analysis_timeframe", "")
        fresh = _freshness_label(event)

        # 1) Action-first banner + one-line TL;DR for fast triage.
        parts = [f"🛢️ *{action_short}*  ·  konviktion *{conviction}/100* {bar}",
                 f"*{headline}*",
                 f"\n🎯 {self._tldr(event, confidence, fresh)}"]

        # 2) Chart snapshot + multi-timeframe trend on one compact line.
        if chart:
            _, _, arrows = self._mtf_alignment(event, mtf_trends)
            disp = self.names.get(chart.symbol, chart.symbol)
            ctf = chart.timeframe or tf
            line = (f"📊 {disp} {ctf} {chart.price:.2f} · "
                    f"trend {chart.trend} · RSI {chart.rsi:.0f} "
                    f"({chart.rsi_state()}) · vol {chart.rel_volume:.1f}x")
            if chart.nearest_support:
                line += f" · stöd {chart.nearest_support:.2f}"
            if chart.nearest_resistance:
                line += f" · motstånd {chart.nearest_resistance:.2f}"
            if ctf == "1d":
                line += ("\n⚠️ DAGSDATA (ingen intradagsfeed) – nivåerna är "
                         "dagsbaserade, inte 5m.")
            elif "scaled" in (chart.source or ""):
                line += ("\nℹ️ Intradag via BNO/USO-ETF skalad till Brent-nivå "
                         "(estimat, ej exakt $/fat).")
            if arrows:
                line += f"\nMTF: {arrows}"
            parts.append(line)
        else:
            # Never invent numbers: if there is no live price feed, say so
            # plainly and omit all levels/targets/stops.
            parts.append("📊 ⚠️ INGEN LIVE PRISDATA – inga nivåer/stop visas "
                         "(kontrollera dataflödet). Bedömningen nedan bygger "
                         "endast på nyheten.")

        # 3) Verdict (substance vs manipulation) + confidence, compact.
        parts.append(
            f"🔎 {self._verdict_word(event)} · substans {event.substance:.2f} · "
            f"manip {event.manipulation:.2f} · konfidens {confidence.upper()}")

        # 4) Historical base rate.
        best = analogs.best_horizon()
        if best and best.n > 0:
            move = "upp" if event.direction == "bullish" else "ned"
            parts.append(
                f"📈 Historik: {move} inom {best.horizon_h:g}h i {best.hit_pct()}% "
                f"av {best.n} liknande fall (median {best.median_return*100:+.1f}%).")
        else:
            parts.append("📈 Historik: för få mognade analoga fall ännu.")

        # 5) Concrete recommendation detail + explicit Brent price ladder.
        parts.append(f"🧭 {recommendation}")
        levels = self._levels_block(chart, stop)
        if levels:
            parts.append(levels)

        # 6) Sources with per-source timing + who was first (lead-time intel).
        parts.append(self._sources_block(event))

        # 7) Keywords, then uncertainties.
        if event.sentiment.matched:
            parts.append("🔬 Nyckelord: " + event.sentiment.explain())
        if uncertainties:
            parts.append("⚠️ " + "  ·  ".join(uncertainties))
        parts.append("\n_Beslutsstöd, ej finansiell rådgivning._")
        return "\n".join(parts)

    def _tldr(self, event, confidence, fresh) -> str:
        n = event.n_sources
        corr = (f"{n} källor bekräftar" if n > 1 else "1 källa (obekräftad)")
        return f"{corr}, {fresh}. Substans {event.substance:.2f}/manip {event.manipulation:.2f}."

    def _verdict_word(self, event) -> str:
        if event.manipulation_flag and not event.is_substantial:
            return "SANNOLIKT BRUS/MANIPULATION"
        if event.is_substantial and not event.manipulation_flag:
            return "SUBSTANSIELL"
        if event.is_substantial and event.manipulation_flag:
            return "BLANDAD (substans + risk)"
        return "OKLAR/LÅG SIGNAL"

    def _sources_block(self, event) -> str:
        published = event.first_ts or event.item.ts
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        detected = datetime.now(timezone.utc)
        if event.source_times:
            listed = "  ·  ".join(
                f"{src} {ts:%H:%M}" for src, ts in event.source_times[:5])
            lead_src, lead_ts = event.source_times[0]
            first = (f"\n⏱ Först: *{lead_src}* {lead_ts:%Y-%m-%d %H:%M UTC} "
                     f"({_fmt_latency(lead_ts, detected)} sedan)")
        else:
            listed = event.item.source
            first = ""
        return (f"🗞 Källor ({event.n_sources}): {listed}{first}\n"
                f"🔗 {event.item.url or '(länk saknas)'}")


def _conviction_bar(conv: int) -> str:
    filled = max(0, min(5, round(conv / 20)))
    return "🟩" * filled + "⬜" * (5 - filled)


def _freshness_label(event) -> str:
    age = getattr(event, "age_minutes", 0.0)
    if age < 1:
        return "just nu"
    if age < 60:
        return f"färsk ({int(age)}m)"
    if age < 1440:
        return f"{age/60:.0f}h gammal"
    return f"{age/1440:.0f}d gammal (inaktuell)"


def _fmt_latency(published, detected) -> str:
    """Human-readable delay between publication and detection."""
    secs = (detected - published).total_seconds()
    if secs < 0:
        return "nypublicerad"
    if secs < 90:
        return f"+{int(secs)}s"
    if secs < 5400:
        return f"+{int(secs // 60)}m"
    if secs < 172800:
        return f"+{int(secs // 3600)}h"
    return f"+{int(secs // 86400)}d"
