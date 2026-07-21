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


def test_x_noise_filter():
    from oiltrader.collectors.x import _is_noise, _rewrite_to_x
    assert _is_noise("25.315792, 60.624411") is True
    assert _is_noise("Inflationary.") is True
    assert _is_noise("RT by @x: something happened here today") is True
    assert _is_noise("IRAN ATTACKS TANKER IN STRAIT OF HORMUZ") is False
    assert _rewrite_to_x("https://nitter.net/DeItaone/status/123#m", "DeItaone") \
        == "https://x.com/DeItaone/status/123"


def test_source_weight_longest_match():
    import types
    from oiltrader.events import EventProcessor
    cfg = types.SimpleNamespace(
        get=lambda k, d=None: {
            "relevance.keywords": {}, "relevance.min_score": 2.0,
            "classification.source_weights": {
                "x/": 0.3, "deitaone": 0.65, "unknown": 0.4},
            "classification.corroboration_window_minutes": 90,
            "classification.confirmation_candles": 4,
            "classification.substance_threshold": 0.5,
            "classification.manipulation_threshold": 0.55,
        }.get(k, d),
        primary_instrument={"symbol": "CL=F"})
    ep = EventProcessor(cfg, storage=None, sentiment=None)
    assert ep.source_weight("x/@deitaone") == 0.65   # specific beats generic x/
    assert ep.source_weight("x/@randomguy") == 0.3    # falls back to x/
    assert ep.source_weight("weirdsource") == 0.4     # unknown


def test_alphavantage_intraday_returns_empty_without_call():
    # Must NOT hit the network for intraday (no oil intraday + saves quota).
    from oiltrader.providers import AlphaVantageProvider
    av = AlphaVantageProvider(api_key="KEY")
    assert av.fetch("BZ=F", "5m", "5d").empty
    assert av.fetch("BZ=F", "1m", "1d").empty
    # unknown symbol also empty
    assert av.fetch("AAPL", "1d", "1y").empty


def test_chain_provider_returns_first_nonempty():
    import pandas as pd
    from oiltrader.providers import ChainProvider

    class Empty:
        name = "empty"
        def fetch(self, *a): return pd.DataFrame(columns=["close"])

    class Good:
        name = "good"
        def fetch(self, *a):
            return pd.DataFrame({"open": [1], "high": [1], "low": [1],
                                 "close": [81.6], "volume": [0]})

    chain = ChainProvider([Empty(), Good()])
    df = chain.fetch("BZ=F", "1d", "1y")
    assert not df.empty and df["close"].iloc[-1] == 81.6
    assert chain.name == "empty+good"


def test_twelvedata_scale_override_wins():
    """A manual scale_override must beat the Alpha Vantage anchor."""
    from oiltrader.providers import TwelveDataProvider

    class _Anchor:
        def fetch(self, *a, **k):
            raise AssertionError("anchor must not be consulted when overridden")

    p = TwelveDataProvider("key", symbol_map={"BZ=F": "BNO"},
                           scale_to_benchmark=True, anchor=_Anchor(),
                           scale_override={"BZ=F": 1.7930})
    assert p._scale_factor("BZ=F") == 1.7930
