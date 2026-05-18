"""FastAPI entrypoint.

Single always-on container hosts both:
  - The scheduler (APScheduler) that runs the poll → analyze → alert pipeline.
  - The dashboard at GET /  (HTTP Basic Auth).
  - Manual trigger at POST /poll  (for testing).
  - Health probe at GET /healthz  (Railway uses this).
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fastapi import File, UploadFile

from . import analysis, dashboard, ingest, news, prices, storage, thesis
from .alerts import detect_triggers
from .config import portfolio, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("monitor")

scheduler = AsyncIOScheduler()
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    """Constant-time check of basic auth against env-var credentials."""
    ok_user = secrets.compare_digest(creds.username, settings.dashboard_user)
    ok_pw = secrets.compare_digest(creds.password, settings.dashboard_password)
    if not (ok_user and ok_pw):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def run_poll_cycle() -> dict:
    """One full cycle: ingest new trades → fetch prices → detect triggers → analyze → email.

    Wrapped so it can be invoked by the scheduler or by POST /poll for tests.
    """
    log.info("=== Poll cycle starting ===")

    # 0. Pick up any newly-dropped trade files first
    ingest_result = ingest.scan_and_process()
    if ingest_result["files"]:
        log.info(
            "Ingest: %d files, %d new transactions",
            ingest_result["files"], ingest_result["txns_new"],
        )
        if ingest_result["errors"]:
            log.warning("Ingest errors: %s", ingest_result["errors"])
        # Reload portfolio config since positions may have changed
        from .config import load_portfolio
        portfolio.holdings = load_portfolio().holdings

    # 1. Fetch all prices
    price_map = prices.fetch_prices()
    tv = sum(1 for _, src in price_map.values() if src == "tradingview")
    cached = sum(1 for _, src in price_map.values() if src == "cached")
    missing = sum(1 for _, src in price_map.values() if src == "missing")
    log.info("Prices: %d TV / %d cached / %d missing", tv, cached, missing)

    # 1a. Record one daily close per (symbol, UTC date) for thesis-drift series.
    #     Only seeds the series with LIVE prices — cached/missing are skipped.
    closes_recorded = 0
    for sym, (px, src) in price_map.items():
        if src == "tradingview" and px:
            if storage.record_daily_close_if_first(sym, px):
                closes_recorded += 1
    if closes_recorded:
        log.info("Daily-close series seeded for %d symbols today", closes_recorded)

    # 2. Detect triggers
    triggers = detect_triggers(price_map)
    log.info("Detected %d triggers", len(triggers))
    for t in triggers:
        log.info("  · [%s] %s", t.severity.value, t.detail)

    # 3. Analyze + alert
    if triggers:
        result = analysis.analyze_and_alert(triggers, price_map)
    else:
        result = {"triggers_in": 0, "actionable": 0, "summary": "no triggers"}

    # 3a. Macro regime synthesis — one Haiku call tying macros to leg exposures.
    #     Cheap (~$0.0003/call) and always-on; the dashboard pulls latest_macro_regime().
    try:
        macro_snapshot = {
            m.symbol: {
                "name": m.name,
                "price": price_map.get(m.symbol, (None, None))[0],
                "key_levels": m.key_levels,
            }
            for m in portfolio.macro_watch
        }
        leg_exposure: dict[str, float] = {}
        for h in portfolio.holdings:
            px = price_map.get(h.yf_symbol, (None, None))[0]
            if px:
                leg_exposure[h.leg] = leg_exposure.get(h.leg, 0) + px * h.quantity
        analysis.synthesize_macro_regime(macro_snapshot, leg_exposure)
    except Exception:
        log.exception("Macro regime synthesis failed (continuing)")

    # 4. Housekeeping: prune old prices weekly-ish
    storage.prune_old_prices(keep_days=60)

    log.info("=== Poll cycle done: %s ===", result)
    return {
        "ingest": {
            "files": ingest_result["files"],
            "txns_new": ingest_result["txns_new"],
            "errors": ingest_result["errors"],
        },
        "prices": {"tv": tv, "cached": cached, "missing": missing},
        "triggers": len(triggers),
        **result,
    }


def run_news_cycle() -> dict:
    """Standalone wrapper for the news monitor.
    Twice-daily scheduled job + POST /run-news for manual triggering.
    """
    log.info("=== News cycle starting ===")
    result = news.run_news_cycle()
    log.info("=== News cycle done: %s ===", result)
    return result


def run_thesis_drift_cycle() -> dict:
    """Standalone wrapper for the thesis-drift sentinel.

    Weekly job (Sunday) + POST /run-thesis-drift for manual triggering.
    When the scorer emits drift triggers, push them through the regular
    analyze_and_alert pipeline so they show up in email like price triggers.
    """
    log.info("=== Thesis-drift cycle starting ===")
    out = thesis.run_thesis_drift_cycle()
    triggers = out.get("triggers", [])
    summary = {"scored": out.get("scored", 0), "triggers": len(triggers)}

    if triggers:
        log.info("Pushing %d thesis-drift trigger(s) to analyze_and_alert", len(triggers))
        try:
            # No price_map needed for context here — drift triggers are
            # narrative, not numeric — pass an empty dict.
            analysis.analyze_and_alert(triggers, {})
        except Exception:
            log.exception("Drift trigger analysis failed (continuing)")
    log.info("=== Thesis-drift cycle done: %s ===", summary)
    return summary


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Init DB, init ingest dirs, start scheduler, optional startup poll."""
    storage.init()
    ingest.init()
    log.info("DB initialized at %s; trades dir at %s", storage.DB_PATH, ingest.TRADES_DIR)

    interval_hours = portfolio.settings.poll_interval_hours
    scheduler.add_job(
        run_poll_cycle,
        trigger=IntervalTrigger(hours=interval_hours),
        id="poll",
        max_instances=1,
        coalesce=True,
    )

    # News monitor: 09:00 and 16:00 IST (= 03:30 and 10:30 UTC).
    # IST = UTC+5:30; these are pre-market and post-market closures for
    # markets the user cares about (US opens 19:00 IST; LSE closes 21:00 IST).
    scheduler.add_job(
        run_news_cycle,
        trigger=CronTrigger(hour="3,10", minute=30),
        id="news",
        max_instances=1,
        coalesce=True,
    )

    # Thesis-drift sentinel: Sundays at 11:00 IST (= Sun 05:30 UTC).
    # Sunday morning gives the weekend's news a chance to settle, and the
    # output is fresh for Monday's planning.
    scheduler.add_job(
        run_thesis_drift_cycle,
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=30),
        id="thesis_drift",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info("Scheduler started: poll every %dh; news 09:00/16:00 IST; "
             "thesis-drift Sun 11:00 IST", interval_hours)

    if settings.poll_on_startup:
        log.info("Running startup poll...")
        try:
            run_poll_cycle()
        except Exception:
            log.exception("Startup poll failed (continuing)")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="Portfolio Monitor", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def root(
    _: str = Depends(auth),
    fx: float | None = None,
    fy: int | None = None,
    cy: int | None = None,
    tab: str = "holdings",
) -> str:
    """Dashboard. Query params control the tax tab:
      ?fx=85.20     flat USD→INR rate
      ?fy=2024      start year of Indian FY (Apr 2024 → Mar 2025 = 2024)
      ?cy=2024      calendar year for Schedule FA (defaults to fy)
      ?tab=tax      open the tax tab on load (default: holdings)
    """
    return dashboard.render_dashboard(
        fx_rate=fx, fy_start_year=fy, cy=cy, active_tab=tab,
    )


