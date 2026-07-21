"""Oljan – an autonomous crude oil (WTI/Brent) watch & analysis engine.

Modules:
    config          configuration loading (YAML + .env secrets)
    logging_setup   rotating file + console logging
    storage         SQLite persistence (candles, events, outcomes, notifications)
    market_data     price/candle/volume retrieval (yfinance)
    indicators      technical indicators + support/resistance
    sentiment       oil-directional sentiment (VADER + domain lexicon)
    collectors      pluggable news/social/official-report collectors
    events          relevance, categorisation, substance vs. manipulation
    historical      time-series-correct event study / analog analysis
    analysis        the "brain": chart + event + history -> recommendation
    charting        candlestick chart rendering
    notifier        Telegram / console push notifications
    daemon          resilient 24/7 orchestration loop
"""

__version__ = "1.0.0"
