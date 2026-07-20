"""Tests for the Yahoo provider parser and notifier title cleaning."""
import pandas as pd

from oiltrader.providers import YahooChartProvider, parse_lookback
from oiltrader.notifier import _first_line, _ascii_header, _truncate


def test_parse_lookback():
    assert parse_lookback("7d") == 7 * 86400
    assert parse_lookback("60d") == 60 * 86400
    assert parse_lookback("12h") == 12 * 3600
    assert parse_lookback("5y") == 5 * 31536000


def test_yahoo_parse_builds_utc_dataframe():
    payload = {"chart": {"error": None, "result": [{
        "timestamp": [1719914400, 1719915300],
        "indicators": {"quote": [{
            "open": [83.1, 83.4], "high": [83.5, 83.6],
            "low": [83.0, 83.2], "close": [83.4, 83.2],
            "volume": [1200, 1500]}]}}]}}
    df = YahooChartProvider._parse(payload)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"
    assert len(df) == 2
    assert df["close"].iloc[-1] == 83.2


def test_yahoo_parse_drops_nan_close():
    payload = {"chart": {"error": None, "result": [{
        "timestamp": [1, 2],
        "indicators": {"quote": [{
            "open": [1, 2], "high": [1, 2], "low": [1, 2],
            "close": [1.0, None], "volume": [10, 20]}]}}]}}
    df = YahooChartProvider._parse(payload)
    assert len(df) == 1


def test_yahoo_parse_empty():
    assert YahooChartProvider._parse({"chart": {"result": []}}).empty


def test_notifier_title_prefers_headline_and_is_latin1():
    text = ("🛢️ *OLJAN* 🟡 konfidens: *MEDIUM*\n"
            "*[INVENTORY · hausse] Surprise crude drawdown*\n"
            "📊 Chart ...")
    title = _first_line(text)
    assert "INVENTORY" in title and "*" not in title
    # header must be latin-1 encodable (no emoji)
    header = _ascii_header(title)
    header.encode("latin-1")  # must not raise
    assert "*" not in header


def test_truncate():
    assert _truncate("abc", 10) == "abc"
    assert len(_truncate("a" * 100, 10)) == 10
