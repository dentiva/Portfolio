"""Price routing layer.

Provider order per symbol:
  1. TradingView (when holding has tv_url configured) — primary
  2. yfinance batch with curl_cffi browser impersonation — fallback
     (curl_cffi spoofs TLS fingerprints so Yahoo treats us like a real Chrome,
      avoiding the cloud-IP blocks that hit plain requests/httpx)
  3. SQLite cache — last resort

Each returned tuple is (price, source) where source ∈
{tradingview, yfinance, cached, missing}. The dashboard surfaces the source
so you can see if anything's gone stale.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import yfinance as yf
from curl_cffi import requests as cc_requests

from . import storage, tradingview
from .config import portfolio

log = logging.getLogger(__name__)

PriceSource = Literal["tradingview", "yfinance", "cached", "missing"]

# Browser-impersonating session shared across all yfinance calls.
# Chrome impersonation handles Yahoo's TLS fingerprinting checks that cause
# "Expecting value: line 1 column 1" errors when called from cloud IPs.
_YF_SESSION = cc_requests.Session(impersonate="chrome")


def _all_symbols() -> list[str]:
    out: set[str] = set()
    for h in portfolio.holdings:
        out.add(h.yf_symbol)
    for m in portfolio.macro_watch:
        out.add(m.symbol)
    return sorted(out)


def _tv_url_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for h in portfolio.holdings:
        tv = getattr(h, "tv_url", None)
        if tv:
            out[h.yf_symbol] = tv
    for m in portfolio.macro_watch:
        tv = getattr(m, "tv_url", None)
        if tv:
            out[m.symbol] = tv
    return out


def fetch_prices(retries: int = 2) -> dict[str, tuple[float | None, PriceSource]]:
    """Top-level fetch. TradingView primary, yfinance fallback, cache last."""
    all_syms = _all_symbols()
    tv_urls = _tv_url_map()
    results: dict[str, tuple[float | None, PriceSource]] = {}

    # 1. TradingView batch
    if tv_urls:
        log.info("TV fetch: %d symbols", len(tv_urls))
        tv_out = tradingview.fetch_batch(tv_urls)
        for sym, (px, _ccy) in tv_out.items():
            if px is not None and px > 0:
                results[sym] = (px, "tradingview")
    tv_hits = len(results)

    # 2. yfinance fallback for anything missing
    missing = [s for s in all_syms if s not in results]
    if missing:
        log.info("yfinance fetch: %d symbols (fallback)", len(missing))
        yf_out = _yf_batch(missing, retries=retries)
        for sym, px in yf_out.items():
            if px is not None and px > 0:
                results[sym] = (px, "yfinance")
    yf_hits = sum(1 for _, (_, src) in results.items() if src == "yfinance")

    # 3. Cache fallback
    cached_hits = 0
    missing_hits = 0
    for sym in all_syms:
        if sym in results:
            continue
        cached, cached_ts = storage.latest_price(sym)
        if cached is not None:
            results[sym] = (cached, "cached")
            cached_hits += 1
            log.warning("Cached price for %s (ts=%s)", sym, cached_ts)
        else:
            results[sym] = (None, "missing")
            missing_hits += 1
            log.error("No price for %s — TV, yfinance, cache all failed", sym)

    log.info(
        "Price routing: %d TV / %d yfinance / %d cached / %d missing",
        tv_hits, yf_hits, cached_hits, missing_hits,
    )

    storage.save_prices({
        s: p for s, (p, src) in results.items()
        if src in ("tradingview", "yfinance") and p
    })
    return results


def _yf_batch(symbols: list[str], retries: int) -> dict[str, float | None]:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            data = yf.Tickers(" ".join(symbols), session=_YF_SESSION)
            out: dict[str, float | None] = {}
            for sym in symbols:
                try:
                    fi = data.tickers[sym].fast_info
                    px = (
                        fi.get("last_price")
                        or fi.get("lastPrice")
                        or fi.get("regular_market_price")
                    )
                    out[sym] = float(px) if px else None
                except Exception as e:
                    log.debug("yfinance per-symbol failure for %s: %s", sym, e)
                    out[sym] = None
            return out
        except Exception as e:
            last_err = e
            log.warning(
                "yfinance batch failed (attempt %d/%d): %s",
                attempt + 1, retries + 1, e,
            )
            time.sleep(2 ** attempt)
    log.error("yfinance batch exhausted retries: %s", last_err)
    return {s: None for s in symbols}


def pct_change_24h(symbol: str, current: float) -> float | None:
    baseline = storage.price_at_age(symbol, hours_ago=22)
    if baseline is None or baseline == 0:
        return None
    return (current - baseline) / baseline * 100


def pct_change_7d(symbol: str, current: float) -> float | None:
    baseline = storage.price_at_age(symbol, hours_ago=24 * 7)
    if baseline is None or baseline == 0:
        return None
    return (current - baseline) / baseline * 100
