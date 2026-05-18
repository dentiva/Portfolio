"""FX rate fetching for non-USD prices.

When TradingView returns a price for a non-US-listed instrument, it's in
the trade currency (GBP for LSE GBP-denominated ETFs, EUR for some LSE
listings, NOK for Oslo, CAD for TSX, etc.). The user's avg_cost_usd is
stored in USD (from IBKR statements, which are USD-base). Comparing
those two directly gives nonsense P&L.

This module fetches the FX rates we need every poll and exposes a
conversion function. Rates are stored in the `prices` table with
synthetic symbols (e.g. `_FX_GBPUSD`) so they share the same caching
+ pruning + latest-lookup machinery.

Currencies covered = whatever currencies show up across the user's
holdings. Update FX_FETCH_PAIRS if you add a holding on a new exchange.
"""

from __future__ import annotations

import logging

from . import storage, tradingview

log = logging.getLogger(__name__)


# Maps currency code → (TV url, "direct" or "inverse")
#   direct  = TV page returns X-per-USD where X is the QUOTE currency we want
#             (e.g. GBPUSD page returns 1.33 USD per GBP → multiply by 1.33 to convert GBP→USD)
#   inverse = TV page returns USD-per-X where X is the BASE currency we want
#             (e.g. USDNOK page returns 9.31 NOK per USD → divide by 9.31 to convert NOK→USD)
#
# We pick whichever side of the pair TV serves more cleanly. To add a new
# currency: try the FX_IDC-CCYUSD URL first; if that 404s, use USDCCY.
FX_FETCH_PAIRS: dict[str, tuple[str, str]] = {
    "GBP": ("https://in.tradingview.com/symbols/GBPUSD/?exchange=FX_IDC", "direct"),
    "EUR": ("https://in.tradingview.com/symbols/EURUSD/?exchange=FX_IDC", "direct"),
    "NOK": ("https://in.tradingview.com/symbols/USDNOK/?exchange=FX_IDC", "inverse"),
    "CAD": ("https://in.tradingview.com/symbols/USDCAD/?exchange=FX_IDC", "inverse"),
    # USDINR is fetched separately as a visible macro; we read its cached value
    # below rather than re-fetching it here.
    "INR": (None, "cache_only"),
}

# Synthetic-symbol prefix used when persisting FX rates to the prices table.
_PREFIX = "_FX_"


def _store_symbol(ccy: str) -> str:
    return f"{_PREFIX}{ccy}USD"


def fetch_fx_rates() -> dict[str, float]:
    """Fetch every FX rate we need. Returns {ccy: usd_per_unit_of_ccy}.

    Always includes USD: 1.0 for identity convenience.
    On per-rate fetch failure, falls back to cached value. Logs WARNING
    when forced to use stale cache.
    """
    rates: dict[str, float] = {"USD": 1.0}

    # Concurrent fetch of all live FX pages
    live_urls = {ccy: url for ccy, (url, _kind) in FX_FETCH_PAIRS.items() if url}
    tv_results = tradingview.fetch_batch(live_urls) if live_urls else {}

    for ccy, (_url, kind) in FX_FETCH_PAIRS.items():
        usd_per_unit: float | None = None

        if kind == "direct":
            px, _ccy = tv_results.get(ccy, (None, None))
            if px and px > 0:
                usd_per_unit = px                   # already USD per unit-of-ccy
        elif kind == "inverse":
            px, _ccy = tv_results.get(ccy, (None, None))
            if px and px > 0:
                usd_per_unit = 1.0 / px             # invert to get USD per unit-of-ccy
        elif kind == "cache_only":
            # INR: read the existing USDINR=X cache populated by the regular poll.
            # On the very first poll of a fresh deploy this won't exist yet —
            # the macro hasn't been fetched. That's fine; no holdings are
            # priced in INR today, so the missing rate doesn't break anything.
            cached, _ts = storage.latest_price("USDINR=X")
            if cached and cached > 0:
                usd_per_unit = 1.0 / cached         # USDINR is INR-per-USD → invert

        if usd_per_unit is not None:
            rates[ccy] = usd_per_unit
            # Persist into prices table so a future cache-fallback works
            storage.save_prices({_store_symbol(ccy): usd_per_unit})
        else:
            # Fall back to cached rate from previous poll
            cached, ts = storage.latest_price(_store_symbol(ccy))
            if cached and cached > 0:
                rates[ccy] = cached
                log.warning("FX %s: live fetch failed, using cached %s (ts=%s)",
                            ccy, cached, ts)
            elif kind != "cache_only":
                # cache_only currencies are expected to be unavailable on
                # first poll; only complain for currencies we actively try to fetch
                log.error("FX %s: no live rate and no cached fallback", ccy)

    log.info("FX rates: %s",
             ", ".join(f"{k}={v:.4f}" for k, v in rates.items() if k != "USD"))
    return rates


def convert_to_usd(price: float, ccy: str, rates: dict[str, float]) -> float | None:
    """Convert a foreign-currency price to USD using fetched rates.
    Returns None when the currency is unknown.
    """
    if not ccy or ccy.upper() == "USD":
        return price
    rate = rates.get(ccy.upper())
    if rate is None:
        log.error("FX: no rate for %s — cannot convert %s", ccy, price)
        return None
    return price * rate
