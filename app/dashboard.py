"""Dashboard data builder.

Pulls latest prices, computes per-holding P&L, totals, leg breakdown,
and recent alerts. Returns a dict the Jinja template renders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import ingest, prices, reconcile, storage, tax
from .config import portfolio, settings

ROOT = Path(__file__).parent.parent
_env = Environment(
    loader=FileSystemLoader(ROOT / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def _fmt_inr(v: float | None) -> str:
    """Format INR using lakh/crore. Thresholds:
       v < 1 lakh        →  ₹12,345                (no decimals, regular comma)
       1 L ≤ v < 1 Cr    →  ₹1.23 L                (two-decimal lakhs)
       v ≥ 1 Cr          →  ₹1.23 Cr               (two-decimal crores)
       v is None         →  —
       For round numbers, drop trailing zeros (₹2.5 L, not ₹2.50 L) for readability.
       1 lakh = 100,000;  1 crore = 100 lakh = 10,000,000.
    """
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    n = abs(v)

    def _trim(x: float, decimals: int = 2) -> str:
        """Format with up to `decimals` places, stripping trailing zeros/dot."""
        s = f"{x:.{decimals}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    if n >= 1_00_00_000:                 # ≥ 1 crore
        return f"{sign}₹{_trim(n / 1_00_00_000)} Cr"
    if n >= 1_00_000:                    # ≥ 1 lakh
        return f"{sign}₹{_trim(n / 1_00_000)} L"
    # Below a lakh: plain rupee value with regular comma grouping, no decimals
    return f"{sign}₹{int(round(n)):,}"


_env.filters["usd"] = _fmt_usd
_env.filters["pct"] = _fmt_pct
_env.filters["inr"] = _fmt_inr


def render_dashboard(
    fx_rate: float | None = None,
    fy_start_year: int | None = None,
    cy: int | None = None,
    active_tab: str = "holdings",
) -> str:
    """Snapshot the current state and render dashboard.html.

    Query params (all optional):
      - fx_rate: flat USD→INR rate used for every INR conversion on the
        tax tab. If None, falls back to cached USDINR=X price; if that's
        also missing, leaves it None and the template surfaces a banner.
      - fy_start_year: start year of the Indian FY to summarize. Defaults
        to the current FY.
      - cy: calendar year for Schedule FA. Defaults to the CY ending
        during the chosen FY (= fy_start_year).
      - active_tab: 'holdings' or 'tax'; controls which tab is open on load.
    """
    rows = []
    total_value = 0.0
    total_cost = 0.0
    leg_totals: dict[str, float] = {}
    leg_costs: dict[str, float] = {}

    # Pre-fetch per-symbol AI artifacts once so we don't N+1 the DB
    news_map = storage.latest_news_for_all()
    drift_map = storage.latest_thesis_drift_for_all()
    ratings_map = storage.latest_ratings_for_all()

    for h in portfolio.holdings:
        px, ts = storage.latest_price(h.yf_symbol)
        # Source label: derived from whether the holding has tv_url configured.
        # When TV is the only live provider, a held price implies TV; absence
        # means TV failed and either cache or "missing" is in play (the true
        # source is logged by prices.py at fetch time).
        source = "tradingview" if h.tv_url and px else "—"
        mv = (px or 0) * h.quantity
        cost = h.avg_cost_usd * h.quantity
        pnl = mv - cost
        pnl_pct = (pnl / cost * 100) if cost else None
        chg_24h = prices.pct_change_24h(h.yf_symbol, px) if px else None
        chg_7d = prices.pct_change_7d(h.yf_symbol, px) if px else None

        # Distance to entry / stop / exit
        dist_entry = ((px - h.entry_target) / h.entry_target * 100) if (px and h.entry_target) else None
        dist_stop = ((px - h.stop_loss) / h.stop_loss * 100) if (px and h.stop_loss) else None
        dist_exit = ((h.exit_target - px) / px * 100) if (px and h.exit_target) else None

        rows.append({
            "ticker": h.ticker,
            "name": h.name,
            "leg": h.leg,
            "source": source,
            "quantity": h.quantity,
            "avg_cost": h.avg_cost_usd,
            "price": px,
            "ts": ts,
            "market_value": mv,
            "cost_basis": cost,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "chg_24h": chg_24h,
            "chg_7d": chg_7d,
            "entry_target": h.entry_target,
            "exit_target": h.exit_target,
            "stop_loss": h.stop_loss,
            "dist_to_entry_pct": dist_entry,
            "dist_to_stop_pct": dist_stop,
            "dist_to_exit_pct": dist_exit,
            "thesis": h.thesis,
            "news": news_map.get(h.yf_symbol),     # {summary, relevant_count, contradicts, headlines, run_ts} or None
            "drift": drift_map.get(h.yf_symbol),   # {score, reasoning, run_ts} or None
            "rating": ratings_map.get(h.yf_symbol),  # {technical_today, technical_1w, analyst_target_*} or None
        })

        # Cost-side totals are computable from config alone, so accumulate
        # unconditionally — these should show even when no poll has run yet.
        total_cost += cost
        leg_costs[h.leg] = leg_costs.get(h.leg, 0) + cost
        # Market-value-side totals only count holdings we have a price for.
        if px:
            total_value += mv
            leg_totals[h.leg] = leg_totals.get(h.leg, 0) + mv

    macro_rows = []
    for m in portfolio.macro_watch:
        px, ts = storage.latest_price(m.symbol)
        chg_24h = prices.pct_change_24h(m.symbol, px) if px else None
        macro_rows.append({
            "name": m.name,
            "symbol": m.symbol,
            "price": px,
            "ts": ts,
            "chg_24h": chg_24h,
            "key_levels": m.key_levels,
        })

    # USD/INR for INR conversion display
    usdinr = next(
        (m["price"] for m in macro_rows if m["symbol"] == "USDINR=X"),
        None,
    )
    gold_price_per_oz = next(
        (m["price"] for m in macro_rows if m["symbol"] == "GC=F"),
        None,
    )
    # Gold equivalent in metric: 1 troy oz = 31.1034768 grams.
    # Portfolio_grams = total_value_USD / (price_per_oz / 31.1034768).
    if gold_price_per_oz and total_value:
        total_gold_g = total_value / gold_price_per_oz * 31.1034768
        total_gold_kg = total_gold_g / 1000.0
    else:
        total_gold_g = None
        total_gold_kg = None

    leg_pcts = {
        leg: (v / total_value * 100) if total_value else 0
        for leg, v in leg_totals.items()
    }
    # Cost-basis-based percentages as a fallback for when no prices have polled yet
    leg_cost_pcts = {
        leg: (v / total_cost * 100) if total_cost else 0
        for leg, v in leg_costs.items()
    }

    recent_alerts = storage.recent_analysis(limit=10)
    recent_txns = ingest.recent_transactions(limit=15)
    recent_ingestions = ingest.recent_ingestions(limit=5)
    txn_total = ingest.transaction_count()

    # Save daily snapshot
    storage.save_snapshot(
        total_value_usd=total_value,
        payload={
            "total_cost": total_cost,
            "leg_totals": leg_totals,
            "holdings_count": len(portfolio.holdings),
        },
    )

    # ---- Tax tab: compute FY summary + Schedule FA ----
    # Default FY: the most recent one with any transactions. Falls back to the
    # current FY if the ledger is empty. This prevents the "I entered the FX
    # rate but everything's still empty" footgun where the dropdown defaulted
    # to a future FY with no data.
    if fy_start_year is None:
        fy_start_year_eff = tax.most_recent_fy_with_data() or tax.current_fy_start_year()
    else:
        fy_start_year_eff = fy_start_year
    cy_eff = cy if cy is not None else tax.relevant_cy_for_fy(fy_start_year_eff)

    # Effective FX rate: explicit > cached > None (template surfaces a banner)
    fx_cached, fx_cached_ts = storage.latest_price("USDINR=X")
    if fx_rate is not None:
        fx_effective = fx_rate
        fx_source = "user-input"
    elif fx_cached is not None:
        fx_effective = fx_cached
        fx_source = f"cached ({fx_cached_ts})"
    else:
        fx_effective = None
        fx_source = "missing"

    # Compute with 0.0 when missing so the table still renders; template
    # shows a prominent banner when fx_effective is None.
    fx_for_calc = fx_effective if fx_effective is not None else 0.0
    realized = tax.realized_gains_for_fy(fy_start_year_eff, fx_for_calc)
    dividends = tax.dividend_income_for_fy(fy_start_year_eff, fx_for_calc)
    sched_fa = tax.schedule_fa_for_cy(cy_eff, fx_for_calc)

    # Year-selector options: cover the last 5 FYs + current
    current_fy = tax.current_fy_start_year()
    fy_options = list(range(current_fy - 4, current_fy + 1))
    cy_options = list(range(current_fy - 4, current_fy + 1))
    fy_tx_counts = tax.transaction_count_by_fy()

    # ---- AI/intelligence panels ----
    reconciliation = reconcile.summary()
    macro_regime = storage.latest_macro_regime()
    conviction_runs = storage.recent_conviction_runs(limit=25)
    high_conviction = [c for c in conviction_runs if c["passed"]]
    suppressed = [c for c in conviction_runs if not c["passed"]]

    template = _env.get_template("dashboard.html")
    return template.render(
        rows=rows,
        macro_rows=macro_rows,
        total_value=total_value,
        total_cost=total_cost,
        total_pnl=total_value - total_cost,
        total_pnl_pct=((total_value - total_cost) / total_cost * 100) if total_cost else 0,
        leg_totals=leg_totals,
        leg_pcts=leg_pcts,
        leg_costs=leg_costs,
        leg_cost_pcts=leg_cost_pcts,
        usdinr=usdinr,
        total_gold_g=total_gold_g,
        total_gold_kg=total_gold_kg,
        total_inr=(total_value * usdinr) if usdinr else None,
        refresh_minutes=portfolio.settings.dashboard_refresh_minutes,
        recent_alerts=recent_alerts,
        recent_txns=recent_txns,
        recent_ingestions=recent_ingestions,
        txn_total=txn_total,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        # tax tab:
        active_tab=active_tab,
        fx_rate=fx_effective,
        fx_source=fx_source,
        fy_start_year=fy_start_year_eff,
        fy_label=tax.fy_label(fy_start_year_eff),
        cy=cy_eff,
        fy_options=fy_options,
        cy_options=cy_options,
        fy_tx_counts=fy_tx_counts,
        # AI/intelligence:
        reconciliation=reconciliation,
        macro_regime=macro_regime,
        high_conviction=high_conviction,
        suppressed=suppressed,
        conviction_threshold=portfolio.settings.conviction_threshold,
        realized=realized,
        dividends=dividends,
        sched_fa=sched_fa,
    )
