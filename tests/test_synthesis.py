"""Synthesis / Edge engine tests (mirror of the web logic)."""
from types import SimpleNamespace

from oiltrader.synthesis import build_synthesis, format_synthesis


class _Levels:
    def __init__(self, res, sup, vwap=None, pdc=None):
        self._res, self._sup = res, sup
        self.vwap, self.pdc = vwap, pdc

    def resistances_above(self):
        return self._res

    def supports_below(self):
        return self._sup


def _chart(price=94.5, rsi=42, atr=0.5, trend="up", os=45, ob=66, close_pos=0.8):
    return SimpleNamespace(price=price, rsi=rsi, atr=atr, trend=trend,
                           rsi_os_dyn=os, rsi_ob_dyn=ob, bar_close_pos=close_pos,
                           nearest_support=94.45, nearest_resistance=95.1)


def _ev(dir="bullish", rel=4.0, sub=0.8, title="Tanker seized near Hormuz", ago_s=600):
    import time
    return {"direction": dir, "relevance": rel, "substance": sub,
            "title": title, "ts": time.time() - ago_s}


def test_supply_trend_dip_is_buy():
    intel = {"regime": "supply-risk", "bias": 0.6, "supply_corroboration": 3}
    syn = build_synthesis(intel, _chart(), {"5m": "up", "1h": "up"}, _Levels(
        [("PDH", 95.1)], [("VWAP", 94.45)]), [_ev()])
    assert syn["side"] == "long"
    assert "KÖP-DIPP" in syn["label"]
    assert syn["alignment"] == "samstämmig"
    assert syn["conviction"] >= 70


def test_war_premium_at_resistance_is_not_a_short_signal():
    intel = {"regime": "war-premium", "bias": 0.5, "supply_corroboration": 0}
    syn = build_synthesis(intel, _chart(price=95.05, rsi=69, close_pos=0.2),
                          {"5m": "up", "1h": "up"},
                          _Levels([("PDH", 95.1)], [("VWAP", 94.45)]), [_ev()])
    assert syn["side"] == "wait"        # shorts gated; don't chase
    assert syn["alignment"] != "samstämmig"


def test_long_setup_but_fresh_bearish_news_conflicts():
    intel = {"regime": "inventory", "bias": -0.4, "supply_corroboration": 0}
    syn = build_synthesis(intel, _chart(rsi=44), {"5m": "up", "1h": "up"},
                          _Levels([("PDH", 95.1)], [("VWAP", 94.45)]),
                          [_ev(dir="bearish", title="Surprise inventory build",
                               rel=2.6, sub=0.7, ago_s=300)])
    assert syn["side"] == "wait"
    assert syn["alignment"] == "konflikt"


def test_format_is_telegram_safe_and_nonempty():
    intel = {"regime": "supply-risk", "bias": 0.6, "supply_corroboration": 3}
    syn = build_synthesis(intel, _chart(), {"5m": "up", "1h": "up"},
                          _Levels([("PDH", 95.1)], [("VWAP", 94.45)]), [_ev()])
    txt = format_synthesis(syn)
    assert "Syntes" in txt and "konviktion" in txt
    assert "<" not in txt and ">" not in txt   # no raw HTML that could break parse


def test_none_chart_returns_none():
    assert build_synthesis({"regime": "mixed", "bias": 0.0}, None, {}, None, []) is None
