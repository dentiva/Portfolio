"""Price routing layer.

Provider order per symbol:
  1. TradingView (when holding has tv_url configured) — primary
  2. SQLite cache — last resort

Each returned tuple is (price, source) where source ∈
{tradingview, cached, missing}. The dashboard surfaces the source
so you can see if anything's gone stale.

Symbols without a tv_url, or whose TV page doesn't expose a price
through the FAQ schema (FX/commodity/index pages, per
tradingview.py's docstring), will fall through to "cached" until a
recent value exists, then "missing". Operators should either populate
tv_url for those symbols or accept that they may go dark.
"""

from __future__ import annotations

import logging
from typing import Literal

from . import storage, tradingview
from .config import portfolio

log = logging.getLogger(__name__)

PriceSource = Literal["tradingview", "cached", "missing"]


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
    """Top-level fetch. TradingView primary, cache last resort.

    `retries` is accepted for backwards-compat with the old yfinance-aware
    signature; it's currently unused since TV fetches don't retry at this layer.
    """
    all_syms = _all_symbols()
    tv_urls = _tv_url_map()
    results: dict[str, tuple[float | None, PriceSource]] = {}

    # 1. TradingView batch — price + ratings in the same HTTP call per symbol
    if tv_urls:
        log.info("TV fetch: %d symbols", len(tv_urls))
        tv_out = tradingview.fetch_batch_with_ratings(tv_urls)
        ratings_count = 0
        for sym, ((px, _ccy), ratings) in tv_out.items():
            if px is not None and px > 0:
                results[sym] = (px, "tradingview")
            if ratings:
                storage.save_ratings(sym, ratings)
                ratings_count += 1
        if ratings_count:
            log.info("Ratings extracted for %d symbols", ratings_count)
    tv_hits = len(results)

    # 2. Cache fallback for anything TV didn't return (no tv_url,
    #    TV scrape failed, or TV page doesn't expose a price)
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
            log.error("No price for %s — TV and cache both failed", sym)

    log.info(
        "Price routing: %d TV / %d cached / %d missing",
        tv_hits, cached_hits, missing_hits,
    )

    storage.save_prices({
        s: p for s, (p, src) in results.items()
        if src == "tradingview" and p
    })
    return results


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
