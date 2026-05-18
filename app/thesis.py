"""Thesis-drift sentinel.

Weekly job. Per holding, hands Claude:
  - the static thesis you wrote when you bought it
  - 30-day price action (closes from the daily_closes table)
  - most recent news_analysis row (filled by news.py)

Claude returns a score 1-5 (1=thesis broken, 5=fully intact) plus a single-
sentence reason. Stored per (symbol, run_ts) so we can see drift over time.

When a holding's new score drops to 2 or 1 AND the previous score was higher,
a `thesis_drift` trigger is emitted into the regular alert pipeline (i.e. it
goes through the same dedup → Haiku triage → email path as price triggers).

Limitations to set expectations:
  - First ~30 days after deploy: price-series is sparse. Output will have
    less to chew on. Don't read too much into early scores.
  - We are not validating the THESIS text for factual claims — the model
    treats it as the ground-truth statement and asks "does evidence support
    it being currently operative."
  - One run per week is deliberately low-noise. Daily runs would whipsaw.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from . import storage
from .alerts import Trigger, TriggerKind, TriggerSeverity
from .config import portfolio, settings

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)


SYSTEM_PROMPT = """You are a portfolio risk analyst scoring how well one
holding's original investment thesis still holds up, given recent price
action and recent news.

You receive:
  - The thesis the investor wrote when they bought it (treat as ground truth)
  - Up to 30 days of daily closing prices
  - The latest news summary for this position (may be empty)

Return ONLY valid JSON, no preamble. Schema:
{
  "score": 1-5,
  "reasoning": "one sentence, max 30 words"
}

Score calibration:
  5 = Thesis fully intact. Price action consistent. News supportive or neutral.
  4 = Thesis intact. Minor headwinds visible but not material.
  3 = Mixed signals. Some evidence supportive, some concerning. Worth watching.
  2 = Material drift. Price action OR news materially undermines key thesis
      claims. Investor should reassess.
  1 = Thesis broken. Core claim of thesis is no longer credible. Investor
      should consider exit.

Calibration anti-patterns to avoid:
- Don't drop a score just because price is down. Long-term theses survive
  short-term drawdowns. Drop the score when price action is INCONSISTENT
  WITH the thesis (e.g. "growth play" trading sideways for 90 days).
- Don't drop a score for generic "broader market down" news.
- Be willing to assign 5. Most weeks, most holdings should score 4 or 5."""


def _build_user_msg(holding, closes: list[tuple[str, float]],
                    news: dict[str, Any] | None) -> str:
    """Compose the per-holding prompt body."""
    series_line = "(insufficient price history yet)"
    if closes:
        first_d, first_p = closes[0]
        last_d, last_p = closes[-1]
        pct = (last_p - first_p) / first_p * 100 if first_p else 0.0
        # Sample up to 8 points evenly for context, otherwise the prompt bloats
        n = len(closes)
        idxs = sorted({0, n - 1, *[round(i * (n - 1) / 7) for i in range(8)]}) if n > 1 else [0]
        sample = [closes[i] for i in idxs]
        series_line = (
            f"{n}-day window: {first_d} ${first_p:.2f} → {last_d} ${last_p:.2f} "
            f"({pct:+.1f}%). Sample points: "
            + ", ".join(f"{d} ${p:.2f}" for d, p in sample)
        )

    news_line = "(no news analysis available)"
    if news:
        bits = []
        if news.get("relevant_count"):
            bits.append(f"{news['relevant_count']} thesis-relevant headlines")
        if news.get("contradicts"):
            bits.append("AT LEAST ONE CONTRADICTS the thesis")
        bits.append(f"summary: {news.get('summary', '')}")
        news_line = " · ".join(bits)

    return (
        f"TICKER: {holding.ticker} ({holding.name})\n"
        f"LEG: {holding.leg}\n"
        f"THESIS:\n{holding.thesis or '(no thesis recorded)'}\n\n"
        f"PRICE ACTION: {series_line}\n"
        f"RECENT NEWS: {news_line}"
    )


def _score_one(holding) -> dict[str, Any] | None:
    """Score one holding. Returns {score, reasoning, raw} or None on hard failure."""
    closes = storage.daily_closes(holding.yf_symbol, days=30)
    news = storage.latest_news_analysis(holding.yf_symbol)
    user_msg = _build_user_msg(holding, closes, news)

    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.warning("Haiku call failed for %s: %s", holding.ticker, e)
        return None

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("Could not parse Haiku JSON for %s: %s — raw: %s",
                    holding.ticker, e, raw[:200])
        return None

    score = parsed.get("score")
    if not isinstance(score, int) or score < 1 or score > 5:
        log.warning("Bad score from Haiku for %s: %r", holding.ticker, score)
        return None

    return {
        "score": score,
        "reasoning": str(parsed.get("reasoning", ""))[:300],
        "raw": raw,
    }


# Threshold below which a NEW low triggers an alert
DRIFT_ALERT_THRESHOLD = 2


def _emit_drift_triggers(score_changes: list[dict[str, Any]]) -> list[Trigger]:
    """For each holding whose latest score dropped to ≤2 from a higher prior,
    build a Trigger so the regular alert pipeline can email it.
    """
    triggers: list[Trigger] = []
    for ch in score_changes:
        if ch["new_score"] > DRIFT_ALERT_THRESHOLD:
            continue
        prior = ch.get("prior_score")
        if prior is not None and prior <= DRIFT_ALERT_THRESHOLD:
            # We already alerted at this level — don't repeat.
            continue
        severity = TriggerSeverity.CRITICAL if ch["new_score"] == 1 else TriggerSeverity.WARN
        triggers.append(Trigger(
            kind=TriggerKind.THESIS_DRIFT,
            severity=severity,
            symbol=ch["symbol"],
            display_name=ch["ticker"],
            price=ch.get("current_price", 0.0),
            detail=(
                f"{ch['ticker']} thesis-drift score dropped to {ch['new_score']}/5"
                + (f" (from {prior}/5)" if prior is not None else "")
                + f" — {ch['reasoning']}"
            ),
            context={
                "score": ch["new_score"],
                "prior_score": prior,
                "reasoning": ch["reasoning"],
                "thesis_kind": "drift",
            },
        ))
    return triggers


def run_thesis_drift_cycle() -> dict[str, Any]:
    """Score every holding; persist; return any drift triggers worth alerting."""
    log.info("Thesis-drift cycle starting (%d holdings)", len(portfolio.holdings))
    score_changes: list[dict[str, Any]] = []

    for h in portfolio.holdings:
        prior = storage.latest_thesis_drift(h.yf_symbol)
        result = _score_one(h)
        if result is None:
            continue

        storage.save_thesis_drift(
            symbol=h.yf_symbol,
            score=result["score"],
            reasoning=result["reasoning"],
            raw=result["raw"],
        )
        score_changes.append({
            "symbol": h.yf_symbol,
            "ticker": h.ticker,
            "new_score": result["score"],
            "prior_score": prior["score"] if prior else None,
            "reasoning": result["reasoning"],
        })

    triggers = _emit_drift_triggers(score_changes)
    log.info("Thesis-drift cycle done: %d scored, %d drift triggers emitted",
             len(score_changes), len(triggers))
    return {"scored": len(score_changes), "triggers": triggers}
