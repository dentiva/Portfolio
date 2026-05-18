"""Trigger detection.

Pure rule-based: walk every holding and macro symbol; emit Trigger records
for entry/exit/stop hits, drawdown thresholds, key-level breaks.

Triggers do NOT go straight to email — analysis.py reviews them with Claude,
and only the actionable subset is alerted.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from . import prices
from .config import portfolio


class TriggerSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class TriggerKind(str, Enum):
    ENTRY_HIT = "entry_hit"        # price <= entry_target (opportunity)
    EXIT_HIT = "exit_hit"          # price >= exit_target (take profit)
    STOP_LOSS = "stop_loss"        # price <= stop_loss (threat)
    DRAWDOWN_24H = "drawdown_24h"
    DRAWDOWN_7D = "drawdown_7d"
    GAIN_24H = "gain_24h"
    KEY_LEVEL_BREAK_UP = "key_level_break_up"
    KEY_LEVEL_BREAK_DOWN = "key_level_break_down"
    MACRO_MOVE = "macro_move"
    THESIS_DRIFT = "thesis_drift"   # emitted by thesis.py, not by rule engine


@dataclass
class Trigger:
    """One detected trigger. Multiple triggers per poll are normal."""

    kind: TriggerKind
    severity: TriggerSeverity
    symbol: str               # the canonical lookup symbol (Holding.yf_symbol / MacroWatch.symbol)
    display_name: str         # human label (ticker or macro name)
    price: float
    detail: str               # human-readable reason
    context: dict[str, Any]   # numbers for Claude to reason about

    def alert_key(self) -> str:
        """Used to dedup against alerts already sent today."""
        return f"{self.symbol}:{self.kind.value}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["severity"] = self.severity.value
        return d


def detect_triggers(
    price_map: dict[str, tuple[float | None, str]],
) -> list[Trigger]:
    """Walk holdings + macro list, return all current triggers."""
    triggers: list[Trigger] = []

    # --- Holdings: entry / exit / stop / drawdown ---
    for h in portfolio.holdings:
        px, src = price_map.get(h.yf_symbol, (None, "missing"))
        if px is None:
            continue

        # Entry target hit (price dropped to or below target = opportunity)
        if h.entry_target and px <= h.entry_target:
            triggers.append(
                Trigger(
                    kind=TriggerKind.ENTRY_HIT,
                    severity=TriggerSeverity.INFO,
                    symbol=h.yf_symbol,
                    display_name=h.ticker,
                    price=px,
                    detail=f"{h.ticker} at ${px:.2f} — at/below entry target ${h.entry_target:.2f}",
                    context={
                        "ticker": h.ticker,
                        "name": h.name,
                        "entry_target": h.entry_target,
                        "current_price": px,
                        "avg_cost_usd": h.avg_cost_usd,
                        "quantity": h.quantity,
                        "thesis": h.thesis,
                    },
                )
            )

        # Exit / profit target hit
        if h.exit_target and px >= h.exit_target:
            triggers.append(
                Trigger(
                    kind=TriggerKind.EXIT_HIT,
                    severity=TriggerSeverity.INFO,
                    symbol=h.yf_symbol,
                    display_name=h.ticker,
                    price=px,
                    detail=f"{h.ticker} at ${px:.2f} — at/above exit target ${h.exit_target:.2f}",
                    context={
                        "ticker": h.ticker,
                        "exit_target": h.exit_target,
                        "current_price": px,
                        "avg_cost_usd": h.avg_cost_usd,
                        "thesis": h.thesis,
                    },
                )
            )

        # Stop loss breached (threat)
        if h.stop_loss and px <= h.stop_loss:
            triggers.append(
                Trigger(
                    kind=TriggerKind.STOP_LOSS,
                    severity=TriggerSeverity.CRITICAL,
                    symbol=h.yf_symbol,
                    display_name=h.ticker,
                    price=px,
                    detail=f"{h.ticker} at ${px:.2f} — BELOW stop-loss ${h.stop_loss:.2f}",
                    context={
                        "ticker": h.ticker,
                        "stop_loss": h.stop_loss,
                        "current_price": px,
                        "avg_cost_usd": h.avg_cost_usd,
                        "quantity": h.quantity,
                        "thesis": h.thesis,
                    },
                )
            )

        # Drawdown 24h
        chg = prices.pct_change_24h(h.yf_symbol, px)
        if chg is not None:
            t = portfolio.settings.drawdown_alert_pct_24h
            if chg <= -t:
                triggers.append(
                    Trigger(
                        kind=TriggerKind.DRAWDOWN_24H,
                        severity=TriggerSeverity.WARN,
                        symbol=h.yf_symbol,
                        display_name=h.ticker,
                        price=px,
                        detail=f"{h.ticker} down {chg:.1f}% in 24h",
                        context={"ticker": h.ticker, "change_24h_pct": chg, "thesis": h.thesis},
                    )
                )
            elif chg >= t * 1.5:  # only flag big upmoves
                triggers.append(
                    Trigger(
                        kind=TriggerKind.GAIN_24H,
                        severity=TriggerSeverity.INFO,
                        symbol=h.yf_symbol,
                        display_name=h.ticker,
                        price=px,
                        detail=f"{h.ticker} up {chg:.1f}% in 24h",
                        context={"ticker": h.ticker, "change_24h_pct": chg, "thesis": h.thesis},
                    )
                )

        # Drawdown 7d
        chg_7d = prices.pct_change_7d(h.yf_symbol, px)
        if chg_7d is not None and chg_7d <= -portfolio.settings.drawdown_alert_pct_7d:
            triggers.append(
                Trigger(
                    kind=TriggerKind.DRAWDOWN_7D,
                    severity=TriggerSeverity.WARN,
                    symbol=h.yf_symbol,
                    display_name=h.ticker,
                    price=px,
                    detail=f"{h.ticker} down {chg_7d:.1f}% in 7d",
                    context={"ticker": h.ticker, "change_7d_pct": chg_7d, "thesis": h.thesis},
                )
            )

    # --- Macro: key-level breaks + intraday moves ---
    for m in portfolio.macro_watch:
        px, _ = price_map.get(m.symbol, (None, "missing"))
        if px is None:
            continue

        above = m.key_levels.get("break_above")
        if above is not None and px >= above:
            triggers.append(
                Trigger(
                    kind=TriggerKind.KEY_LEVEL_BREAK_UP,
                    severity=TriggerSeverity.WARN,
                    symbol=m.symbol,
                    display_name=m.name,
                    price=px,
                    detail=f"{m.name} broke ABOVE {above:.2f} — current {px:.2f}",
                    context={"name": m.name, "level": above, "current": px},
                )
            )

        below = m.key_levels.get("break_below")
        if below is not None and px <= below:
            triggers.append(
                Trigger(
                    kind=TriggerKind.KEY_LEVEL_BREAK_DOWN,
                    severity=TriggerSeverity.WARN,
                    symbol=m.symbol,
                    display_name=m.name,
                    price=px,
                    detail=f"{m.name} broke BELOW {below:.2f} — current {px:.2f}",
                    context={"name": m.name, "level": below, "current": px},
                )
            )

        chg = prices.pct_change_24h(m.symbol, px)
        if chg is not None and abs(chg) >= m.threshold_pct_24h:
            triggers.append(
                Trigger(
                    kind=TriggerKind.MACRO_MOVE,
                    severity=TriggerSeverity.WARN,
                    symbol=m.symbol,
                    display_name=m.name,
                    price=px,
                    detail=f"{m.name} moved {chg:+.1f}% in 24h (threshold {m.threshold_pct_24h}%)",
                    context={"name": m.name, "change_24h_pct": chg, "current": px},
                )
            )

    return triggers
