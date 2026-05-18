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

-- Daily close snapshot. One row per (symbol, UTC date). Populated by the
-- first poll of each day. Used by thesis-drift sentinel for 30-day price
-- series. Sparse to start — fills out over time.
CREATE TABLE IF NOT EXISTS daily_closes (
    symbol TEXT NOT NULL,
    close_date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, close_date)
);

-- News-monitor output. One row per (symbol, run_ts). The headlines field
-- stores the raw JSON list we sent to Claude so we can audit later.
CREATE TABLE IF NOT EXISTS news_analysis (
    symbol TEXT NOT NULL,
    run_ts TEXT NOT NULL,
    relevant_count INTEGER NOT NULL,
    contradicts INTEGER NOT NULL,         -- bool 0/1
    summary TEXT NOT NULL,
    headlines TEXT,                       -- JSON: [{title, url, age_hours, source}]
    raw_response TEXT,                    -- Claude's raw JSON for audit
    PRIMARY KEY (symbol, run_ts)
);
CREATE INDEX IF NOT EXISTS news_symbol_ts ON news_analysis(symbol, run_ts DESC);

-- Thesis-drift sentinel weekly output. Score 1-5 (1=thesis broken, 5=fully intact).
CREATE TABLE IF NOT EXISTS thesis_drift (
    symbol TEXT NOT NULL,
    run_ts TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    reasoning TEXT NOT NULL,
    raw_response TEXT,
    PRIMARY KEY (symbol, run_ts)
);
CREATE INDEX IF NOT EXISTS thesis_symbol_ts ON thesis_drift(symbol, run_ts DESC);

-- Macro regime synthesis. Latest row matters; older rows kept for history.
CREATE TABLE IF NOT EXISTS macro_regime (
    run_ts TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    macros_snapshot TEXT NOT NULL,        -- JSON
    raw_response TEXT
);

-- TradingView ratings. Refreshed every poll alongside prices.
--   technical_today / technical_1w: one of strong_sell|sell|neutral|buy|strong_buy
--   analyst_target_min / analyst_target_max / analyst_target_currency:
--     from the FAQ "future price" answer; absent on ETFs/indices.
CREATE TABLE IF NOT EXISTS ratings (
    symbol TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    technical_today TEXT,
    technical_1w TEXT,
    analyst_target_min REAL,
    analyst_target_max REAL,
    analyst_target_currency TEXT,
    PRIMARY KEY (symbol, fetched_at)
);
CREATE INDEX IF NOT EXISTS ratings_symbol_ts ON ratings(symbol, fetched_at DESC);

-- Conviction double-check results. One row per actionable item per run.
-- A row exists for BOTH high- and low-conviction items so the dashboard
-- can show "alerts suppressed by noise filter" alongside the ones that
-- passed. The threshold is recorded so we can recompute later if changed.
CREATE TABLE IF NOT EXISTS conviction_runs (
    run_ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    score INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    passed INTEGER NOT NULL,                 -- bool 0/1
    reasoning TEXT,
    signals TEXT,                            -- JSON
    first_pass_reasoning TEXT,
    PRIMARY KEY (run_ts, symbol, kind)
);
CREATE INDEX IF NOT EXISTS conviction_runs_ts ON conviction_runs(run_ts DESC);
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


# ─────────────────────────── daily closes ───────────────────────────

