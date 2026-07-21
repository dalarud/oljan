"""Proactive setup-monitor tests."""
from types import SimpleNamespace

from oiltrader.setups import SetupMonitor, format_setup


def _cfg(**over):
    base = {
        "trader_profile": {"style": "mean_reversion", "rsi_overbought": 70,
                           "rsi_oversold": 30},
        "setups.enabled": True,
        "setups.level_proximity_pct": 0.35,
    }
    base.update(over)
    return SimpleNamespace(get=lambda k, d=None: base.get(k, d))


def _chart(rsi, price=88.12, atr=0.2):
    return SimpleNamespace(rsi=rsi, price=price, atr=atr, price_sane=True,
                           last_candle_age_min=2.0, nearest_support=88.10,
                           nearest_resistance=89.00)


def _levels():
    return SimpleNamespace(vwap=88.43, pdc=88.4,
                           resistances_above=lambda: [("rund", 89.00)],
                           supports_below=lambda: [("nivå", 88.10)])


def test_oversold_reclaim_at_support_fires_long():
    m = SetupMonitor(_cfg())
    assert m.update_and_detect("BZ=F", _chart(28), _levels(), "up") is None  # prime
    s = m.update_and_detect("BZ=F", _chart(33, price=88.12), _levels(), "up")
    assert s is not None and s.side == "long"
    assert s.with_trend is True and s.confidence == "stark"
    assert s.target == 88.43            # mean/VWAP above price


def test_overbought_reclaim_at_resistance_fires_short():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(73, price=88.95), _levels(), "range")
    s = m.update_and_detect("BZ=F", _chart(67, price=88.95), _levels(), "range")
    assert s is not None and s.side == "short"


def test_no_fire_without_reclaim():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(45), _levels(), "up")
    assert m.update_and_detect("BZ=F", _chart(50), _levels(), "up") is None


def test_counter_trend_reclaim_is_cautious():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(28, price=88.12), _levels(), "down")
    s = m.update_and_detect("BZ=F", _chart(33, price=88.12), _levels(), "down")
    assert s is not None and s.with_trend is False
    assert s.confidence in ("måttlig", "försiktig")


def test_news_conflict_flagged():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(28, price=88.12), _levels(), "up")
    s = m.update_and_detect("BZ=F", _chart(33, price=88.12), _levels(), "up",
                            news_bias=-0.5)  # fresh bearish vs a long
    assert s.news_conflict is True and s.confidence == "försiktig"
    msg = format_setup(s, "Brent")
    assert "SETUP KÖP" in msg and "motstridig" in msg


def test_far_from_level_does_not_fire():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(28, price=90.00), _levels(), "up")
    # price 90.00 is far from support 88.10 -> no setup
    assert m.update_and_detect("BZ=F", _chart(33, price=90.00), _levels(),
                               "up") is None


def test_disabled_when_not_mean_reversion():
    cfg = _cfg(trader_profile={"style": "momentum"})
    m = SetupMonitor(cfg)
    assert m.enabled is False
