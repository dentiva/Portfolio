"""Trade-file ingestion.

Watches a directory for new IBKR statements (PDF or CSV), parses them,
stores every transaction in SQLite (deduplicating across re-uploads),
and rewrites quantity + avg_cost_usd in portfolio.json from the
full transaction history.

Flow:
  1. Drop file in $TRADES_DIR (e.g. ./trades/ locally, /data/trades/ on Railway)
  2. ingest.scan_and_process() runs on every poll cycle (and on startup)
  3. New transactions appended to SQLite (dedup by date+symbol+qty+net)
  4. Positions recomputed from full ledger → portfolio.json patched
  5. Processed file moved to trades/processed/YYYYMMDD-<name>

The same flow runs from the dashboard /upload endpoint.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .config import APP_DIR, settings

log = logging.getLogger(__name__)


TRADES_DIR = settings.data_dir / "trades"
PROCESSED_DIR = TRADES_DIR / "processed"
ERRORS_DIR = TRADES_DIR / "errors"
PORTFOLIO_JSON = APP_DIR / "portfolio.json"


@dataclass
class Transaction:
    date: str            # YYYY-MM-DD
    type: str            # Buy|Sell|Dividend|Deposit|Interest|FX|Adjustment
    symbol: str          # ticker or '' for non-stock
    description: str
    quantity: float | None
    price: float | None
    price_ccy: str
    gross_usd: float
    commission_usd: float
    net_usd: float

    def dedup_key(self) -> tuple:
        return (self.date, self.symbol, self.type, self.quantity or 0, round(self.net_usd, 2))


# ─── Schema extension ──────────────────────────────────────

TRANSACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    description TEXT,
    quantity REAL,
    price REAL,
    price_ccy TEXT,
    gross_usd REAL,
    commission_usd REAL,
    net_usd REAL NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (date, symbol, type, quantity, net_usd)
);
CREATE INDEX IF NOT EXISTS txn_symbol ON transactions(symbol);
CREATE INDEX IF NOT EXISTS txn_date ON transactions(date);

CREATE TABLE IF NOT EXISTS ingest_log (
    ts TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    txns_total INTEGER,
    txns_new INTEGER,
    status TEXT,
    error TEXT
);
"""


def init() -> None:
    """Create dirs + extend DB schema. Idempotent."""
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)
    ERRORS_DIR.mkdir(exist_ok=True)
    with storage.conn() as c:
        c.executescript(TRANSACTIONS_SCHEMA)
    # Drop a README so the directory is self-documenting
    readme = TRADES_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Drop IBKR transaction statements here (PDF or CSV).\n"
            "They will be parsed automatically on the next poll cycle.\n"
            "Processed files move to ./processed/, parse failures to ./errors/.\n"
            "\n"
            "Best practice: export 'Activity Flex Statement' as CSV from IBKR\n"
            "(Reports > Flex Queries) — much more reliable than PDF parsing.\n"
        )


# ─── Public entrypoints ───────────────────────────────────

