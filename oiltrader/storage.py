"""SQLite persistence.

We persist candles ourselves so the historical event-study retains price
history well beyond the free API's intraday window (yfinance only serves
~60 days of intraday data). Events, their forward-return outcomes, seen
items (for dedup) and sent notifications are also stored here.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts       INTEGER NOT NULL,           -- epoch seconds, UTC
    open     REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               INTEGER NOT NULL,   -- epoch seconds, UTC
    symbol           TEXT,
    source           TEXT,
    title            TEXT,
    url              TEXT,
    content          TEXT,
    category         TEXT,
    direction        TEXT,               -- bullish | bearish | neutral
    magnitude        REAL,
    relevance        REAL,
    substance        REAL,
    manipulation     REAL,
    confidence       TEXT,
    matured          INTEGER DEFAULT 0,
    extra            TEXT                 -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_events_cat_dir ON events(category, direction);
CREATE INDEX IF NOT EXISTS idx_events_matured ON events(matured);

CREATE TABLE IF NOT EXISTS outcomes (
    event_id   INTEGER NOT NULL,
    horizon_h  REAL NOT NULL,
    ref_price  REAL,
    fwd_price  REAL,
    fwd_return REAL,                      -- fractional, e.g. 0.012 = +1.2%
    PRIMARY KEY (event_id, horizon_h)
);

CREATE TABLE IF NOT EXISTS seen_items (
    hash TEXT PRIMARY KEY,
    ts   INTEGER NOT NULL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    event_id INTEGER,
    kind     TEXT,
    dedup    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per PUSHED alert, scored against the later price move so the
-- engine can measure its own precision and (optionally) self-tune.
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,            -- when the alert fired (UTC epoch)
    event_id   INTEGER,
    symbol     TEXT,
    category   TEXT,
    direction  TEXT,                        -- bullish | bearish | neutral
    conviction INTEGER,
    ref_price  REAL,                        -- price at alert time (engine basis)
    horizon_h  REAL,
    scored     INTEGER DEFAULT 0,
    correct    INTEGER,                     -- 1/0 once scored, NULL until then
    fwd_return REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_scored ON alerts(scored);
"""


