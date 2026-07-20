"""Configuration loading.

Loads a YAML config file and merges secrets from the environment (optionally
via a .env file). Access nested values with a dotted path::

    cfg.get("news.poll_seconds", 180)
    cfg.secret("TELEGRAM_BOT_TOKEN")
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # optional convenience
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


SECRET_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "NTFY_TOKEN",
    "X_BEARER_TOKEN",
    "ALPHAVANTAGE_API_KEY",
    "NEWSAPI_KEY",
    "EIA_API_KEY",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
]


@dataclass
class Config:
    raw: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)
    path: str | None = None

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        if load_dotenv is not None:
            load_dotenv()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Config file not found: {p}. Copy config.example.yaml to "
                f"config.yaml and edit it."
            )
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        secrets = {k: os.environ.get(k, "") for k in SECRET_KEYS}
        return cls(raw=raw, secrets=secrets, path=str(p))

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def secret(self, key: str, default: str = "") -> str:
        return self.secrets.get(key) or os.environ.get(key, default)

    @property
    def data_dir(self) -> Path:
        d = Path(self.get("general.data_dir", "./data"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def instruments(self) -> list[dict[str, Any]]:
        return self.get("instruments", []) or []

    @property
    def primary_instrument(self) -> dict[str, Any]:
        insts = self.instruments
        for i in insts:
            if i.get("primary"):
                return i
        return insts[0] if insts else {"name": "WTI", "symbol": "CL=F"}
