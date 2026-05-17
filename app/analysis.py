"""Claude analysis + email dispatch.

Flow:
  1. Triggers come in (rule-based, possibly many).
  2. We dedup any already alerted today.
  3. Remaining triggers + macro snapshot get sent to Claude in one structured prompt.
  4. Claude returns a JSON verdict with the actionable subset + reasoning.
  5. We email only the actionable items, with the dashboard link.

Why send all triggers in one call: Claude can see the *whole picture*
(e.g. "AMZN drawdown coincided with S&P -3%" → not stock-specific, lower
priority than an isolated drawdown). One call is also cheaper than per-trigger.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import resend

from . import storage
from .alerts import Trigger
from .config import portfolio, settings

log = logging.getLogger(__name__)

resend.api_key = settings.resend_api_key
_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)


SYSTEM_PROMPT = """You are a portfolio risk analyst reviewing alert triggers
from an India-based long-term investor's offshore (IBKR) portfolio.

You receive a structured list of triggers + macro context. Your job is to
return a JSON verdict identifying which triggers are ACTIONABLE vs noise.

Calibration:
- This investor is sophisticated, 10+ year horizon, drawdown tolerance ~30%.
- Stop-loss breaches on conviction holdings = actionable (CRITICAL).
- Entry-target hits on PLANNED buys = actionable (OPPORTUNITY).
- Single-name -5% on a broad-market -3% day = likely NOT actionable (just beta).
- Macro moves (USD/INR break, oil spike) that change portfolio thesis = actionable.
- Random intraday volatility on small positions = NOT actionable.

For each trigger, decide actionable: true|false with one-sentence reasoning.
Then write a brief markdown summary (max 200 words) of the actionable items
the user should consider. Be specific and direct. No hedging language.

Return ONLY valid JSON, no preamble. Schema:
{
  "actionable_triggers": [
    {"symbol": "AMZN", "kind": "stop_loss", "reasoning": "..."}
  ],
  "ignored_triggers": [
    {"symbol": "MOO", "kind": "drawdown_24h", "reasoning": "..."}
  ],
  "summary_markdown": "## Actionable items\\n- ...",
  "overall_severity": "info|warn|critical"
}
"""


def analyze_and_alert(triggers: list[Trigger], price_map: dict) -> dict[str, Any]:
    """Run Claude over the triggers and send email if anything is actionable.

    Returns a dict for logging/dashboard display.
    """
    # 1. Dedup
    fresh = [t for t in triggers if not storage.already_sent_today(t.alert_key())]
    if not fresh:
        log.info("No fresh triggers (all dedup'd or none detected)")
        return {"triggers_in": len(triggers), "actionable": 0, "summary": "(no fresh triggers)"}

    # 2. Rate limit guard
    if storage.alerts_sent_today_count() >= portfolio.settings.max_alerts_per_day:
        log.warning("Daily alert cap reached — skipping Claude analysis")
        return {"triggers_in": len(triggers), "actionable": 0, "summary": "(daily alert cap reached)"}

    # 3. Compose context for Claude
    user_payload = {
        "triggers": [t.to_dict() for t in fresh],
        "macro_snapshot": _macro_snapshot(price_map),
        "portfolio_legs": _portfolio_summary(price_map),
    }

    log.info("Calling Claude with %d fresh triggers", len(fresh))
    verdict = _call_claude(user_payload)

    # 4. Email actionable items
    actionable = verdict.get("actionable_triggers", [])
    actionable_keys = {(a["symbol"], a["kind"]) for a in actionable}

    if actionable:
        _send_email(verdict, fresh, actionable_keys)
        # Record alerts so we don't re-send
        for t in fresh:
            if (t.symbol, t.kind.value) in actionable_keys:
                storage.mark_alert_sent(t.alert_key(), t.to_dict())

    storage.log_analysis(
        triggers_in=len(fresh),
        actionable=len(actionable),
        summary=verdict.get("summary_markdown", ""),
        raw=json.dumps(verdict),
    )

    return {
        "triggers_in": len(fresh),
        "actionable": len(actionable),
        "summary": verdict.get("summary_markdown", ""),
        "overall_severity": verdict.get("overall_severity", "info"),
    }


def _macro_snapshot(price_map: dict) -> dict[str, float | None]:
    return {
        m.name: (price_map.get(m.symbol, (None, None))[0])
        for m in portfolio.macro_watch
    }


def _portfolio_summary(price_map: dict) -> dict[str, Any]:
    total_value = 0.0
    leg_values: dict[str, float] = {}
    for h in portfolio.holdings:
        px, _ = price_map.get(h.yf_symbol, (None, None))
        if px is None:
            continue
        v = px * h.quantity
        total_value += v
        leg_values[h.leg] = leg_values.get(h.leg, 0) + v
    return {
        "total_value_usd": round(total_value, 2),
        "by_leg": {k: round(v, 2) for k, v in leg_values.items()},
        "holdings_count": len(portfolio.holdings),
    }


def _call_claude(payload: dict) -> dict[str, Any]:
    """One API call; parse and return Claude's verdict."""
    try:
        msg = _client.messages.create(
            model=portfolio.settings.claude_model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(payload, indent=2, default=str),
                }
            ],
        )
        text = msg.content[0].text.strip()
        # Strip code fences if Claude added them
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception as e:
        log.exception("Claude call failed; falling back to dumb passthrough")
        return {
            "actionable_triggers": [
                {"symbol": t["symbol"], "kind": t["kind"], "reasoning": t["detail"]}
                for t in payload["triggers"]
                if t["severity"] == "critical"
            ],
            "ignored_triggers": [],
            "summary_markdown": "**(Claude analysis failed — showing CRITICAL triggers only)**",
            "overall_severity": "warn",
            "_error": str(e),
        }


