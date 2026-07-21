"""Tests for indicator correctness and support/resistance detection."""
import numpy as np
import pandas as pd
import pytest

from oiltrader import indicators as ind


class FakeCfg:
    def __init__(self, d):
        self.d = d

    def get(self, dotted, default=None):
        return self.d.get(dotted, default)


def _cfg():
    return FakeCfg({
        "indicators.ema_fast": 12, "indicators.ema_slow": 26,
        "indicators.rsi_period": 14, "indicators.atr_period": 14,
        "indicators.bb_period": 20, "indicators.bb_std": 2.0,
        "indicators.volume_avg_period": 20, "indicators.sr_lookback": 300,
        "indicators.sr_pivot_width": 3, "indicators.sr_cluster_pct": 0.5,
    })


def _make_df(closes, highs=None, lows=None, vols=None):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    vols = vols or [1000] * n
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    }, index=idx)


def test_rsi_bounds_and_uptrend():
    closes = list(np.linspace(50, 70, 60))  # steady uptrend
    r = ind.rsi(pd.Series(closes), 14)
    assert (r >= 0).all() and (r <= 100).all()
    assert r.iloc[-1] > 70  # strong uptrend => overbought


def test_rsi_downtrend_oversold():
    closes = list(np.linspace(70, 50, 60))
    r = ind.rsi(pd.Series(closes), 14)
    assert r.iloc[-1] < 30


def test_atr_positive():
    df = _make_df(list(np.linspace(60, 65, 40)))
    a = ind.atr(df, 14)
    assert a.iloc[-1] > 0


def test_support_resistance_detects_levels():
    # Construct a clear swing high at 80 and swing low at 40.
    closes = ([50, 55, 60, 80, 60, 55, 50, 45, 40, 45, 50, 55] * 3)
    df = _make_df(closes)
    supports, resistances = ind.support_resistance(
        df, price=52, lookback=300, width=2, cluster_pct=0.5)
    assert any(abs(r - 80) < 2 for r in resistances)
    assert any(abs(s - 40) < 2 for s in supports)


def test_cluster_merges_close_levels():
    merged = ind._cluster([100.0, 100.2, 100.4, 110.0], cluster_pct=0.5)
    # 100.0/100.2/100.4 are within 0.5% => one cluster; 110 separate.
    assert len(merged) == 2


def test_compute_full_context():
    closes = list(np.linspace(60, 70, 80))
    df = _make_df(closes, vols=[1000] * 79 + [3000])
    ctx = ind.compute(df, "CL=F", _cfg())
    assert ctx.price == pytest.approx(70, abs=0.01)
    assert ctx.trend == "up"
    assert ctx.rel_volume > 2  # last volume spike
    assert ctx.atr > 0
