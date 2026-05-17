# Portfolio Monitor

Automated price-and-thesis monitor for a multi-exchange offshore portfolio.
Polls live prices every 3 hours, asks Claude to filter signal from noise,
emails only the actionable items, and hosts a live dashboard. Ingests IBKR
transaction statements automatically — drop a file in `trades/` (or upload
through the dashboard) and positions recompute.

---

## How it works

```
┌──────────────────────┐
│ APScheduler (3h cron)│
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Scan trades/ folder  │  ← parse new IBKR statements (PDF/CSV)
│ → recompute positions│    update portfolio.json (preserves thresholds)
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ TradingView scrape   │  ← static HTML, no JS rendering — primary source
│ + yfinance fallback  │    yfinance handles macros + any TV miss
│ + SQLite cache       │    cached prices when both live providers fail
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Rule-based triggers  │  ← entry/exit/stop hits, drawdowns, key-level breaks
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Dedup vs today's log │  ← SQLite alerts_sent table
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Claude Haiku analysis│  ← all triggers + macro + portfolio context
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Email actionable only│  ← Resend → your inbox + dashboard link
└──────────────────────┘
```

Dashboard at `https://yourapp.up.railway.app/` (HTTP Basic Auth).

### Price source routing

Each holding has a `tv_url` (TradingView ticker page URL) and a `yf_symbol`
(yfinance symbol). The router does this for every poll:

1. **TradingView first** for any symbol with `tv_url` set.
   The scraper fetches the static HTML once, finds the JSON-LD `FAQPage`
   schema TradingView embeds in the page, and pulls the current price from
   the answer text ("The current price of AMZN is 264.14 USD..."). No JS
   rendering, no login. Tested working for NASDAQ, NYSE, AMEX, LSE UCITS,
   Oslo, TSX.