def scan_and_process() -> dict:
    """Process every new file in trades/. Called by scheduler + on startup."""
    init()
    results = {"files": 0, "txns_new": 0, "txns_total": 0, "errors": []}
    for f in sorted(TRADES_DIR.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.name == "README.txt":
            continue
        if f.suffix.lower() not in {".pdf", ".csv"}:
            log.warning("Skipping unknown file type: %s", f.name)
            continue
        try:
            new, total = _process_file(f)
            results["files"] += 1
            results["txns_new"] += new
            results["txns_total"] += total
        except Exception as e:
            log.exception("Failed to process %s: %s", f.name, e)
            results["errors"].append({"file": f.name, "error": str(e)})
            _move_with_timestamp(f, ERRORS_DIR)
            _log_ingestion(f.name, 0, 0, "error", str(e))

    if results["txns_new"] > 0:
        recompute_positions()
        log.info("Positions recomputed after %d new transactions", results["txns_new"])

    return results


def ingest_uploaded_file(content: bytes, original_name: str) -> dict:
    """Save uploaded bytes to trades/ then process immediately."""
    init()
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
    dest = TRADES_DIR / safe_name
    if dest.exists():  # avoid clobber
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = TRADES_DIR / f"{ts}-{safe_name}"
    dest.write_bytes(content)
    log.info("Uploaded file written to %s", dest)
    return scan_and_process()


# ─── Per-file processing ──────────────────────────────────

def _process_file(path: Path) -> tuple[int, int]:
    """Parse + store one file. Returns (new_count, total_in_file)."""
    log.info("Processing %s", path.name)
    txns = parse_file(path)
    total = len(txns)
    new = _store_transactions(txns, source=path.name)
    _move_with_timestamp(path, PROCESSED_DIR)
    _log_ingestion(path.name, total, new, "ok", "")
    log.info("  · %s: %d total, %d new", path.name, total, new)
    return new, total


def _move_with_timestamp(src: Path, dest_dir: Path) -> Path:
    """Move src → dest_dir/YYYYMMDD-<name> to avoid name collisions."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = dest_dir / f"{ts}-{src.name}"
    n = 1
    while target.exists():
        target = dest_dir / f"{ts}-{n}-{src.name}"
        n += 1
    shutil.move(str(src), str(target))
    return target


def _store_transactions(txns: list[Transaction], source: str) -> int:
    """Insert with OR IGNORE so duplicates (same dedup key) are skipped silently."""
    if not txns:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    with storage.conn() as c:
        for t in txns:
            cur = c.execute(
                "INSERT OR IGNORE INTO transactions "
                "(date, type, symbol, description, quantity, price, price_ccy, "
                " gross_usd, commission_usd, net_usd, source_file, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t.date, t.type, t.symbol, t.description, t.quantity, t.price,
                 t.price_ccy, t.gross_usd, t.commission_usd, t.net_usd, source, now),
            )
            inserted += cur.rowcount
    return inserted


def _log_ingestion(filename: str, total: int, new: int, status: str, error: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with storage.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ingest_log "
            "(ts, source_file, txns_total, txns_new, status, error) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, filename, total, new, status, error),
        )


# ─── Parsers ──────────────────────────────────────────────

def parse_file(path: Path) -> list[Transaction]:
    if path.suffix.lower() == ".csv":
        return parse_csv(path)
    if path.suffix.lower() == ".pdf":
        return parse_pdf(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")


def parse_csv(path: Path) -> list[Transaction]:
    """IBKR Activity Flex Statement CSV.

    Two header-naming conventions in the wild:
      - Compact / IBKR Flex native: TradeDate, TradePrice, CurrencyPrimary, NetCash
      - Spaced / generic: "Trade Date", "Trade Price", "Currency", "Net Amount"

    We normalize whitespace and underscores before matching so both work.
    NetCash from IBKR Flex is in the trade's LOCAL currency, not USD —
    we convert to USD at parse time using today's FX rates. (Historical
    per-trade-date FX would be more accurate; today's is a reasonable
    approximation for portfolio-monitoring purposes and avoids depending
    on a historical-FX provider. The tax tab applies its own FX field.)
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []

    def _normhdr(s: str) -> str:
        """Lowercase + strip whitespace, underscores, slashes — so 'TradeDate',
        'trade date', 'trade_date', and 'trade/date' all become 'tradedate'."""
        return re.sub(r"[\s_/]+", "", s.strip().lower())

    # Heuristic: find header row with date+symbol+quantity+amount columns
    header_idx = None
    for i, row in enumerate(rows[:30]):
        norm = [_normhdr(c) for c in row]
        has_date = any("date" in c for c in norm)
        has_sym = any(c in {"symbol", "ticker"} for c in norm)
        has_qty = any(c in {"quantity", "qty"} for c in norm)
        has_amt = any("amount" in c or "proceeds" in c or "netcash" in c for c in norm)
        if has_date and has_sym and has_qty and has_amt:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not locate header row in CSV — check format")

    header = [_normhdr(c) for c in rows[header_idx]]

    def col(*names: str) -> int | None:
        """Look up by ANY of the supplied names (also normalized)."""
        for n in names:
            key = _normhdr(n)
            if key in header:
                return header.index(key)
        return None

    c_date = col("date", "tradedate", "settledate", "datetime")
    c_sym = col("symbol", "ticker")
    c_type = col("buysell", "transactiontype", "type")
    c_qty = col("quantity", "qty")
    c_price = col("price", "tradeprice")
    c_ccy = col("currency", "currencyprimary", "pricecurrency")
    c_gross = col("grossamount", "proceeds", "trademoney")
    c_comm = col("commission", "ibcommission", "commfee")
    c_net = col("netamount", "netcash")
    c_desc = col("description", "name")
    # AssetClass filter: IBKR Flex puts internal FX conversions (CASH rows
    # like "EUR.USD") alongside real equity trades. We only want STK rows.
    c_assetclass = col("assetclass")

    def f(row, idx):
        if idx is None or idx >= len(row):
            return None
        v = (row[idx] or "").strip()
        return v or None

    def fnum(row, idx, default=0.0):
        v = f(row, idx)
        if v is None:
            return default
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return default

    out: list[Transaction] = []
    for row in rows[header_idx + 1:]:
        if not row or not f(row, c_date):
            continue
        # Skip non-equity rows (FX conversions are CASH, not STK).
        # If AssetClass column is absent (older / generic CSVs), we don't filter.
        if c_assetclass is not None:
            ac = (f(row, c_assetclass) or "").upper()
            if ac and ac != "STK":
                continue
        # Normalize date to YYYY-MM-DD
        d_raw = f(row, c_date) or ""
        d = _normalize_date(d_raw)
        if d is None:
            continue
        ttype_raw = (f(row, c_type) or "").upper()
        ttype = "Buy" if ttype_raw.startswith("BUY") else "Sell" if ttype_raw.startswith("SELL") else ttype_raw.title() or "Buy"
        sym = f(row, c_sym) or ""
        out.append(Transaction(
            date=d,
            type=ttype,
            symbol=sym,
            description=f(row, c_desc) or "",
            quantity=fnum(row, c_qty),
            price=fnum(row, c_price),
            price_ccy=(f(row, c_ccy) or "USD").upper(),
            gross_usd=fnum(row, c_gross),
            commission_usd=fnum(row, c_comm),
            net_usd=fnum(row, c_net),
        ))

    # FX-normalize: NetCash/Proceeds/Commission from IBKR Flex are in the
    # trade's LOCAL currency. Convert non-USD rows to USD using today's FX
    # rates. (Historical per-trade-date rates would be more accurate but
    # require a separate historical-FX provider; today's rates are a
    # reasonable approximation for portfolio P&L. For tax filing, the tax
    # tab applies its own user-supplied FX rate per Rule 115.)
    _fx_normalize(out)
    return out


