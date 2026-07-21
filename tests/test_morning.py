"""Morning report + quiet-hours tests."""
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

from oiltrader.notifier import _time_in_windows
from oiltrader.morning import (_bias_from_events, _catalysts_today,
                               _day_plan, build_morning_report)

try:
    from zoneinfo import ZoneInfo
    STHLM = ZoneInfo("Europe/Stockholm")
except Exception:
    STHLM = timezone.utc


# ---------------------------------------------------------------- quiet hours
def test_quiet_window_wraps_midnight():
    win = ["22:00-06:00"]
    assert _time_in_windows(time(23, 0), win) is True
    assert _time_in_windows(time(3, 0), win) is True
    assert _time_in_windows(time(6, 1), win) is False
    assert _time_in_windows(time(12, 0), win) is False


def test_quiet_window_same_day():
    assert _time_in_windows(time(2, 0), ["01:00-03:00"]) is True
    assert _time_in_windows(time(4, 0), ["01:00-03:00"]) is False


def test_no_windows_never_quiet():
    assert _time_in_windows(time(3, 0), []) is False


# ---------------------------------------------------------------- catalysts
def test_catalysts_wednesday_includes_eia():
    # 2026-07-22 is a Wednesday
    wed = datetime(2026, 7, 22, 6, 0, tzinfo=STHLM)
    labels = " ".join(l for _, l in _catalysts_today(wed, STHLM))
    assert "EIA" in labels
    assert "Europaöppning" in labels


def test_catalysts_weekend_empty_or_no_session():
    sun = datetime(2026, 7, 26, 6, 0, tzinfo=STHLM)  # Sunday
    cats = _catalysts_today(sun, STHLM)
    # no weekday session markers on the weekend
    assert all("Europaöppning" not in l and "US-öppning" not in l
               for _, l in cats)


# ---------------------------------------------------------------- bias / plan
def _ev(direction, rel=3.0, sub=0.6, cat="geopolitical"):
    return {"direction": direction, "relevance": rel, "substance": sub,
            "category": cat, "title": "t", "url": "u", "ts": 0}


def test_bias_strong_hausse():
    evs = [_ev("bullish") for _ in range(5)] + [_ev("bearish")]
    label, nn, nb, nbe = _bias_from_events(evs)
    assert nn > 0 and nb == 5 and nbe == 1
    assert "HAUSSE" in label.upper()


def test_day_plan_references_levels_and_pivot():
    chart = SimpleNamespace(price=87.5, atr=0.2, nearest_resistance=88.0,
                            nearest_support=87.0, timeframe="5m")
    levels = SimpleNamespace(
        vwap=87.4, pdc=87.3, pdh=88.3, pdl=86.0, day_high=88.3, day_low=86.0,
        resistances_above=lambda: [("nivå", 87.66), ("rund", 88.0)],
        supports_below=lambda: [("rund", 87.5), ("nivå", 87.18)])
    plan = "\n".join(_day_plan(chart, levels, [_ev("bullish")], {"1h": "up"}))
    assert "87.66" in plan and "87.5" in plan
    assert "Pivot" in plan and "long-luta" in plan


def test_build_report_has_all_sections():
    chart = SimpleNamespace(price=87.5, atr=0.2, rsi=55, nearest_resistance=88.0,
                            nearest_support=87.0, timeframe="5m",
                            last_candle_age_min=500,
                            rsi_state=lambda: "neutral")
    levels = SimpleNamespace(
        vwap=87.4, pdc=87.3, pdh=88.3, pdl=86.0, day_high=88.3, day_low=86.0,
        resistances_above=lambda: [("PDH", 88.3)],
        supports_below=lambda: [("rund", 87.0)])

    class _St:
        def recent_events(self, since):
            return [dict(_ev("bullish"), ts=int(datetime.now(timezone.utc)
                                                 .timestamp()))]
    cfg = SimpleNamespace(get=lambda k, d=None: d)
    rep = build_morning_report(
        cfg, _St(), name="Brent", symbol="BZ=F", chart=chart, levels=levels,
        mtf_trends={"5m": "up", "1h": "up"}, cross=None, night_hours=9, tz=STHLM)
    assert "Morgonrapport" in rep
    assert "Nattens läge" in rep
    assert "Nuläge & nivåer" in rep
    assert "Dagens plan" in rep
    assert "referens från föregående session" in rep  # staleness note
