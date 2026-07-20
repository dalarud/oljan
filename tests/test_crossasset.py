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