def _fx_normalize(txns: list[Transaction]) -> None:
    """In-place: convert net_usd/gross_usd/commission_usd from price_ccy → USD
    for every non-USD transaction. Single FX fetch per call regardless of row count.
    """
    needs_fx = {t.price_ccy for t in txns if t.price_ccy and t.price_ccy != "USD"}
    if not needs_fx:
        return
    # Import locally to avoid a hard dep cycle at module load
    from . import fx as fx_mod
    log.info("FX-normalizing %d trade(s) in non-USD currencies: %s",
             sum(1 for t in txns if t.price_ccy != "USD"), sorted(needs_fx))
    rates = fx_mod.fetch_fx_rates()
    for t in txns:
        if not t.price_ccy or t.price_ccy == "USD":
            continue
        rate = rates.get(t.price_ccy)
        if rate is None:
            log.error("No FX rate for %s — leaving %s %s as-is (will be wrong)",
                      t.price_ccy, t.symbol, t.date)
            continue
        t.net_usd = t.net_usd * rate
        t.gross_usd = t.gross_usd * rate
        t.commission_usd = t.commission_usd * rate


# Compiled patterns reused across PDF parsing
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_NUM_RE = r"-?[\d,]+\.?\d*"


def parse_pdf(path: Path) -> list[Transaction]:
    """Best-effort IBKR Transaction History PDF parser.

    IBKR PDFs are text-extractable. We split by line, identify transaction
    rows by the leading date, and parse columns positionally where possible
    or via regex anchors otherwise.

    Less reliable than CSV — recommend Flex CSV export for monthly use.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber not installed — pip install pdfplumber") from e

    out: list[Transaction] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            out.extend(_parse_pdf_text(text))
    # Dedup within file
    seen = set()
    deduped = []
    for t in out:
        k = t.dedup_key()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(t)
    _fx_normalize(deduped)
    return deduped


def _parse_pdf_text(text: str) -> list[Transaction]:
    """Parse a single page's text. Each transaction starts with YYYY-MM-DD."""
    txns: list[Transaction] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _DATE_RE.match(line)
        if not m:
            i += 1
            continue

        # Multi-line row: concat 1-2 follow-on lines if they don't start with date
        chunk = [line]
        j = i + 1
        while j < len(lines) and not _DATE_RE.match(lines[j].strip()) and j - i < 3:
            if lines[j].strip():
                chunk.append(lines[j].strip())
            j += 1
        i = j

        row_text = " ".join(chunk)
        parsed = _parse_pdf_row(row_text)
        if parsed:
            txns.append(parsed)
    return txns


