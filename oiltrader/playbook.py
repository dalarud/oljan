"""Intelligence-driven day playbook.

The levels alone are just geometry. What makes them tradeable is the regime
the intelligence picture puts you in — and crude has a well-known asymmetry:

  * A geopolitical *risk premium* (missiles, threats, troop movements) tends to
    MEAN-REVERT — it fades within hours/days unless barrels actually leave the
    market. In that regime you fade spikes into resistance.
  * A *physical supply disruption* (a confirmed tanker loss, a Hormuz/Strait
    closure, an export halt, a hit refinery) TRENDS — real barrels are gone, so
    you buy dips and give it room.

So the same overnight rally means opposite things depending on whether the
headlines are premium or physical. This module classifies the night's flow into
a regime, states the thesis and the pivotal question, then maps each key level
to that thesis with time markers and the specific catalysts that would flip it.

Rule-based and transparent; every regime call is explainable from the terms it
matched. Decision support, not a signal service.
"""
from __future__ import annotations

import re
from typing import Optional

# Signal groups (substring, lowercased). Ordered by supply-criticality.
_HORMUZ = ["hormuz", "strait", "bab el-mandeb", "bab-el-mandeb", "red sea",
           "suez", "tanker", "vessel", "shipping lane", "chokepoint"]
_PHYSICAL = ["halt export", "halts export", "export halt", "cut export",
             "shut in", "shut-in", "offline", "refinery", "facility",
             "pipeline", "force majeure", "production halt", "output halt",
             "seized", "seizes", "blockade", "disrupt", "damage", "knocked out",
             "ablaze", "on fire", "spill"]
_ESCALATION = ["strike", "missile", "attack", "airstrike", "air strike",
               "drone", "bombing", "bombed", "retaliat", "troops", "escalat",
               "nuclear", "centrifuge", "warhead", "mobiliz", "launch"]
_DEESCALATION = ["ceasefire", "cease-fire", "truce", "de-escalat", "deescalat",
                 "talks", "negotiat", "diplomat", "peace", "backs down",
                 "back down", "withdraw", "stand down", "restraint"]
_HEDGE = ["say", "says", "claim", "alleg", "reportedly", "reports", "unconfirmed",
          "denies", "denied", "threat"]

_INVENTORY = ["inventory", "stockpile", "draw", "build", "eia", "api ",
              "crude stocks"]
_OPEC = ["opec", "quota", "production cut", "output cut", "spare capacity"]


def _matched(events, terms) -> list:
    pat = re.compile("|".join(re.escape(t) for t in terms))
    return [e for e in events if pat.search((e.get("title") or "").lower())]


def _score(events, terms) -> tuple[float, int]:
    w = 0.0
    hits = _matched(events, terms)
    for e in hits:
        w += float(e.get("relevance") or 0) * max(
            float(e.get("substance") or 0), 0.2)
    return round(w, 1), len(hits)


def _hedged_fraction(events, terms) -> float:
    """Share of the term-matching headlines that are themselves hedged
    ('say', 'claim', 'reportedly', 'denies', 'threat'). High => the supply
    story rests on unverified claims, so it must NOT be called confirmed."""
    hits = _matched(events, terms)
    if not hits:
        return 1.0
    hedge_pat = re.compile("|".join(re.escape(t) for t in _HEDGE))
    hedged = sum(1 for e in hits
                 if hedge_pat.search((e.get("title") or "").lower()))
    return hedged / len(hits)


def _net_bias(events) -> float:
    bull = sum(1 for e in events if e.get("direction") == "bullish")
    bear = sum(1 for e in events if e.get("direction") == "bearish")
    tot = bull + bear or 1
    return (bull - bear) / tot


