"""Tests for the event-study: forward-return correctness + no look-ahead."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from oiltrader.storage import Storage
from oiltrader.historical import HistoricalEngine


class FakeCfg:
    def __init__(self, d):
        self.d = d

    def get(self, dotted, default=None):
        return self.d.get(dotted, default)


def _cfg():
    return FakeCfg({
        "historical.horizons_hours": [1, 2],
        "historical.min_sample_for_stats": 2,
        "market_data.intraday_interval": "15m",
    })


def _seed_candles(storage, symbol, start, n, start_price, step):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    prices = [start_price + i * step for i in range(n)]
    df = pd.DataFrame({
        "open": prices, "high": [p + 0.1 for p in prices],
        "low": [p - 0.1 for p in prices], "close": prices,
        "volume": [100] * n,
    }, index=idx)
    storage.upsert_candles(symbol, "15m", df)


def test_forward_returns_use_only_future(tmp_path):
    storage = Storage(tmp_path / "t.db")
    sym = "CL=F"
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # rising price: +0.10 every 15 min => +0.40/hour
    _seed_candles(storage, sym, start, 40, 60.0, 0.10)

    event_ts = start + timedelta(hours=1)  # price ~ 60.40
    eid = storage.insert_event({
        "ts": event_ts, "symbol": sym, "source": "eia.gov",
        "title": "draw", "url": "", "content": "unexpected draw",
        "category": "inventory", "direction": "bullish", "magnitude": 2.0,
        "relevance": 3.0, "substance": 0.8, "manipulation": 0.1,
        "confidence": "high", "extra": {},
    })

    eng = HistoricalEngine(_cfg(), storage)
    # "now" is far in the future so horizons are elapsed
    eng.mature_events()

    # ref price at event (~60.40), price 1h later (~60.80) => +~0.66%
    outcomes = storage.analog_outcomes("inventory", "bullish", 1.0)
    assert len(outcomes) == 1
    assert outcomes[0] > 0  # rising market => positive forward return


def test_unmatured_events_excluded_from_stats(tmp_path):
    storage = Storage(tmp_path / "t2.db")
    sym = "CL=F"
    now = datetime.now(timezone.utc)
    # Event 5 minutes ago: horizon not elapsed => must NOT contribute.
    _seed_candles(storage, sym, now - timedelta(hours=1), 8, 70.0, 0.05)
    storage.insert_event({
        "ts": now - timedelta(minutes=5), "symbol": sym, "source": "reddit",
        "title": "rumor", "url": "", "content": "supply cut rumor",
        "category": "opec", "direction": "bullish", "magnitude": 2.0,
        "relevance": 3.0, "substance": 0.3, "manipulation": 0.7,
        "confidence": "low", "extra": {},
    })
    eng = HistoricalEngine(_cfg(), storage)
    eng.mature_events()
    # Horizon (1h/2h) not elapsed => no matured outcome for stats.
    assert storage.analog_outcomes("opec", "bullish", 1.0) == []


def test_analog_report_excludes_self(tmp_path):
    storage = Storage(tmp_path / "t3.db")
    sym = "CL=F"
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _seed_candles(storage, sym, start, 60, 50.0, 0.10)

    ids = []
    for k in range(3):
        ids.append(storage.insert_event({
            "ts": start + timedelta(hours=1 + k), "symbol": sym,
            "source": "eia.gov", "title": f"draw {k}", "url": "",
            "content": "draw", "category": "inventory", "direction": "bullish",
            "magnitude": 2.0, "relevance": 3.0, "substance": 0.8,
            "manipulation": 0.1, "confidence": "high", "extra": {},
        }))
    eng = HistoricalEngine(_cfg(), storage)
    eng.mature_events()

    rep_all = eng.analog_report("inventory", "bullish")
    rep_excl = eng.analog_report("inventory", "bullish", exclude_event_id=ids[0])
    n_all = next(s.n for s in rep_all.stats if s.horizon_h == 1.0)
    n_excl = next(s.n for s in rep_excl.stats if s.horizon_h == 1.0)
    assert n_excl == n_all - 1