@app.post("/poll")
def manual_poll(_: str = Depends(auth)) -> JSONResponse:
    """Run a poll cycle on demand (for testing). Returns the cycle result."""
    return JSONResponse(run_poll_cycle())


@app.post("/run-news")
def manual_news(_: str = Depends(auth)) -> JSONResponse:
    """Run the news monitor on demand. Normally cron-scheduled twice daily."""
    return JSONResponse(run_news_cycle())


@app.post("/run-thesis-drift")
def manual_thesis_drift(_: str = Depends(auth)) -> JSONResponse:
    """Run the thesis-drift sentinel on demand. Normally weekly on Sunday.

    Returns counts only — full per-symbol output is on the dashboard.
    Triggers (if any) flow through analyze_and_alert → email.
    """
    out = run_thesis_drift_cycle()
    return JSONResponse(out)


@app.get("/state")
def state(_: str = Depends(auth)) -> JSONResponse:
    """JSON snapshot of current holdings + macro for sanity checking."""
    out = {"holdings": [], "macro": []}
    for h in portfolio.holdings:
        px, ts = storage.latest_price(h.yf_symbol)
        out["holdings"].append({
            "ticker": h.ticker,
            "price": px,
            "ts": ts,
            "quantity": h.quantity,
            "value": (px or 0) * h.quantity,
        })
    for m in portfolio.macro_watch:
        px, ts = storage.latest_price(m.symbol)
        out["macro"].append({"name": m.name, "symbol": m.symbol, "price": px, "ts": ts})
    return JSONResponse(out)


