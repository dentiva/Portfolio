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

    IBKR Flex statements record tickers without exchange suffix (REMX, INFR,
    XEG) while portfolio.json uses the canonical yf_symbol (REMX.L, INFR.L,
    XEG.TO). We map raw → canonical before comparing so non-US holdings
    don't appear as duplicate "untracked + unconfirmed" pairs.
    """
    derived_raw = _derived_positions()
    configured = {h.yf_symbol: h for h in portfolio.holdings}

    # Build the same raw-ticker → yf_symbol map that recompute_positions uses.
    # Both the ticker field and the base of yf_symbol (REMX in REMX.L) map to
    # the canonical yf_symbol.
    ibkr_to_yf: dict[str, str] = {}
    for h in portfolio.holdings:
        base = h.yf_symbol.split(".")[0].split("=")[0].split("^")[0]
        ibkr_to_yf[base] = h.yf_symbol
        ibkr_to_yf[h.ticker] = h.yf_symbol

    # Re-key the derived positions onto the canonical yf_symbol. Anything that
    # doesn't map is left under its raw IBKR ticker so it shows up as
    # "untracked" (genuinely new ticker — no matching config entry).
    derived: dict[str, tuple[float, str | None]] = {}
    for raw_sym, (qty, last_dt) in derived_raw.items():
        canonical = ibkr_to_yf.get(raw_sym, raw_sym)
        if canonical in derived:
            # Two raw tickers mapped to the same canonical — sum quantities,
            # keep the later last_txn_date
            prev_q, prev_d = derived[canonical]
            new_d = max(prev_d, last_dt) if prev_d and last_dt else (last_dt or prev_d)
            derived[canonical] = (prev_q + qty, new_d)
        else:
            derived[canonical] = (qty, last_dt)

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
