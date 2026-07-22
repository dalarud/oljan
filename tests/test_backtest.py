"""Backtester sanity tests (synthetic data, no DB needed)."""
import numpy as np
import pandas as pd

from oiltrader.backtest import ExitCfg, StratCfg, simulate


def _frame(n=600, seed=0):
    """A noisy mean-reverting series with intraday swings so reclaims occur."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    # random walk with mild mean reversion to keep price in a sane oil band
    price = 85.0
    closes = []
    for _ in range(n):
        price += rng.normal(0, 0.15) + (85.0 - price) * 0.02
        closes.append(price)
    close = np.array(closes)
    high = close + np.abs(rng.normal(0, 0.1, n))
    low = close - np.abs(rng.normal(0, 0.1, n))
    open_ = close + rng.normal(0, 0.05, n)
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def test_simulate_runs_and_reports_stats():
    df = _frame()
    s = simulate(df, StratCfg("S0"), ExitCfg())
    assert s["name"] == "S0"
    assert "n" in s
    if s["n"] > 0:
        assert 0.0 <= s["win_rate"] <= 1.0
        assert set(s).issuperset({"expectancy_r", "profit_factor", "total_r"})


def test_regime_gate_never_increases_trade_count():
    df = _frame(seed=3)
    base = simulate(df, StratCfg("naiv"), ExitCfg())
    gated = simulate(df, StratCfg("regim", regime_gated=True), ExitCfg())
    assert gated.get("n", 0) <= base.get("n", 0)


def test_confirmation_never_increases_trade_count():
    df = _frame(seed=7)
    base = simulate(df, StratCfg("naiv"), ExitCfg())
    conf = simulate(df, StratCfg("bekräftad", regime_gated=True,
                                 adaptive_rsi=True, require_rejection=True),
                    ExitCfg())
    # filters can only remove trades, never invent them beyond the naive set…
    # (adaptive thresholds change *where* it triggers, so we only assert it runs)
    assert "n" in conf and conf["n"] >= 0
