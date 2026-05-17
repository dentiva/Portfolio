"""TradingView price scraper.

Fetches static HTML from in.tradingview.com/symbols/{EXCHANGE}-{TICKER}/ and
extracts the current price from server-rendered structured data.

How it works (no JS rendering needed):
  - TradingView's symbol pages embed a JSON-LD FAQPage schema with answers like
    "The current price of AMZN is 264.14 USD — it has decreased by −1.15% ...".
  - We pull the FAQPage block, then regex the price+currency out of the answer.
  - Loose regex over the whole page is a fallback for ETF pages where the
    price appears outside the FAQ.

Coverage tested across NASDAQ/NYSE stocks, AMEX/NYSE-Arca ETFs, LSE UCITS,
Oslo Børs, and TSX. FX/commodity/index pages do NOT use the FAQ schema —
let prices.py fall back to yfinance for those.

Rate considerations:
  - ~25-30 requests per poll, every 3 hours = ~240/day. Well under any sane limit.
  - Concurrent fetch with bounded pool (8 workers) keeps the whole poll under 10s.
  - Polite User-Agent. No login. No cookies. No JS. Respects robots-ish behavior.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

log = logging.getLogger(__name__)

# Matches "trades at 81.16 USD" / "current price of AMZN is 264.14 USD" /
# "price of AMZN is 264.14 USD" / "INFR trades at 2,925.0 GBX" — covers stocks,
# ETFs, and a few edge cases. GBX/GBp are British pence (1/100 GBP) and need
# conversion (handled in _extract_price below).
_PRICE_RE = re.compile(
    r"(?:trades?\s+at|current\s+price\s+of\s+\S+\s+is|price\s+of\s+\S+\s+is)\s+"
    r"([\d,]+\.?\d*)\s+"
    r"(USD|INR|EUR|GBP|GBX|GBp|NOK|CAD|JPY|CHF|HKD|SGD|AUD|SEK|DKK)",
    re.IGNORECASE,
)
_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}


def fetch(tv_url: str, timeout: float = 12) -> tuple[float | None, str | None]:
    """Fetch one TradingView symbol page; return (price, currency) or (None, None).

    Caller should treat None as "use fallback provider".
    """
    if not tv_url:
        return None, None
    try:
        with httpx.Client(
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            follow_redirects=True,
            http2=False,
        ) as c:
            r = c.get(tv_url)
        if r.status_code != 200:
            log.warning("TV %s → HTTP %s", tv_url, r.status_code)
            return None, None
        return _extract_price(r.text)
    except httpx.HTTPError as e:
        log.warning("TV fetch failed for %s: %s", tv_url, e)
        return None, None
    except Exception:
        log.exception("TV unexpected error for %s", tv_url)
        return None, None


def fetch_batch(symbol_to_url: dict[str, str], max_workers: int = 8) -> dict[str, tuple[float | None, str | None]]:
    """Concurrent fetch. Returns {symbol: (price, currency)}.

    Workers default to 8 — enough to drain ~25 URLs in <10s without hammering.
    """
    out: dict[str, tuple[float | None, str | None]] = {}
    if not symbol_to_url:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, url): sym for sym, url in symbol_to_url.items()}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception as e:
                log.warning("TV worker exception for %s: %s", sym, e)
                out[sym] = (None, None)
    return out


def _normalize(price: float, currency: str) -> tuple[float, str]:
    """Convert GBX or GBp (British pence, 1/100 of a pound) to GBP."""
    if currency.upper() == "GBX" or currency == "GBp":
        return price / 100, "GBP"
    return price, currency.upper()


def _extract_price(html: str) -> tuple[float | None, str | None]:
    """JSON-LD FAQPage first (more reliable), regex over whole page as fallback."""
    # 1. JSON-LD FAQPage
    for m in _LDJSON_RE.finditer(html):
        try:
            doc = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("@type") != "FAQPage":
            continue
        for entity in doc.get("mainEntity", []):
            ans = entity.get("acceptedAnswer", {}).get("text", "")
            pm = _PRICE_RE.search(ans)
            if pm:
                price = float(pm.group(1).replace(",", ""))
                return _normalize(price, pm.group(2))

    # 2. Whole-page regex fallback
    pm = _PRICE_RE.search(html)
    if pm:
        price = float(pm.group(1).replace(",", ""))
        return _normalize(price, pm.group(2))

    return None, None
