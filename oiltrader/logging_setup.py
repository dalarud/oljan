"""Rotating file + console logging."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: str = "./data/logs",
                  name: str = "oljan") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Reset handlers so repeated setup() calls don't duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = RotatingFileHandler(
        Path(log_dir) / f"{name}.log", maxBytes=5_000_000, backupCount=5,
        encoding="utf-8",
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # Silence chatty third-party libraries.
    for noisy in ("urllib3", "yfinance", "peewee", "matplotlib", "praw",
                  "prawcore", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)
