"""Watchdog / self-monitoring tests."""
from types import SimpleNamespace

from oiltrader.watchdog import SourceHealth, Watchdog


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)
        return True

    def send_ambient(self, text):
        self.sent.append(text)
        return True


class _Storage:
    def __init__(self):
        self.meta = {}

    def set_meta(self, k, v):
        self.meta[k] = v

    def get_meta(self, k, default=None):
        return self.meta.get(k, default)

    def get_candles(self, *a, **k):
        import pandas as pd
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _cfg(**over):
    base = {
        "watchdog.enabled": True,
        "watchdog.consecutive_fail_alert": 3,
        "watchdog.source_stale_seconds": 600,
        "watchdog.market_stale_minutes": 15,
        "news.poll_seconds": 60,
        "market_data.refresh_seconds": 120,
    }
    base.update(over)
    return SimpleNamespace(get=lambda k, d=None: base.get(k, d))


def _wd(health, notifier, storage):
    market = SimpleNamespace(analysis_tf="5m", intervals=["5m"])
    return Watchdog(_cfg(), health, market, notifier, storage, "BZ=F")


def test_healthy_no_alert():
    h = SourceHealth()
    h.record_ok("rss", 3)
    n, s = _Notifier(), _Storage()
    wd = _wd(h, n, s)
    wd.evaluate()
    assert n.sent == []
    assert s.get_meta("watchdog_status") == "ok"


def test_consecutive_failures_alert_once_then_recovers():
    h = SourceHealth()
    for _ in range(3):
        h.record_err("gdelt", "timeout")
    n, s = _Notifier(), _Storage()
    wd = _wd(h, n, s)

    wd.evaluate()                       # transition healthy -> degraded
    assert len(n.sent) == 1
    assert "gdelt" in n.sent[0]
    assert s.get_meta("watchdog_status") == "degraded"

    wd.evaluate()                       # still degraded: no repeat alert
    assert len(n.sent) == 1

    h.record_ok("gdelt", 1)             # recovery
    wd.evaluate()
    assert len(n.sent) == 2
    assert "återställd" in n.sent[1]
    assert s.get_meta("watchdog_status") == "ok"


def test_disabled_is_silent():
    h = SourceHealth()
    for _ in range(5):
        h.record_err("rss", "boom")
    n, s = _Notifier(), _Storage()
    market = SimpleNamespace(analysis_tf="5m", intervals=["5m"])
    wd = Watchdog(_cfg(**{"watchdog.enabled": False}), h, market, n, s, "BZ=F")
    wd.evaluate()
    assert n.sent == []


def test_status_line_reports_counts():
    h = SourceHealth()
    h.record_ok("rss", 4)
    h.record_err("x", "nitter down")
    n, s = _Notifier(), _Storage()
    wd = _wd(h, n, s)
    line = wd.status_line()
    assert "rss:ok(4)" in line
    assert "x:1" in line
