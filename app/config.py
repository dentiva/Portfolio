"""Settings & config loader.

Env vars are loaded by pydantic-settings; portfolio.json is loaded once on import.
Override DATA_DIR for local dev (e.g. set to ./data); Railway provides a persistent volume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).parent
ROOT_DIR = APP_DIR.parent


class Settings(BaseSettings):
    """Environment-driven settings.

    All these are configurable in Railway's environment-variables panel.
    """

    # Anthropic
    anthropic_api_key: str
    claude_model: str = "claude-haiku-4-5-20251001"

    # Resend (email)
    resend_api_key: str
    email_to: str
    email_from: str = "alerts@yourdomain.com"  # must be a verified Resend domain

    # Dashboard auth
    dashboard_user: str = "admin"
    dashboard_password: str

    # Storage location (Railway mounts /data; locally falls back to ./data)
    data_dir: Path = Path("/data") if Path("/data").is_dir() else ROOT_DIR / "data"

    # Public base URL (used in email links to dashboard)
    public_base_url: str = "http://localhost:8000"

    # Force first-run diagnostic poll on startup
    poll_on_startup: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class Holding(BaseModel):
    ticker: str
    yf_symbol: str
    tv_url: str | None = None     # TradingView URL — primary price source if set
    name: str
    leg: str
    quantity: float
    avg_cost_usd: float
    entry_target: float | None = None
    exit_target: float | None = None
    stop_loss: float | None = None
    thesis: str = ""


class MacroWatch(BaseModel):
    symbol: str
    tv_url: str | None = None     # Optional — usually macros work better via yfinance
    name: str
    key_levels: dict[str, float] = {}
    threshold_pct_24h: float = 2.0


class PollSettings(BaseModel):
    poll_interval_hours: int = 3
    max_alerts_per_day: int = 8
    drawdown_alert_pct_24h: float = 5.0
    drawdown_alert_pct_7d: float = 10.0
    dashboard_refresh_minutes: int = 30
    claude_model: str = "claude-haiku-4-5-20251001"


class Portfolio(BaseModel):
    settings: PollSettings
    holdings: list[Holding]
    macro_watch: list[MacroWatch]


def load_portfolio() -> Portfolio:
    """Load portfolio.json. Raises if malformed — fail fast on startup."""
    path = APP_DIR / "portfolio.json"
    raw: dict[str, Any] = json.loads(path.read_text())
    raw.pop("_comment", None)
    return Portfolio(**raw)


# Eager singletons — fail fast on bad config rather than mid-poll
settings = Settings()
portfolio = load_portfolio()

# Ensure data dir exists
settings.data_dir.mkdir(parents=True, exist_ok=True)
