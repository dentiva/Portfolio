"""TradingView price scraper.

Fetches static HTML from in.tradingview.com/symbols/{EXCHANGE}-{TICKER}/ and
extracts the current price from server-rendered structured data.

How it works (no JS rendering needed). Three extraction strategies, tried in
order:
  1. JSON-LD FAQPage. Most stock / ETF / index pages embed a FAQPage schema
     whose answers read like "The current price of AMZN is 264.14 USD" or
     "The current rate of USDINR is 95.95 INR".
  2. Whole-page regex. Same phrasings, matched outside the FAQ block — used
     for ETF pages where the relevant copy sits in HTML rather than schema.
  3. Embedded JSON state. Some pages (notably XAUUSD/OANDA spot gold) have
     no FAQ at all; the price lives only in inlined JS as `"price":"4540.645"`.
     Currency is inferred from the URL's trailing symbol pair (last 3 chars
     of e.g. XAUUSD → USD) when present, else left as None.

Coverage tested across NASDAQ/NYSE stocks, AMEX/NYSE-Arca ETFs, LSE UCITS,
Oslo Børs, TSX, NYMEX/COMEX commodity futures (CL1!, GC1!), the SP-SPX
index, ICEUS DXY (DX1!), TVC-VIX, FX_IDC-USDINR, and OANDA-XAUUSD.

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
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Matches several phrasings TradingView uses across asset types:
#   "MOO trades at 81.16 USD"                                       ← stocks/ETFs
#   "The current price of AMZN is 264.14 USD"                       ← stocks
#   "The current price of U.S. Dollar Index Futures is 99.21 USD"   ← multi-word name
#   "The current value of S&P 500 Index is 7,408.49 USD"            ← indices use "value"
#   "The current rate of USDINR is 95.9550 INR"                     ← FX pairs use "rate"
#   "INFR trades at 2,925.0 GBX"                                    ← pence (1/100 GBP)
_PRICE_RE = re.compile(
    r"(?:trades?\s+at|"
    r"current\s+(?:price|value|rate)\s+of\s+.+?\s+is|"
    r"(?:price|value|rate)\s+of\s+.+?\s+is)\s+"
    r"([\d,]+\.?\d*)\s+"
    r"(USD|INR|EUR|GBP|GBX|GBp|NOK|CAD|JPY|CHF|HKD|SGD|AUD|SEK|DKK)",
    re.IGNORECASE | re.DOTALL,
)
# Last-resort: pages without a FAQPage schema (e.g. XAUUSD/OANDA spot gold)
# embed the live price as `"price":"4540.645"` or `"price":4540.645` in the
# inlined JS state. Loose match — first hit wins. Currency is NOT in this
# context; the caller infers it from the symbol pair where possible.
_JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d{1,8}(?:\.\d{1,6})?)"?')
_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
# 6-char FX-style symbol pair (XAUUSD, USDINR, EURUSD…) → last 3 chars are the quote ccy
_SYMBOL_PAIR_RE = re.compile(r"/symbols/([A-Z]{6})/", re.IGNORECASE)

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
        return _extract_price(r.text, tv_url)
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


def _extract_price(html: str, url: str | None = None) -> tuple[float | None, str | None]:
    """Extraction strategies, tried in order. See module docstring."""
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

    # 3. Embedded JSON state fallback (XAUUSD/OANDA spot gold, similar pages
    #    that have no FAQPage at all). The price appears as `"price":"NNN.NN"`
    #    in inlined JS. Currency isn't in this context — infer from the URL's
    #    trailing FX-style pair, else return None and let the caller decide.
    jm = _JSON_PRICE_RE.search(html)
    if jm:
        price = float(jm.group(1))
        ccy: str | None = None
        if url:
            sm = _SYMBOL_PAIR_RE.search(url)
            if sm:
                ccy = sm.group(1)[-3:].upper()  # XAUUSD → USD, USDINR → INR
        return price, ccy

    return None, None


# ─────────────────────────── ratings extraction ───────────────────────────

# Phrasings seen across asset types:
#
# Stocks (NASDAQ-AMZN etc):
#   "Amazon technincal analysis shows the neutral today" (typo in TV's copy)
#   "and its 1 week rating is buy"
# ETFs (LSE-VHYL):
#   "Vanguard technical analysis shows the buy rating"
#   "its 1-week rating is strong buy"
# FX (USDINR):
#   "The technical rating for the pair is buy today"
#   "According to our 1 week rating the USDINR shows the buy signal"
# Futures (NYMEX-CL1!, ICEUS-DX1!):
#   "Today its technical rating is buy"  (no 1w on these)
#
# The pattern: find "technical" (or its typo "technincal") then the
# nearest rating-vocabulary word inside the same sentence. Width-limited
# to ~70 chars to avoid catching unrelated "buy" further along.
_RATING_VOCAB = r"(strong\s+sell|strong\s+buy|sell|buy|neutral)"
_TECH_TODAY_RE = re.compile(
    r"tech(?:nical|nincal)[^.]{0,70}?\b" + _RATING_VOCAB + r"\b",
    re.IGNORECASE | re.DOTALL,
)
# 1-week pattern: find "1 week" / "1-week" then the nearest rating word.
# Both "rating is buy" and "rating ... shows the buy signal" variants pass.
_TECH_1W_RE = re.compile(
    r"1[\s-]?week\s+rating[^.]{0,60}?\b" + _RATING_VOCAB + r"\b",
    re.IGNORECASE | re.DOTALL,
)
# "AMZN price has a max estimate of 370.00 USD and a min estimate of 207.00 USD."
_ANALYST_TARGET_RE = re.compile(
    r"max\s+estimate\s+of\s+([\d,]+\.?\d*)\s+([A-Z]{3,4}).{0,40}?"
    r"min\s+estimate\s+of\s+([\d,]+\.?\d*)",
    re.IGNORECASE | re.DOTALL,
)


def _norm_rating(s: str | None) -> str | None:
    """'Strong Buy' / 'strong buy' / 'STRONG BUY' → 'strong_buy'."""
    if not s:
        return None
    return re.sub(r"\s+", "_", s.strip().lower())


def _extract_ratings(html: str) -> dict[str, Any]:
    """Parse FAQ for technical rating + analyst price targets.
    Empty dict if nothing parseable. Never raises.

    Note on freshness: the FAQ-served rating updates on TV's server cadence —
    expect it to lag the live in-page gauge by hours. For our 3-hour poll
    cycle this is fine. The in-page gauge itself is JS-rendered (uses CSS
    var `--recommendation` with values like -90..+90) and would need a
    headless browser to scrape live.
    """
    out: dict[str, Any] = {}
    faq_text = ""
    for m in _LDJSON_RE.finditer(html):
        try:
            doc = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or doc.get("@type") != "FAQPage":
            continue
        for ent in doc.get("mainEntity", []):
            faq_text += " " + ent.get("acceptedAnswer", {}).get("text", "")
        break
    if not faq_text:
        return out

    tt = _TECH_TODAY_RE.search(faq_text)
    if tt:
        out["technical_today"] = _norm_rating(tt.group(1))
    t1w = _TECH_1W_RE.search(faq_text)
    if t1w:
        out["technical_1w"] = _norm_rating(t1w.group(1))

    at = _ANALYST_TARGET_RE.search(faq_text)
    if at:
        try:
            mx = float(at.group(1).replace(",", ""))
            mn = float(at.group(3).replace(",", ""))
            ccy = at.group(2).upper()
            if mn > mx:
                mn, mx = mx, mn
            out["analyst_target_min"] = mn
            out["analyst_target_max"] = mx
            out["analyst_target_currency"] = ccy
        except (ValueError, IndexError):
            pass
    return out


def fetch_with_ratings(tv_url: str, timeout: float = 12) -> tuple[
    tuple[float | None, str | None], dict[str, Any]
]:
    """Single-shot fetch returning ((price, ccy), ratings_dict).
    Saves one round-trip when callers want both.
    """
    if not tv_url:
        return (None, None), {}
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
            return (None, None), {}
        return _extract_price(r.text, tv_url), _extract_ratings(r.text)
    except httpx.HTTPError as e:
        log.warning("TV fetch failed for %s: %s", tv_url, e)
        return (None, None), {}
    except Exception:
        log.exception("TV unexpected error for %s", tv_url)
        return (None, None), {}


def fetch_batch_with_ratings(
    symbol_to_url: dict[str, str], max_workers: int = 8,
) -> dict[str, tuple[tuple[float | None, str | None], dict[str, Any]]]:
    """Concurrent variant. Returns {symbol: ((price, ccy), ratings_dict)}."""
    out: dict[str, tuple[tuple[float | None, str | None], dict[str, Any]]] = {}
    if not symbol_to_url:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_with_ratings, url): sym for sym, url in symbol_to_url.items()}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception as e:
                log.warning("TV+rating worker exception for %s: %s", sym, e)
                out[sym] = ((None, None), {})
    return out