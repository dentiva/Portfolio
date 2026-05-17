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

from . import ingest, prices, storage, tax
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
    """Indian-locale grouping with the rupee sign. ₹1,23,456.78 style."""
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) <= 3:
        return f"{sign}₹{whole}.{frac}"
    # Indian grouping: last 3 digits, then groups of 2 walking left
    last3, rest = whole[-3:], whole[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return f"{sign}₹{','.join(groups + [last3])}.{frac}"


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
    gold_oz = next(
        (m["price"] for m in macro_rows if m["symbol"] == "GC=F"),
        None,
    )

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
    fy_start_year_eff = fy_start_year if fy_start_year is not None else tax.current_fy_start_year()
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
        gold_oz=gold_oz,
        total_inr=(total_value * usdinr) if usdinr else None,
        total_gold_oz=(total_value / gold_oz) if gold_oz else None,
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
        realized=realized,
        dividends=dividends,
        sched_fa=sched_fa,
    )
