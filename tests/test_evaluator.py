"""Alert self-evaluation tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from oiltrader.evaluator import AlertEvaluator
from oiltrader.storage import Storage


def _cfg(**over):
    base = {
        "alert_eval.enabled": True,
        "alert_eval.horizon_hours": 1.0,
        "alert_eval.deadband_pct": 0.15,
        "market_data.analysis_timeframe": "5m",
        "alert_eval.auto_tune": True,
        "alert_eval.tune_min_sample": 4,
        "alert_eval.tune_floor": 30,
        "alert_eval.tune_ceiling": 70,
        "alert_eval.tune_step": 5,
        "alert_eval.precision_floor": 0.45,
    }
    base.update(over)
    ns = SimpleNamespace(get=lambda k, d=None: base.get(k, d))
    ns.primary_instrument = {"symbol": "BZ=F"}
    return ns


def _seed_candles(st, symbol, start, minutes, price_fn):
    idx = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    px = [price_fn(i) for i in range(minutes)]
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                       "volume": 1000}, index=idx)
    st.upsert_candles(symbol, "5m", df)


def _analysis(direction, conviction, category="opec"):
    ev = SimpleNamespace(symbol="BZ=F", event_id=1, category=category,
                         direction=direction)
    return SimpleNamespace(event=ev, conviction=conviction)


def test_correct_bullish_scored_hit(tmp_path):
    st = Storage(tmp_path / "e.db")
    ev = AlertEvaluator(_cfg(), st, None)
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    # rising price: +1% over the horizon
    _seed_candles(st, "BZ=F", t0 - timedelta(minutes=5), 200,
                  lambda i: 80.0 * (1 + 0.00005 * i))
    st.insert_alert(t0, 1, "BZ=F", "opec", "bullish", 65, 80.0, 1.0)
    assert ev.score_due() == 1
    rows = st.alert_stats()
    assert len(rows) == 1 and rows[0]["correct"] == 1
    assert rows[0]["fwd_return"] > 0


def test_wrong_direction_scored_miss(tmp_path):
    st = Storage(tmp_path / "e.db")
    ev = AlertEvaluator(_cfg(), st, None)
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    _seed_candles(st, "BZ=F", t0 - timedelta(minutes=5), 200,
                  lambda i: 80.0 * (1 + 0.00005 * i))  # price rose
    st.insert_alert(t0, 1, "BZ=F", "opec", "bearish", 65, 80.0, 1.0)  # called down
    ev.score_due()
    assert st.alert_stats()[0]["correct"] == 0


def test_deadband_counts_as_miss(tmp_path):
    st = Storage(tmp_path / "e.db")
    ev = AlertEvaluator(_cfg(), st, None)
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    _seed_candles(st, "BZ=F", t0 - timedelta(minutes=5), 200,
                  lambda i: 80.0)  # flat
    st.insert_alert(t0, 1, "BZ=F", "opec", "bullish", 65, 80.0, 1.0)
    ev.score_due()
    assert st.alert_stats()[0]["correct"] == 0


def test_not_due_is_not_scored(tmp_path):
    st = Storage(tmp_path / "e.db")
    ev = AlertEvaluator(_cfg(), st, None)
    now = datetime.now(timezone.utc)
    st.insert_alert(now, 1, "BZ=F", "opec", "bullish", 65, 80.0, 1.0)
    assert ev.score_due() == 0
    assert st.alert_stats() == []


def test_scorecard_and_autotune_raises_threshold(tmp_path):
    st = Storage(tmp_path / "e.db")
    ev = AlertEvaluator(_cfg(), st, None)
    # 6 scored alerts in the 40-59 band, mostly wrong -> raise threshold
    for i in range(6):
        aid = st.insert_alert(
            datetime.now(timezone.utc) - timedelta(days=1), 1, "BZ=F", "opec",
            "bullish", 45, 80.0, 1.0)
        st.score_alert(aid, 1 if i == 0 else 0, -0.01)
    card = ev.scorecard(30)
    assert card and "träffsäkerhet" in card
    new_min = ev.maybe_tune(40)
    assert new_min == 45
    assert st.get_meta("tuned_min_conviction") == "45"
