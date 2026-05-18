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

    # 4. Identify actionable items from the first pass
    actionable_first_pass = verdict.get("actionable_triggers", [])
    actionable_keys_first = {(a["symbol"], a["kind"]) for a in actionable_first_pass}

    # 4a. Conviction double-check — independent second pass.
    #     Takes the first-pass actionables + all available cross-signals
    #     (ratings, thesis drift, news, macro regime) and scores each one
    #     0-100. Only items scoring above the threshold proceed to email
    #     AND get marked as "high conviction" on the dashboard.
    convictions: dict[tuple[str, str], dict[str, Any]] = {}
    if actionable_first_pass:
        convictions = _conviction_double_check(
            actionable_first_pass, fresh, price_map,
        )

    threshold = portfolio.settings.conviction_threshold
    high_conviction = [
        a for a in actionable_first_pass
        if convictions.get((a["symbol"], a["kind"]), {}).get("score", 0) >= threshold
    ]
    actionable_keys = {(a["symbol"], a["kind"]) for a in high_conviction}

    log.info(
        "Conviction filter: %d/%d actionable passed (threshold %d)",
        len(high_conviction), len(actionable_first_pass), threshold,
    )

    # 5. Email only high-conviction items
    if high_conviction:
        _send_email(verdict, fresh, actionable_keys)
        # Record alerts so we don't re-send
        for t in fresh:
            if (t.symbol, t.kind.value) in actionable_keys:
                storage.mark_alert_sent(t.alert_key(), t.to_dict())

    # 6. Persist convictions per (symbol, kind) so the dashboard can show
    #    BOTH "high" and "suppressed by low conviction" categories.
    storage.save_conviction_run(
        actionable_first_pass=actionable_first_pass,
        convictions=convictions,
        threshold=threshold,
    )

    storage.log_analysis(
        triggers_in=len(fresh),
        actionable=len(high_conviction),
        summary=verdict.get("summary_markdown", ""),
        raw=json.dumps(verdict),
    )

    return {
        "triggers_in": len(fresh),
        "actionable_first_pass": len(actionable_first_pass),
        "actionable": len(high_conviction),
        "suppressed_low_conviction": len(actionable_first_pass) - len(high_conviction),
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


# ─────────────────────────── macro regime synthesis ───────────────────────

REGIME_SYSTEM_PROMPT = """You are a portfolio-aware macro strategist. You get
6 macro readings + the investor's portfolio leg exposures. In 2-3 sentences,
state the current macro regime AND tie it to portfolio-level implications.

Style:
- Direct. No hedging. No "could/might/may". No "monitor closely".
- Cite specific numbers. Use the leg names verbatim.
- Last sentence MUST be portfolio-tied (e.g. "favors X leg over Y", "raises
  hurdle for Z thesis"), not just macro commentary.

Return PLAIN TEXT only, no markdown headers, no JSON."""


def synthesize_macro_regime(
    macro_snapshot: dict[str, Any],
    leg_exposure: dict[str, float],
) -> dict[str, Any] | None:
    """Run Haiku over the macro snapshot. Persist to storage. Returns
    {summary, run_ts} or None on hard failure.

    macro_snapshot: {symbol: {name, price, change_24h_pct?, key_levels?}}
    leg_exposure:   {leg_name: usd_value}
    """
    if not macro_snapshot:
        log.info("Macro regime: no macros available, skipping")
        return None

    macros_text = "\n".join(
        f"- {m.get('name', sym)}: {m.get('price', 'n/a')}"
        + (f" (24h {m['change_24h_pct']:+.1f}%)" if m.get("change_24h_pct") is not None else "")
        for sym, m in macro_snapshot.items()
    )
    legs_text = "\n".join(
        f"- {leg}: ${v:,.0f}" for leg, v in sorted(leg_exposure.items(), key=lambda kv: -kv[1])
    ) or "(no leg exposure data)"

    user_msg = f"MACROS:\n{macros_text}\n\nLEG EXPOSURES:\n{legs_text}"

    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=REGIME_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.warning("Macro regime Haiku call failed: %s", e)
        return None

    storage.save_macro_regime(
        summary=raw,
        snapshot=macro_snapshot,
        raw=raw,
    )
    log.info("Macro regime synthesized (%d chars)", len(raw))
    return {"summary": raw}


# ─────────────────────────── conviction double-check ───────────────────────

CONVICTION_SYSTEM_PROMPT = """You are a noise-filter for a long-term investor's
alert system. The first pass already decided an item is "actionable". Your job
is independent: gauge cross-signal CONVICTION on a 0-100 scale.

You receive one actionable trigger plus every available independent signal
for that symbol:
  - The trigger itself (price-based event: stop-loss, entry-hit, etc.)
  - TradingView technical rating (today + 1-week timeframe)
  - TradingView analyst price-target range (where available)
  - Latest thesis-drift score (1-5 weekly)
  - Latest position-level news classification (relevance + contradiction flag)
  - Current macro regime synthesis (broad context)

For each, decide whether it CONFIRMS or CONTRADICTS the trigger. A stop-loss
breach with "Strong sell" technicals, thesis-drift score of 2, and contradicting
news is very high conviction (≥85). A stop-loss breach with "Strong buy"
technicals, thesis-drift 5, and supportive news suggests beta noise — the
trigger is real but conviction is low (≤40).

Return ONLY JSON, no preamble. Schema:
{
  "score": 0-100,
  "signals": {
    "trigger_quality": "strong|moderate|weak",
    "technical_alignment": "confirms|contradicts|neutral|unavailable",
    "analyst_alignment": "confirms|contradicts|neutral|unavailable",
    "thesis_drift_alignment": "confirms|contradicts|neutral|unavailable",
    "news_alignment": "confirms|contradicts|neutral|unavailable",
    "macro_alignment": "confirms|contradicts|neutral|unavailable"
  },
  "reasoning": "one sentence, max 30 words, citing the strongest signal"
}

Scoring calibration:
  90-100: Multiple independent signals all confirm. No contradictions.
  70-89:  Trigger + ≥2 confirming signals. Minor or no contradictions.
  50-69:  Mixed. Trigger valid but cross-signals split. Worth watching, not alerting.
  30-49:  Trigger valid but mostly contradicted by other signals. Likely noise.
  0-29:   Trigger is technically valid but every other signal says ignore."""


def _conviction_double_check(
    actionable_items: list[dict[str, Any]],
    triggers: list[Trigger],
    price_map: dict,
) -> dict[tuple[str, str], dict[str, Any]]:
    """One Haiku call per actionable item, scored against all cross-signals.

    Returns {(symbol, kind): {score, signals, reasoning}}.
    Concurrent execution would help latency but actionable count is usually
    1-3, so sequential is fine and reads more deterministically in logs.
    """
    # Bulk-fetch all signals once (cheap SQL).
    ratings_map = storage.latest_ratings_for_all()
    drift_map = storage.latest_thesis_drift_for_all()
    news_map = storage.latest_news_for_all()
    macro = storage.latest_macro_regime()
    trigger_by_key = {(t.symbol, t.kind.value): t for t in triggers}

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in actionable_items:
        key = (item["symbol"], item["kind"])
        trigger = trigger_by_key.get(key)
        if trigger is None:
            log.warning("Conviction: no trigger for %s — defaulting to score 50", key)
            out[key] = {"score": 50, "reasoning": "(could not match trigger)", "signals": {}}
            continue

        live_px = price_map.get(item["symbol"], (None, None))[0]
        ratings = ratings_map.get(item["symbol"])
        drift = drift_map.get(item["symbol"])
        news = news_map.get(item["symbol"])

        # Compose a focused, tabular prompt body. Each section is omitted if
        # the underlying signal is missing — Claude is told to treat absence
        # as "unavailable" not as contradiction.
        sections = [
            f"TRIGGER: {trigger.kind.value} on {trigger.display_name} — {trigger.detail}",
            f"FIRST-PASS REASONING: {item.get('reasoning', '')}",
            f"CURRENT PRICE: {live_px}",
        ]
        if ratings:
            tech_parts = []
            if ratings.get("technical_today"):
                tech_parts.append(f"today={ratings['technical_today']}")
            if ratings.get("technical_1w"):
                tech_parts.append(f"1w={ratings['technical_1w']}")
            if tech_parts:
                sections.append(f"TV TECHNICAL RATING: {', '.join(tech_parts)}")
            if ratings.get("analyst_target_min") is not None:
                mn = ratings["analyst_target_min"]
                mx = ratings["analyst_target_max"]
                ccy = ratings.get("analyst_target_currency", "")
                mid = (mn + mx) / 2
                vs_mid = (live_px - mid) / mid * 100 if live_px else None
                sections.append(
                    f"ANALYST PRICE TARGET: {mn:.2f}–{mx:.2f} {ccy} (mid {mid:.2f})"
                    + (f", current is {vs_mid:+.1f}% from mid" if vs_mid is not None else "")
                )
        else:
            sections.append("TV TECHNICAL RATING: unavailable")
        if drift:
            sections.append(f"THESIS-DRIFT SCORE: {drift['score']}/5 — {drift['reasoning']}")
        else:
            sections.append("THESIS-DRIFT SCORE: unavailable")
        if news:
            tag = "CONTRADICTS thesis" if news.get("contradicts") else "no contradiction"
            sections.append(
                f"RECENT NEWS: {news['relevant_count']} relevant headline(s), {tag} — "
                f"{news.get('summary', '')}"
            )
        else:
            sections.append("RECENT NEWS: unavailable")
        if macro:
            sections.append(f"MACRO REGIME: {macro['summary']}")
        else:
            sections.append("MACRO REGIME: unavailable")

        user_msg = "\n".join(sections)

        try:
            resp = _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=CONVICTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
        except Exception as e:
            log.warning("Conviction call failed for %s: %s — defaulting to 50", key, e)
            out[key] = {"score": 50, "reasoning": "(check failed, default)", "signals": {}}
            continue

        # Strip fences
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.warning("Conviction JSON parse failed for %s: %s — raw: %s",
                        key, e, raw[:200])
            out[key] = {"score": 50, "reasoning": "(parse error, default)", "signals": {}}
            continue

        score = parsed.get("score")
        if not isinstance(score, (int, float)) or score < 0 or score > 100:
            log.warning("Bad conviction score for %s: %r", key, score)
            out[key] = {"score": 50, "reasoning": "(bad score, default)", "signals": {}}
            continue

        out[key] = {
            "score": int(round(score)),
            "signals": parsed.get("signals", {}),
            "reasoning": str(parsed.get("reasoning", ""))[:300],
        }
        log.info("Conviction %d for %s (%s): %s",
                 out[key]["score"], key, item.get("kind", ""), out[key]["reasoning"])
    return out
