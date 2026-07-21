"""Tests for canonical intraday reference levels."""
from datetime import datetime, timezone, timedelta
import pandas as pd
from oiltrader.levels import KeyLevels, compute_levels


class FakeCfg:
    def __init__(self, d): self.d = d
    def get(self, k, dflt=None): return self.d.get(k, dflt)


def _cfg():
    return FakeCfg({"indicators.sr_lookback": 300, "indicators.sr_pivot_width": 3,
                    "indicators.sr_cluster_pct": 0.5})


def test_keylevels_labeling_and_order():
    kl = KeyLevels(price=81.62, pdh=82.40, pdl=79.90, pdc=80.56,
                   day_high=82.10, day_low=80.25, vwap=81.31,
                   swing_supports=[80.0], swing_resistances=[83.0])
    res = kl.resistances_above()
    sup = kl.supports_below()
    # resistances sorted ascending, all above price, labelled
    vals = [v for _, v in res]
    assert vals == sorted(vals) and all(v > kl.price for v in vals)
    assert any(lbl == "PDH" for lbl, _ in res)
    # supports sorted descending, all below price
    svals = [v for _, v in sup]
    assert svals == sorted(svals, reverse=True) and all(v < kl.price for v in svals)


def test_compute_levels_from_candles():
    now = datetime.now(timezone.utc)
    idx = pd.date_range(now - timedelta(hours=6), periods=72, freq="5min", tz="UTC")
    base = [80 + (i % 20) * 0.1 for i in range(72)]
    intr = pd.DataFrame({"open": base, "high": [b + 0.2 for b in base],
                         "low": [b - 0.2 for b in base], "close": base,
                         "volume": [1000] * 72}, index=idx)
    didx = pd.date_range(now - timedelta(days=3), periods=3, freq="1D", tz="UTC")
    daily = pd.DataFrame({"open": [79, 80, 81], "high": [80, 81, 82],
                          "low": [78, 79, 80], "close": [79.5, 80.5, 81.5],
                          "volume": [0, 0, 0]}, index=didx)
    price = float(intr["close"].iloc[-1])
    kl = compute_levels(intr, daily, price, _cfg())
    assert kl.day_high >= kl.day_low
    assert kl.vwap is not None                 # volume present
    assert kl.pdh is not None and kl.pdl is not None  # prior completed day