2. **yfinance fallback** for any symbol where TV returned nothing.
   This handles macro indicators (USD/INR, oil, gold, VIX, S&P, DXY — these
   don't have FAQ schemas) and any TV mis-hit on the holdings.

3. **SQLite cache** as last resort. The dashboard shows price source per
   row so you can see which holdings are live vs stale.

Routing happens transparently — you don't manage it. If TradingView ever
blocks the IP or changes their schema, yfinance picks up the slack
automatically and the dashboard surfaces "yfinance" on each affected row.

---

## Setup

### 1. Get API keys

- **Anthropic** — https://console.anthropic.com → API keys → create one
- **Resend** — https://resend.com → API keys → create one
  - For `from:` to work properly, verify a domain you control under Domains.
  - For testing: `onboarding@resend.dev` is Resend's sandbox sender (free, 100/day).

### 2. Local sanity check (optional)

```bash
git clone <your-repo>
cd portfolio-monitor
cp .env.example .env       # edit with your keys

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data trades
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/`, log in with the credentials from `.env`.
Drop a test PDF in `./trades/` or use the upload widget on the dashboard.

### 3. Deploy to Railway

```bash
# Install Railway CLI: https://docs.railway.com/guides/cli
railway login
railway init                       # name the project "portfolio-monitor"
railway up                         # builds & deploys
```

In the Railway dashboard:

1. **Variables** — paste every key from your `.env`
2. **Settings → Volumes** — add a persistent 1GB volume mounted at `/data`
   - The `trades/` folder lives under `/data/trades/` on Railway
   - The SQLite DB lives at `/data/portfolio.db`
3. **Settings → Networking** — generate a public domain. Copy the URL into
   `PUBLIC_BASE_URL`, redeploy.

That's it.

---

## Updating positions from IBKR statements

### Option A: Dashboard upload (recommended)

Open the dashboard → "Upload Trade Statement" section → choose file → Upload.
File is parsed, transactions stored, positions recomputed, page reloads. ~2 sec.

### Option B: Drop file in `trades/` folder

- Local: drop into `./trades/`
- Railway: SSH into the container OR use the upload endpoint

The next poll cycle (or restart) will pick it up automatically.

### Recommended IBKR export format

**CSV is strongly preferred over PDF.** PDF parsing is best-effort and can
mis-parse net amounts when row text bleeds together (you'll see clearly wrong
numbers in the Recent Transactions table — spot-check after upload).

To get CSV from IBKR:
1. Client Portal → **Reports** → **Flex Queries**
2. Create a query (one-time setup) with: Trades, Cash Transactions, Deposits/Withdrawals
3. Download as CSV
4. Upload it via the dashboard

CSV parsing is exact. PDF works for the common case but expect ~5% of rows
to need manual correction if your statement has dense formatting.

### What ingestion does

1. **Reads** every transaction (Buy, Sell, Dividend, Deposit, Interest, FX, Adjustment)
2. **Dedupes** against existing ledger (by `date + symbol + type + qty + net_usd`)
3. **Stores** new transactions in SQLite (`transactions` table)
4. **Recomputes** `quantity` and `avg_cost_usd` per ticker from full ledger
5. **Patches** `portfolio.json` in place — preserves your thresholds, thesis, leg
6. **Adds stubs** for new tickers (with null thresholds — you fill in)
7. **Removes** zero-quantity holdings (unless marked PLANNED in thesis)
8. **Moves** processed file to `trades/processed/YYYYMMDD-name.pdf`
9. **Logs** errors to `trades/errors/` with reason in `ingest_log` table

The dashboard's "Recent Transactions" panel shows the latest 15 ingested rows
so you can verify the parse looks right.

---

## Editing the portfolio

`app/portfolio.json` is the single source of truth.

**Auto-managed by ingestion:**
- `quantity` (recomputed from transactions every ingest)
- `avg_cost_usd` (recomputed from transactions every ingest)

**You manage manually:**
- `ticker`, `yf_symbol`, `name`, `leg`
- `entry_target`, `exit_target`, `stop_loss`
- `thesis`
- `macro_watch` array
- `settings` block

`yf_symbol` suffixes:
- LSE UCITS: `.L` (e.g. `VWRA.L`)
- Oslo: `.OL`
- TSX: `.TO`
- US: no suffix
- FX: `USDINR=X`, `EURUSD=X`
- Commodities: `CL=F` (WTI), `GC=F` (gold), `SI=F` (silver)
- Indices: `^GSPC`, `^VIX`, `^IXIC`

After editing manually, restart the Railway deploy.

---

## Customization

### Alert sensitivity (`portfolio.json` → `settings`)
- `poll_interval_hours` — default 3
- `max_alerts_per_day` — caps total emails (default 8)
- `drawdown_alert_pct_24h` — single-day drawdown threshold (default 5%)
- `drawdown_alert_pct_7d` — weekly drawdown threshold (default 10%)

### Claude prompt
`app/analysis.py` → `SYSTEM_PROMPT`. The calibration paragraph at the top tells
Claude what your tolerances and priorities are. Tune this when Claude under-
or over-alerts after a week of real use.

### Swap price source
The router in `app/prices.py` tries TradingView first, then yfinance. To
disable TradingView for a specific holding, set `tv_url: null` in
`portfolio.json` — that holding will go straight to yfinance.

To change the URL format: each holding's `tv_url` follows
`https://in.tradingview.com/symbols/{EXCHANGE}-{TICKER}/`. Use the page that
loads when you click the ticker in TradingView's search — copy the URL
verbatim. Verified working exchange codes: `NASDAQ`, `NYSE`, `AMEX`, `LSE`,
`OSL`, `TSX`. ETFs and stocks both work; FX/commodities/indices don't have
the embedded FAQ schema so they're left to yfinance.

If both TradingView and yfinance fail for too long, alternatives that need
only a `prices.py` rewrite:
- **Twelve Data**: free 800 calls/day, official API, US-heavy coverage
- **EOD Historical Data**: ~$20/mo, best international coverage

---

## Expected costs

| Item | Estimate |
|------|----------|
| Railway container (always-on, 512MB) | ~$5/month |
| Railway volume (1GB) | ~$0.25/month |
| Anthropic Claude Haiku 4.5 (~8 calls/day) | ~$2-4/month |
| Resend (free tier 100/day) | $0 |
| **Total** | **~$8-10/month** |

Switching to Claude Sonnet 4.6 for analysis: add ~$10-15/month.

---

## Endpoints

| Path | Purpose |
|------|---------|
| `GET /` | Dashboard (HTTP Basic Auth) |
| `POST /upload` | Upload a trade statement file |
| `POST /ingest` | Scan trades/ folder and process new files |
| `POST /poll` | Run a full poll cycle manually |
| `GET /state` | JSON snapshot of holdings + macro |
| `GET /transactions?limit=50` | Recent ingested transactions |
| `GET /healthz` | Liveness probe (used by Railway) |
| `GET /docs` | Auto-generated FastAPI docs (Swagger UI) |

---

## Operational notes

- **yfinance is unofficial.** Falls back to cached prices when it fails; the
  dashboard flags stale data.
- **Weekends / market holidays.** Stale prices, no triggers fire. Normal.
- **Alerts dedup per calendar day.** AMZN below stop at 09:00 + still below
  at 12:00 = one email, not two. Resets at UTC midnight.
- **Claude can fail.** Network blip, rate limit, malformed JSON. System falls
  back to passthrough that emails CRITICAL severity triggers only with
  `(Claude analysis failed)` in the subject.
- **Database lives on the Railway volume.** Survives redeploys.
- **Manual run for testing**: `POST /poll` via dashboard URL + `/docs`.

---

## File map

```
portfolio-monitor/
├── README.md
├── requirements.txt
├── railway.json
├── .env.example
├── .gitignore
├── trades/                ← drop IBKR statements HERE
│   ├── processed/         ← auto-moved after successful parse
│   └── errors/            ← auto-moved on parse failure
├── app/
│   ├── __init__.py
│   ├── main.py            ← FastAPI + scheduler + auth + endpoints
│   ├── config.py          ← settings + portfolio.json parser
│   ├── portfolio.json     ← YOUR HOLDINGS, THRESHOLDS, MACRO WATCH
│   ├── ingest.py          ← trade-file parser + position updater
│   ├── tradingview.py     ← primary price source (TV page scraper)
│   ├── prices.py          ← router: TV → yfinance → cache
│   ├── alerts.py          ← rule-based trigger detection
│   ├── analysis.py        ← Claude analysis + email dispatch
│   ├── storage.py         ← SQLite (prices, alerts, txns, snapshots)
│   └── dashboard.py       ← Jinja context builder
└── templates/
    └── dashboard.html     ← single-page dashboard with upload widget
```

---

## Caveats

This is operational tooling, not investment advice. It surfaces information;
it doesn't make decisions. Claude is calibrated to your stated risk profile
but is no substitute for judgment. Always read the reasoning before acting.

PDF parsing is best-effort — spot-check the Recent Transactions table after
each upload. For monthly use, switch to IBKR CSV (Flex Statement) export —
much more reliable.

If yfinance returns None for a symbol persistently (Yahoo changed their HTML),
either wait for upstream fix, swap the price provider, or temporarily hardcode
in `prices.py`.