def classify_intel(events) -> dict:
    hormuz = _score(events, _HORMUZ)
    physical = _score(events, _PHYSICAL)
    escal = _score(events, _ESCALATION)
    deesc = _score(events, _DEESCALATION)
    inv = _score(events, _INVENTORY)
    opec = _score(events, _OPEC)
    hedge = _score(events, _HEDGE)
    bias = _net_bias(events)

    supply_risk = hormuz[0] * 1.5 + physical[0]
    # Corroboration, not "confirmation": count DISTINCT sources carrying an
    # UNHEDGED physical/Hormuz headline. Free OSINT can never truly confirm a
    # disruption intraday, so this only grades how many independent reports
    # exist — the language downstream always says "verify", never "certain".
    hedge_pat = re.compile("|".join(re.escape(t) for t in _HEDGE))
    supply_hits = _matched(events, _HORMUZ + _PHYSICAL)
    unhedged_srcs = {e.get("source") for e in supply_hits
                     if not hedge_pat.search((e.get("title") or "").lower())}
    supply_corroboration = len(unhedged_srcs)
    confirmed_supply = supply_corroboration >= 3

    if supply_risk >= 3 and bias > 0:
        regime = "supply-risk"
    elif escal[0] >= 3 and bias > 0 and supply_risk < 3:
        regime = "war-premium"
    elif deesc[0] >= 2 and deesc[0] >= escal[0]:
        regime = "premium-unwind"
    elif inv[0] >= max(escal[0], opec[0], 2):
        regime = "inventory"
    elif opec[0] >= 2:
        regime = "opec"
    else:
        regime = "mixed"

    return {
        "regime": regime, "bias": bias, "confirmed_supply": confirmed_supply,
        "supply_corroboration": supply_corroboration,
        "hormuz": hormuz, "physical": physical, "escalation": escal,
        "deescalation": deesc, "inventory": inv, "opec": opec, "hedge": hedge,
    }


def _lvl(levels, chart, up: bool, idx: int) -> Optional[float]:
    seq = (levels.resistances_above() if up else levels.supports_below()) \
        if levels else []
    if idx < len(seq):
        return seq[idx][1]
    if idx == 0:
        return (chart.nearest_resistance if up else chart.nearest_support) \
            if chart else None
    return None


def _pivot(levels, chart):
    if levels and getattr(levels, "vwap", None):
        return levels.vwap, "VWAP"
    if levels and getattr(levels, "pdc", None):
        return levels.pdc, "gårdagens stäng"
    return (chart.price if chart else None), "pris"


