"""Proactive setup-monitor tests.

These encode the backtest-driven rules (see oiltrader/backtest.py): a
confirmed long reclaim needs a rejection candle; shorts are off by default
(they backtested to negative edge) and require a real downtrend when enabled.
"""
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


def _chart(rsi, price=88.12, atr=0.2, close_pos=0.8, os_dyn=30.0, ob_dyn=70.0):
    return SimpleNamespace(rsi=rsi, price=price, atr=atr, price_sane=True,
                           last_candle_age_min=2.0, nearest_support=88.10,
                           nearest_resistance=89.00, bar_close_pos=close_pos,
                           rsi_os_dyn=os_dyn, rsi_ob_dyn=ob_dyn)


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
    assert s.quality >= 75


def test_long_reclaim_needs_rejection_candle():
    # Same reclaim but the bar closed near its low -> not confirmed.
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(28, close_pos=0.2), _levels(), "up")
    s = m.update_and_detect("BZ=F", _chart(33, price=88.12, close_pos=0.2),
                            _levels(), "up")
    assert s is None


def test_short_off_by_default():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(73, price=88.95, close_pos=0.1),
                        _levels(), "down")
    s = m.update_and_detect("BZ=F", _chart(67, price=88.95, close_pos=0.1),
                            _levels(), "down")
    assert s is None                    # shorts disabled -> no fire


def test_short_fires_only_with_downtrend_when_allowed():
    m = SetupMonitor(_cfg(**{"setups.allow_shorts": True}))
    # in a range short must NOT fire (needs a genuine downtrend)
    m.update_and_detect("BZ=F", _chart(73, price=88.95, close_pos=0.1),
                        _levels(), "range")
    assert m.update_and_detect("BZ=F", _chart(67, price=88.95, close_pos=0.1),
                               _levels(), "range") is None
    # in a downtrend with a rejection candle it fires, always cautious
    m.update_and_detect("BZ=F", _chart(73, price=88.95, close_pos=0.1),
                        _levels(), "down")
    s = m.update_and_detect("BZ=F", _chart(67, price=88.95, close_pos=0.1),
                            _levels(), "down")
    assert s is not None and s.side == "short" and s.confidence == "försiktig"


def test_adaptive_threshold_catches_with_trend_pullback():
    # In an uptrend RSI never hits 30; adaptive oversold sits at ~45. A dip to
    # 42 reclaiming 48 should trigger a with-trend long.
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(42, os_dyn=45.0), _levels(), "up")
    s = m.update_and_detect("BZ=F", _chart(48, price=88.12, os_dyn=45.0),
                            _levels(), "up")
    assert s is not None and s.side == "long" and s.with_trend is True


def test_no_fire_without_reclaim():
    m = SetupMonitor(_cfg())
    m.update_and_detect("BZ=F", _chart(45), _levels(), "up")
    assert m.update_and_detect("BZ=F", _chart(50), _levels(), "up") is None


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