@app.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    _: str = Depends(auth),
) -> JSONResponse:
    """Upload an IBKR statement (PDF or CSV) and process it immediately.

    Returns the ingest summary. Filename is sanitized; file is stored under
    trades/ and moved to trades/processed/ after parsing.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > 20 * 1024 * 1024:  # 20MB cap
        raise HTTPException(413, "File too large (max 20MB)")
    result = ingest.ingest_uploaded_file(content, file.filename or "uploaded")
    return JSONResponse(result)


@app.post("/ingest")
def run_ingest(_: str = Depends(auth)) -> JSONResponse:
    """Manually scan the trades/ directory and process any new files."""
    return JSONResponse(ingest.scan_and_process())


@app.post("/recompute")
def manual_recompute(_: str = Depends(auth)) -> JSONResponse:
    """Re-derive quantity + avg_cost_usd for every holding from the full
    transaction ledger using FIFO, then patch portfolio.json in place.

    Use this after a code change that fixes cost-basis math, or any time
    you suspect the JSON drifted from the ledger. Reads-only against
    transactions; writes only portfolio.json.

    Reloads in-memory portfolio config after patching, so the next /
    request sees the new values without restart.
    """
    result = ingest.recompute_positions()
    # Hot-reload portfolio config so the dashboard reflects new values
    from .config import load_portfolio
    portfolio.holdings = load_portfolio().holdings
    return JSONResponse(result)


@app.get("/transactions")
def transactions(limit: int = 50, _: str = Depends(auth)) -> JSONResponse:
    """Return recent transactions from the ingested ledger."""
    return JSONResponse({
        "count": ingest.transaction_count(),
        "recent": ingest.recent_transactions(limit=limit),
        "ingestions": ingest.recent_ingestions(limit=10),
    })


@app.get("/diagnostics")
def diagnostics(_: str = Depends(auth)) -> JSONResponse:
    """Run a live, read-only check on every symbol. Does NOT write to the DB.

    For each symbol returns:
      - cached_price / cached_ts: what's currently in storage (what the dashboard reads)
      - live_tv_price / live_tv_currency: what TradingView returns RIGHT NOW
      - live_tv_error: exception message if the fetch threw
      - has_tv_url: whether tv_url is configured

    Useful when the dashboard is empty: tells you whether the issue is "no poll
    has run" (cached_price=None, live_tv_price=number) or "TV scraping is failing"
    (both None) per-symbol.
    """
    from . import tradingview
    out = []
    # Holdings
    for h in portfolio.holdings:
        cached_px, cached_ts = storage.latest_price(h.yf_symbol)
        live_px, live_ccy, err = None, None, None
        if h.tv_url:
            try:
                live_px, live_ccy = tradingview.fetch(h.tv_url, timeout=10)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        out.append({
            "kind": "holding",
            "symbol": h.yf_symbol,
            "ticker": h.ticker,
            "has_tv_url": bool(h.tv_url),
            "cached_price": cached_px,
            "cached_ts": cached_ts,
            "live_tv_price": live_px,
            "live_tv_currency": live_ccy,
            "live_tv_error": err,
        })
    # Macro
    for m in portfolio.macro_watch:
        cached_px, cached_ts = storage.latest_price(m.symbol)
        live_px, live_ccy, err = None, None, None
        if m.tv_url:
            try:
                live_px, live_ccy = tradingview.fetch(m.tv_url, timeout=10)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        out.append({
            "kind": "macro",
            "symbol": m.symbol,
            "ticker": m.name,
            "has_tv_url": bool(m.tv_url),
            "cached_price": cached_px,
            "cached_ts": cached_ts,
            "live_tv_price": live_px,
            "live_tv_currency": live_ccy,
            "live_tv_error": err,
        })

    summary = {
        "total": len(out),
        "have_cached": sum(1 for r in out if r["cached_price"] is not None),
        "have_live_tv": sum(1 for r in out if r["live_tv_price"] is not None),
        "tv_failed_with_url": sum(
            1 for r in out if r["has_tv_url"] and r["live_tv_price"] is None
        ),
        "no_tv_url": sum(1 for r in out if not r["has_tv_url"]),
    }
    return JSONResponse({"summary": summary, "rows": out})