def _parse_pdf_row(s: str) -> Transaction | None:
    """Extract one Transaction from a concatenated row string.

    Approach: try to detect a stock Buy/Sell pattern FIRST (strict regex anchor).
    Only fall back to keyword-based classification if the strict pattern fails.
    This avoids misclassification when pdfplumber emits text out-of-order and
    keywords like "Adjustment" bleed in from adjacent rows.
    """
    date_m = _DATE_RE.match(s)
    if not date_m:
        return None
    date = date_m.group(1)

    def to_f(x: str) -> float:
        try:
            return float(x.replace(",", ""))
        except ValueError:
            return 0.0

    # ── 1. Strict stock-trade detection ─────────────────────
    # Pattern: "(Buy|Sell) SYMBOL qty price CCY" — anchored on the action verb
    # followed by a ticker, two numbers, and a 3-letter currency code.
    trade_m = re.search(
        r"\b(Buy|Sell)\s+([A-Z][A-Z0-9.]{0,9})\s+([\-\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+(USD|EUR|GBP|NOK|CAD|CHF|JPY|HKD|SGD|AUD)\b",
        s,
    )
    if trade_m:
        ttype = trade_m.group(1)
        symbol = trade_m.group(2)
        qty = to_f(trade_m.group(3))
        price = to_f(trade_m.group(4))
        ccy = trade_m.group(5)

        # Extract trailing numbers for gross/commission/net
        # Use only numbers AFTER the currency code to avoid cross-row bleed
        tail = s[trade_m.end():]
        nums = re.findall(_NUM_RE, tail)
        if not nums:
            return None
        net = to_f(nums[-1])
        commission = to_f(nums[-2]) if len(nums) >= 2 else 0.0
        gross = to_f(nums[-3]) if len(nums) >= 3 else net

        if ttype == "Sell" and qty > 0:
            qty = -qty

        desc = re.sub(r"^\d{4}-\d{2}-\d{2}\s+U\S+\s+", "", s)
        desc = desc[:120]

        return Transaction(
            date=date, type=ttype, symbol=symbol, description=desc,
            quantity=qty, price=price, price_ccy=ccy,
            gross_usd=gross, commission_usd=commission, net_usd=net,
        )

    # ── 2. Non-trade classification (dividends, deposits, etc.) ─
    # Check more specific keywords first.
    type_keywords = [
        ("Forex Trade Component", "FX"),
        ("FX Translations", "Adjustment"),
        ("Credit Interest", "Interest"),
        ("Debit Interest", "Interest"),
        ("Cash Dividend", "Dividend"),
        ("Dividend", "Dividend"),
        ("Electronic Fund Transfer", "Deposit"),
        ("Deposit", "Deposit"),
        ("Withdrawal", "Withdrawal"),
        ("Adjustment", "Adjustment"),
    ]
    ttype = None
    for kw, label in type_keywords:
        if kw in s:
            ttype = label
            break
    if ttype is None:
        return None

    # For non-trade rows, just grab the last number as the net amount.
    nums = re.findall(_NUM_RE, s)
    if not nums:
        return None
    net = to_f(nums[-1])

    # Symbol for dividends: try to extract
    symbol = ""
    sym_m = re.search(r"\bDividend\s+([A-Z][A-Z0-9.]{1,9})\b", s)
    if sym_m:
        symbol = sym_m.group(1)

    desc = re.sub(r"^\d{4}-\d{2}-\d{2}\s+U\S+\s+", "", s)
    desc = desc[:120]

    return Transaction(
        date=date, type=ttype, symbol=symbol, description=desc,
        quantity=None, price=None, price_ccy="USD",
        gross_usd=net, commission_usd=0.0, net_usd=net,
    )


