"""Cross-asset confirmation tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from oiltrader.crossasset import CrossAssetMonitor
from oiltrader.storage import Storage


def _cfg(**over):
    base = {
        "cross_asset.enabled": True,
        "cross_asset.window_minutes": 60,
        "cross_asset.interval": "15m",
        "cross_asset.move_threshold_pct": 0.2,
        "cross_asset.stale_minutes": 90,
        "cross_asset.proxies": {"USD": "UUP", "Aktier": "SPY", "Guld": "GLD"},
        "market_data.analysis_timeframe": "5m",
    }
    base.update(over)
    ns = SimpleNamespace(get=lambda k, d=None: base.get(k, d))
    ns.secret = lambda *_: ""
    return ns


def _seed(st, symbol, interval, ret_pct):
    """Flat at `base` through the 60-min window, then a jump to `end` at the
    last candle, so the window return equals ret_pct."""
    now = datetime.now(timezone.utc)
    idx = pd.date_range(now - timedelta(minutes=90), periods=7, freq="15min",
                        tz="UTC")
    base = 100.0
    end = base * (1 + ret_pct / 100.0)
    px = [base] * 6 + [end]          # base at/through cutoff, jump at last
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                       "volume": 1}, index=idx)
    st.upsert_candles(symbol, interval, df)


def test_oil_specific_when_proxies_flat(tmp_path):
    st = Storage(tmp_path / "c.db")
    m = CrossAssetMonitor(_cfg(), st, "BZ=F")
    _seed(st, "BZ=F", "5m", 1.0)       # oil +1%
    _seed(st, "UUP", "15m", 0.0)
    _seed(st, "SPY", "15m", 0.0)
    _seed(st, "GLD", "15m", 0.0)
    snap = m.snapshot()
    assert snap.regime == "oljespecifik"


def test_macro_driven_when_usd_and_equities_move(tmp_path):
    st = Storage(tmp_path / "c.db")
    m = CrossAssetMonitor(_cfg(), st, "BZ=F")
    _seed(st, "BZ=F", "5m", -1.0)      # oil down
    _seed(st, "UUP", "15m", 0.8)       # dollar up (explains oil down)
    _seed(st, "SPY", "15m", -0.9)      # equities down (risk-off, with oil)
    _seed(st, "GLD", "15m", 0.0)
    snap = m.snapshot()
    assert snap.regime == "makro-driven"
    assert snap.is_macro()


def test_calm_when_oil_barely_moves(tmp_path):
    st = Storage(tmp_path / "c.db")
    m = CrossAssetMonitor(_cfg(), st, "BZ=F")
    _seed(st, "BZ=F", "5m", 0.05)      # below move threshold
    snap = m.snapshot()
    assert snap.regime == "lugnt"


def test_disabled_returns_none(tmp_path):
    st = Storage(tmp_path / "c.db")
    m = CrossAssetMonitor(_cfg(**{"cross_asset.enabled": False}), st, "BZ=F")
    assert m.snapshot() is None


def test_conviction_tempered_by_macro():
    from oiltrader.analysis import Analyzer
    from oiltrader.crossasset import CrossAssetSnapshot
    an = Analyzer(_cfg(), SimpleNamespace(min_sample=5))
    ev = SimpleNamespace(direction="bullish", category="geopolitical")
    macro = CrossAssetSnapshot(oil_ret=0.01, regime="makro-driven")
    specific = CrossAssetSnapshot(oil_ret=0.01, regime="oljespecifik")
    assert an._apply_cross(80, ev, macro) == 68        # 80 * 0.85
    assert an._apply_cross(80, ev, specific) == 86      # 80 * 1.08
    assert an._apply_cross(80, ev, None) == 80


def _mk_analyzer():
    from oiltrader.analysis import Analyzer
    from types import SimpleNamespace
    prof = {"style": "mean_reversion", "rsi_overbought": 70, "rsi_oversold": 30}
    base = {"trader_profile": prof, "notifications.stale_after_minutes": 20}
    cfg = SimpleNamespace(get=lambda k, d=None: base.get(k, d))
    cfg.instruments = []
    return Analyzer(cfg, SimpleNamespace(min_sample=5))


def _mk_event(direction="neutral", substantial=False, fresh=1.0, is_action=None):
    from types import SimpleNamespace
    return SimpleNamespace(direction=direction, is_substantial=substantial,
                           freshness=fresh,
                           factors={"is_action": is_action})


def _mk_chart(rsi, age=1.0):
    from types import SimpleNamespace
    return SimpleNamespace(rsi=rsi, price_sane=True, last_candle_age_min=age,
                           nearest_resistance=89.0, nearest_support=88.0)


def _mk_levels():
    from types import SimpleNamespace
    return SimpleNamespace(vwap=88.43, pdc=88.4,
                           resistances_above=lambda: [("rund", 89.0)],
                           supports_below=lambda: [("nivå", 88.1)])


def test_style_tip_overbought_uptrend_is_cautious():
    an = _mk_analyzer()
    tip = an._style_tip(_mk_event(), _mk_chart(74), _mk_levels(), "up")
    assert tip and "överköpt" in tip and "upptrend" in tip
    assert "litet" in tip.lower()


def test_style_tip_oversold_range_is_reversion_buy():
    an = _mk_analyzer()
    tip = an._style_tip(_mk_event(), _mk_chart(26), _mk_levels(), "range")
    assert tip and "översålt" in tip and "reversions-köp" in tip


def test_style_tip_news_momentum_guard():
    an = _mk_analyzer()
    ev = _mk_event(direction="bullish", substantial=True, fresh=0.9)
    tip = an._style_tip(ev, _mk_chart(75), _mk_levels(), "up")
    assert "momentum" in tip and "INTE" in tip


def test_style_tip_quiet_when_rsi_midrange():
    an = _mk_analyzer()
    assert an._style_tip(_mk_event(), _mk_chart(50), _mk_levels(), "up") is None


def test_style_tip_quiet_when_stale():
    an = _mk_analyzer()
    assert an._style_tip(_mk_event(), _mk_chart(75, age=99), _mk_levels(), "up") is None
