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
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fastapi import File, UploadFile

from . import analysis, dashboard, ingest, prices, storage
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
    yf = sum(1 for _, src in price_map.values() if src == "yfinance")
    cached = sum(1 for _, src in price_map.values() if src == "cached")
    missing = sum(1 for _, src in price_map.values() if src == "missing")
    log.info("Prices: %d TV / %d yfinance / %d cached / %d missing", tv, yf, cached, missing)

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

    # 4. Housekeeping: prune old prices weekly-ish
    storage.prune_old_prices(keep_days=60)

    log.info("=== Poll cycle done: %s ===", result)
    return {
        "ingest": {
            "files": ingest_result["files"],
            "txns_new": ingest_result["txns_new"],
            "errors": ingest_result["errors"],
        },
        "prices": {"tv": tv, "yfinance": yf, "cached": cached, "missing": missing},
        "triggers": len(triggers),
        **result,
    }


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
    scheduler.start()
    log.info("Scheduler started (every %d hours)", interval_hours)

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
def root(_: str = Depends(auth)) -> str:
    return dashboard.render_dashboard()


@app.post("/poll")
def manual_poll(_: str = Depends(auth)) -> JSONResponse:
    """Run a poll cycle on demand (for testing). Returns the cycle result."""
    return JSONResponse(run_poll_cycle())


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


@app.get("/transactions")
def transactions(limit: int = 50, _: str = Depends(auth)) -> JSONResponse:
    """Return recent transactions from the ingested ledger."""
    return JSONResponse({
        "count": ingest.transaction_count(),
        "recent": ingest.recent_transactions(limit=limit),
        "ingestions": ingest.recent_ingestions(limit=10),
    })