def build_playbook(events, chart, levels, cross=None, leverage=1.0,
                   profile=None) -> list[str]:
    """Return a coherent, intelligence-driven day plan as text lines."""
    intel = classify_intel(events)
    reg = intel["regime"]
    out: list[str] = []
    if chart is None:
        return ["Ingen tillförlitlig prisdata – avvakta nivåsättning till "
                "Europaöppning; läs av underrättelseflödet under tiden."]

    r1 = _lvl(levels, chart, True, 0)
    r2 = _lvl(levels, chart, True, 1)
    s1 = _lvl(levels, chart, False, 0)
    s2 = _lvl(levels, chart, False, 1)
    piv, piv_name = _pivot(levels, chart)
    hi = getattr(levels, "day_high", None) if levels else None
    pdh = getattr(levels, "pdh", None) if levels else None
    spike_hi = pdh or hi or r2 or r1

    def px(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"

    # ---- Thesis + pivotal question, per regime -------------------------
    if reg == "supply-risk":
        n = intel.get("supply_corroboration", 0)
        if intel["confirmed_supply"]:
            conf = (f"{n} oberoende källor rapporterar fysisk störning – men "
                    f"VERIFIERA på primärkälla (Reuters/tankertracking) innan du "
                    f"behandlar den som säker")
            confirm_phrase = ("Håller verifieringen är detta trend, inte brus – "
                              "köp dippar med tilltro. ")
        else:
            conf = ("men den vilar på obekräftade uppgifter/claims (”säger/"
                    "påstår”)")
            confirm_phrase = ("Tills en primärkälla bekräftar fysisk störning "
                              "behandla uppgången som LÖS premie som kan tömmas "
                              "snabbt. ")
        out.append(
            f"*Tes:* Utbudsrisk-regim – nattens flöde pekar på fysisk hotbild "
            f"mot faktiska barrels (Hormuz/tanker/export), {conf}. Om barrels "
            f"verkligen försvinner TRENDAR olja – då köps dippar, inte säljs "
            f"toppar.")
        out.append(
            f"*Nyckelfråga:* Är den fysiska störningen verklig och bestående? "
            f"{confirm_phrase}Följ Hormuz-/export-läget före allt annat.")
        primary = (f"Håll över {piv_name} {px(piv)} och {px(s1)} → premien är "
                   f"bibehållen; köp *retester* mot {px(s1)}/{px(piv)} hellre än "
                   f"utbrott. Mål {px(r1)}→{px(r2)}.")
        escalate = (f"NY bekräftad störning (tanker sänkt/träffad, Hormuz "
                    f"stängd, export stoppad) + brott över {px(spike_hi)} → "
                    f"trendben, sikta runda nivån ovan {px(r2)}.")
        fade = (f"Faller under {px(s1)} UTAN ny störning + hedge-ord "
                f"('säger/påstår/förnekar') dominerar → premie tömms, "
                f"reversering mot {px(s2)}.")
    elif reg == "war-premium":
        out.append(
            "*Tes:* Krigspremie-regim – eskalering (missiler/attacker/hot) men "
            "inga barrels ännu ur marknaden. Sådan premie MEAN-REVERTAR "
            "historiskt: den byggs på rubriker och tömms när inget fysiskt "
            "följer. Grundinställning: fade styrka, inte jaga.")
        out.append(
            "*Nyckelfråga:* Kommer eskaleringen att träffa faktiskt utbud "
            "(Hormuz/export/anläggning)? Får den det byter regim till "
            "utbudsrisk (köp dippar); annars fadear premien.")
        primary = (f"Fade rusningar mot {px(r1)}/{px(spike_hi)} med tajt risk; "
                   f"premien tunnas ofta ut in i {piv_name} {px(piv)}. "
                   f"Nedsida {px(s1)}→{px(s2)}.")
        escalate = (f"Fysisk störning bekräftas (Hormuz/tanker/export) → riv "
                    f"upp fade-tesen, vänd till köp-dipp; brott {px(spike_hi)} "
                    f"öppnar {px(r2)}.")
        fade = (f"Lugnande/diplomati eller bara tystnad → premien töms; "
                f"under {piv_name} {px(piv)} och {px(s1)} sikta {px(s2)}.")
    elif reg == "premium-unwind":
        out.append(
            "*Tes:* Premie-avveckling – avtrappning/diplomati dominerar "
            "flödet. Riskpremie som byggts tidigare töms; grundinställning "
            "kort-bias, sälj studsar.")
        out.append(
            "*Nyckelfråga:* Är avtrappningen trovärdig och bestående? Bryts "
            "den av en ny attack vänder allt snabbt uppåt igen.")
        primary = (f"Sälj studsar mot {px(r1)}/{piv_name} {px(piv)}; sikta "
                   f"{px(s1)}→{px(s2)} allteftersom premien lämnar.")
        escalate = (f"Ny attack/eskalering → snabb återprissättning upp, "
                    f"täck kort, brott {px(r1)} öppnar {px(spike_hi)}.")
        fade = (f"Fortsatt lugn → kontrollerad glidning ned mot {px(s2)}.")
    elif reg in ("inventory", "opec"):
        what = "lagerstatistik" if reg == "inventory" else "OPEC-utbud"
        out.append(
            f"*Tes:* {what.capitalize()}-driven dag – rörelsen styrs av "
            f"utbuds-/efterfrågesiffror snarare än geopolitik. Vänta in "
            f"datan och handla reaktionen, inte förväntan.")
        out.append(
            "*Nyckelfråga:* Kommer utfallet in över eller under förväntan? "
            "Överraskningen (inte nivån) sätter riktningen.")
        primary = (f"Range mellan {px(s1)} och {px(r1)} in i katalysatorn; "
                   f"handla utbrottet EFTER siffran, inte före.")
        escalate = (f"Bullisk överraskning + brott {px(r1)} → {px(r2)}.")
        fade = (f"Bearish överraskning + brott {px(s1)} → {px(s2)}.")
    else:
        out.append(
            "*Tes:* Blandad/otydlig drivkraft – ingen enskild regim dominerar "
            "nattens flöde. Lägre övertygelse; låt nivåerna leda och håll "
            "storleken nere tills en sida ger vika.")
        out.append("*Nyckelfråga:* Vilken drivkraft tar över när volymen kommer "
                    "(Europa/US)?")
        primary = (f"Handla intervallet {px(s1)}–{px(r1)} runt {piv_name} "
                   f"{px(piv)}; agera på brott med volym.")
        escalate = f"Brott {px(r1)} med volym → {px(r2)}."
        fade = f"Brott {px(s1)} med volym → {px(s2)}."

    # cross-asset overlay
    if cross is not None and getattr(cross, "regime", "") == "makro-driven":
        out.append("⚠️ *Makroöverlägg:* rörelsen samvarierar med USD/aktier – "
                    "en del av dagens drivkraft är bred makro, inte oljespecifik. "
                    "Dämpa övertygelsen i de oljespecifika scenarierna.")

    out.append(f"\n*Primärt ({_prob(intel)}):* {primary}")
    out.append(f"*↑ Eskalering:* {escalate}")
    out.append(f"*↓ Fade/avtrappning:* {fade}")

    # ---- Regime-flip watchers -----------------------------------------
    out.append("\n*Regim-vakter (ändrar tesen):*")
    out.append("🔺 Bekräftad fysisk störning – tanker sänkt/träffad, Hormuz "
               "stängd, export/anläggning offline → köp-dipp-regim, premien "
               "blir bestående.")
    out.append("🔻 Eldupphör / diplomati / Iran backar → premien töms snabbt, "
               "sälj studsar.")

    # ---- Time line -----------------------------------------------------
    out.append("\n*Tidslinje:*")
    out.append(f"• Nu–09:00: tunn likviditet, premien sätts i Asien på rubriker "
               f"– agera inte på spikar, notera bara var {piv_name} {px(piv)} "
               f"håller.")
    out.append(f"• 09:00 Europa: första riktiga prissättningen. Håller {px(s1)} "
               f"→ köparsidan seriös; tappas den tidigt → premie under press.")
    out.append(f"• 15:30 US: största flödet, dagens riktning avgörs ofta här. "
               f"Se om {px(r1)}/{px(spike_hi)} tas med volym eller avvisas.")
    out.append("• 22:30 API-lager: i en utbudsrisk-dag förstärker en draw "
               "budet; en stor build kan vara enda som tömmer premien kortsiktigt.")

    # ---- Risk ----------------------------------------------------------
    if leverage and leverage > 1:
        out.append(f"\n*Risk:* Geopolitik = hoppig tape (gap på rubriker). Med "
                   f"x{leverage:g} håll mindre storlek än vanligt, undvik att "
                   f"jaga spikar, och sätt stopp bortom brus (>1 ATR), inte "
                   f"precis under nivån.")
    else:
        out.append("\n*Risk:* Geopolitik = hoppig tape; undvik att jaga spikar, "
                   "vänta på retest, stopp bortom brus (>1 ATR).")

    # ---- Personal style overlay (e.g. RSI mean-reversion scalps) -------
    if profile and str(profile.get("style", "")).lower() == "mean_reversion":
        out.extend(_style_meanrev(intel, chart, levels, profile,
                                  r1, r2, s1, s2, piv, piv_name, spike_hi))
    return out


def _trend_dir(intel) -> str:
    reg, bias = intel["regime"], intel["bias"]
    if reg == "supply-risk" and bias > 0:
        return "up"
    if reg == "premium-unwind" or bias < -0.3:
        return "down"
    if reg == "war-premium":
        return "fade-up"        # upside spikes expected to fade
    return "range"


def _style_meanrev(intel, chart, levels, profile, r1, r2, s1, s2, piv,
                   piv_name, spike_hi) -> list[str]:
    ob = profile.get("rsi_overbought", 70)
    os_ = profile.get("rsi_oversold", 30)
    tf = profile.get("timeframe", getattr(chart, "timeframe", "5m"))
    rsi = getattr(chart, "rsi", None)
    atr = getattr(chart, "atr", None)

    def px(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"

    rsi_txt = (f"RSI nu {rsi:.0f} ({chart.rsi_state()})" if rsi is not None
               else "RSI n/a")
    trend = _trend_dir(intel)
    out = ["\n*── Din stil: mean reversion (RSI, korta trades) ──*",
           f"{rsi_txt} på {tf}. Regim: {intel['regime']}."]

    if trend == "up":
        out.append(
            f"⚠️ Trenddag UPP → RSI-mean-reversion blir *asymmetrisk*: handla "
            f"reversion MED trenden. Köp RSI-översålt (<{os_}) på dipp mot "
            f"{px(s1)}/{piv_name} {px(piv)}, sikta åter mot medel "
            f"{px(piv)}/{px(r1)}.")
        out.append(
            f"Var restriktiv med att KORTA överköpt: i en utbudsrisk-trend kan "
            f"RSI ligga kvar >{ob} länge. Korta bara mot starkt motstånd "
            f"({px(r2 or spike_hi)}/{px(spike_hi)}), liten storlek, tajt stopp – "
            f"och ta vinst snabbt tillbaka mot {px(piv)}.")
    elif trend == "down":
        out.append(
            f"⚠️ Trenddag NED → handla reversion MED trenden: korta RSI-"
            f"överköpt (>{ob}) på studs mot {px(r1)}/{piv_name} {px(piv)}, "
            f"sikta åter mot {px(piv)}/{px(s1)}.")
        out.append(
            f"Var restriktiv med att köpa översålt: RSI kan ligga <{os_} länge "
            f"i en nedtrend. Köp bara mot starkt stöd ({px(s2)}), liten storlek, "
            f"tajt stopp, snabb vinst mot {px(piv)}.")
    elif trend == "fade-up":
        out.append(
            f"Premien väntas fade → din stil passar: KORTA RSI-överköpt (>{ob}) "
            f"vid motstånd {px(r1)}/{px(spike_hi)}, mål {piv_name} {px(piv)}/"
            f"{px(s1)}. Köp översålt (<{os_}) vid {px(s1)} bara för studs mot "
            f"{px(piv)} – inte för en ny uppgång.")
    else:  # range
        out.append(
            f"Range-dag → fade båda extremer: korta RSI>{ob} vid {px(r1)}, köp "
            f"RSI<{os_} vid {px(s1)}, mål mitten {piv_name} {px(piv)} i båda "
            f"fall. Mindre storlek när priset är mitt i intervallet.")

    out.append(
        f"Entry: vänta på RECLAIM (RSI vänder tillbaka in under {ob}/över {os_}) "
        f"+ helst divergens – fånga inte exakta toppen/botten. Mål = medel "
        f"({piv_name} {px(piv)}). Stopp bortom nivån / >1 ATR"
        + (f" (~{atr:.2f})." if isinstance(atr, (int, float)) else "."))
    out.append(
        "Nyhetsspärr: fade ALDRIG ett RSI-extremläge som sammanfaller med en "
        "FÄRSK eskalerings-/störningsrubrik – då är det momentum, inte "
        "reversion. Kolla regim-vakterna först.")
    out.append(
        "Tidsfönster: mean reversion är bäst i etablerad session med "
        "tvåvägsflöde. Undvik tunn för-Europa (spikar reverterar oförutsägbart) "
        "och var extra försiktig i US-öppningens första volatilitet 15:30.")
    return out


def _prob(intel) -> str:
    b = abs(intel["bias"])
    if b > 0.6:
        return "hög sannolikhet"
    if b > 0.3:
        return "basfall"
    return "svag lutning"
