"""Synthesis / Edge — fuse technicals with the fundamental/regime picture.

Engine-side mirror of web/lib/synthesis.js so the same fused, explainable read
can be pushed to Telegram (in setup alerts and the market pulse), not only
shown in the web panel.

Edge grounding (transparent, no black box):
  * regime overrides the naive "bullish news -> buy" reflex — a *physical*
    supply disruption TRENDS (buy dips), a geopolitical *risk premium*
    MEAN-REVERTS (don't chase; fade/wait);
  * recommendations are long-biased and gate shorts to a real downtrend +
    rejection, per the backtest (long reclaims ~+0.2..+0.4R, shorts ~-0.5R).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

_REGIME = {
    "supply-risk": ("trend", "Fysisk utbudsstörning – riktiga fat borta → trendar, köp dippar."),
    "war-premium": ("revert", "Krigspremie utan bekräftat bortfall → mattas oftast, jaga inte toppar."),
    "premium-unwind": ("revert_down", "De-eskalering → premien släpper, studsar säljs."),
    "inventory": ("event", "Lagerdrivet → reagera på siffran, annars mean-reversion vid nivåer."),
    "opec": ("trend", "OPEC-styrt utbud → följ beskedets riktning."),
    "mixed": ("range", "Spretig bild → range/mean-reversion tills en tes vinner."),
}
_NEAR = 0.0035


def _nearest(levels, chart, up: bool):
    if levels is not None:
        seq = levels.resistances_above() if up else levels.supports_below()
        if seq:
            return seq[0][1]
    v = (getattr(chart, "nearest_resistance", None) if up
         else getattr(chart, "nearest_support", None))
    return v


def build_synthesis(intel: dict, chart, mtf: dict, levels,
                    events: Optional[list] = None) -> Optional[dict]:
    if chart is None or getattr(chart, "price", None) is None:
        return None
    price = float(chart.price)
    os_ = float(getattr(chart, "rsi_os_dyn", 30) or 30)
    ob = float(getattr(chart, "rsi_ob_dyn", 70) or 70)
    rsi = float(getattr(chart, "rsi", 50) or 50)

    res = _nearest(levels, chart, up=True)
    sup = _nearest(levels, chart, up=False)
    d_res = (res - price) / price if res else None
    d_sup = (price - sup) / price if sup else None
    if d_res is not None and 0 <= d_res <= _NEAR:
        at_level = "motstånd"
    elif d_sup is not None and 0 <= d_sup <= _NEAR:
        at_level = "stöd"
    else:
        at_level = "mitt emellan"

    rsi_state = "överköpt" if rsi >= ob else "översåld" if rsi <= os_ else "neutral"
    atr = float(getattr(chart, "atr", 0) or 0)
    atr_pct = (atr / price * 100.0) if price else None
    vol = ("okänd" if atr_pct is None else "hög" if atr_pct >= 0.9
           else "låg" if atr_pct <= 0.35 else "normal")

    mtf = mtf or {}
    slow = mtf.get("1h") or mtf.get("4h") or getattr(chart, "trend", "sideways")
    fast = mtf.get("5m") or getattr(chart, "trend", "sideways")

    tech_lean = "neutral"
    if at_level == "stöd" and rsi_state != "överköpt":
        tech_lean = "long"
    elif at_level == "motstånd" and rsi_state != "översåld":
        tech_lean = "short"
    elif rsi_state == "översåld":
        tech_lean = "long"
    elif rsi_state == "överköpt":
        tech_lean = "short"

    close_pos = float(getattr(chart, "bar_close_pos", 0.5) or 0.5)
    rejection = (close_pos >= 0.6 if tech_lean == "long"
                 else close_pos <= 0.4 if tech_lean == "short" else False)
    with_slow = ((tech_lean == "long" and slow == "up") or
                 (tech_lean == "short" and slow == "down"))

    regime = intel.get("regime", "mixed")
    behav, regime_sv = _REGIME.get(regime, _REGIME["mixed"])
    biasf = float(intel.get("bias", 0.0) or 0.0)
    fund_bias = "long" if biasf > 0.1 else "short" if biasf < -0.1 else "neutral"
    corr = int(intel.get("supply_corroboration", 0) or 0)

    # fresh driver
    events = events or []
    now = datetime.now(timezone.utc).timestamp()

    def _ts(e):
        t = e.get("ts")
        if isinstance(t, datetime):
            return t.timestamp()
        try:
            return float(t)
        except (TypeError, ValueError):
            return 0.0

    def _score(e):
        return float(e.get("relevance") or 0) * max(float(e.get("substance") or 0), 0.15)

    fresh = sorted([e for e in events if _ts(e) > now - 45 * 60],
                   key=_score, reverse=True)
    fresh_top = fresh[0] if fresh else None
    driver = ("nyhetsdriven" if fresh_top
              else "flödesdriven" if getattr(chart, "trend", "flat") != "sideways"
              else "teknisk")

    side, label, tone = "wait", "STÅ UTANFÖR", "neutral"
    conflicts, notes = [], []
    fresh_opp = fresh_top is not None and (
        (tech_lean == "long" and fresh_top.get("direction") == "bearish") or
        (tech_lean == "short" and fresh_top.get("direction") == "bullish"))

    if tech_lean == "long":
        if behav == "revert_down" or fund_bias == "short":
            side, label = "wait", "VÄNTA (mottrend-long)"
            conflicts.append("fundamenta/regim pekar ned – long vore mottrend")
        elif behav == "trend" and (slow == "up" or fund_bias == "long"):
            side, label, tone = "long", "KÖP-DIPP (med trend)", "bull"
        else:
            side, label, tone = "long", "REVERSION LÅNG", "bull"
        if side == "long" and not rejection:
            side, label, tone = "wait", "VÄNTA PÅ RECLAIM", "neutral"
            notes.append("ingen avvisningsstake än – vänta på reclaim genom RSI-linjen")
    elif tech_lean == "short":
        if slow == "down" and rejection and (
                behav in ("revert_down", "revert") or fund_bias == "short"):
            side, label, tone = "short", "FADE (försiktig, liten)", "bear"
        else:
            side, label = "wait", "AVSTÅ SHORT"
            notes.append("short-fade har negativ historisk edge – kräver klar nedtrend + avvisning")
    else:
        side, label = "wait", "INGEN SETUP"
        notes.append("pris mitt emellan nivåer, RSI neutral – inget läge")

    if fresh_opp and side != "wait":
        conflicts.append("färsk motstridig rubrik – momentum, fadea inte")
        tone = "warn"
        if side == "short":
            side, label = "wait", "VÄNTA (nyhet emot)"
    if conflicts and side != "wait":
        tone = "warn"

    conv = 35
    if side != "wait":
        if with_slow:
            conv += 20
        if rejection:
            conv += 12
        if at_level != "mitt emellan":
            conv += 10
        if (side == "long" and fund_bias == "long") or (side == "short" and fund_bias == "short"):
            conv += 12
        if corr >= 3 and behav == "trend":
            conv += 8
        if vol == "hög":
            conv -= 8
    if conflicts:
        conv -= 18 * min(len(conflicts), 2)
    conv = max(5, min(100, conv))

    if side == "long":
        edge = ("Backtest: med-trend long-reclaim ~+0.3–0.7R, 45–56 % träff (litet urval)."
                if with_slow else
                "Backtest: long-reclaim positiv men svagare mot-trend (~+0.1R) – snålt mål, mindre storlek.")
    elif side == "short":
        edge = "Backtest: short-fade negativ edge generellt (~−0.5R) – detta är undantaget. Liten storlek, tajt stopp."
    else:
        edge = "Ingen uppmätt edge just nu – vänta tills teknik och fundamenta pekar åt samma håll."

    align = "konflikt" if conflicts else ("samstämmig" if side != "wait" else "neutral")

    return {
        "side": side, "label": label, "tone": tone, "conviction": conv,
        "alignment": align, "regime": regime, "regime_behav": behav,
        "regime_sv": regime_sv, "driver": driver, "edge": edge,
        "conflicts": conflicts, "notes": notes,
        "at_level": at_level, "rsi_state": rsi_state, "vol": vol,
        "slow_trend": slow, "fast_trend": fast,
        "nearest_res": res, "nearest_sup": sup,
    }


def format_synthesis(syn: dict, disp=lambda v: v) -> str:
    """Compact Telegram (HTML-mode-safe) synthesis block."""
    if not syn:
        return ""
    lines = [f"🧠 *Syntes: {syn['label']}* · konviktion {syn['conviction']}/100 "
             f"({syn['alignment']})"]
    lines.append(f"Regim {syn['regime']} · {syn['driver']} · "
                 f"teknik {syn['slow_trend']}/{syn['rsi_state']} vid {syn['at_level']}")
    lines.append(f"🎯 {syn['edge']}")
    for c in syn.get("conflicts", []):
        lines.append(f"⚠️ {c}")
    return "\n".join(lines)