def record_daily_close_if_first(symbol: str, price: float) -> bool:
    """If no row exists yet for (symbol, today_utc), insert one. Returns True
    when a new row was inserted. Callers should pass a fresh live price only —
    cached or stale prices shouldn't seed the close series.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM daily_closes WHERE symbol=? AND close_date=?",
            (symbol, today),
        ).fetchone()
        if row:
            return False
        c.execute(
            "INSERT INTO daily_closes (symbol, close_date, close) VALUES (?, ?, ?)",
            (symbol, today, price),
        )
    return True


def daily_closes(symbol: str, days: int = 30) -> list[tuple[str, float]]:
    """Most-recent `days` daily closes for `symbol`, oldest-first."""
    with conn() as c:
        rows = c.execute(
            "SELECT close_date, close FROM daily_closes "
            "WHERE symbol = ? ORDER BY close_date DESC LIMIT ?",
            (symbol, days),
        ).fetchall()
    return [(r["close_date"], r["close"]) for r in reversed(rows)]


# ─────────────────────────── news analysis ───────────────────────────

def save_news_analysis(
    symbol: str,
    relevant_count: int,
    contradicts: bool,
    summary: str,
    headlines: Any,
    raw_response: str,
) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO news_analysis "
            "(symbol, run_ts, relevant_count, contradicts, summary, headlines, raw_response) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, utcnow_iso(), relevant_count, 1 if contradicts else 0,
             summary, json.dumps(headlines), raw_response),
        )


def latest_news_analysis(symbol: str) -> dict[str, Any] | None:
    """Most recent news_analysis row for a symbol."""
    with conn() as c:
        r = c.execute(
            "SELECT run_ts, relevant_count, contradicts, summary, headlines "
            "FROM news_analysis WHERE symbol=? ORDER BY run_ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if not r:
        return None
    out = dict(r)
    out["contradicts"] = bool(out["contradicts"])
    try:
        out["headlines"] = json.loads(out["headlines"] or "[]")
    except json.JSONDecodeError:
        out["headlines"] = []
    return out


def latest_news_for_all() -> dict[str, dict[str, Any]]:
    """Most recent news_analysis row per symbol, keyed by symbol."""
    with conn() as c:
        rows = c.execute(
            "SELECT n.symbol, n.run_ts, n.relevant_count, n.contradicts, n.summary, n.headlines "
            "FROM news_analysis n "
            "INNER JOIN (SELECT symbol, MAX(run_ts) mt FROM news_analysis GROUP BY symbol) m "
            "ON n.symbol = m.symbol AND n.run_ts = m.mt"
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        d["contradicts"] = bool(d["contradicts"])
        try:
            d["headlines"] = json.loads(d["headlines"] or "[]")
        except json.JSONDecodeError:
            d["headlines"] = []
        out[r["symbol"]] = d
    return out


# ─────────────────────────── thesis drift ───────────────────────────

def save_thesis_drift(symbol: str, score: int, reasoning: str, raw: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO thesis_drift "
            "(symbol, run_ts, score, reasoning, raw_response) VALUES (?, ?, ?, ?, ?)",
            (symbol, utcnow_iso(), score, reasoning, raw),
        )


def latest_thesis_drift(symbol: str) -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute(
            "SELECT run_ts, score, reasoning FROM thesis_drift "
            "WHERE symbol=? ORDER BY run_ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(r) if r else None


def previous_thesis_drift(symbol: str) -> dict[str, Any] | None:
    """The row immediately before the latest — used to detect score drops."""
    with conn() as c:
        r = c.execute(
            "SELECT run_ts, score, reasoning FROM thesis_drift "
            "WHERE symbol=? ORDER BY run_ts DESC LIMIT 1 OFFSET 1",
            (symbol,),
        ).fetchone()
    return dict(r) if r else None


def latest_thesis_drift_for_all() -> dict[str, dict[str, Any]]:
    """Most recent thesis_drift row per symbol, keyed by symbol."""
    with conn() as c:
        rows = c.execute(
            "SELECT t.symbol, t.run_ts, t.score, t.reasoning "
            "FROM thesis_drift t "
            "INNER JOIN (SELECT symbol, MAX(run_ts) mt FROM thesis_drift GROUP BY symbol) m "
            "ON t.symbol = m.symbol AND t.run_ts = m.mt"
        ).fetchall()
    return {r["symbol"]: dict(r) for r in rows}


# ─────────────────────────── macro regime ───────────────────────────

def save_macro_regime(summary: str, snapshot: dict[str, Any], raw: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO macro_regime (run_ts, summary, macros_snapshot, raw_response) "
            "VALUES (?, ?, ?, ?)",
            (utcnow_iso(), summary, json.dumps(snapshot), raw),
        )


def latest_macro_regime() -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute(
            "SELECT run_ts, summary, macros_snapshot FROM macro_regime "
            "ORDER BY run_ts DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    out = dict(r)
    try:
        out["macros_snapshot"] = json.loads(out["macros_snapshot"])
    except json.JSONDecodeError:
        out["macros_snapshot"] = {}
    return out


# ─────────────────────────── ratings ───────────────────────────

def save_ratings(symbol: str, ratings: dict[str, Any]) -> None:
    """Insert a ratings row. ratings can have any subset of the columns."""
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ratings "
            "(symbol, fetched_at, technical_today, technical_1w, "
            " analyst_target_min, analyst_target_max, analyst_target_currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, utcnow_iso(),
             ratings.get("technical_today"),
             ratings.get("technical_1w"),
             ratings.get("analyst_target_min"),
             ratings.get("analyst_target_max"),
             ratings.get("analyst_target_currency")),
        )


def latest_ratings_for_all() -> dict[str, dict[str, Any]]:
    """Most recent ratings row per symbol."""
    with conn() as c:
        rows = c.execute(
            "SELECT r.* FROM ratings r "
            "INNER JOIN (SELECT symbol, MAX(fetched_at) mt FROM ratings GROUP BY symbol) m "
            "ON r.symbol = m.symbol AND r.fetched_at = m.mt"
        ).fetchall()
    return {r["symbol"]: dict(r) for r in rows}


def latest_rating(symbol: str) -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM ratings WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(r) if r else None


# ─────────────────────────── conviction runs ───────────────────────────

def save_conviction_run(
    actionable_first_pass: list[dict[str, Any]],
    convictions: dict[tuple[str, str], dict[str, Any]],
    threshold: int,
) -> None:
    """One run = one analyze_and_alert call. Persist every actionable item
    that the first pass surfaced, with its conviction score and whether it
    passed. Empty inputs are a no-op (we don't want a blank row per poll).
    """
    if not actionable_first_pass:
        return
    ts = utcnow_iso()
    with conn() as c:
        for item in actionable_first_pass:
            key = (item["symbol"], item["kind"])
            conv = convictions.get(key, {})
            score = int(conv.get("score", 0))
            c.execute(
                "INSERT OR REPLACE INTO conviction_runs "
                "(run_ts, symbol, kind, score, threshold, passed, "
                " reasoning, signals, first_pass_reasoning) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, key[0], key[1], score, threshold,
                 1 if score >= threshold else 0,
                 conv.get("reasoning", ""),
                 json.dumps(conv.get("signals", {})),
                 item.get("reasoning", "")),
            )


def recent_conviction_runs(limit: int = 25) -> list[dict[str, Any]]:
    """Recent conviction-scored items, newest first. Includes BOTH passed
    and suppressed rows so the dashboard can show both buckets.
    """
    with conn() as c:
        rows = c.execute(
            "SELECT run_ts, symbol, kind, score, threshold, passed, "
            "       reasoning, signals, first_pass_reasoning "
            "FROM conviction_runs ORDER BY run_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["passed"] = bool(d["passed"])
        try:
            d["signals"] = json.loads(d["signals"] or "{}")
        except json.JSONDecodeError:
            d["signals"] = {}
        out.append(d)
    return out