def _epoch(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


class Storage:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ candles
    def upsert_candles(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = []
        for ts, row in df.iterrows():
            rows.append((
                symbol, interval, _epoch(ts.to_pydatetime()),
                _f(row.get("open")), _f(row.get("high")), _f(row.get("low")),
                _f(row.get("close")), _f(row.get("volume")),
            ))
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles "
                "(symbol, interval, ts, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_candles(self, symbol: str, interval: str,
                    since: datetime | None = None,
                    until: datetime | None = None) -> pd.DataFrame:
        q = "SELECT ts, open, high, low, close, volume FROM candles " \
            "WHERE symbol=? AND interval=?"
        params: list[Any] = [symbol, interval]
        if since is not None:
            q += " AND ts>=?"; params.append(_epoch(since))
        if until is not None:
            q += " AND ts<=?"; params.append(_epoch(until))
        q += " ORDER BY ts ASC"
        with self._lock:
            cur = self._conn.execute(q, params)
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low",
                                         "close", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df.drop(columns=["ts"])
        return df

    def price_at(self, symbol: str, interval: str, when: datetime,
                 direction: str = "before") -> tuple[datetime, float] | None:
        """Closest stored close price at/before (or at/after) `when`."""
        op = "<=" if direction == "before" else ">="
        order = "DESC" if direction == "before" else "ASC"
        with self._lock:
            cur = self._conn.execute(
                f"SELECT ts, close FROM candles WHERE symbol=? AND interval=? "
                f"AND ts {op} ? ORDER BY ts {order} LIMIT 1",
                (symbol, interval, _epoch(when)),
            )
            row = cur.fetchone()
        if not row:
            return None
        return (datetime.fromtimestamp(row["ts"], tz=timezone.utc), row["close"])

    # ------------------------------------------------------------------- events
    def insert_event(self, event: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, symbol, source, title, url, content, "
                "category, direction, magnitude, relevance, substance, "
                "manipulation, confidence, matured, extra) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _epoch(event["ts"]), event.get("symbol"),
                    event.get("source"), event.get("title"), event.get("url"),
                    event.get("content"), event.get("category"),
                    event.get("direction"), event.get("magnitude"),
                    event.get("relevance"), event.get("substance"),
                    event.get("manipulation"), event.get("confidence"),
                    0, json.dumps(event.get("extra", {})),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_events(self, since: datetime,
                      category: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE ts>=?"
        params: list[Any] = [_epoch(since)]
        if category:
            q += " AND category=?"; params.append(category)
        q += " ORDER BY ts DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def unmatured_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE matured=0 ORDER BY ts ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_matured(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET matured=1 WHERE id=?",
                               (event_id,))
            self._conn.commit()

    def insert_outcome(self, event_id: int, horizon_h: float, ref_price: float,
                       fwd_price: float, fwd_return: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO outcomes "
                "(event_id, horizon_h, ref_price, fwd_price, fwd_return) "
                "VALUES (?,?,?,?,?)",
                (event_id, horizon_h, ref_price, fwd_price, fwd_return),
            )
            self._conn.commit()

    def analog_outcomes(self, category: str, direction: str,
                        horizon_h: float,
                        exclude_event_id: int | None = None) -> list[float]:
        """Forward returns of *matured* past events matching category+direction.

        Only matured events are included, so no look-ahead: an event's outcome
        exists only once the horizon has fully elapsed in recorded data.
        """
        q = ("SELECT o.fwd_return FROM outcomes o JOIN events e "
             "ON o.event_id=e.id WHERE e.category=? AND e.direction=? "
             "AND o.horizon_h=? AND e.matured=1")
        params: list[Any] = [category, direction, horizon_h]
        if exclude_event_id is not None:
            q += " AND e.id<>?"; params.append(exclude_event_id)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [r["fwd_return"] for r in rows if r["fwd_return"] is not None]

    # ------------------------------------------------------------------- dedup
    def seen(self, item_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_items WHERE hash=?", (item_hash,)
            ).fetchone()
        return row is not None

    def mark_seen(self, item_hash: str, source: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_items (hash, ts, source) "
                "VALUES (?,?,?)",
                (item_hash, int(datetime.now(timezone.utc).timestamp()), source),
            )
            self._conn.commit()

    # ------------------------------------------------------------ notifications
    def record_notification(self, event_id: int | None, kind: str,
                            dedup: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notifications (ts, event_id, kind, dedup) "
                "VALUES (?,?,?,?)",
                (int(datetime.now(timezone.utc).timestamp()), event_id, kind,
                 dedup),
            )
            self._conn.commit()

    def notified_since(self, dedup: str, since: datetime) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM notifications WHERE dedup=? AND ts>=? LIMIT 1",
                (dedup, _epoch(since)),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ alerts
    def insert_alert(self, ts: datetime, event_id: int | None, symbol: str,
                     category: str, direction: str, conviction: int,
                     ref_price: float | None, horizon_h: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (ts, event_id, symbol, category, direction, "
                "conviction, ref_price, horizon_h, scored) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (_epoch(ts), event_id, symbol, category, direction,
                 int(conviction), ref_price, float(horizon_h)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def unscored_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE scored=0 AND direction!='neutral' "
                "ORDER BY ts ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def score_alert(self, alert_id: int, correct: int,
                    fwd_return: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET scored=1, correct=?, fwd_return=? WHERE id=?",
                (int(correct), float(fwd_return), alert_id),
            )
            self._conn.commit()

    def alert_stats(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Scored alerts (optionally since a time), newest first."""
        q = "SELECT * FROM alerts WHERE scored=1"
        params: list[Any] = []
        if since is not None:
            q += " AND ts>=?"; params.append(_epoch(since))
        q += " ORDER BY ts DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default


def _f(v: Any) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
