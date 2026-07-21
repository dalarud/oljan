from datetime import datetime, timezone, timedelta
from oiltrader.storage import Storage
from oiltrader.pulse import build_pulse


def _ev(source, direction, cat, rel, sub):
    return {"ts": datetime.now(timezone.utc) - timedelta(minutes=10),
            "symbol": "BZ=F", "source": source, "title": f"{direction} {cat}",
            "url": "", "content": "", "category": cat, "direction": direction,
            "magnitude": 2.0, "relevance": rel, "substance": sub,
            "manipulation": 0.2, "confidence": "high", "extra": {}}


def test_pulse_net_bias(tmp_path):
    s = Storage(tmp_path / "p.db")
    for i in range(3):
        s.insert_event(_ev(f"src{i}", "bullish", "opec", 3.0, 0.7))
    s.insert_event(_ev("srcX", "bearish", "macro", 2.0, 0.5))
    msg = build_pulse(s, 3, 74.5, "up", "Brent")
    assert "HAUSSE" in msg
    assert "bull 3 / bear 1" in msg
    assert "opec" in msg  # dominant driver


def test_pulse_quiet_window(tmp_path):
    s = Storage(tmp_path / "q.db")
    msg = build_pulse(s, 3, 74.5, "up", "Brent")
    assert "Lugnt" in msg


def test_pulse_excludes_seed(tmp_path):
    s = Storage(tmp_path / "s.db")
    s.insert_event(_ev("seed", "bullish", "opec", 3.0, 0.7))
    assert "Lugnt" in build_pulse(s, 3, 74.5, "up", "Brent")