def _send_email(verdict: dict, triggers: list[Trigger], actionable_keys: set) -> None:
    severity = verdict.get("overall_severity", "info")
    emoji = {"info": "📊", "warn": "⚠️", "critical": "🚨"}.get(severity, "📊")
    subject = f"{emoji} Portfolio: {len(actionable_keys)} actionable trigger(s)"

    actionable_triggers = [
        t for t in triggers if (t.symbol, t.kind.value) in actionable_keys
    ]

    rows = "".join(
        f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee">
            <strong>{t.display_name}</strong><br>
            <span style="color:#666;font-size:12px">{t.kind.value} · {t.severity.value}</span>
          </td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee">${t.price:.2f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee">{t.detail}</td>
        </tr>"""
        for t in actionable_triggers
    )

    summary_html = verdict.get("summary_markdown", "").replace("\n", "<br>")

    body = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:680px;margin:0 auto">
      <h2 style="color:#1F4E78">{emoji} Portfolio Alert</h2>
      <p>{len(actionable_keys)} of {len(triggers)} triggers flagged as actionable.</p>

      <h3>Claude's analysis</h3>
      <div style="background:#f6f8fa;padding:16px;border-left:3px solid #1F4E78;border-radius:4px">
        {summary_html}
      </div>

      <h3 style="margin-top:24px">Actionable triggers</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead><tr style="background:#1F4E78;color:white">
          <th style="padding:8px;text-align:left">Symbol</th>
          <th style="padding:8px;text-align:left">Price</th>
          <th style="padding:8px;text-align:left">Detail</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>

      <p style="margin-top:24px"><a href="{settings.public_base_url}"
        style="background:#1F4E78;color:white;padding:10px 16px;text-decoration:none;border-radius:4px">
        Open dashboard →</a></p>

      <p style="color:#999;font-size:12px;margin-top:32px">
        Sent by portfolio-monitor. Adjust thresholds in portfolio.json.
      </p>
    </div>
    """

    try:
        resend.Emails.send({
            "from": settings.email_from,
            "to": [settings.email_to],
            "subject": subject,
            "html": body,
        })
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.exception("Failed to send email: %s", e)
