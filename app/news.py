"""Position-level news monitor.

For each holding, fetch the most recent ~24h of headlines from Google News
RSS, hand them to Claude Haiku with the stored thesis, and ask: are any of
these thesis-relevant? Do any contradict? One short summary.

Why Google News RSS:
  - Zero credentials, zero rate-limit risk (it's a free public feed).
  - Headlines + source + publish time. We don't need article bodies; for
    portfolio monitoring, the headline carries 95% of the signal.
  - Filter to ~24h by parsing the pubDate; older items are noise.

Per call cost (Haiku):
  - Input: ~50 words thesis + 5-8 headlines × ~15 words = ~150-200 words
  - Output: small JSON (~50 words)
  - 21 holdings × 2 runs/day ≈ 42 calls/day ≈ $0.02/day.

Runs from APScheduler at 09:00 and 16:00 IST (= 03:30 and 10:30 UTC).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import anthropic
import httpx

from . import storage
from .config import portfolio, settings

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# Google News' RSS endpoint. We query with `"<TICKER>" stock` for stocks/ETFs;
# the quotes prevent partial matches (e.g. "MOO" the cow joke).
_RSS_URL = (
    "https://news.google.com/rss/search?"
    "q=%22{q}%22+stock&hl=en-US&gl=US&ceid=US:en"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

SYSTEM_PROMPT = """You are a portfolio analyst reviewing news headlines for
one position in an India-based long-term investor's offshore portfolio.

You receive the original investment thesis and a list of recent headlines.
Your job: classify how thesis-relevant the news is.

Return ONLY valid JSON, no preamble. Schema:
{
  "relevant_count": 0-N,        // how many headlines genuinely move the thesis (not generic market noise)
  "contradicts": true|false,    // does any relevant news directly contradict the thesis?
  "summary": "max-25-word plain-English summary; '(no relevant news)' if relevant_count=0"
}

Calibration:
- Earnings beat/miss = relevant
- Management change, M&A, regulation, fundamental product news = relevant
- "Stock up/down X%", "analyst price target", "best dividend stocks" listicles = NOT relevant
- For ETFs, sector news matching the ETF's mandate = relevant; individual holdings = usually not
- Contradicts = the headline's claim, if true, would invalidate the thesis"""


def _fetch_rss(query: str, lookback_hours: int = 24, timeout: float = 8) -> list[dict[str, Any]]:
    """Fetch one Google News RSS feed, return headlines from the last N hours.
    Each item: {title, url, source, age_hours, published}.
    """
    url = _RSS_URL.format(q=urllib.parse.quote(query))
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(url)
    except httpx.HTTPError as e:
        log.warning("News RSS fetch failed for %s: %s", query, e)
        return []
    if r.status_code != 200:
        log.warning("News RSS HTTP %s for %s", r.status_code, query)
        return []

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        log.warning("News RSS parse failed for %s: %s", query, e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        try:
            pub = parsedate_to_datetime(pub_raw)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if pub < cutoff:
            continue
        # Google News titles often end with " - <Source>"; strip for cleanliness
        title = re.sub(r"\s+-\s+[^-]+$", "", title)
        age_hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        out.append({
            "title": title,
            "url": link,
            "source": source,
            "age_hours": round(age_hours, 1),
            "published": pub.isoformat(),
        })
    # Sort newest first, cap to 8 (Claude context stays tight)
    out.sort(key=lambda h: h["age_hours"])
    return out[:8]


def _classify(thesis: str, headlines: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask Haiku to classify headlines against thesis. Returns the parsed JSON.
    On any failure returns a benign default — never raises into the caller.
    """
    if not headlines:
        return {"relevant_count": 0, "contradicts": False, "summary": "(no recent headlines)",
                "raw_response": ""}

    user_msg = (
        f"THESIS:\n{thesis or '(no thesis recorded)'}\n\n"
        "HEADLINES (newest first):\n"
        + "\n".join(f"- [{h['source']}, {h['age_hours']}h ago] {h['title']}" for h in headlines)
    )
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        log.warning("Haiku call failed: %s", e)
        return {"relevant_count": 0, "contradicts": False,
                "summary": "(analysis unavailable)", "raw_response": ""}

    # Strip ```json fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("Could not parse Haiku JSON: %s — raw: %s", e, raw[:200])
        return {"relevant_count": 0, "contradicts": False,
                "summary": "(parse error)", "raw_response": raw}

    return {
        "relevant_count": int(parsed.get("relevant_count", 0)),
        "contradicts": bool(parsed.get("contradicts", False)),
        "summary": str(parsed.get("summary", ""))[:200],
        "raw_response": raw,
    }


def _process_one(holding) -> tuple[str, dict[str, Any]] | None:
    """One holding → fetch RSS, classify, persist. Returns (symbol, result)."""
    query = holding.ticker  # ticker is the most disambiguating term
    headlines = _fetch_rss(query)
    result = _classify(holding.thesis or "", headlines)
    storage.save_news_analysis(
        symbol=holding.yf_symbol,
        relevant_count=result["relevant_count"],
        contradicts=result["contradicts"],
        summary=result["summary"],
        headlines=headlines,
        raw_response=result.get("raw_response", ""),
    )
    return holding.yf_symbol, result


def run_news_cycle(max_workers: int = 4) -> dict[str, Any]:
    """Process every holding. Concurrent but bounded — Anthropic rate limits
    apply and we'd rather not spike them.
    """
    log.info("News cycle starting (%d holdings)", len(portfolio.holdings))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_process_one, h): h.ticker for h in portfolio.holdings}
        for fut in as_completed(futures):
            try:
                sym_result = fut.result()
                if sym_result:
                    sym, res = sym_result
                    results[sym] = res
            except Exception as e:
                log.exception("News worker exception for %s: %s", futures[fut], e)

    relevant = sum(1 for r in results.values() if r["relevant_count"] > 0)
    contradicts = sum(1 for r in results.values() if r["contradicts"])
    log.info("News cycle done: %d processed, %d with relevant news, %d contradicting",
             len(results), relevant, contradicts)
    return {"processed": len(results), "relevant": relevant, "contradicts": contradicts}
