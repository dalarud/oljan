"""Topic-cooldown (noise reduction) tests."""
from types import SimpleNamespace

from oiltrader.daemon import Daemon


class _St:
    def __init__(self):
        self.m = {}

    def get_meta(self, k, d=None):
        return self.m.get(k, d)

    def set_meta(self, k, v):
        self.m[k] = v


def _stub(cooldown_s=2700, delta=15):
    s = SimpleNamespace()
    s.storage = _St()
    s.topic_cooldown_s = cooldown_s
    s.topic_escalation_delta = delta
    return s


def _ev(cat="geopolitical", direction="bullish"):
    return SimpleNamespace(category=cat, direction=direction)


def test_repeat_same_topic_is_suppressed():
    s = _stub()
    assert Daemon._topic_suppressed(s, _ev(), 52) is False   # first fires
    assert Daemon._topic_suppressed(s, _ev(), 51) is True    # repeat -> suppressed
    assert Daemon._topic_suppressed(s, _ev(), 55) is True    # small bump -> still


def test_material_escalation_gets_through():
    s = _stub()
    assert Daemon._topic_suppressed(s, _ev(), 52) is False
    assert Daemon._topic_suppressed(s, _ev(), 70) is False   # +18 >= delta 15


def test_neutral_never_suppressed():
    s = _stub()
    Daemon._topic_suppressed(s, _ev(direction="bullish"), 50)
    assert Daemon._topic_suppressed(s, _ev(direction="neutral"), 50) is False


def test_different_topic_not_suppressed():
    s = _stub()
    Daemon._topic_suppressed(s, _ev(cat="geopolitical"), 52)
    assert Daemon._topic_suppressed(s, _ev(cat="inventory"), 52) is False


def test_zero_window_never_suppresses():
    s = _stub(cooldown_s=0)
    Daemon._topic_suppressed(s, _ev(), 52)
    assert Daemon._topic_suppressed(s, _ev(), 52) is False
