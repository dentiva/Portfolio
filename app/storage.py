"""SQLite storage layer.

Single file at $DATA_DIR/portfolio.db. Three tables:
- prices: every poll's price for every symbol (for trend/drawdown calc)
- alerts_sent: dedup ledger keyed by (alert_key, calendar_date)
- snapshots: one row per day, holds portfolio JSON for the dashboard timeline
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import settings


DB_PATH = settings.data_dir / "portfolio.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS prices_symbol_ts ON prices(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS alerts_sent (
    alert_key TEXT NOT NULL,
    sent_date TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    payload TEXT,
    PRIMARY KEY (alert_key, sent_date)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date TEXT PRIMARY KEY,
    total_value_usd REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_log (
    ts TEXT PRIMARY KEY,
    triggers_in INTEGER NOT NULL,
    actionable INTEGER NOT NULL,
    summary TEXT,
    raw_response TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def init() -> None:
    """Create tables if not present."""
    with conn() as c:
        c.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ----- prices -----

def save_prices(prices: dict[str, float]) -> None:
    ts = utcnow_iso()
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO prices (symbol, ts, price) VALUES (?, ?, ?)",
            [(sym, ts, px) for sym, px in prices.items() if px is not None],
        )


def latest_price(symbol: str) -> tuple[float | None, str | None]:
    with conn() as c:
        row = c.execute(
            "SELECT price, ts FROM prices WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if row is None:
        return None, None
    return row["price"], row["ts"]


def price_at_age(symbol: str, hours_ago: int) -> float | None:
    """Best-effort: returns the closest price >= hours_ago old."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with conn() as c:
        row = c.execute(
            "SELECT price FROM prices WHERE symbol = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (symbol, cutoff),
        ).fetchone()
    return row["price"] if row else None


def price_history(symbol: str, days: int = 30) -> list[tuple[str, float]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with conn() as c:
        rows = c.execute(
            "SELECT ts, price FROM prices WHERE symbol = ? AND ts >= ? ORDER BY ts ASC",
            (symbol, cutoff),
        ).fetchall()
    return [(r["ts"], r["price"]) for r in rows]


def prune_old_prices(keep_days: int = 60) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with conn() as c:
        cur = c.execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
        return cur.rowcount


# ----- alerts dedup -----

def already_sent_today(alert_key: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM alerts_sent WHERE alert_key = ? AND sent_date = ?",
            (alert_key, today_iso()),
        ).fetchone()
    return row is not None


def mark_alert_sent(alert_key: str, payload: dict[str, Any]) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO alerts_sent "
            "(alert_key, sent_date, sent_at, payload) VALUES (?, ?, ?, ?)",
            (alert_key, today_iso(), utcnow_iso(), json.dumps(payload)),
        )


def alerts_sent_today_count() -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM alerts_sent WHERE sent_date = ?", (today_iso(),)
        ).fetchone()
    return row["n"]


# ----- snapshots -----

def save_snapshot(total_value_usd: float, payload: dict[str, Any]) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(snapshot_date, total_value_usd, payload) VALUES (?, ?, ?)",
            (today_iso(), total_value_usd, json.dumps(payload)),
        )


def recent_snapshots(days: int = 30) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with conn() as c:
        rows = c.execute(
            "SELECT snapshot_date, total_value_usd, payload FROM snapshots "
            "WHERE snapshot_date >= ? ORDER BY snapshot_date ASC",
            (cutoff,),
        ).fetchall()
    return [
        {
            "date": r["snapshot_date"],
            "total_value_usd": r["total_value_usd"],
            **json.loads(r["payload"]),
        }
        for r in rows
    ]


# ----- analysis log -----

def log_analysis(triggers_in: int, actionable: int, summary: str, raw: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO analysis_log "
            "(ts, triggers_in, actionable, summary, raw_response) VALUES (?, ?, ?, ?, ?)",
            (utcnow_iso(), triggers_in, actionable, summary, raw),
        )


def recent_analysis(limit: int = 20) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT ts, triggers_in, actionable, summary FROM analysis_log "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
