"""Intelligence-driven playbook tests."""
from types import SimpleNamespace

from oiltrader.playbook import classify_intel, build_playbook


def _ev(title, direction="bullish", rel=3.0, sub=0.6, source="reuters"):
    return {"title": title, "direction": direction, "relevance": rel,
            "substance": sub, "category": "geopolitical", "source": source}


def _chart():
    return SimpleNamespace(price=88.4, atr=0.2, nearest_resistance=88.5,
                           nearest_support=88.1, timeframe="5m")


def _levels():
    return SimpleNamespace(
        vwap=None, pdc=88.43, pdh=89.18, pdl=86.9, day_high=89.18, day_low=86.9,
        resistances_above=lambda: [("rund", 88.5), ("rund", 89.0)],
        supports_below=lambda: [("nivå", 88.1), ("rund", 88.0)])


def test_corroborated_physical_supply_is_supply_risk_trend():
    # three DISTINCT unhedged sources reporting physical disruption
    evs = [_ev("Tanker ablaze after strike, Hormuz exports halted", source="reuters"),
           _ev("Saudi refinery knocked offline, output halt", source="rigzone"),
           _ev("Pipeline damage cuts crude exports", source="oilprice")]
    intel = classify_intel(evs)
    assert intel["regime"] == "supply-risk"
    assert intel["confirmed_supply"] is True
    assert intel["supply_corroboration"] >= 3


def test_single_source_physical_not_treated_as_corroborated():
    # same physical story from ONE source -> not corroborated
    evs = [_ev("Tanker ablaze, Hormuz exports halted", source="x/@one"),
           _ev("Pipeline damage cuts exports", source="x/@one")]
    intel = classify_intel(evs)
    assert intel["confirmed_supply"] is False


def test_unverified_claim_stays_unconfirmed():
    # hedged supply headlines ("say", "claims") must NOT read as confirmed
    evs = [_ev("Iran's Guards say they hit two tankers in Hormuz"),
           _ev("Iran claims strike on shipping in the strait"),
           _ev("Missile attack reported near Bahrain")]
    intel = classify_intel(evs)
    assert intel["confirmed_supply"] is False


def test_pure_escalation_is_war_premium():
    evs = [_ev("Missile strike hits military base"),
           _ev("Drone attack and retaliation escalate"),
           _ev("Troops mobilize after airstrike")]
    intel = classify_intel(evs)
    assert intel["regime"] == "war-premium"


def test_deescalation_is_premium_unwind():
    evs = [_ev("Ceasefire agreed, talks resume", direction="bearish"),
           _ev("Diplomatic breakthrough as sides stand down", direction="bearish"),
           _ev("Truce holds, Iran withdraws", direction="bearish")]
    intel = classify_intel(evs)
    assert intel["regime"] == "premium-unwind"


def test_playbook_supply_risk_buys_dips_and_names_levels():
    evs = [_ev("Tanker ablaze, Hormuz exports halted"),
           _ev("Refinery offline, output halt confirmed")]
    plan = "\n".join(build_playbook(evs, _chart(), _levels(), None, leverage=10))
    assert "Utbudsrisk" in plan
    assert "köp" in plan.lower()          # buy dips in a supply-risk regime
    assert "89.18" in plan                # references the spike high
    assert "Regim-vakter" in plan and "Tidslinje" in plan
    assert "x10" in plan                  # leverage-aware risk note


def test_playbook_war_premium_fades():
    evs = [_ev("Missile strike and drone attack escalate"),
           _ev("Airstrike hits base, retaliation vowed")]
    plan = "\n".join(build_playbook(evs, _chart(), _levels(), None, leverage=1))
    assert "premie" in plan.lower()
    assert "fade" in plan.lower() or "mean-revert" in plan.lower()


def test_playbook_handles_no_chart():
    plan = build_playbook([_ev("x")], None, None, None)
    assert len(plan) == 1 and "Ingen tillförlitlig prisdata" in plan[0]


_PROFILE = {"style": "mean_reversion", "rsi_oversold": 30,
            "rsi_overbought": 70, "timeframe": "5m"}


def test_style_overlay_trend_day_favours_with_trend_reversion():
    evs = [_ev("Tanker ablaze, Hormuz exports halted", source="reuters"),
           _ev("Refinery offline, output halt", source="rigzone"),
           _ev("Pipeline damage cuts exports", source="oilprice")]
    c = _chart(); c.rsi = 72; c.rsi_state = lambda: "överköpt"
    plan = "\n".join(build_playbook(evs, c, _levels(), None, leverage=10,
                                    profile=_PROFILE))
    assert "Din stil: mean reversion" in plan
    assert "asymmetrisk" in plan.lower()            # trend-day warning
    assert "restriktiv med att korta" in plan.lower()
    assert "aldrig" in plan.lower()                 # news guard


def test_style_overlay_range_day_fades_both_sides():
    evs = [_ev("Mixed signals, quiet session", direction="neutral")]
    c = _chart(); c.rsi = 55; c.rsi_state = lambda: "neutral"
    plan = "\n".join(build_playbook(evs, c, _levels(), None, leverage=1,
                                    profile=_PROFILE))
    assert "Din stil: mean reversion" in plan
    assert "fade båda extremer" in plan.lower()


def test_no_style_overlay_without_profile():
    evs = [_ev("Missile strike escalates")]
    c = _chart(); c.rsi = 60; c.rsi_state = lambda: "neutral"
    plan = "\n".join(build_playbook(evs, c, _levels(), None))
    assert "Din stil" not in plan
