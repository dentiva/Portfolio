"""Indian-tax helpers for the offshore (IBKR) portfolio.

What this computes:
  - Realized capital gains for an Indian FY (Apr 1 – Mar 31), via FIFO lot
    matching across the full transaction history (so pre-FY lots are
    available for matching against in-FY sales).
  - Holding period split: > 24 months on foreign listed shares = LTCG;
    ≤ 24 months = STCG. (Foreign listed shares fall under the "other
    capital asset" 24-month rule, not the 12-month rule for shares listed
    on a recognised Indian exchange.)
  - LTCG tax estimate at 12.5% without indexation, the post-23-Jul-2024
    regime (Finance (No.2) Act 2024). The ₹1.25L exemption applies to
    *domestic* listed equity only and is NOT applied here.
  - STCG amount only (taxed at the assessee's slab — we don't know yours).
  - Dividend income totals for the FY (taxed at slab as "Income from
    Other Sources"). No DTAA credit netting.
  - Schedule FA – Section A3 ("Foreign Equity & Debt Interest") row per
    symbol for a calendar year, with peak / closing balance, dividend,
    and gross sale proceeds.

What this DOES NOT compute (all flagged in the UI):
  - Rule-115 per-transaction FX rates. We accept ONE flat USD→INR rate
    and apply it uniformly. Real filing needs SBI TT-buying rate on the
    last day of the month preceding each transaction (Rule 115 IT Rules).
  - Indexation for pre-23-Jul-2024 sales (the older 20%-with-indexation
    LTCG regime). All LTCG is presented at the 12.5%-without-indexation
    rate.
  - Surcharge & cess. The "tax estimate" is base tax only.
  - DTAA foreign tax credit on dividends withheld at source.
  - Section 112A / 111A specifics for Indian-listed equity (not relevant
    to a fully-offshore IBKR portfolio).

Schedule FA notes:
  - The "calendar year" relevant to a given FY is the CY ending during
    that FY. For FY 2024-25 → CY 2024. We default cy = fy_start_year.
  - Peak balance is APPROXIMATED as the max of (current value, lifetime
    cost basis). True peak would need daily price history we don't keep.
  - "Closing balance" uses the latest cached price × quantity held at
    CY end — so if today's date is past CY end and there have been
    later transactions, this column drifts. Re-run inside the CY for
    cleaner numbers, or use a CY-end snapshot.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import storage


# Holding-period threshold for foreign listed shares to qualify as long-term.
# 24 months ≈ 730 days. Tax law speaks in months but day-counting is the
# operational proxy; edge cases around month-end can shift by ±1 day —
# users at the boundary should verify with a CA.
_LT_THRESHOLD_DAYS = 730

# LTCG rate for foreign shares (resident assessee), post-Finance (No.2) Act 2024.
_LTCG_RATE = 0.125


# ─────────────────────────── data shapes ───────────────────────────


@dataclass
class _Lot:
    """One purchase lot remaining in the FIFO queue."""
    date: str                 # YYYY-MM-DD
    qty: float                # shares remaining in this lot
    cost_per_share_usd: float # cost basis including commission (absolute USD per share)


@dataclass
class GainRecord:
    """One realized-gain row: a Sell matched against (part of) one Buy lot."""
    symbol: str
    sell_date: str
    sell_qty: float
    sell_proceeds_usd: float
    buy_date: str
    buy_cost_usd: float
    holding_days: int
    gain_usd: float
    is_long_term: bool
    # INR-converted at the flat rate provided by the caller:
    sell_proceeds_inr: float
    buy_cost_inr: float
    gain_inr: float


# ─────────────────────────── date helpers ───────────────────────────


def current_fy_start_year() -> int:
    """Start year of the current Indian FY.
    Indian FY runs Apr 1 → Mar 31. So in Jan–Mar, the FY started LAST year.
    """
    t = datetime.now()
    return t.year if t.month >= 4 else t.year - 1


def most_recent_fy_with_data() -> int | None:
    """Return the FY start year of the latest Buy/Sell/Dividend in the DB.
    None if no transactions at all. Used as the dashboard's default FY so
    the Tax tab opens to actual data instead of an empty current year.
    """
    with storage.conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM transactions "
            "WHERE type IN ('Buy', 'Sell', 'Dividend')"
        ).fetchone()
    if not row or not row["d"]:
        return None
    # Parse YYYY-MM-DD; FY starts Apr 1
    y, m = int(row["d"][:4]), int(row["d"][5:7])
    return y if m >= 4 else y - 1


def transaction_count_by_fy() -> dict[int, int]:
    """Count Buy/Sell/Dividend transactions grouped by FY start year.
    Returned dict keyed by FY start year. Used to annotate the FY dropdown.
    """
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date FROM transactions "
            "WHERE type IN ('Buy', 'Sell', 'Dividend')"
        ).fetchall()
    counts: dict[int, int] = {}
    for r in rows:
        d = r["date"]
        if not d or len(d) < 7:
            continue
        y, m = int(d[:4]), int(d[5:7])
        fy = y if m >= 4 else y - 1
        counts[fy] = counts.get(fy, 0) + 1
    return counts


def fy_label(fy_start_year: int) -> str:
    """'2024-25' style label for an FY starting in fy_start_year."""
    return f"{fy_start_year}-{str(fy_start_year + 1)[2:]}"


def fy_range(fy_start_year: int) -> tuple[str, str]:
    """(start_iso, end_iso) inclusive for an FY (Apr 1 → Mar 31)."""
    return (f"{fy_start_year}-04-01", f"{fy_start_year + 1}-03-31")


def cy_range(cy: int) -> tuple[str, str]:
    """(start_iso, end_iso) inclusive for a calendar year."""
    return (f"{cy}-01-01", f"{cy}-12-31")


def relevant_cy_for_fy(fy_start_year: int) -> int:
    """The CY ending during a given FY (the one Schedule FA wants).
    FY 2024-25 (Apr 24 → Mar 25) → CY 2024 ends within it → return 2024.
    """
    return fy_start_year


def _holding_days(buy_iso: str, sell_iso: str) -> int:
    b = datetime.strptime(buy_iso, "%Y-%m-%d").date()
    s = datetime.strptime(sell_iso, "%Y-%m-%d").date()
    return (s - b).days


# ─────────────────────────── core: FIFO gain matching ───────────────


def _load_buy_sell_history(end_date_iso: str) -> list[dict]:
    """All Buy/Sell rows ever (≤ end_date_iso), ordered chronologically.
    We need pre-FY lots in scope so the FIFO queue is correct when we hit
    an in-FY sale.
    """
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date, type, symbol, quantity, net_usd, gross_usd "
            "FROM transactions "
            "WHERE type IN ('Buy', 'Sell') AND symbol != '' AND date <= ? "
            "ORDER BY date ASC, rowid ASC",
            (end_date_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def realized_gains_for_fy(fy_start_year: int, fx_rate: float) -> dict[str, Any]:
    """FIFO-match every Sell against prior Buys; emit a GainRecord per
    matched chunk. Filter to those whose SELL date falls inside the FY.
    """
    fy_start, fy_end = fy_range(fy_start_year)
    rows = _load_buy_sell_history(end_date_iso=fy_end)

    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    in_fy_gains: list[GainRecord] = []
    unmatched_sells: list[dict] = []  # for diagnostics

    for r in rows:
        sym = r["symbol"]
        qty = abs(r["quantity"] or 0.0)
        if qty <= 0:
            continue

        if r["type"] == "Buy":
            # Cost per share including commission: |net_usd| / qty
            net = r["net_usd"] or 0.0
            cost_per = abs(net) / qty
            lots[sym].append(_Lot(date=r["date"], qty=qty, cost_per_share_usd=cost_per))
            continue

        # Sell: pop from oldest lots until the sell quantity is matched
        net = r["net_usd"] or 0.0  # positive for a sell
        sell_per_share = net / qty if qty else 0.0
        sell_qty_left = qty

        while sell_qty_left > 1e-9 and lots[sym]:
            lot = lots[sym][0]
            matched = min(sell_qty_left, lot.qty)
            buy_cost = matched * lot.cost_per_share_usd
            proceeds = matched * sell_per_share
            gain = proceeds - buy_cost
            days = _holding_days(lot.date, r["date"])

            if fy_start <= r["date"] <= fy_end:
                in_fy_gains.append(GainRecord(
                    symbol=sym,
                    sell_date=r["date"],
                    sell_qty=matched,
                    sell_proceeds_usd=proceeds,
                    buy_date=lot.date,
                    buy_cost_usd=buy_cost,
                    holding_days=days,
                    gain_usd=gain,
                    is_long_term=(days > _LT_THRESHOLD_DAYS),
                    sell_proceeds_inr=proceeds * fx_rate,
                    buy_cost_inr=buy_cost * fx_rate,
                    gain_inr=gain * fx_rate,
                ))

            lot.qty -= matched
            sell_qty_left -= matched
            if lot.qty <= 1e-9:
                lots[sym].popleft()

        if sell_qty_left > 1e-9:
            unmatched_sells.append({
                "date": r["date"], "symbol": sym, "unmatched_qty": sell_qty_left,
            })

    ltcg_inr = sum(g.gain_inr for g in in_fy_gains if g.is_long_term)
    stcg_inr = sum(g.gain_inr for g in in_fy_gains if not g.is_long_term)
    ltcg_usd = sum(g.gain_usd for g in in_fy_gains if g.is_long_term)
    stcg_usd = sum(g.gain_usd for g in in_fy_gains if not g.is_long_term)

    return {
        "fy_label": fy_label(fy_start_year),
        "fy_start": fy_start,
        "fy_end": fy_end,
        "fx_rate": fx_rate,
        "gains": sorted(in_fy_gains, key=lambda g: (g.sell_date, g.symbol)),
        "ltcg_usd": ltcg_usd,
        "stcg_usd": stcg_usd,
        "ltcg_inr": ltcg_inr,
        "stcg_inr": stcg_inr,
        "ltcg_tax_inr": ltcg_inr * _LTCG_RATE if ltcg_inr > 0 else 0.0,
        "ltcg_rate_pct": _LTCG_RATE * 100,
        "unmatched_sells": unmatched_sells,
    }


# ─────────────────────────── dividends ───────────────────────────


def dividend_income_for_fy(fy_start_year: int, fx_rate: float) -> dict[str, Any]:
    """Sum every Dividend row in the FY; INR-convert at the flat rate."""
    fy_start, fy_end = fy_range(fy_start_year)
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date, symbol, description, net_usd "
            "FROM transactions "
            "WHERE type = 'Dividend' AND date >= ? AND date <= ? "
            "ORDER BY date ASC, symbol ASC",
            (fy_start, fy_end),
        ).fetchall()
    items = []
    total_usd = 0.0
    for r in rows:
        amt = r["net_usd"] or 0.0
        total_usd += amt
        items.append({
            "date": r["date"],
            "symbol": r["symbol"] or "—",
            "description": (r["description"] or "")[:80],
            "usd": amt,
            "inr": amt * fx_rate,
        })
    return {
        "fy_label": fy_label(fy_start_year),
        "rows": items,
        "total_usd": total_usd,
        "total_inr": total_usd * fx_rate,
        "fx_rate": fx_rate,
    }


# ─────────────────────────── Schedule FA (A3) ───────────────────────


def schedule_fa_for_cy(cy: int, fx_rate: float) -> dict[str, Any]:
    """One row per symbol for the calendar year, with the columns
    Section A3 of Schedule FA wants:
      - Country code, name & address of issuer  (we leave issuer fields
        blank — pulled from IBKR statement when filing).
      - Date of acquisition (first Buy date).
      - Initial investment (cumulative buy cost USD up to first holding).
      - Peak balance during the period (INR).
      - Closing balance (INR).
      - Gross interest / dividend (INR).
      - Gross proceeds on sale/redemption (INR).
    """
    cy_start, cy_end = cy_range(cy)

    # Pull everything ≤ CY end; we need pre-CY buys for opening qty.
    with storage.conn() as c:
        rows = c.execute(
            "SELECT date, type, symbol, quantity, net_usd, gross_usd "
            "FROM transactions WHERE symbol != '' AND date <= ? "
            "ORDER BY date ASC, rowid ASC",
            (cy_end,),
        ).fetchall()

    # Per-symbol running state
    state: dict[str, dict[str, Any]] = {}

    def _s(sym: str) -> dict[str, Any]:
        return state.setdefault(sym, {
            "symbol": sym,
            "first_buy_date": None,
            "qty": 0.0,
            "cost_basis_usd": 0.0,   # running cost basis (decrements proportionally on sales)
            "total_buys_usd": 0.0,   # lifetime cumulative buys (for initial-investment column)
            "dividend_cy_usd": 0.0,
            "proceeds_cy_usd": 0.0,
            "activity_in_cy": False,
        })

    for r in rows:
        sym = r["symbol"]
        d = _s(sym)
        qty = r["quantity"] or 0.0
        net = r["net_usd"] or 0.0
        gross = r["gross_usd"] or 0.0

        if r["type"] == "Buy":
            buy_qty = abs(qty)
            buy_cost = abs(net)
            d["qty"] += buy_qty
            d["cost_basis_usd"] += buy_cost
            d["total_buys_usd"] += buy_cost
            if d["first_buy_date"] is None or r["date"] < d["first_buy_date"]:
                d["first_buy_date"] = r["date"]
            if cy_start <= r["date"] <= cy_end:
                d["activity_in_cy"] = True

        elif r["type"] == "Sell":
            sell_qty = abs(qty)
            if d["qty"] > 1e-9:
                # Reduce cost basis proportionally (FIFO already handled in
                # realized_gains_for_fy; here we only need running qty/value).
                frac = min(sell_qty / d["qty"], 1.0)
                d["cost_basis_usd"] *= (1.0 - frac)
            d["qty"] = max(d["qty"] - sell_qty, 0.0)
            if cy_start <= r["date"] <= cy_end:
                # Use gross_usd for Schedule FA "gross proceeds" if parsed,
                # else fall back to |net_usd|.
                proceeds = abs(gross) if gross else abs(net)
                d["proceeds_cy_usd"] += proceeds
                d["activity_in_cy"] = True

        elif r["type"] == "Dividend":
            if cy_start <= r["date"] <= cy_end:
                d["dividend_cy_usd"] += abs(net)
                d["activity_in_cy"] = True

    # Now emit one row per symbol that was held at any point during CY
    # OR had any income/proceeds within CY.
    rows_out: list[dict[str, Any]] = []
    for sym, d in state.items():
        held_during_cy = d["qty"] > 1e-9  # held at CY end
        if not (held_during_cy or d["activity_in_cy"]):
            continue

        px, px_ts = storage.latest_price(sym)
        closing_usd = (px or 0.0) * d["qty"]
        # Peak proxy: max(current value, cost basis remaining, total ever invested)
        # The third term catches the case where the position is now closed
        # but had real value mid-CY before being sold.
        peak_usd = max(closing_usd, d["cost_basis_usd"], d["total_buys_usd"])

        rows_out.append({
            "symbol": sym,
            "first_buy_date": d["first_buy_date"] or "—",
            "closing_qty": d["qty"],
            "closing_usd": closing_usd,
            "closing_inr": closing_usd * fx_rate,
            "peak_usd": peak_usd,
            "peak_inr": peak_usd * fx_rate,
            "initial_investment_usd": d["total_buys_usd"],
            "initial_investment_inr": d["total_buys_usd"] * fx_rate,
            "dividend_usd": d["dividend_cy_usd"],
            "dividend_inr": d["dividend_cy_usd"] * fx_rate,
            "proceeds_usd": d["proceeds_cy_usd"],
            "proceeds_inr": d["proceeds_cy_usd"] * fx_rate,
            "price_ts": px_ts,
        })

    rows_out.sort(key=lambda r: r["symbol"])

    totals = {
        "closing_inr": sum(r["closing_inr"] for r in rows_out),
        "peak_inr": sum(r["peak_inr"] for r in rows_out),
        "dividend_inr": sum(r["dividend_inr"] for r in rows_out),
        "proceeds_inr": sum(r["proceeds_inr"] for r in rows_out),
        "initial_investment_inr": sum(r["initial_investment_inr"] for r in rows_out),
    }

    return {
        "cy": cy,
        "cy_start": cy_start,
        "cy_end": cy_end,
        "fx_rate": fx_rate,
        "rows": rows_out,
        "totals": totals,
    }
