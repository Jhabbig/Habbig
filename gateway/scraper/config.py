"""
Scraper service configuration — loads .env and exposes typed settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Main server ──────────────────────────────────────────────────────────────

MAIN_SERVER_URL: str = os.getenv("MAIN_SERVER_URL", "http://localhost:7000").rstrip("/")
SCRAPER_API_KEY: str = os.getenv("SCRAPER_API_KEY", "")

# ── Scraper API ──────────────────────────────────────────────────────────────

SCRAPER_PORT: int = int(os.getenv("SCRAPER_PORT", "8001"))
SCRAPER_HOST: str = os.getenv("SCRAPER_HOST", "127.0.0.1")

# ── Intervals ────────────────────────────────────────────────────────────────

TWITTER_INTERVAL_MINUTES: int = int(os.getenv("TWITTER_INTERVAL_MINUTES", "20"))
TRUTHSOCIAL_INTERVAL_MINUTES: int = int(os.getenv("TRUTHSOCIAL_INTERVAL_MINUTES", "15"))
RETRY_TRANSMISSION_INTERVAL_MINUTES: int = int(os.getenv("RETRY_TRANSMISSION_INTERVAL_MINUTES", "5"))

# ── Scraping limits ──────────────────────────────────────────────────────────

MAX_POSTS_PER_KEYWORD: int = int(os.getenv("MAX_POSTS_PER_KEYWORD", "100"))
MAX_TRANSMISSION_ATTEMPTS: int = int(os.getenv("MAX_TRANSMISSION_ATTEMPTS", "10"))

# ── Rate limiting ────────────────────────────────────────────────────────────

TWITTER_DELAY_BETWEEN_KEYWORDS: int = int(os.getenv("TWITTER_DELAY_BETWEEN_KEYWORDS_SECONDS", "45"))
TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS: int = int(os.getenv("TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS_SECONDS", "30"))
MIN_DELAY_JITTER: int = int(os.getenv("MIN_DELAY_JITTER_SECONDS", "10"))

# ── Browser ──────────────────────────────────────────────────────────────────

PLAYWRIGHT_HEADLESS: bool = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
BROWSER_TYPE: str = os.getenv("BROWSER_TYPE", "chromium")

# ── Storage ──────────────────────────────────────────────────────────────────

SCRAPER_DB_PATH: str = os.getenv("SCRAPER_DB_PATH", str(BASE_DIR / "scraper.db"))
SESSION_PROFILE_PATH: str = os.getenv("SESSION_PROFILE_PATH", str(BASE_DIR / "stealth" / "profiles"))

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", str(BASE_DIR / "logs" / "scraper.log"))

# ── Keywords ─────────────────────────────────────────────────────────────────
# Initial keywords from .env; the running keyword list is stored in the DB
# and managed via the admin panel API.

_TWITTER_KW_ENV = os.getenv("TWITTER_KEYWORDS", "will win,% chance,predict,bet on,odds of,probability")
_TRUTHSOCIAL_KW_ENV = os.getenv("TRUTHSOCIAL_KEYWORDS", "election,win,predict,poll")

DEFAULT_TWITTER_KEYWORDS: list[str] = [k.strip() for k in _TWITTER_KW_ENV.split(",") if k.strip()]
DEFAULT_TRUTHSOCIAL_KEYWORDS: list[str] = [k.strip() for k in _TRUTHSOCIAL_KW_ENV.split(",") if k.strip()]

# ── TruthSocial prominent accounts ──────────────────────────────────────────

_PROMINENT_ENV = os.getenv("TRUTHSOCIAL_PROMINENT_ACCOUNTS", "realDonaldTrump,DonaldTrumpJr")
TRUTHSOCIAL_PROMINENT_ACCOUNTS: list[str] = [a.strip() for a in _PROMINENT_ENV.split(",") if a.strip()]

# ── Runtime state file (keywords, intervals — persists across restarts) ──────

RUNTIME_STATE_PATH = BASE_DIR / "runtime_state.json"


def load_runtime_state() -> dict:
    """Load mutable runtime state from disk (keywords, intervals)."""
    if RUNTIME_STATE_PATH.exists():
        with open(RUNTIME_STATE_PATH) as f:
            return json.load(f)
    return {}


def save_runtime_state(state: dict) -> None:
    """Persist mutable runtime state to disk."""
    with open(RUNTIME_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
