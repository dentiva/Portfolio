"""Statement reconciliation.

Walks every Buy/Sell transaction in the ledger and derives the *implied*
position quantity per ticker. Compares that against the configured quantity
in portfolio.json. Any mismatch is a drift candidate — usually a corporate
action (split, scrip dividend, spin-off) the user forgot to update in config,
or a missed IBKR statement.

Pure rule-based. No Claude. No external calls. Runs in <50ms over thousands
of transactions.

Drift severity buckets:
  - exact match → omit (most rows)
  - missing in config → "untracked": ledger says you hold it, config doesn't
  - missing in ledger → "unconfirmed": config says you hold it, ledger has no trades
  - small fractional mismatch (<0.5% or <1 share) → "minor"
  - everything else → "significant"
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from . import storage
from .config import portfolio


@dataclass
class Drift:
    symbol: str
    ticker: str | None         # display ticker if known to config
    configured_qty: float      # quantity in portfolio.json (0 if untracked)
    derived_qty: float         # quantity implied by Buy/Sell ledger
    delta: float               # derived - configured
    last_txn_date: str | None  # most recent Buy/Sell in the ledger
    severity: str              # "untracked" | "unconfirmed" | "minor" | "significant"

    @property
    def severity_rank(self) -> int:
        return {"significant": 3, "untracked": 2, "unconfirmed": 1, "minor": 0}[self.severity]


def _derived_positions() -> dict[str, tuple[float, str | None]]:
    """Per-symbol: (net qty implied by Buy/Sell ledger, last_txn_date).
    Ignores Dividend rows since they don't change qty.
    """
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date, type, symbol, quantity FROM transactions "
            "WHERE type IN ('Buy', 'Sell') AND symbol != '' "
            "ORDER BY date ASC, rowid ASC"
        ).fetchall()

    qty: dict[str, float] = defaultdict(float)
    last_date: dict[str, str] = {}
    for r in rows:
        sym = r["symbol"]
        q = abs(r["quantity"] or 0.0)
        if r["type"] == "Buy":
            qty[sym] += q
        else:  # Sell
            qty[sym] -= q
        last_date[sym] = r["date"]
    return {s: (q, last_date.get(s)) for s, q in qty.items()}


def _classify(configured: float, derived: float) -> str | None:
    """Return severity bucket, or None if the row should be suppressed."""
    if configured == 0 and derived <= 1e-6:
        return None                          # nothing on either side
    if configured == 0:
        return "untracked"                    # ledger has it, config doesn't
    if derived <= 1e-6:
        return "unconfirmed"                  # config has it, ledger empty
    delta = abs(derived - configured)
    if delta < max(1.0, configured * 0.005):  # <1 share OR <0.5% of position
        if delta < 1e-6:
            return None                       # exact match
        return "minor"
    return "significant"


def compute_drifts() -> list[Drift]:
    """Run reconciliation against current config + ledger. Sorted by severity
    descending, then by absolute delta descending.
    """
    derived = _derived_positions()
    configured = {h.yf_symbol: h for h in portfolio.holdings}

    all_symbols = set(derived) | set(configured)
    out: list[Drift] = []
    for sym in all_symbols:
        d_qty, last_dt = derived.get(sym, (0.0, None))
        cfg = configured.get(sym)
        cfg_qty = cfg.quantity if cfg else 0.0
        sev = _classify(cfg_qty, d_qty)
        if sev is None:
            continue
        out.append(Drift(
            symbol=sym,
            ticker=cfg.ticker if cfg else None,
            configured_qty=cfg_qty,
            derived_qty=d_qty,
            delta=d_qty - cfg_qty,
            last_txn_date=last_dt,
            severity=sev,
        ))
    out.sort(key=lambda d: (-d.severity_rank, -abs(d.delta)))
    return out


def summary() -> dict[str, Any]:
    """Counts by severity. Used for the dashboard badge."""
    drifts = compute_drifts()
    counts: dict[str, int] = {"significant": 0, "untracked": 0, "unconfirmed": 0, "minor": 0}
    for d in drifts:
        counts[d.severity] += 1
    return {
        "total": len(drifts),
        "counts": counts,
        "drifts": drifts,
        "needs_attention": counts["significant"] + counts["untracked"] > 0,
    }