def _normalize_date(s: str) -> str | None:
    """Try common date formats; return YYYY-MM-DD or None."""
    s = s.strip().split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ─── Position recomputation ───────────────────────────────

def recompute_positions() -> dict:
    """Read all Buy/Sell transactions, compute current quantity + FIFO-remaining
    avg cost per symbol, and patch portfolio.json in place — preserving
    user-set thresholds and thesis.

    FIFO note: cost basis is computed over remaining shares only. After a
    partial sale, the older lots are popped first; only the remaining lots
    contribute to avg_cost. This matches what tax.py uses for capital-gains
    matching and produces the "true" cost basis for shares you currently
    hold (not the lifetime weighted average across shares you've already sold).
    """
    from collections import deque

    with storage.conn() as c:
        rows = c.execute(
            "SELECT symbol, type, quantity, net_usd FROM transactions "
            "WHERE type IN ('Buy', 'Sell') AND symbol != '' "
            "ORDER BY date ASC, rowid ASC"
        ).fetchall()

    # Per-symbol FIFO lot queue. Each lot: {"qty", "cost_per_share"}.
    # cost_per_share carries commission since net_usd is net of fees — that
    # matches the tax-cost convention.
    lots: dict[str, deque] = {}
    for r in rows:
        sym = r["symbol"]
        qty = r["quantity"] or 0          # sells are already negative per the parser
        net = r["net_usd"] or 0           # buys negative (cash out), sells positive (cash in)
        if sym not in lots:
            lots[sym] = deque()

        if r["type"] == "Buy":
            buy_qty = abs(qty)
            if buy_qty <= 1e-9:
                continue
            cost_per = abs(net) / buy_qty
            lots[sym].append({"qty": buy_qty, "cost_per_share": cost_per})
        else:  # Sell — FIFO-pop from oldest lots until matched
            sell_qty = abs(qty)
            while sell_qty > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                matched = min(sell_qty, lot["qty"])
                lot["qty"] -= matched
                sell_qty -= matched
                if lot["qty"] <= 1e-9:
                    lots[sym].popleft()
            # If sell_qty > 0 here, there's a data gap — we silently
            # zero out the position rather than over-pop. The reconcile
            # tab surfaces unmatched sells as drift.

    # Reduce each symbol's lot queue to (remaining_qty, weighted_avg_cost)
    positions: dict[str, dict[str, float]] = {}
    for sym, queue in lots.items():
        total_q = sum(l["qty"] for l in queue)
        if total_q <= 1e-9:
            positions[sym] = {"qty": 0.0, "avg_cost": 0.0}
            continue
        total_basis = sum(l["qty"] * l["cost_per_share"] for l in queue)
        positions[sym] = {
            "qty": total_q,
            "avg_cost": total_basis / total_q,
        }

    # Build new portfolio.json patches
    cfg = json.loads(PORTFOLIO_JSON.read_text())
    existing_by_yf = {h["yf_symbol"]: h for h in cfg["holdings"]}

    # Map IBKR ticker -> yf_symbol heuristically. If user already has it
    # listed, we preserve the mapping. Otherwise we insert a STUB with yf_symbol
    # equal to the ticker (user must adjust if it needs .L / .OL / .TO etc).
    ibkr_to_yf = {}
    for h in cfg["holdings"]:
        base = h["yf_symbol"].split(".")[0].split("=")[0].split("^")[0]
        ibkr_to_yf[base] = h["yf_symbol"]
        ibkr_to_yf[h["ticker"]] = h["yf_symbol"]

    updates_log: list[str] = []
    new_holdings: list[str] = []

    for sym, p in positions.items():
        yf_sym = ibkr_to_yf.get(sym, sym)
        avg_cost = p["avg_cost"]
        rounded_qty = round(p["qty"], 4)

        if yf_sym in existing_by_yf:
            h = existing_by_yf[yf_sym]
            if h["quantity"] != rounded_qty or abs(h["avg_cost_usd"] - avg_cost) > 0.01:
                updates_log.append(
                    f"{sym}: qty {h['quantity']} → {rounded_qty}, "
                    f"avg ${h['avg_cost_usd']:.2f} → ${avg_cost:.2f}"
                )
                h["quantity"] = rounded_qty
                h["avg_cost_usd"] = round(avg_cost, 4)
        else:
            # New ticker — add stub with null thresholds (user must fill)
            new_h = {
                "ticker": sym,
                "yf_symbol": sym,
                "name": f"{sym} (auto-added — please verify yf_symbol)",
                "leg": "Uncategorized",
                "quantity": rounded_qty,
                "avg_cost_usd": round(avg_cost, 4),
                "entry_target": None,
                "exit_target": None,
                "stop_loss": None,
                "thesis": "AUTO-ADDED from transactions. Verify yf_symbol (.L/.OL/.TO/etc.) and fill thresholds.",
            }
            cfg["holdings"].append(new_h)
            new_holdings.append(sym)
            updates_log.append(f"{sym}: NEW (qty {rounded_qty}, avg ${avg_cost:.2f}) — verify yf_symbol!")

    # Remove holdings that have zero quantity AND aren't manually marked as planned buys
    cfg["holdings"] = [
        h for h in cfg["holdings"]
        if h["quantity"] != 0 or "PLANNED" in h.get("thesis", "").upper()
    ]

    PORTFOLIO_JSON.write_text(json.dumps(cfg, indent=2))

    log.info("Position update: %d updates, %d new holdings", len(updates_log), len(new_holdings))
    for u in updates_log:
        log.info("  · %s", u)

    return {
        "updates": updates_log,
        "new_holdings": new_holdings,
        "total_positions": len(positions),
    }


# ─── Read API for dashboard ───────────────────────────────

def recent_transactions(limit: int = 25) -> list[dict]:
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date, type, symbol, description, quantity, net_usd, source_file, ingested_at "
            "FROM transactions ORDER BY date DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_ingestions(limit: int = 10) -> list[dict]:
    with storage.conn() as c:
        rows = c.execute(
            "SELECT ts, source_file, txns_total, txns_new, status, error "
            "FROM ingest_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def transaction_count() -> int:
    with storage.conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()
    return row["n"]
