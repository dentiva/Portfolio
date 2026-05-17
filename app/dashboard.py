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

from . import ingest, prices, storage
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


_env.filters["usd"] = _fmt_usd
_env.filters["pct"] = _fmt_pct


def render_dashboard() -> str:
    """Snapshot the current state and render dashboard.html."""
    rows = []
    total_value = 0.0
    total_cost = 0.0
    leg_totals: dict[str, float] = {}
    leg_costs: dict[str, float] = {}

    for h in portfolio.holdings:
        px, ts = storage.latest_price(h.yf_symbol)
        # Source label: derived from whether the holding has tv_url configured
        # (this is best-effort; the true source is logged by prices.py at fetch time)
        source = "tradingview" if h.tv_url and px else ("yfinance" if px else "—")
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

        if px:
            total_value += mv
            total_cost += cost
            leg_totals[h.leg] = leg_totals.get(h.leg, 0) + mv
            leg_costs[h.leg] = leg_costs.get(h.leg, 0) + cost

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
    )
