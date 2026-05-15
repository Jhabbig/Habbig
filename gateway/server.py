#!/usr/bin/env python3
"""
Polymarket Dashboard Gateway
============================
Single entry point for all dashboards. Routes by subdomain:

    narve.ai              → apex (login, signup, "my dashboards", billing)
    <subdomain>.narve.ai  → reverse-proxied to the matching local dashboard

Session cookie is scoped to `.narve.ai` so one login covers every subdomain.
Per-request subscription check gates access to each dashboard.

Environment variables:
    PRODUCTION=1               Disable the localhost dev bypass, flip the session
                               cookie to secure=True. Set this on the live server.
    GATEWAY_COOKIE_SECRET=…    Reserved for future signed-cookie use; currently
                               only checked for presence in production logging.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, Response, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db
from sidebar import render_sidebar

# Declarative rate-limit decorator used by a few admin/log endpoints. Lives
# in security/rate_limiter.py so it can be shared across modules. Falls back
# to a no-op if the subpackage is missing so the main module still imports.
try:
    from security.rate_limiter import rate_limit
except ImportError:
    def rate_limit(*args, **kwargs):
        def deco(fn):
            return fn
        return deco

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DOMAIN: str = CONFIG["domain"]
# Optional aliases so the gateway can serve more than one apex at once
# (needed during the habbig.com → narve.ai rebrand). Configured via
# ``"domain_aliases": ["narve.ai", ...]`` in config.json. DOMAIN remains
# the canonical/default apex used when no request context is available.
_RAW_ALIASES = CONFIG.get("domain_aliases", []) or []
ALLOWED_DOMAINS: tuple[str, ...] = tuple(
    dict.fromkeys([DOMAIN.lower(), *[a.lower() for a in _RAW_ALIASES]])
)
GATEWAY_PORT: int = CONFIG["gateway_port"]
DASHBOARDS: dict = CONFIG["dashboards"]

# Build reverse lookup: subdomain → dashboard_key
SUBDOMAIN_TO_KEY = {cfg["subdomain"]: key for key, cfg in DASHBOARDS.items()}


def _request_host(request: Request) -> str:
    """Lowercased host header without port."""
    return request.headers.get("host", "").split(":")[0].lower()


def _request_apex(request: Request) -> Optional[str]:
    """Return the apex domain from ALLOWED_DOMAINS that matches this request.

    Used anywhere we need to route back to the apex the user actually came
    from (cookie Domain, /gate redirect, dashboard subdomain links). Returns
    None for unknown hosts so callers can decide whether to fall back to
    the default DOMAIN or treat the request as untrusted.
    """
    host = _request_host(request)
    if not host:
        return None
    for apex in ALLOWED_DOMAINS:
        if host == apex or host.endswith("." + apex):
            return apex
    return None

# Rich preview content for each dashboard's /preview/<key> product page.
DASHBOARD_PREVIEWS = {
    "sports": {
        "tagline": "Find the edge the bookmakers miss. Compare Polymarket odds against every major sportsbook in real time and surface arbitrage opportunities before they vanish.",
        "features": [
            {"icon": "\u26a1", "title": "Live Odds Comparison", "desc": "Side-by-side Polymarket vs. DraftKings, FanDuel, and Pinnacle odds updated every 30 seconds."},
            {"icon": "\U0001f4ca", "title": "Arbitrage Scanner", "desc": "Automated detection of guaranteed-profit spreads across platforms with position sizing."},
            {"icon": "\U0001f514", "title": "Line Movement Alerts", "desc": "Push notifications when odds shift beyond configurable thresholds on tracked markets."},
            {"icon": "\U0001f9e0", "title": "Sharpe Ratio Signals", "desc": "Risk-adjusted scoring for every market so you know which bets have the best expected value."},
            {"icon": "\U0001f3af", "title": "Historical Accuracy", "desc": "Track record visualization showing past signal performance across sports categories."},
            {"icon": "\U0001f4b0", "title": "P&L Tracker", "desc": "Portfolio-level performance tracking for positions entered through the dashboard's signals."},
        ],
        "includes": [
            "Real-time odds from 5+ sportsbooks",
            "Arbitrage and mispricing alerts",
            "Full historical signal backtest data",
            "Customizable watchlists and filters",
            "30-second auto-refresh across all panels",
            "WebSocket live feed for instant updates",
            "Exportable CSV reports",
            "Priority access to new sports markets",
        ],
    },
    "weather": {
        "tagline": "Beat the weather markets with better data. Combine forecast models with market prices to spot mispricings on rain, temperature, and storm events.",
        "features": [
            {"icon": "\U0001f327\ufe0f", "title": "Forecast vs. Market", "desc": "Multi-model weather forecasts overlaid with current Polymarket prices to highlight divergence."},
            {"icon": "\U0001f4c8", "title": "Mispricing Heatmap", "desc": "Visual grid showing which weather markets are furthest from model consensus fair value."},
            {"icon": "\U0001f30e", "title": "City-Level Coverage", "desc": "Granular data for every city with an active weather market on Polymarket."},
            {"icon": "\u2705", "title": "Accuracy Leaderboard", "desc": "Track which forecast models and which markets have the best historical calibration."},
            {"icon": "\u23f0", "title": "Settlement Countdown", "desc": "Timers and probability curves that update as resolution deadlines approach."},
            {"icon": "\U0001f4ca", "title": "Ensemble Model View", "desc": "Aggregated probability from GFS, ECMWF, and other models weighted by recent accuracy."},
        ],
        "includes": [
            "Ensemble forecasts from 4+ weather models",
            "Automatic mispricing detection",
            "City-by-city market breakdown",
            "Accuracy tracking for all active markets",
            "Historical resolution data and calibration curves",
            "Daily email digest of top opportunities",
            "Mobile-friendly responsive layout",
            "All data updated every 15 minutes",
        ],
    },
    "world": {
        "tagline": "Geopolitics in real time. Track conflicts, elections, and global headlines alongside prediction market sentiment so you always see the full picture.",
        "features": [
            {"icon": "\U0001f30d", "title": "Global Conflict Tracker", "desc": "Live map and timeline of active conflicts, escalations, and diplomatic developments."},
            {"icon": "\U0001f4f0", "title": "Headline Aggregator", "desc": "Curated feed of world news from 30+ sources, ranked by market relevance and impact."},
            {"icon": "\U0001f4ca", "title": "Sentiment Analysis", "desc": "NLP-powered sentiment scores for key geopolitical topics, updated hourly."},
            {"icon": "\U0001f5f3\ufe0f", "title": "Election Monitor", "desc": "Track global elections and referendums with polling data cross-referenced against market odds."},
            {"icon": "\U0001f6a8", "title": "Escalation Alerts", "desc": "Get notified when a tracked situation escalates or when market prices move sharply."},
            {"icon": "\U0001f4c5", "title": "Event Timeline", "desc": "Chronological view of past events and their market impact for pattern recognition."},
        ],
        "includes": [
            "News aggregation from 30+ global sources",
            "Conflict and crisis tracking dashboard",
            "Sentiment analysis on major geopolitical themes",
            "Election polling aggregation",
            "Customizable alert thresholds",
            "Historical event-to-market-move analysis",
            "Weekly geopolitical briefing summary",
            "Data updated every 30 minutes",
        ],
    },
    "crypto": {
        "tagline": "Quantitative crypto signals powered by ensemble machine learning. Cut through the noise with data-driven BTC and altcoin predictions.",
        "features": [
            {"icon": "\U0001f916", "title": "Ensemble ML Predictor", "desc": "Six independent models vote on direction and magnitude, giving you a confidence-weighted signal."},
            {"icon": "\U0001f4c9", "title": "Market Sentiment Index", "desc": "Aggregated fear/greed score from on-chain data, social media, and funding rates."},
            {"icon": "\U0001f50d", "title": "Whale Activity Monitor", "desc": "Track large wallet movements and exchange inflows/outflows that precede price action."},
            {"icon": "\u26a1", "title": "Real-Time Signals", "desc": "WebSocket-powered price feeds and model updates so you never miss a regime change."},
            {"icon": "\U0001f4ca", "title": "Backtest Dashboard", "desc": "Full transparency into model performance with walk-forward backtests over 3+ years."},
            {"icon": "\U0001f6e1\ufe0f", "title": "Risk Management", "desc": "Position sizing guidance and drawdown alerts based on current volatility regime."},
        ],
        "includes": [
            "Ensemble ML signals for BTC, ETH, and top altcoins",
            "Real-time WebSocket price and signal feed",
            "On-chain analytics and whale alerts",
            "Sentiment aggregation across social and on-chain data",
            "3+ year backtest with walk-forward validation",
            "Configurable risk and position-size calculator",
            "Model confidence breakdowns per prediction",
            "Data refreshed every 60 seconds",
        ],
    },
    "midterm": {
        "tagline": "Multi-source election intelligence. Aggregate polls, prediction markets, and expert forecasts into one unified view of every competitive race.",
        "features": [
            {"icon": "\U0001f5f3\ufe0f", "title": "Polling Aggregation", "desc": "Weighted average of major polls with recency and quality adjustments, updated daily."},
            {"icon": "\U0001f4ca", "title": "Multi-Market Comparison", "desc": "Side-by-side odds from Polymarket, Kalshi, PredictIt, and Metaculus for every race."},
            {"icon": "\U0001f50e", "title": "Race-Level Deep Dives", "desc": "Detailed breakdowns for every Senate, House, and Governor race with demographic overlays."},
            {"icon": "\U0001f4c8", "title": "Swing-o-Meter", "desc": "Real-time visualization of how races have shifted over time with key event annotations."},
            {"icon": "\U0001f9e9", "title": "Scenario Builder", "desc": "Model different turnout and polling-error scenarios to see how the overall map changes."},
            {"icon": "\U0001f4e2", "title": "Breaking News Impact", "desc": "Track how major news events ripple through polls and markets within hours."},
        ],
        "includes": [
            "Aggregated polling from 15+ pollsters",
            "Odds comparison across 4 prediction platforms",
            "Race-by-race probability estimates",
            "Historical accuracy benchmarks",
            "Interactive scenario modeling tool",
            "Daily shift summaries and email alerts",
            "Demographic overlay data per race",
            "Updated every 6 hours (hourly near election day)",
        ],
    },
    "top_traders": {
        "tagline": "Follow the smart money. Track the highest-performing Polymarket traders, see what they are buying, and reverse-engineer their strategies.",
        "features": [
            {"icon": "\U0001f3c6", "title": "Performance Leaderboard", "desc": "Ranked list of top traders by ROI, volume, and win rate, filterable by time period."},
            {"icon": "\U0001f50d", "title": "Whale Tracker", "desc": "Real-time alerts when top wallets enter or exit large positions on any market."},
            {"icon": "\U0001f4bc", "title": "Portfolio X-Ray", "desc": "See the full position breakdown of any tracked trader, including entry prices and P&L."},
            {"icon": "\U0001f4c8", "title": "Strategy Classification", "desc": "Algorithmic tagging of trader behavior patterns: momentum, contrarian, event-driven, etc."},
            {"icon": "\U0001f465", "title": "Consensus Heatmap", "desc": "Visual grid showing which markets have the most top-trader agreement in one direction."},
            {"icon": "\U0001f4e5", "title": "Copy-Trade Signals", "desc": "Optional alerts when a configurable number of top traders converge on the same market."},
        ],
        "includes": [
            "Tracking 200+ top Polymarket wallets",
            "Real-time position change alerts",
            "Trader P&L and performance history",
            "Strategy style classification per trader",
            "Consensus and divergence signals",
            "Customizable watchlists for specific wallets",
            "Filterable by market category and time window",
            "Data refreshed every 5 minutes",
        ],
    },
}

# Production flag: set PRODUCTION=1 on the deployed server. Disables the
# localhost dev bypass and flips the session cookie to secure=True.
IS_PRODUCTION: bool = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")

COOKIE_NAME = "pm_gateway_session"
GATE_COOKIE_NAME = "narve_gate_access"
GATE_COOKIE_TTL = 7 * 86400  # 7 days
# TODO(security C4): replace site-wide SITE_ACCESS_TOKEN with per-user invite-
# token gate validation. Single shared secret = full gate bypass if leaked,
# with no rotation story. See NARVE_SECURITY_AUDIT.md critical item C4.
SITE_ACCESS_TOKEN = os.environ.get("SITE_ACCESS_TOKEN", "")

# Shared secret stamped on dashboard-proxy requests so downstream subproduct
# services can trust the X-Gateway-User-* identity headers without relying on
# peer-IP checks. MUST be set in production — if empty, the gateway would omit
# X-Gateway-Secret entirely and an equally-empty downstream secret could let
# hmac.compare_digest("", "") return True (full SSO bypass). See the startup
# checks in lifespan() and the proxy_request fail-closed guard further down
# for the matching enforcement.
GATEWAY_SSO_SECRET = os.environ.get("GATEWAY_SSO_SECRET", "")

# Impersonation cookie — see impersonation.py.
IMPERSONATION_COOKIE_NAME = "narve_impersonation"
IMPERSONATION_COOKIE_TTL = 4 * 60 * 60  # 4 hours

# Leading dot on the resolved Domain attribute makes the cookie apply to
# every subdomain of the matched apex — computed per-request so we can serve
# multiple apexes (habbig.com + narve.ai) from a single gateway without
# leaking cookies between them.


def cookie_domain_for(request: Request) -> Optional[str]:
    """Return the Domain attribute to use for Set-Cookie for this request.

    Rules:
      * If the request host matches (or is a subdomain of) one of
        ALLOWED_DOMAINS → return ``.<matched_apex>`` so the cookie applies
        across every subdomain of that apex (and only that apex — cookies
        never leak between habbig.com and narve.ai).
      * If the request host is localhost or *.localhost → return None so the
        browser stores the cookie for the exact host (works for preview/dev).
      * Otherwise → None (safest fallback; browser scopes to exact host).
    """
    apex = _request_apex(request)
    if apex and "." in apex and apex != "localhost":
        return f".{apex}"
    return None

# ── Logging ──────────────────────────────────────────────────────────────
# Centralised structured-JSON logging. SERVICE_NAME defaults to "app" so
# the gateway uses LOGTAIL_TOKEN_APP if BetterStack is configured.
os.environ.setdefault("SERVICE_NAME", "app")
from logging_config import (
    configure_logging,
    get_logger,
    set_request_context,
    clear_request_context,
    ring_buffer as _log_ring_buffer,
    is_logtail_configured,
    SERVICE_NAME as _LOG_SERVICE_NAME,
)
configure_logging(base_dir=BASE_DIR)
log = get_logger("gateway")

# Simple but defensible email regex (no attempt to RFC 5322; just common cases).
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def is_valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s)) and len(s) <= 254


# ── Input length caps ────────────────────────────────────────────────────────
#
# Upper bounds on every free-text Form/JSON field the gateway accepts. The
# 1 MB request-body limit in SecurityHeadersMiddleware is a backstop, not a
# primary defense — fields smaller than the body cap but still absurdly long
# (a 500 KB "username", a 900 KB "topic name") would otherwise flow into SQL,
# templates, or log lines. Reject at the handler edge with a clean 400.

FIELD_MAX = {
    "username": 20,
    "email": 254,
    "password": 256,
    "invite_token": 64,
    "reset_token": 128,
    "topic_name": 100,
    "topic_keyword": 50,
    "support_subject": 200,
    "support_body": 5000,
    "enquiry_message": 5000,
    "enquiry_name": 100,
    "feedback_body": 5000,
    "display_name": 100,
    "bio": 500,
    "url": 500,
    "generic": 1000,
}


def _bounded(value, max_len: int, name: str = "field") -> str:
    """Strip and length-check a free-text field. 400s on overflow.

    Used at the top of handlers so every oversized field turns into a clean
    validation error instead of reaching the DB / template / logs."""
    s = (value or "").strip()
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{name} exceeds maximum length")
    return s


# ── App setup ─────────────────────────────────────────────────────────────────

# Prefer orjson for JSON serialisation when available — it's 3-5× faster
# than stdlib json on our typical payloads (feed lists, market detail
# responses) and handles datetime/decimal/bytes natively. Fall back to
# the stdlib JSONResponse when orjson isn't installed so dev setups
# without the wheel keep working identically.
try:
    from fastapi.responses import ORJSONResponse as _DefaultJSONResponse  # noqa: F401
    import orjson  # noqa: F401 — ensures orjson is actually available
    _JSON_SERIALIZER = "orjson"
except Exception:  # pragma: no cover
    from fastapi.responses import JSONResponse as _DefaultJSONResponse  # type: ignore[assignment]
    _JSON_SERIALIZER = "stdlib"


# Persistent httpx client for upstream proxying (connection pooling).
# Defined here (rather than alongside the proxy handlers) because the
# lifespan context manager below assigns to it during startup and must
# be defined before the FastAPI() constructor so it can be passed in.
HTTP_CLIENT: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup logic before yield, shutdown after.

    Replaces the deprecated ``@app.on_event("startup"|"shutdown")``
    handlers (FastAPI ≥0.93 emits DeprecationWarning for those).
    Body matches the prior on_event handlers verbatim — see
    git history for the original split.
    """
    global HTTP_CLIENT
    # ── Startup ──────────────────────────────────────────────────────────
    # Config validation runs BEFORE any other startup work so a
    # misconfigured production server fails loudly (sys.exit(2)) instead
    # of trickling a cryptic error deep in a handler. Dev mode only
    # warns so local iteration with partial env stays unblocked.
    # See gateway/config.py for the spec.
    try:
        import config as _cfg
        _cfg.validate_config()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — never let config import itself break startup
        log.exception("config.validate_config() crashed — continuing (legacy env)")

    HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    mode = "PRODUCTION" if IS_PRODUCTION else "dev (localhost bypass enabled)"
    log.info("Gateway started on port %d, domain=%s, mode=%s", GATEWAY_PORT, DOMAIN, mode)
    log.info("Dashboards: %s", ", ".join(f"{k}→:{v['target']}" for k, v in DASHBOARDS.items()))
    if IS_PRODUCTION and not os.environ.get("GATEWAY_COOKIE_SECRET"):
        log.error("FATAL: PRODUCTION=1 but GATEWAY_COOKIE_SECRET is unset — refusing to start.")
        raise RuntimeError("GATEWAY_COOKIE_SECRET must be set in production (signs pending-token + gate cookies)")
    if IS_PRODUCTION and len(os.environ.get("GATEWAY_COOKIE_SECRET", "")) < 32:
        log.error("FATAL: GATEWAY_COOKIE_SECRET is too short (<32 chars) — refusing to start.")
        raise RuntimeError("GATEWAY_COOKIE_SECRET must be at least 32 characters")
    # AUDIT FIX (HIGH × 3, audit_extension_auth.md §1): EXTENSION_JWT_SECRET
    # must be set in production. The previous code silently fell back to
    # GATEWAY_COOKIE_SECRET (coupling two distinct secrets) and ultimately
    # to the repo-committed literal ``"narve-extension-dev"``. A prod deploy
    # that forgot the env var would boot cleanly and sign 7-day extension
    # JWTs with a public string. Mirror the GATEWAY_COOKIE_SECRET pattern
    # directly above and refuse to start. The defense-in-depth check in
    # extension_routes._jwt_secret() also raises on every sign/verify call.
    if IS_PRODUCTION and not os.environ.get("EXTENSION_JWT_SECRET"):
        log.error("FATAL: PRODUCTION=1 but EXTENSION_JWT_SECRET is unset — refusing to start.")
        raise RuntimeError("EXTENSION_JWT_SECRET must be set in production (signs 7-day Chrome extension JWTs)")
    if IS_PRODUCTION and len(os.environ.get("EXTENSION_JWT_SECRET", "")) < 32:
        log.error("FATAL: EXTENSION_JWT_SECRET is too short (<32 chars) — refusing to start.")
        raise RuntimeError("EXTENSION_JWT_SECRET must be at least 32 characters")
    if IS_PRODUCTION and not SITE_ACCESS_TOKEN:
        log.error("FATAL: PRODUCTION=1 but SITE_ACCESS_TOKEN is unset — refusing to start.")
        raise RuntimeError("SITE_ACCESS_TOKEN must be set in production")
    if IS_PRODUCTION and SITE_ACCESS_TOKEN and len(SITE_ACCESS_TOKEN) < 32:
        log.error("FATAL: SITE_ACCESS_TOKEN is too short (%d chars) — refusing to start.", len(SITE_ACCESS_TOKEN))
        raise RuntimeError("SITE_ACCESS_TOKEN must be at least 32 characters")
    # GATEWAY_SSO_SECRET stamps X-Gateway-Secret on dashboard-proxy requests so
    # downstream subproducts can trust the gateway-supplied identity headers.
    # If left unset in production, proxy_request would silently omit the header
    # and an equally-empty downstream secret could let hmac.compare_digest("",
    # "") return True, granting unauthenticated access. Fail fast.
    if IS_PRODUCTION and not GATEWAY_SSO_SECRET:
        log.error("FATAL: PRODUCTION=1 but GATEWAY_SSO_SECRET is unset — refusing to start.")
        raise RuntimeError("GATEWAY_SSO_SECRET must be set in production")
    if IS_PRODUCTION and GATEWAY_SSO_SECRET and len(GATEWAY_SSO_SECRET) < 32:
        log.error("FATAL: GATEWAY_SSO_SECRET is too short (%d chars) — refusing to start.", len(GATEWAY_SSO_SECRET))
        raise RuntimeError("GATEWAY_SSO_SECRET must be at least 32 characters")
    # IP_HASH_SALT defends against rainbow-table reversal of analytics_events.ip_hash
    # rows if the DB is ever exfiltrated. A constant in source code offers zero
    # protection (SHA-256 over IPv4 is a few CPU-hours), so production MUST set a
    # per-deploy random salt of ≥32 chars. Dev / tests are allowed to run on the
    # deterministic fallback declared next to _hash_ip, but we log a WARNING so
    # nobody assumes the analytics hashes are cryptographically protected.
    if IS_PRODUCTION and not _IP_HASH_SALT_ENV:
        log.error("FATAL: PRODUCTION=1 but IP_HASH_SALT is unset — refusing to start.")
        raise RuntimeError(
            "IP_HASH_SALT must be set in production (per-deploy random ≥32 chars)"
        )
    if IS_PRODUCTION and _IP_HASH_SALT_ENV and len(_IP_HASH_SALT_ENV) < 32:
        log.error(
            "FATAL: IP_HASH_SALT is too short (%d chars) — refusing to start.",
            len(_IP_HASH_SALT_ENV),
        )
        raise RuntimeError("IP_HASH_SALT must be ≥32 characters")
    if not IS_PRODUCTION and not _IP_HASH_SALT_ENV:
        log.warning(
            "IP_HASH_SALT not set — using deterministic dev fallback; "
            "analytics ip_hash values are NOT cryptographically protected. "
            "Production MUST set IP_HASH_SALT to a random ≥32-char value."
        )
    # Invite-token bootstrap removed 2026-05-15. Admin promotion is now
    # handled via /admin/users/{id}/promote against a regular account
    # created at /register behind the /gate perimeter.

    # Run versioned migrations before anything else hits the DB.
    try:
        import migrations as _migrations
        _migrations.upgrade_to_head()
    except Exception as e:
        log.exception("migration upgrade failed at startup: %s", e)

    # The Fernet encryption key MUST be configured in production regardless
    # of whether any TOTP users currently exist — otherwise the first
    # encryption write (TOTP enrolment, Kalshi token, etc.) silently stores
    # plaintext or 500s under load. Fail fast with a clear error.
    _cred_key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if IS_PRODUCTION and not _cred_key:
        log.error(
            "FATAL: PRODUCTION=1 but CREDENTIALS_ENCRYPTION_KEY is unset — refusing to start."
        )
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY must be set in production"
        )
    if IS_PRODUCTION and _cred_key:
        # Fernet keys must be 32 url-safe base64-encoded bytes. Validate at
        # boot so we don't discover a typo only when a user enrols TOTP.
        try:
            from cryptography.fernet import Fernet as _Fernet
            _Fernet(_cred_key.encode())
        except Exception as _e:
            log.error("FATAL: CREDENTIALS_ENCRYPTION_KEY invalid Fernet key: %s", _e)
            raise RuntimeError(
                f"CREDENTIALS_ENCRYPTION_KEY invalid Fernet key: {_e}"
            )

    # Start the background job queue (in-process by default). This drives
    # the *one-shot* enqueued work (emails, pipeline kicks) via
    # ``jobs/backend.py`` and writes ``background_jobs``.
    try:
        from jobs import start_worker as _start_worker
        await _start_worker()
    except Exception as e:
        log.exception("job queue start failed: %s", e)

    # Start the APScheduler-backed recurring scheduler. Separate concern
    # from ``jobs/backend.py``: this drives the *scheduled* recurring
    # jobs (health checks, weekly reports, etc.) and writes ``job_runs``.
    # Single-process guard lives inside ``scheduler.start`` — see
    # RUNBOOK.md for the leader-election story.
    try:
        from scheduler.registry import register_all as _register_scheduler
        from scheduler import scheduler as _scheduler
        _register_scheduler()
        _scheduler.start()
    except Exception as e:
        log.exception("scheduler start failed: %s", e)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()
    # Close market API clients to prevent connection leaks
    try:
        await POLY_CLIENT.close()
    except Exception:
        pass
    try:
        await KALSHI_CLIENT.close()
    except Exception:
        pass
    # Stop the job queue so cron loops exit cleanly.
    try:
        from jobs import stop_worker as _stop_worker
        await _stop_worker()
    except Exception:
        pass
    # Stop APScheduler.
    try:
        from scheduler import scheduler as _scheduler
        _scheduler.shutdown(wait=False)
    except Exception:
        pass


app = FastAPI(
    title="narve.ai API",
    version="1.0",
    description=(
        "Public REST surface for narve.ai. Bearer-token endpoints live under "
        "/api/public/v1/*; session-scoped endpoints under /api/*; embed widgets "
        "under /api/embed/* with X-API-Key; subproducts behind subdomain HMAC. "
        "Human-readable reference: /api/docs."
    ),
    contact={
        "name": "narve.ai support",
        "url": "https://narve.ai/feedback",
        "email": "hello@narve.ai",
    },
    license_info={
        "name": "Proprietary — narve.ai Terms of Service",
        "url": "https://narve.ai/terms",
    },
    terms_of_service="https://narve.ai/terms",
    openapi_tags=[
        {"name": "Public API v1", "description": "Bearer-token authenticated REST surface under /api/public/v1/*. Stable contract; documented in /api/docs."},
        {"name": "Predictions", "description": "Single-prediction reads and the public predictions feed."},
        {"name": "Markets", "description": "Per-market reads — listings, detail, history, predictions per market."},
        {"name": "Sources", "description": "Forecaster profile reads — listings, detail, history, predictions per source."},
        {"name": "Feed", "description": "Cross-market discovery — feed, best bets, upcoming calendar."},
        {"name": "Usage", "description": "Authenticated rate-limit and quota introspection."},
        {"name": "Embeds", "description": "X-API-Key authenticated widget endpoints under /api/embed/*."},
        {"name": "Account", "description": "Session-scoped user account, profile, takes, and saved-view endpoints."},
        {"name": "AI", "description": "Pro-tier AI endpoints — explain, summarize, coach. Subscription required."},
        {"name": "Health", "description": "Liveness, readiness, and version probes."},
    ],
    docs_url=None,
    redoc_url=None,
    # Machine-readable OpenAPI for the /api/docs reference page. Enabled
    # so SDK generators, agents, and integrators can fetch the schema
    # without scraping HTML. Internal routes set include_in_schema=False
    # to stay out of the public surface.
    openapi_url="/api/openapi.json",
    default_response_class=_DefaultJSONResponse,
    lifespan=lifespan,
)

# Application metadata for /health and RUNBOOK tooling.
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
APP_ENVIRONMENT = os.environ.get("ENVIRONMENT", "production" if IS_PRODUCTION else "dev")
APP_START_TIME = time.time()
APP_SERVICE_NAME = "narve-gateway"


def _read_git_sha() -> Optional[str]:
    """Return a short git SHA (4-12 hex chars) or None.

    Read order: GIT_SHA env, GIT_COMMIT env, then `<repo>/GIT_SHA` file.
    Anything malformed is silently dropped so /health stays cheap.
    """
    for env_key in ("GIT_SHA", "GIT_COMMIT"):
        v = os.environ.get(env_key, "").strip()
        if v:
            short = v[:12]
            if 4 <= len(short) <= 12 and all(ch in "0123456789abcdefABCDEF" for ch in short):
                return short
    try:
        repo_root = Path(__file__).resolve().parent.parent
        for fname in ("GIT_SHA", "GIT_COMMIT"):
            f = repo_root / fname
            if f.exists():
                v = f.read_text().strip()[:12]
                if 4 <= len(v) <= 12 and all(ch in "0123456789abcdefABCDEF" for ch in v):
                    return v
    except Exception:
        pass
    return None


def _read_deployed_at() -> Optional[str]:
    """Return an ISO-8601 deployment timestamp or None.

    Read order: DEPLOYED_AT env, then mtime of `<repo>/DEPLOYED_AT` file.
    """
    v = os.environ.get("DEPLOYED_AT", "").strip()
    if v:
        return v
    try:
        repo_root = Path(__file__).resolve().parent.parent
        f = repo_root / "DEPLOYED_AT"
        if f.exists():
            import datetime as _dt
            return _dt.datetime.fromtimestamp(f.stat().st_mtime, tz=_dt.timezone.utc).isoformat()
    except Exception:
        pass
    return None


APP_GIT_SHA = _read_git_sha()
APP_DEPLOYED_AT = _read_deployed_at()


# ── OpenAPI customization ──────────────────────────────────────────────────
#
# FastAPI builds the spec by walking decorators; many older routes were
# registered before tags/security/include_in_schema conventions landed.
# Rather than re-touch every decorator, we post-process the generated spec:
#
#   1. Drop any /admin/* or /settings/* paths that slipped through —
#      these are internal surfaces, never part of the public contract.
#   2. Inject reusable security schemes (Bearer for /api/public/v1/*,
#      session cookie for /api/*, X-API-Key for /api/embed/*).
#   3. Apply a tag to every operation based on path prefix, so the spec
#      groups endpoints meaningfully in /api/docs and generated clients.
#   4. Attach security requirements to authenticated path families so
#      SDK generators emit the right auth handling.
#
# All transformations are read-only on the routing layer — no behavior
# change, additive metadata only.

def _custom_openapi():  # type: ignore[no-untyped-def]
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        contact=app.contact,
        license_info=app.license_info,
        terms_of_service=app.terms_of_service,
    )

    # Hide internal surfaces. /admin/* and /settings/* are session-cookie
    # gated dashboards meant for humans, not the public API contract.
    paths = schema.get("paths", {})
    for p in list(paths.keys()):
        if p.startswith("/admin/") or p == "/admin" or p.startswith("/settings/"):
            paths.pop(p, None)

    # Reusable security schemes. We document three auth modes:
    #   - BearerAuth: Authorization: Bearer <token> for /api/public/v1/*
    #   - SessionCookie: browser session cookie for in-app /api/* calls
    #   - ApiKeyHeader: X-API-Key for /api/embed/* widget endpoints
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update({
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
            "description": "Public API v1 token. Generate at /settings/api-keys; pass as `Authorization: Bearer nrv_...`.",
        },
        "SessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "narve_session",
            "description": "Browser session cookie set by /login. Used by session-scoped /api/* endpoints called from the web app.",
        },
        "ApiKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Per-widget embed key. Pass as `X-API-Key: <key>` on /api/embed/* calls.",
        },
    })

    # Tag + secure operations based on path prefix. This avoids touching
    # ~300 decorators while still producing a well-grouped spec.
    def _classify(path: str) -> tuple[list[str], list[dict]]:
        # Returns (tags, security_requirements).
        if path.startswith("/api/public/v1/markets") or path.startswith("/api/v1/markets"):
            return (["Markets"], [{"BearerAuth": []}])
        if path.startswith("/api/public/v1/sources") or path.startswith("/api/v1/sources"):
            return (["Sources"], [{"BearerAuth": []}])
        if path.startswith("/api/public/v1/predictions") or path.startswith("/api/v1/predictions"):
            return (["Predictions"], [{"BearerAuth": []}])
        if (path.startswith("/api/public/v1/feed")
                or path.startswith("/api/public/v1/best-bets")
                or path.startswith("/api/public/v1/calendar")):
            return (["Feed"], [{"BearerAuth": []}])
        if path.startswith("/api/public/v1/usage"):
            return (["Usage"], [{"BearerAuth": []}])
        if path.startswith("/api/public/v1/") or path.startswith("/api/v1/"):
            return (["Public API v1"], [{"BearerAuth": []}])
        if path.startswith("/api/embed/") or path.startswith("/embed/"):
            return (["Embeds"], [{"ApiKeyHeader": []}])
        if (path.startswith("/api/ai/")
                or path.startswith("/api/explain")
                or path.startswith("/api/summarize")):
            return (["AI"], [{"SessionCookie": []}])
        if path in ("/health", "/healthz", "/readyz", "/api/version"):
            return (["Health"], [])
        if path.startswith("/api/"):
            return (["Account"], [{"SessionCookie": []}])
        return ([], [])

    # Legacy ad-hoc tags that pre-date the taxonomy and should be replaced
    # so the spec uses a single consistent grouping in /api/docs.
    _LEGACY_TAGS = {"public-api-v1", "v1"}
    for path, methods in paths.items():
        tags, security = _classify(path)
        if not tags and not security:
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch", "head", "options"):
                continue
            if not isinstance(op, dict):
                continue
            existing = op.get("tags") or []
            keep = [t for t in existing if t not in _LEGACY_TAGS]
            if tags and not keep:
                op["tags"] = tags
            elif tags and keep != existing:
                op["tags"] = keep + [t for t in tags if t not in keep]
            if security and "security" not in op:
                op["security"] = security

    # Servers block helps SDK generators emit a base URL out of the box.
    schema["servers"] = [
        {"url": "https://narve.ai", "description": "Production"},
    ]

    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]


db.init_db()


# ── Global exception handler — never expose stack traces ───────────────────

from starlette.requests import Request as StarletteRequest


from json import JSONDecodeError as _JSONDecodeError
from fastapi.exceptions import RequestValidationError as _RequestValidationError


@app.exception_handler(_JSONDecodeError)
async def _json_decode_exception_handler(request: StarletteRequest, exc: _JSONDecodeError):
    """Reject malformed JSON cleanly with 400 instead of a 500 crash."""
    return JSONResponse({"error": "Malformed JSON body"}, status_code=400)


@app.exception_handler(_RequestValidationError)
async def _validation_exception_handler(request: StarletteRequest, exc: _RequestValidationError):
    """Generic 400 for any FastAPI/Pydantic validation failure. Field detail
    goes to the log only, never to the client."""
    log.info("Request validation failed on %s %s: %s", request.method, request.url.path, exc.errors())
    return JSONResponse({"error": "Invalid request"}, status_code=400)


@app.exception_handler(Exception)
async def global_exception_handler(request: StarletteRequest, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# NOTE: Startup/shutdown logic lives in the ``lifespan`` async-context
# manager near the FastAPI() constructor above. ``HTTP_CLIENT`` is also
# declared up there because lifespan needs to assign it before the app
# is built. Migrated from the deprecated @app.on_event handlers.


# Static files for apex pages (CSS, JS, images).
# We wrap StaticFiles with a subclass that adds long-lived Cache-Control
# headers so Cloudflare's edge and the client browser both cache aggressively.
# Cache-busting is achieved via content-hash query strings (see static_url()).


class _CachedStaticFiles(StaticFiles):
    """StaticFiles that attaches Cache-Control + Vary headers to every response.

    The 30-day TTL with `immutable` matches Cloudflare's cache rules for
    /_gateway_static/* in CLOUDFLARE_CHANGES.md. Clients bust the cache by
    appending a content-hash query string, never by changing the path.
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        # Only decorate successful hits — don't cache 404s.
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
            resp.headers["Vary"] = "Accept-Encoding"
        return resp


if STATIC_DIR.exists():
    app.mount(
        "/_gateway_static",
        _CachedStaticFiles(directory=str(STATIC_DIR)),
        name="gateway_static",
    )


# ── Static asset cache-busting ──────────────────────────────────────────────
# `static_url("css/main.css")` returns "/_gateway_static/css/main.css?v=abc12345"
# where the hash is the first 8 chars of the MD5 of the file contents.
# The hash is computed once per file per process-lifetime and cached in memory,
# so repeated template renders don't re-read files from disk.
#
# Usage in templates: replace `/_gateway_static/gateway.css?v=3` literal with a
# `{{ static_url('gateway.css') }}` substitution handled by render_page().

_static_hash_cache: dict[str, str] = {}


def static_url(path: str) -> str:
    """Return a content-hashed URL for a static asset under /_gateway_static/.

    If the file can't be read (missing, permissions), return the unhashed
    URL so the page still renders — a stale cache is better than a 500.
    """
    rel = path.lstrip("/")
    cached = _static_hash_cache.get(rel)
    if cached is not None:
        return f"/_gateway_static/{rel}?v={cached}"
    try:
        full = STATIC_DIR / rel
        if full.is_file():
            import hashlib as _hl
            # Content-addressable cache key only — not a security hash.
            # `usedforsecurity=False` silences bandit B324 and is correct:
            # MD5 collision resistance is irrelevant for a ?v= cachebuster.
            digest = _hl.md5(
                full.read_bytes(), usedforsecurity=False
            ).hexdigest()[:8]
            _static_hash_cache[rel] = digest
            return f"/_gateway_static/{rel}?v={digest}"
    except Exception as exc:
        log.debug("static_url hash failed for %s: %s", rel, exc)
    return f"/_gateway_static/{rel}"


# ── Security headers middleware ──────────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware
from collections import defaultdict

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Deprecated — the legacy XSS auditor it referenced can itself
    # introduce XSS via universal-XSS bugs. Modern OWASP guidance is 0.
    "X-XSS-Protection": "0",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Permissions-Policy: deny every browser sensor/payment/synced API by
    # default. Adding a feature you actually use? Edit this list — but
    # consider whether you really need browser-level access first. Keep
    # `clipboard-write=(self)` so copy buttons keep working; everything
    # else is deny.
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "midi=(), magnetometer=(), gyroscope=(), accelerometer=(), "
        "ambient-light-sensor=(), autoplay=(), encrypted-media=(), "
        "fullscreen=(self), picture-in-picture=(), publickey-credentials-get=(self), "
        "sync-xhr=(), bluetooth=(), display-capture=(), serial=(), hid=(), "
        "clipboard-read=(), clipboard-write=(self), idle-detection=(), "
        "interest-cohort=(), browsing-topics=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    # Cross-Origin-Resource-Policy: blocks attacker pages from reading our
    # responses via cross-origin <img>/<script> probes — closes one of the
    # Spectre-class side-channel vectors. We don't intentionally serve any
    # cross-origin embeds at this origin, so same-origin is safe.
    "Cross-Origin-Resource-Policy": "same-origin",
}
if IS_PRODUCTION:
    # 2-year max-age + preload directive. `preload` is the prerequisite for
    # submitting the domain at hstspreload.org (which baked-into-the-browser
    # HSTS, immune to first-visit downgrade attacks). Stays harmless until
    # the submission is approved. 2 years is the minimum browsers accept
    # for preload.
    SECURITY_HEADERS["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"

CSP = "; ".join([
    "default-src 'self'",
    # js.stripe.com is required for the Stripe.js checkout integration —
    # without it the browser blocks Stripe Elements with a CSP violation.
    "script-src 'self' 'unsafe-inline' https://js.stripe.com",
    "worker-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    # connect-src must allow https://api.stripe.com so Stripe Elements can
    # talk to its tokenisation API.
    "connect-src 'self' https: https://api.stripe.com",
    # Stripe checkout opens an iframe from js.stripe.com / hooks.stripe.com.
    "frame-src https://kalshi.com https://*.kalshi.com https://polymarket.com https://*.polymarket.com https://js.stripe.com https://hooks.stripe.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


MAX_REQUEST_BODY = 1_048_576  # 1MB

# CSP for /embed/* responses. The embed route handler adds a
# `frame-ancestors https://{widget.domain}` clause so only the registered
# partner can iframe the widget. If the handler omits frame-ancestors
# (e.g. on a bare error page), we fall back to `frame-ancestors \'none\'` —
# fail closed.
EMBED_CSP_DEFAULT = "; ".join([
    "default-src \'self\'",
    "style-src \'self\' \'unsafe-inline\'",
    "script-src \'self\'",
    "img-src \'self\' data: https:",
    "font-src \'self\' data:",
    "base-uri \'self\'",
    "frame-ancestors \'none\'",
])


def _apply_security_headers(response, *, is_embed: bool) -> None:
    """Stamp SECURITY_HEADERS + CSP onto response in place.

    Audit HIGH FIX C: extracted so every branch in the middleware (the
    413 early-return AND the normal pass-through, including empty-body
    302 redirects) goes through the same code path.
    """
    for header, value in SECURITY_HEADERS.items():
        if is_embed and header == "X-Frame-Options":
            continue
        response.headers[header] = value
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            EMBED_CSP_DEFAULT if is_embed else CSP
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject CSP / HSTS / X-Frame-Options on every response.

    Audit HIGH FIX C. Headers go on every status code and every body
    length — including empty-body 302/301 redirects. Without this, a
    bare RedirectResponse leaks past CSP because Starlette only sets
    Location + Content-Length: 0.
    """

    async def dispatch(self, request, call_next):
        # Reject oversized requests. Even the early-return 413 carries
        # the full set of security headers.
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY:
            too_large = JSONResponse({"error": "Request too large"}, status_code=413)
            _apply_security_headers(too_large, is_embed=False)
            return too_large
        is_embed = request.url.path.startswith("/embed/")
        response = await call_next(request)
        _apply_security_headers(response, is_embed=is_embed)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct and not is_embed:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Centralised error handlers + request-id middleware. Registered early
# so every other middleware's downstream exceptions get caught + logged
# with a request id. Adds RequestIDMiddleware (runs first in reverse-
# add order) so every response carries X-Request-ID.
try:
    import error_handlers as _error_handlers  # noqa: E402
    _error_handlers.register(app)
except Exception as _eh_exc:  # pragma: no cover
    log.warning("error_handlers registration failed: %s — continuing without it", _eh_exc)

# PWA + a11y HTML injection. Lives in a middleware (not render_page)
# so it applies to every text/html response, and isn't affected by
# upstream refactors of render_page(). Imported lazily so a syntax
# error in the module doesn't take down the server.
try:
    from pwa_middleware import PWAInjectionMiddleware as _PWAMW  # noqa: E402
    app.add_middleware(_PWAMW)
except Exception as _pwa_exc:  # pragma: no cover
    log.warning("PWA middleware import failed: %s — continuing without it", _pwa_exc)


# ── Staging subdomain proxy ─────────────────────────────────────────────────
# The production gateway on port 7000 transparently forwards requests with
# Host: staging.narve.ai to the staging uvicorn on port 7001. This lets us
# run staging on the same host without a dedicated DNS record, Cloudflare
# Tunnel ingress edit, or sudo access — the existing *.narve.ai wildcard
# already points traffic at port 7000, and this middleware re-routes any
# staging.* requests to the dedicated staging process.
#
# Isolation preserved:
#   - Different process (staging is its own uvicorn on 7001)
#   - Different SQLite database (GATEWAY_DB_PATH=auth-staging.db)
#   - Different SITE_ACCESS_TOKEN and CREDENTIALS_ENCRYPTION_KEY
#   - Different environment name, different email mode (dry_run)
#
# Isolation NOT preserved:
#   - Same host, same disk, same cloudflared tunnel
#   - Prod's StagingProxyMiddleware runs first for staging traffic, but only
#     passes through — gate/CSRF/rate-limit run inside the staging process.

STAGING_BACKEND_URL = os.environ.get("STAGING_BACKEND_URL", "http://127.0.0.1:7001")
STAGING_HOST_PREFIX = "staging."

_STAGING_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",  # httpx recomputes
})


_STAGING_CLIENT: Optional[httpx.AsyncClient] = None


class StagingProxyMiddleware(BaseHTTPMiddleware):
    """Forward Host: staging.* requests to the staging uvicorn on 7001.

    Registered as the outermost middleware so staging traffic never hits the
    production gate / CSRF / rate-limit logic in this process — the staging
    process applies those independently with its own config.

    If the staging backend is unreachable we return 502 rather than falling
    back to prod, because silently leaking staging traffic into production
    data would defeat the whole point of staging.
    """

    async def dispatch(self, request, call_next):
        # Only the PRODUCTION process acts as a host-header proxy. If the
        # staging process (environment=staging) also tried to forward
        # staging.* traffic, it would loop back into itself because the
        # staging uvicorn listens on the upstream port we're forwarding to.
        if APP_ENVIRONMENT == "staging":
            return await call_next(request)

        host = (request.headers.get("host") or "").split(":")[0].lower()
        if not host.startswith(STAGING_HOST_PREFIX):
            return await call_next(request)

        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        upstream_url = f"{STAGING_BACKEND_URL}{path}"

        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _STAGING_HOP_BY_HOP
        }
        fwd_headers["X-Forwarded-Host"] = host
        fwd_headers["X-Forwarded-Proto"] = "https"
        fwd_headers["X-Forwarded-For"] = _get_client_ip(request)
        # Preserve the original Host so the staging process sees the real
        # client-visible hostname for cookie scoping / Set-Cookie Domain.
        fwd_headers["Host"] = host

        body = await request.body()

        global _STAGING_CLIENT
        if _STAGING_CLIENT is None or _STAGING_CLIENT.is_closed:
            _STAGING_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=3.0),
                follow_redirects=False,
            )

        try:
            upstream = await _STAGING_CLIENT.request(
                request.method,
                upstream_url,
                headers=fwd_headers,
                content=body,
            )
        except httpx.ConnectError:
            log.warning("staging proxy: backend %s unreachable", STAGING_BACKEND_URL)
            return JSONResponse(
                {"error": "staging backend unreachable"},
                status_code=502,
                headers={"X-Staging-Proxy": "connect-failed"},
            )
        except httpx.RequestError as exc:
            log.warning("staging proxy request error: %s", exc)
            return JSONResponse(
                {"error": "staging backend error"},
                status_code=502,
                headers={"X-Staging-Proxy": "request-error"},
            )

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _STAGING_HOP_BY_HOP
        }
        resp_headers["X-Staging-Proxy"] = "hit"
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=resp_headers,
        )


app.add_middleware(StagingProxyMiddleware)


# ── CSRF protection (double-submit cookie) ────────────────────────────────

CSRF_COOKIE_NAME = "_csrf"
CSRF_FORM_FIELD = "_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_TOKEN_LENGTH = 32

# Phase-1 rollout flag for PATCH/PUT/DELETE CSRF enforcement.
#
# Historically the in-line middleware below only enforced CSRF on POST.
# PATCH/PUT/DELETE are equally mutating verbs and need the same protection,
# but flipping enforcement on cold would break any client code that isn't
# yet sending x-csrf-token on those methods. Two-phase rollout:
#
#   Phase 1 (now, flag default false): PATCH/PUT/DELETE are inspected. If
#     a valid token is missing/mismatched we LOG a warning but still let the
#     request through. Gives us telemetry on which routes/clients aren't
#     yet sending the header without taking the site down.
#
#   Phase 2 (next sprint, set CSRF_PATCH_DELETE_ENFORCE=true): same path,
#     but a failed validation returns 403 just like POST does today.
#
# When the flag is true the behaviour is identical to POST enforcement.
# Audit HIGH FIX A: default flipped to "true". Env var stays as the
# explicit opt-out (``CSRF_PATCH_DELETE_ENFORCE=false``) for emergency
# rollback. Mirrors security/csrf.py — keep the two in lockstep.
CSRF_PATCH_DELETE_ENFORCE = os.environ.get(
    "CSRF_PATCH_DELETE_ENFORCE", "true"
).lower() in ("1", "true", "yes", "on")

# Routes that skip CSRF validation (public GET-only, static files, proxied)
_CSRF_SKIP_PREFIXES = ("/_gateway_static", "/ws")

# POST endpoints exempt from CSRF because they have no user session to anchor
# a CSRF token to (called from public unauthenticated pages). These are still
# protected by per-IP rate limiting + email format validation.
_CSRF_EXEMPT_POSTS = frozenset({
    "/api/newsletter",
    # Stripe webhook — called server-to-server by Stripe with HMAC-signed
    # body. Defence is the Stripe-Signature header (verified via
    # stripe.Webhook.construct_event in stripe_webhook_routes) plus the
    # IP allowlist; CSRF cookies are irrelevant because Stripe never
    # carries one.
    "/stripe/webhook",
    # Public status page subscribe/unsubscribe. Called from the unauthenticated
    # /status page (no session to anchor CSRF to). Email format is validated
    # and the endpoint is read-only for unknown addresses, so bot noise is
    # bounded — no privileged state change is possible from a forgery.
    "/api/status/subscribe",
    "/api/status/unsubscribe",
    # Search click logging — fires from the command palette on nav
    # intent, often via fetch keepalive while the page is unloading.
    # Appending a result click to an analytics row is not a
    # state-change a forgery could exploit (the row already belongs
    # to the user's query_id; we never expose cross-user data).
    "/api/search/click",
    # Anonymous analytics beacon — POSTed via sendBeacon from landing /
    # dashboard pages before any session exists, so there is no CSRF
    # cookie to anchor against. The endpoint is heavily defended in its
    # own right (per-IP rate-limit, body size cap, event_type regex,
    # PII scrub on properties — see api_analytics_event) and writes only
    # to the append-only analytics_events table. A forgery can at worst
    # log a junk row that the abuse filters already throw away.
    "/api/analytics/event",
    # Subproduct-landing signup form — the <form> lives on
    # <slug>.narve.ai and POSTs to the apex, so the double-submit cookie
    # can't bridge the subdomain↔apex boundary. The route compensates
    # with Origin/Referer apex-match, per-IP + per-email rate limits,
    # and a strict SUBPRODUCTS slug whitelist. See
    # subproduct_signup_routes.subproduct_signup for the full posture.
    "/subproduct-signup",
})

# Prefix-matched POST exemptions for endpoints with dynamic path segments.
# Each prefix must be independently rate-limited so a forged cross-origin
# POST can't escalate.
_CSRF_EXEMPT_POST_PREFIXES = (
    # Referral-link acceptance: /api/invite/{code}/accept — per-IP
    # (20/hour) + per-email (3/day) rate limited, only emits a single-use
    # token to the provided email. A forgery can't leak user data or take
    # over an account.
    "/api/invite/",
    # Public developer API — Bearer-token auth, not session-cookie auth.
    # CSRF is irrelevant here because a forged cross-origin request can't
    # see or reuse the caller's Bearer header; the whole surface is
    # already rate-limited per-key (see api_public/auth.py) and every key
    # is revocable, so CSRF adds no additional guarantee.
    "/api/public/v1/",
)


@app.get("/favicon.ico")
async def favicon():
    """Return the narve.ai logo as the root-level favicon.

    Browsers hit /favicon.ico automatically for every tab. We short-circuit
    to static/img/logo.png (PNG is fine — modern browsers don't require ICO).
    """
    from fastapi.responses import FileResponse
    logo_path = STATIC_DIR / "img" / "logo.png"
    if logo_path.exists():
        return FileResponse(
            logo_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )
    raise HTTPException(status_code=404)

# ── PWA: manifest + service worker ──────────────────────────────────
# Both files must be served from the site root — the manifest needs a
# root-scoped start_url, and a service worker served under a subdir
# would only control that prefix.
@app.get("/manifest.json")
async def manifest():
    from fastapi.responses import FileResponse
    path = STATIC_DIR / "manifest.json"
    if path.exists():
        return FileResponse(
            path,
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    raise HTTPException(status_code=404)


@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import FileResponse
    path = STATIC_DIR / "sw.js"
    if path.exists():
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )
    raise HTTPException(status_code=404)



# Note: /register, /auth/register, /auth/login etc. are handled by
# server_features.py. Don't add stubs here — they'd shadow the real
# handlers via FastAPI's first-match routing.


def _generate_csrf_token() -> str:
    return secrets.token_urlsafe(_CSRF_TOKEN_LENGTH)


def _set_csrf_cookie(response, token: str, request) -> None:
    domain = cookie_domain_for(request) if IS_PRODUCTION else None
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=7200,         # M8: 2h rotation instead of 24h
        httponly=False,       # JS needs to read this for API calls
        samesite="lax",
        secure=IS_PRODUCTION,
        path="/",
        **({"domain": domain} if domain else {}),
    )


def _validate_csrf(request, submitted_token: str | None) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        return False
    return hmac.compare_digest(cookie_token, submitted_token)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    - On GET requests to HTML pages: ensures _csrf cookie is set.
    - On POST/PATCH/PUT/DELETE requests: validates the submitted token
      (form field or header) matches the cookie value. PATCH/PUT/DELETE
      enforcement is gated by ``CSRF_PATCH_DELETE_ENFORCE`` during rollout
      (soft-warn when false, hard-403 when true). See the constant above.
    - Skips static files, WebSocket, and reverse-proxied subdomain routes.
    """
    async def dispatch(self, request, call_next):
        path = request.url.path

        # Skip CSRF for static/ws paths
        if any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES):
            return await call_next(request)

        # Skip for subdomain-proxied requests (they have their own auth).
        # Any request whose host is a subdomain of one of the allowed
        # apexes (habbig.com, narve.ai, …) is considered proxied.
        if IS_PRODUCTION:
            host = _request_host(request)
            if host and any(host != apex and host.endswith("." + apex) for apex in ALLOWED_DOMAINS):
                return await call_next(request)

        method = request.method
        is_mutating = method in ("POST", "PATCH", "PUT", "DELETE")
        # PATCH/PUT/DELETE are inspected unconditionally for telemetry, but
        # only enforce 403 when the rollout flag is on. POST always enforces.
        enforce_failure = (method == "POST") or CSRF_PATCH_DELETE_ENFORCE

        # Exempt public mutating endpoints that don't have a session to anchor
        # to. The current exempt lists are POST-only; if a PATCH/DELETE
        # variant of an exempt path is ever introduced, add it explicitly.
        if method == "POST" and path in _CSRF_EXEMPT_POSTS:
            return await call_next(request)
        # Prefix-matched variants for dynamic path segments (e.g. invite/{code}).
        if method == "POST" and any(
            path.startswith(p) for p in _CSRF_EXEMPT_POST_PREFIXES
        ):
            return await call_next(request)

        if is_mutating:
            # Extract token from form field or header
            content_type = request.headers.get("content-type", "")
            submitted_token = None

            if "application/json" in content_type:
                submitted_token = request.headers.get(CSRF_HEADER_NAME)
            elif "application/x-www-form-urlencoded" in content_type:
                # Parse body manually to avoid consuming it before FastAPI
                from urllib.parse import parse_qs
                body = await request.body()
                parsed = parse_qs(body.decode("utf-8", errors="replace"))
                submitted_token = parsed.get(CSRF_FORM_FIELD, [None])[0]
            else:
                # Other content types (multipart, no body, etc.) still allow
                # the header as a fallback — matches the JS auto-inject path.
                submitted_token = request.headers.get(CSRF_HEADER_NAME)

            # Origin/Referer check as secondary defense. Compare against the
            # request's Host header rather than the configured DOMAIN — that
            # way a multi-domain front (habbig.com + narve.ai) still validates
            # cleanly without hardcoding each alias. Cross-origin requests
            # are rejected; same-origin (including subdomains sharing the
            # same apex) pass through. Origin enforcement applies to every
            # mutating verb regardless of the PATCH/DELETE rollout flag —
            # mismatched origin is a strong attack signal on its own.
            origin = request.headers.get("origin")
            if origin and IS_PRODUCTION:
                from urllib.parse import urlparse
                parsed_origin = urlparse(origin)
                req_host = request.headers.get("host", "").split(":")[0].lower()
                origin_host = (parsed_origin.hostname or "").lower()
                if origin_host and req_host:
                    # Extract the apex (last two labels) for both; a subdomain
                    # of the same apex is still "same site".
                    def _apex(h: str) -> str:
                        parts = h.split(".")
                        return ".".join(parts[-2:]) if len(parts) >= 2 else h
                    if origin_host != req_host and _apex(origin_host) != _apex(req_host):
                        return JSONResponse({"error": "Invalid origin"}, status_code=403)

            if not _validate_csrf(request, submitted_token):
                if enforce_failure:
                    return JSONResponse({"error": "CSRF validation failed"}, status_code=403)
                # Soft-warn mode for PATCH/PUT/DELETE during Phase 1. We
                # let the request through but log enough to triage which
                # client/route still needs a CSRF header before flipping
                # CSRF_PATCH_DELETE_ENFORCE on.
                log.warning(
                    "CSRF soft-warn: %s %s missing/invalid token "
                    "(enforce=false) — fix before flipping "
                    "CSRF_PATCH_DELETE_ENFORCE=true",
                    method, path,
                )

        # Pre-generate CSRF token for first-visit GET requests so render_page
        # and the cookie use the same token.
        if request.method == "GET" and not request.cookies.get(CSRF_COOKIE_NAME):
            request.state.csrf_token = _generate_csrf_token()

        response = await call_next(request)

        # Set CSRF cookie on GET HTML responses if not present
        csrf_token = getattr(request.state, "csrf_token", None)
        if csrf_token:
            ct = response.headers.get("content-type", "")
            if "text/html" in ct:
                _set_csrf_cookie(response, csrf_token, request)

        return response


app.add_middleware(CSRFMiddleware)


def _csrf_field(request) -> str:
    """Return a hidden CSRF input field for server-generated forms."""
    token = request.cookies.get(CSRF_COOKIE_NAME) or _generate_csrf_token()
    return f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{html.escape(token)}">'



# ── Pre-release gate middleware ────────────────────────────────────────────

# Routes that are fully public (no gate cookie needed)
_PUBLIC_PATHS = frozenset({
    "/", "/gate", "/health",
    # Token-first auth entry points (public because they bootstrap the flow)
    "/register", "/login", "/signup",
    "/auth/register", "/auth/login", "/auth/logout",
    "/auth/forgot-password", "/auth/reset-password",
    "/forgot-password", "/reset-password",
    # Legal + marketing
    "/terms", "/privacy", "/dpa",
    "/unsubscribe",
    # Public API endpoints called from the prerelease page
    "/api/newsletter", "/api/newsletter/position",
    # Double-opt-in confirmation + footer unsubscribe (no session by design).
    "/api/newsletter/confirm", "/api/newsletter/unsubscribe",
    # Anonymous analytics beacon (POST) — fires from landing/dashboards
    # before any session exists. See analytics.js + the
    # /api/analytics/event handler near _hash_ip.
    "/api/analytics/event",
    "/sitemap.xml", "/robots.txt",
    "/favicon.ico",
    "/.well-known/security.txt",
    # Public status page (incidents, uptime, component health, RSS, subscribe)
    "/status", "/status/feed.xml", "/status/unsubscribe",
    "/api/status", "/api/status/subscribe", "/api/status/unsubscribe",
    # PWA: fetched by browsers/OS installers before any session exists
    "/manifest.json", "/sw.js",
    # PWA offline shell (SW falls back here on cold-start network failure)
    "/offline",
    # Public SEO content pages — see seo_routes.py
    "/about", "/how-it-works", "/methodology", "/faq",
    "/team", "/press", "/changelog", "/changelog.rss", "/narve",
    # Developer docs — public page describing /api/public/v1/* for SEO.
    "/api/docs",
    # Machine-readable OpenAPI schema referenced from /api/docs.
    "/api/openapi.json",
})
# The gate is bypassed on these prefixes. The public developer API
# (/api/public/v1/*) uses Bearer-token auth and has no gate cookie, so
# we whitelist the whole prefix rather than enumerate every endpoint.
_PUBLIC_PREFIXES = ("/_gateway_static", "/sources/", "/auth/",
                    "/predictions/public/", "/api/public/v1/",
                    # OG card endpoints need to be crawler-reachable so
                    # Twitter / Slack / Discord can fetch social previews
                    # for public URLs. No sensitive data — every card is
                    # computed from already-public model output.
                    "/og/",
                    # Public-profile pages (/u/{handle}) are opt-in and
                    # designed to be crawled. The handler returns 404 for
                    # any user that hasn't opted in (existence-hide), so
                    # exposure here is bounded to consenting users.
                    "/u/")


class GateMiddleware(BaseHTTPMiddleware):
    """Redirect to /gate if the request lacks a valid gate access cookie.

    Only / (pre-release) and /gate are public. Everything else requires
    the site access cookie. In production, an unset SITE_ACCESS_TOKEN
    is a fatal misconfiguration (startup refuses to launch); the runtime
    check here is a belt-and-braces fail-closed guard. Dev/localhost
    (PRODUCTION=0) with no token falls through for convenience.
    """
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Static + pre-release root stay reachable even on misconfig.
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        if not SITE_ACCESS_TOKEN:
            if IS_PRODUCTION:
                # Fail closed: refuse to serve anything behind the gate.
                return JSONResponse(
                    {"error": "Site gate not configured"}, status_code=503
                )
            return await call_next(request)
        if _gate_cookie_is_valid(request.cookies.get(GATE_COOKIE_NAME, "")):
            return await call_next(request)
        # Subdomains share the cookie with their own apex (Domain=.<apex>),
        # so bounce the visitor to the apex gate they actually came from —
        # never cross-redirect habbig.com ↔ narve.ai.
        if IS_PRODUCTION:
            apex = _request_apex(request)
            host = _request_host(request)
            if apex and host and host != apex:
                return RedirectResponse(f"https://{apex}/gate", status_code=302)
        return RedirectResponse("/gate", status_code=302)


app.add_middleware(GateMiddleware)


# Subproduct host routing — runs BEFORE session auth so an invalid Host
# or a direct-origin hit (missing CF-Connecting-IP in prod) is rejected
# with 400/403 without touching the DB. See middleware/subproduct.py.
try:
    from middleware.subproduct import SubproductMiddleware as _SubMW  # noqa: E402
    app.add_middleware(_SubMW)
except Exception as _sub_exc:  # pragma: no cover
    log.warning("SubproductMiddleware import failed: %s — continuing without it", _sub_exc)


# Session middleware — reads the hardened narve_session cookie, attaches
# request.state.user on every request. Registered AFTER GateMiddleware so
# the gate bounces public visitors without a DB hit. Guarded so the gateway
# still boots if the auth package is missing (the legacy session cookie
# path still works).
try:
    from auth.middleware import SessionMiddleware as _HardenedSessionMiddleware  # noqa: E402
    app.add_middleware(_HardenedSessionMiddleware)
except Exception as _exc:  # pragma: no cover
    log.warning("hardened session middleware unavailable: %s", _exc)


class ImpersonationMiddleware(BaseHTTPMiddleware):
    """Admin "view as" — see impersonation.py for details.

    Defence-in-depth: a valid impersonation cookie alone is NOT
    sufficient. Every request must ALSO present a ``narve_session``
    cookie that resolves to the same admin user that started the
    session. A stolen impersonation cookie used without the admin's
    own session is rejected and the cookie is cleared. This closes
    the cookie-replay vector flagged in the audit alongside the
    at-rest hashing fix (migration 192 + queries/admin.py).
    """
    async def dispatch(self, request, call_next):
        token = request.cookies.get(IMPERSONATION_COOKIE_NAME) or ""
        imp_row = None
        if token:
            try:
                imp_row = db.get_impersonation_session_by_token(token)
            except Exception as exc:
                log.warning("impersonation lookup failed: %s", exc)

        if not imp_row or imp_row["ended_at"] is not None:
            response = await call_next(request)
            if token:
                _clear_impersonation_cookie(response, request)
            return response

        if int(time.time()) - int(imp_row["started_at"] or 0) > IMPERSONATION_COOKIE_TTL:
            try:
                db.end_impersonation_session(imp_row["id"], end_reason="expired")
            except Exception:
                pass
            response = await call_next(request)
            _clear_impersonation_cookie(response, request)
            return response

        # Cross-check: the request MUST carry the admin's own
        # narve_session cookie, and that session MUST resolve to
        # impersonation_sessions.admin_user_id. Without this, anyone
        # who steals the impersonation cookie (XSS, MITM on a stale
        # tab, etc.) gets full target-user access. SessionMiddleware
        # is registered BEFORE this one and therefore runs INSIDE us
        # (Starlette: last-added = outermost), so request.state.user
        # is not yet populated — validate the raw cookie ourselves.
        session_cookie = request.cookies.get("narve_session", "") or ""
        admin_session_user = None
        if session_cookie:
            try:
                admin_session_user = db.validate_user_session(session_cookie)
            except Exception as exc:
                log.warning("impersonation admin-session lookup failed: %s", exc)
        if (
            admin_session_user is None
            or admin_session_user["user_id"] != imp_row["admin_user_id"]
        ):
            try:
                db.end_impersonation_session(
                    imp_row["id"], end_reason="admin_session_mismatch"
                )
            except Exception:
                pass
            log.warning(
                "impersonation rejected: cookie session_id=%s admin_user_id=%s "
                "request had session_user_id=%s",
                imp_row["id"], imp_row["admin_user_id"],
                None if admin_session_user is None else admin_session_user["user_id"],
            )
            response = RedirectResponse("/admin/users", status_code=302)
            _clear_impersonation_cookie(response, request)
            return response

        try:
            admin_row = db.get_user_by_id(imp_row["admin_user_id"])
            target_row = db.get_user_by_id(imp_row["target_user_id"])
        except Exception as exc:
            log.warning("impersonation user lookup failed: %s", exc)
            return await call_next(request)

        if not admin_row or not target_row:
            try:
                db.end_impersonation_session(imp_row["id"], end_reason="user_missing")
            except Exception:
                pass
            response = await call_next(request)
            _clear_impersonation_cookie(response, request)
            return response

        request.state.impersonation = {
            "session_id": imp_row["id"],
            "admin_user_id": admin_row["id"],
            "admin_email": admin_row["email"],
            "target_user_id": target_row["id"],
            "target_row": target_row,
            "started_at": imp_row["started_at"],
        }

        import impersonation as _imp
        method = request.method
        path_ = request.url.path
        if _imp.is_action_blocked(method, path_):
            try:
                db.record_impersonation_action(
                    session_id=imp_row["id"], method=method, path=path_,
                    status_code=403, was_blocked=True,
                )
            except Exception:
                pass
            try:
                from security import audit as _audit
                _audit.log_action(
                    admin_user_id=admin_row["id"], admin_email=admin_row["email"],
                    action=_audit.AuditAction.IMPERSONATION_BLOCKED,
                    target_type="user", target_id=target_row["id"],
                    target_description=target_row["email"],
                    request=request, notes=f"{method} {path_}",
                )
            except Exception:
                pass
            return HTMLResponse(_imp.blocked_response_html(method, path_), status_code=403)

        response = await call_next(request)
        try:
            db.record_impersonation_action(
                session_id=imp_row["id"], method=method, path=path_,
                status_code=response.status_code, was_blocked=False,
            )
        except Exception:
            pass
        return response


app.add_middleware(ImpersonationMiddleware)


def _set_impersonation_cookie(response, token: str, request) -> None:
    kwargs = dict(key=IMPERSONATION_COOKIE_NAME, value=token,
                  max_age=IMPERSONATION_COOKIE_TTL, httponly=True,
                  samesite="lax", secure=IS_PRODUCTION, path="/")
    domain = cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def _clear_impersonation_cookie(response, request) -> None:
    kwargs = dict(key=IMPERSONATION_COOKIE_NAME, path="/")
    domain = cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.delete_cookie(**kwargs)


# ── Rate limiting ────────────────────────────────────────────────────────────
#
# Two backends:
#
# * In-memory (default) — thread-safe per-process dict. Fine for single-worker
#   uvicorn, which is how the gateway is currently deployed.
# * Redis (optional, enabled by setting REDIS_URL) — cross-worker / cross-host
#   shared counters using sorted-set sliding windows. Needed the moment you
#   ever run `--workers N > 1`, otherwise the effective limit becomes
#   `N * configured_limit` because each worker has its own dict.
#
# The Redis path is best-effort: a connection error falls back to the in-memory
# dict rather than failing open (which would disable rate limiting entirely)
# or failing closed (which would take the site offline). Errors are logged.

_rate_store: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 300
_RATE_MAX_LOGIN = 10
_RATE_MAX_SIGNUP = 5
_RATE_MAX_FORGOT = 3
_RATE_MAX_ENQUIRE = 5
_RATE_MAX_SUPPORT = 5
_RATE_MAX_SUBSCRIBE = 10
_rate_last_cleanup = 0.0

# Optional Redis backend.
_REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis_client = None
if _REDIS_URL:
    try:
        import redis as _redis_mod
        _redis_client = _redis_mod.from_url(_REDIS_URL, socket_timeout=1.0)
        _redis_client.ping()
        log.info("Rate limiter: Redis backend connected (%s)", _REDIS_URL.split("@")[-1])
    except Exception as exc:
        log.warning("Rate limiter: REDIS_URL set but connection failed (%s); using in-memory fallback", exc)
        _redis_client = None


def _rate_cleanup():
    global _rate_last_cleanup
    now = time.time()
    if now - _rate_last_cleanup < 60:
        return
    _rate_last_cleanup = now
    cutoff = now - 3600  # Clean entries older than 1 hour (max window)
    stale = [k for k, v in _rate_store.items() if not v or v[-1] < cutoff]
    for k in stale:
        del _rate_store[k]


def _is_rate_limited_redis(key: str, limit: int, window: int) -> Optional[bool]:
    """Sliding-window check via Redis sorted set. Returns None on Redis error
    so the caller can fall back to the in-memory path."""
    try:
        now = time.time()
        redis_key = f"rl:{key}"
        pipe = _redis_client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now - window)
        pipe.zadd(redis_key, {f"{now}:{secrets.token_hex(4)}": now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window + 10)
        _, _, count, _ = pipe.execute()
        return count > limit
    except Exception as exc:
        log.warning("Redis rate-limit check failed for %s: %s", key, exc)
        return None


def _is_rate_limited(key: str, limit: int, window: int = _RATE_WINDOW) -> bool:
    """Check if *key* has exceeded *limit* hits within *window* seconds.

    Uses Redis if configured and reachable; otherwise falls back to the
    per-process in-memory sliding window.
    """
    if _redis_client is not None:
        redis_result = _is_rate_limited_redis(key, limit, window)
        if redis_result is not None:
            return redis_result
        # Fall through to in-memory on Redis error.
    _rate_cleanup()
    now = time.time()
    timestamps = _rate_store[key]
    cutoff = now - window
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)
    if len(timestamps) >= limit:
        return True
    timestamps.append(now)
    return False


# ── Account lockout ──────────────────────────────────────────────────────────

_login_failures: dict[str, list[float]] = defaultdict(list)
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_WINDOW = 900          # 15 minutes — short-term lockout
_IDENT_CEILING_THRESHOLD = 30  # Absolute ceiling on failures per identifier
_IDENT_CEILING_WINDOW = 86400  # ...within 24 hours


def _is_account_locked(identifier: str) -> bool:
    """Return True if *identifier* is currently locked out.

    Two independent caps stack:
    - Short-term: 5 failures in 15 minutes — normal per-session lockout.
    - Long-term ceiling: 30 failures in 24 hours — blocks distributed
      brute-force attacks that rotate IPs to evade the short-term lockout.

    Keying on the identifier (not the pair with IP) intentionally allows a
    remote attacker to lock the victim out of their own account. That's the
    cost of defending against a slow botnet; acceptable here because reset
    is available via email.
    """
    key = identifier.lower()
    now = time.time()
    timestamps = _login_failures[key]
    # Prune anything older than the wider ceiling window.
    cutoff_long = now - _IDENT_CEILING_WINDOW
    while timestamps and timestamps[0] < cutoff_long:
        timestamps.pop(0)
    if len(timestamps) >= _IDENT_CEILING_THRESHOLD:
        return True
    # Short-term window is a suffix of the long list.
    cutoff_short = now - _LOCKOUT_WINDOW
    short_count = sum(1 for t in timestamps if t >= cutoff_short)
    return short_count >= _LOCKOUT_THRESHOLD


def _record_login_failure(identifier: str) -> None:
    _login_failures[identifier.lower()].append(time.time())


def _clear_login_failures(identifier: str) -> None:
    _login_failures.pop(identifier.lower(), None)


# Only these direct peers are allowed to set client-identification headers.
# The gateway listens on 127.0.0.1 behind a Cloudflare Tunnel, so legitimate
# traffic always arrives from loopback. Anything else is either the user's
# own machine in dev mode or a bypass attempt — in both cases we must refuse
# to trust cf-connecting-ip / x-forwarded-for, otherwise an attacker who
# reaches the gateway off-tunnel can forge arbitrary source IPs and evade
# every rate limit and audit log entry in this module.
_TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _get_client_ip(request: Request) -> str:
    peer = (request.client.host if request.client else "") or ""
    if peer in _TRUSTED_PROXY_HOSTS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Leftmost entry is the original client per XFF convention.
            return xff.split(",")[0].strip()
    return peer or "unknown"


RATE_LIMITED_RESPONSE = HTMLResponse(
    "<h1>Too many requests</h1>"
    "<p>You've made too many attempts. Please wait a few minutes and try again.</p>",
    status_code=429,
)


# ── Global per-IP rate limit ─────────────────────────────────────────────────
#
# Catches every endpoint (not just the handful of routes with inline
# _is_rate_limited calls) so a single IP cannot scrape unmetered GETs or
# hammer un-decorated APIs. Runs BEFORE CSRF/Gate so a flooding attacker is
# throttled before any other middleware does meaningful work.
#
# Static assets and the health probe are exempt — a single page load pulls
# several files and a 600/min cap would penalise normal browsing.
#
# Tunable via GLOBAL_RATE_LIMIT_PER_MIN env var. Starlette runs middleware
# in reverse-add order, so this add_middleware call must come AFTER the
# existing ones (SecurityHeaders/CSRF/Gate) to run FIRST.

GLOBAL_RATE_LIMIT_PER_MIN = int(os.environ.get("GLOBAL_RATE_LIMIT_PER_MIN", "600"))

_GLOBAL_RL_SKIP_PREFIXES = ("/_gateway_static", "/ws")
_GLOBAL_RL_SKIP_PATHS = frozenset({"/health"})


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in _GLOBAL_RL_SKIP_PATHS or any(path.startswith(p) for p in _GLOBAL_RL_SKIP_PREFIXES):
            return await call_next(request)
        ip = _get_client_ip(request)
        if _is_rate_limited(f"global:{ip}", GLOBAL_RATE_LIMIT_PER_MIN, 60):
            reset = int(time.time()) + 60
            return JSONResponse(
                {"error": "Rate limit exceeded. Slow down."},
                status_code=429,
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(GLOBAL_RATE_LIMIT_PER_MIN),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset),
                },
            )
        return await call_next(request)


app.add_middleware(GlobalRateLimitMiddleware)


# ── Bulk-data exfiltration budget ─────────────────────────────────────────
# Counts rows in JSON list responses per-user-per-hour. Enforces a 5k/h
# cap (429) and flags >20k/24h for review. Registered AFTER the global
# rate limit so cheap floods get dropped early, but before request
# handlers so it can see their responses.
try:
    from middleware.bulk_data_ratelimit import BulkDataRateLimitMiddleware as _BulkDataMW
    app.add_middleware(_BulkDataMW)
except Exception as _exc:  # pragma: no cover
    log.warning("BulkDataRateLimitMiddleware import failed: %s — continuing without it", _exc)


# ── Logging context middleware ───────────────────────────────────────────────
#
# Attaches a short request_id and (when resolvable) the user_id to every log
# record emitted during the request. Added LAST so it sits at the top of the
# middleware stack — that guarantees the context is set before any other
# middleware or handler logs.

import uuid as _uuid


class LoggingContextMiddleware(BaseHTTPMiddleware):
    """Attach request_id (and best-effort user_id) to logging context.

    Inbound ``X-Request-ID`` header is honoured when present so upstream
    proxies / client trace-ids thread through our logs cleanly. We
    sanitise the value to ``[A-Za-z0-9_-]`` up to 64 chars — stops a
    malformed / injected header (newlines, control chars) from
    poisoning a log line. Freshly-minted ids are 8-char hex for
    compact tail-f readability; inbound ids keep their original shape
    so a full upstream UUID survives intact.
    """

    # Shape guard for inbound ids.
    _INBOUND_ID_MAX = 64

    def _inbound_id(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        raw = raw.strip()[: self._INBOUND_ID_MAX]
        if not raw:
            return None
        for ch in raw:
            if not (ch.isalnum() or ch in "-_"):
                return None
        return raw

    async def dispatch(self, request, call_next):
        request_id = (
            self._inbound_id(request.headers.get("x-request-id", ""))
            or _uuid.uuid4().hex[:8]
        )
        user_id: Optional[int] = None

        # Best-effort user_id lookup from the session cookie. We deliberately
        # do NOT validate the session freshness here — handlers still enforce
        # auth. This is only for log correlation.
        try:
            session_cookie = request.cookies.get(COOKIE_NAME)
            if session_cookie:
                session = db.get_session(session_cookie)
                if session:
                    user_id = session["user_id"]
        except Exception:
            pass

        set_request_context(request_id, user_id=user_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            clear_request_context()


app.add_middleware(LoggingContextMiddleware)


# ── Response compression (outer) ─────────────────────────────────────────────
# GZip every text response over 1 KB. Starlette's GZipMiddleware sets the
# Content-Encoding header + handles client Accept-Encoding negotiation. The
# 1 KB minimum is the canonical cutoff — below it the per-response gzip
# framing overhead outweighs the payload savings. Added BEFORE the timing
# middleware so the wall-clock we report in X-Response-Time-ms already
# reflects the compressed-send latency (cheaper to audit than gzip-then-
# measure when investigating slow-send complaints from mobile clients).
try:
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)
except Exception as _gzip_exc:  # pragma: no cover
    log.warning("gzip middleware import failed: %s — continuing without it", _gzip_exc)


# ── Request timing ───────────────────────────────────────────────────────────
# Sets X-Response-Time-ms on every response and logs requests that cross
# the slow-request threshold. Wraps every application middleware below
# but NOT the body-size cap, which sits in front so the wall-clock for
# a rejected oversized request doesn't get counted.
try:
    from middleware.perf import RequestTimingMiddleware
    app.add_middleware(RequestTimingMiddleware)
except Exception as _perf_exc:  # pragma: no cover
    log.warning("timing middleware import failed: %s — continuing without it", _perf_exc)


# ── Body-size cap (audit HIGH FIX D — outermost) ─────────────────────────────
# Reject inbound requests whose Content-Length exceeds MAX_BODY_BYTES
# (default 2 MB) BEFORE any other middleware reads the body. Chunked /
# transfer-encoding requests are tallied as they stream in and aborted
# on the first byte over cap. Registered LAST in add_middleware order so
# Starlette puts it FIRST in dispatch — every other body-reading
# middleware (CSRF, hardened session, bulk-data) sees a request whose
# body is already cap-checked.
try:
    from middleware.body_size_limit import BodySizeLimitMiddleware as _BodyCapMW
    app.add_middleware(_BodyCapMW)
except Exception as _bsl_exc:  # pragma: no cover
    log.warning("body-size middleware import failed: %s — continuing without it", _bsl_exc)


# ── Shared auth rate limit ───────────────────────────────────────────────────
#
# Every auth POST (gate/invite/login/signup/forgot-password/reset-password)
# checks a single shared "auth:<ip>" bucket at 5 attempts per 15 minutes.
# Shared across routes so an attacker cannot multiply attempts by rotating
# between auth endpoints. Sits ON TOP of the existing per-route
# _is_rate_limited calls and the account lockout — all three stack.

AUTH_RATE_LIMIT_COUNT = 5
AUTH_RATE_LIMIT_WINDOW = 900  # 15 minutes


def _auth_rate_limited(ip: str) -> bool:
    """True if this IP has exceeded 5 auth attempts in the last 15 minutes
    (across any auth route)."""
    return _is_rate_limited(f"auth:{ip}", AUTH_RATE_LIMIT_COUNT, AUTH_RATE_LIMIT_WINDOW)


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_subdomain(request: Request) -> Optional[str]:
    """Extract the subdomain portion of the Host header.

    Examples:
        habbig.com            → ""       (apex)
        narve.ai              → ""       (apex, alias)
        crypto.habbig.com     → "crypto"
        crypto.narve.ai       → "crypto"
        staging.narve.ai      → ""       (environment alias, treated as apex)
        localhost             → ""
        crypto.localhost      → "crypto"

    ``staging.*`` is deliberately treated as an apex, not a dashboard
    subdomain. The staging uvicorn runs the same codebase as production
    and needs every handler (the `/` prerelease page, /login, /gate, etc.)
    to behave as if it were serving the main apex. If we returned
    ``"staging"`` here, every call site that does ``if sub: proxy_request``
    would try to reverse-proxy to a nonexistent dashboard and bounce the
    user to https://narve.ai/, defeating the whole point of staging.
    """
    host = _request_host(request)
    if not host or host == "localhost":
        return ""
    # Strip any configured apex (DOMAIN + aliases)
    for apex in ALLOWED_DOMAINS:
        if host == apex:
            return ""
        if host.endswith("." + apex):
            sub = host[: -(len(apex) + 1)]
            # Environment aliases (staging, preview, …) behave as the apex.
            if sub == "staging":
                return ""
            return sub
    # Localhost subdomain testing: crypto.localhost → "crypto"
    if host.endswith(".localhost"):
        return host[: -len(".localhost")]
    # Unknown host — treat as apex
    return ""


def is_local_host(request: Request) -> bool:
    """True if the request comes from localhost or *.localhost (dev mode).

    Always returns False in production (PRODUCTION=1) regardless of host,
    so a misconfigured reverse proxy can't accidentally trigger the dev
    bypass on the live server.
    """
    if IS_PRODUCTION:
        return False
    host = request.headers.get("host", "").split(":")[0].lower()
    return host == "localhost" or host.endswith(".localhost") or host == "127.0.0.1"


DEV_USER_EMAIL = "dev@local"
# Dev-only convenience account. In production the whole `ensure_dev_user` path
# is gated off (see `ensure_dev_user` below), but guard the constant too so no
# generated bytes even exist in a prod process image.
if IS_PRODUCTION:
    DEV_USER_PASSWORD = ""  # unused in prod; ensure_dev_user is a no-op
else:
    DEV_USER_PASSWORD = secrets.token_urlsafe(24)


def ensure_dev_user() -> int:
    """Create a dev user (if missing) and grant it every dashboard for free.
    Used only in local/dev mode to skip signup when previewing on localhost.
    """
    if IS_PRODUCTION:
        raise RuntimeError("ensure_dev_user() must never run in production")
    existing = db.get_user_by_email(DEV_USER_EMAIL)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(DEV_USER_EMAIL, DEV_USER_PASSWORD, username="dev", is_admin=True)
    # Auto-subscribe to every dashboard so the dashboards page shows full access.
    for key in DASHBOARDS.keys():
        if not db.has_active_subscription(user_id, key):
            db.upsert_subscription(
                user_id=user_id,
                dashboard_key=key,
                plan="dev",
                duration_days=3650,  # 10 years
                source="dev_bypass",
            )
    return user_id


def _real_admin_user(request: Request) -> Optional[dict]:
    """The actual logged-in user, ignoring impersonation.

    Mirrors current_user() session-cookie lookup but never swaps in the
    target. Admin routes rely on this so they stay reachable while
    impersonating (particularly /admin/impersonations/end).
    """
    hardened = getattr(getattr(request, "state", None), "user", None)
    if hardened:
        return {
            "user_id": hardened["user_id"],
            "username": hardened["username"],
            "email": hardened["email"],
            "is_admin": hardened["is_admin"],
            "is_super_admin": hardened["is_super_admin"],
            "admin_level": hardened["admin_level"],
        }
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = db.get_session(token)
        if session:
            admin_level = session["is_admin"] or 0
            return {
                "user_id": session["user_id"],
                "username": session["username"],
                "email": session["email"],
                "is_admin": bool(admin_level),
                "is_super_admin": admin_level >= 2,
                "admin_level": admin_level,
            }
    if is_local_host(request):
        user_id = ensure_dev_user()
        row = db.get_user_by_id(user_id)
        if not row:
            return None
        admin_level = row["is_admin"] or 0
        return {
            "user_id": user_id,
            "username": row["username"] if "username" in row.keys() else "dev",
            "email": row["email"],
            "is_admin": bool(admin_level),
            "is_super_admin": admin_level >= 2,
            "admin_level": admin_level,
            "_dev_bypass": True,
        }
    return None


def current_user(request: Request) -> Optional[dict]:
    """Return a dict describing the current session user, or None.

    Always returns a plain dict (never a sqlite3.Row) so callers can use
    ``.get()`` and ``["key"]`` uniformly. Keys:
        user_id, email, is_admin, _dev_bypass (optional)

    During impersonation this returns the TARGET user with extra
    underscore-prefixed keys — callers that need the admin use
    _real_admin_user() instead.
    """
    imp = getattr(getattr(request, "state", None), "impersonation", None)
    if imp:
        t = imp["target_row"]
        t_admin = (t["is_admin"] or 0) if ("is_admin" in t.keys()) else 0
        return {
            "user_id": t["id"],
            "username": t["username"] if ("username" in t.keys()) else "",
            "email": t["email"],
            "is_admin": bool(t_admin),
            "is_super_admin": t_admin >= 2,
            "admin_level": t_admin,
            "_impersonating": True,
            "_real_admin_id": imp["admin_user_id"],
            "_real_admin_email": imp["admin_email"],
            "_impersonation_session_id": imp["session_id"],
            "_impersonation_started_at": imp["started_at"],
        }
    # Prefer the hardened session cookie (narve_session) — attached by
    # SessionMiddleware at request.state.user. Falls back to the legacy
    # pm_gateway_session cookie so existing routes keep working during the
    # rollout window.
    hardened = getattr(getattr(request, "state", None), "user", None)
    if hardened:
        return {
            "user_id": hardened["user_id"],
            "username": hardened["username"],
            "email": hardened["email"],
            "is_admin": hardened["is_admin"],
            "is_super_admin": hardened["is_super_admin"],
            "admin_level": hardened["admin_level"],
        }
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = db.get_session(token)
        if session:
            admin_level = session["is_admin"] or 0
            return {
                "user_id": session["user_id"],
                "username": session["username"],
                "email": session["email"],
                "is_admin": bool(admin_level),
                "is_super_admin": admin_level >= 2,
                "admin_level": admin_level,
            }
    # Dev bypass: if this is a localhost request, return a synthetic "logged in"
    # dict for the dev user so the UI is usable without a real signup flow.
    if is_local_host(request):
        user_id = ensure_dev_user()
        row = db.get_user_by_id(user_id)
        if not row:
            # Extremely rare race (user deleted mid-request). Fail closed.
            return None
        admin_level = row["is_admin"] or 0
        return {
            "user_id": user_id,
            "username": row["username"] if "username" in row.keys() else "dev",
            "email": row["email"],
            "is_admin": bool(admin_level),
            "is_super_admin": admin_level >= 2,
            "admin_level": admin_level,
            "_dev_bypass": True,
        }
    return None


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    kwargs = dict(
        key=COOKIE_NAME,
        value=token,
        max_age=db.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=IS_PRODUCTION,  # Requires HTTPS when PRODUCTION=1
        path="/",
    )
    domain = cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def clear_session_cookie(response: Response, request: Request) -> None:
    kwargs = dict(key=COOKIE_NAME, path="/")
    domain = cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.delete_cookie(**kwargs)


def has_gate_access(request: Request) -> bool:
    """Check if the request has a valid gate access cookie.

    In production, an unset token means the site is misconfigured —
    never treat a request as "past the gate" in that case.
    """
    if not SITE_ACCESS_TOKEN:
        return not IS_PRODUCTION
    return _gate_cookie_is_valid(request.cookies.get(GATE_COOKIE_NAME, ""))


def _gate_cookie_secret() -> bytes:
    # Falls back to SITE_ACCESS_TOKEN in dev; GATEWAY_COOKIE_SECRET is required
    # in production by the startup check above.
    return (os.environ.get("GATEWAY_COOKIE_SECRET") or SITE_ACCESS_TOKEN or "dev-gate-secret").encode()


def _mint_gate_cookie_value() -> str:
    """Produce `<issued_at>:<hmac>`, HMAC-signed with GATEWAY_COOKIE_SECRET."""
    issued_at = int(time.time())
    mac = hmac.new(_gate_cookie_secret(), f"gate:{issued_at}".encode(), hashlib.sha256).hexdigest()
    return f"{issued_at}:{mac}"


def _gate_cookie_is_valid(cookie_value: str) -> bool:
    if not cookie_value or ":" not in cookie_value:
        return False
    issued_str, _, sig = cookie_value.partition(":")
    if not issued_str.isdigit() or not sig:
        return False
    issued_at = int(issued_str)
    # Reject values issued in the future (clock skew) or past the cookie TTL.
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > GATE_COOKIE_TTL:
        return False
    expected = hmac.new(_gate_cookie_secret(), f"gate:{issued_at}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def set_gate_cookie(response: Response, request: Request) -> None:
    kwargs = dict(
        key=GATE_COOKIE_NAME,
        value=_mint_gate_cookie_value(),
        max_age=GATE_COOKIE_TTL,
        httponly=True,
        samesite="strict",
        secure=IS_PRODUCTION,
        path="/",
    )
    domain = cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def require_gate(request: Request) -> Optional[Response]:
    """Return a redirect to /gate if the request lacks gate access, else None."""
    if has_gate_access(request):
        return None
    return RedirectResponse("/gate", status_code=302)


def _forensic_sign(user, data, endpoint: str):
    """Thin wrapper around forensics.signer.sign_response that never raises.

    Accepts either a user dict (with ``user_id``) or a raw int user_id.
    Used at list-endpoint return sites so the call is a single expression:
    ``return JSONResponse(_forensic_sign(user, payload, "endpoint_name"))``.
    """
    if not data:
        return data
    try:
        if isinstance(user, dict):
            uid = user.get("user_id") or user.get("id")
        else:
            uid = int(user) if user is not None else None
        if not uid:
            return data
        from forensics import signer as _sign
        return _sign.sign_response(int(uid), data, endpoint)
    except Exception as _exc:
        log.warning("forensic sign failed endpoint=%s: %s", endpoint, _exc)
        return data


def _is_sub_active(sub_row, is_admin: bool = False) -> bool:
    """Check if a subscription is truly active (status + not expired)."""
    if is_admin:
        return True
    if sub_row is None:
        return False
    if sub_row["status"] != "active":
        return False
    expires_at = sub_row["expires_at"]
    if expires_at is not None and expires_at <= int(time.time()):
        return False
    return True


def _inject_watermark_layer(page: str, request) -> str:
    """Append forensic watermark overlay + anti-capture script to rendered page.

    Only fires when:
      - the caller has a ``<body>`` tag (skips fragment responses), and
      - the request resolves to an authenticated user (skips /, /gate, /login).

    Each layer carries per-request context: email + user_id + session
    suffix + masked IP go into the visible SVG; a deterministic 32-bit
    seed (also persisted to ``watermark_seeds``) drives the invisible
    canvas. See gateway/watermark.py for the generation helpers.
    """
    if not request or "<body" not in page:
        return page
    try:
        user = current_user(request)
    except Exception:
        user = None
    if not user:
        return page

    import watermark as _wm
    try:
        from security_routes import (
            upsert_watermark_seed as _upsert_seed,
            get_user_privacy_prefs as _get_prefs,
        )
    except Exception:
        _upsert_seed = None
        _get_prefs = None

    user_id = int(user.get("user_id") or 0)
    email = str(user.get("email") or "")
    # Prefer the raw session cookie so the suffix is stable per browser.
    raw_session = request.cookies.get(COOKIE_NAME, "") or request.cookies.get("narve_session", "")
    session_suffix_value = _wm.session_suffix(raw_session)
    seed = _wm._derive_seed(user_id, session_suffix_value)
    if _upsert_seed:
        try:
            _upsert_seed(user_id, session_suffix_value, seed)
        except Exception:
            pass
    ip_masked = _wm.mask_ip(_wm.resolve_ip_from_request(request))
    overlay_html = _wm.overlay_html(
        email=email,
        user_id=user_id,
        session_suffix_value=session_suffix_value,
        ip_masked=ip_masked,
        seed=seed,
    )

    prefs = {"inactive_blur": True, "devtools_blur": True}
    if _get_prefs:
        try:
            prefs = _get_prefs(user_id)
        except Exception:
            pass

    prefs_script = (
        '<script>window.__NARVE_WATERMARK_PREFS__ = '
        + json.dumps({
            "inactive_blur": bool(prefs.get("inactive_blur", True)),
            "devtools_blur": bool(prefs.get("devtools_blur", True)),
        })
        + ';</script>'
    )
    wm_assets = (
        '<link rel="stylesheet" href="/_gateway_static/watermark.css">\n'
        + prefs_script
        + '<script src="/_gateway_static/watermark.js" defer></script>'
    )
    if "watermark.js" not in page:
        head_idx = page.lower().rfind("</head>")
        if head_idx != -1:
            page = page[:head_idx] + wm_assets + "\n" + page[head_idx:]
    if "nv-watermark-visible" not in page:
        # Right after <body> so the overlay sits above every subsequent
        # element. pointer-events:none guarantees click-through.
        page = re.sub(
            r"(<body[^>]*>)",
            lambda m: m.group(1) + "\n" + overlay_html,
            page, count=1,
        )
    # Psychological "view source" deterrent — cheap, correct, and zero cost.
    marker = (
        f"\n<!-- narve.ai — session-watermarked. "
        f"Session fragment sid:{session_suffix_value}. "
        f"Leaks are traceable. -->\n"
    )
    if "narve.ai — session-watermarked" not in page:
        body_close = page.lower().rfind("</body>")
        if body_close != -1:
            page = page[:body_close] + marker + page[body_close:]
    return page


import json as _json_breadcrumb


def render_breadcrumb(items) -> str:
    """Render a breadcrumb trail as a <nav class="nv-breadcrumb"><ol>...</ol></nav>.

    `items` is an iterable of (label, href) pairs. The LAST item is
    always rendered with aria-current="page" regardless of whether its
    href is None — matches WAI-ARIA breadcrumb pattern + crawler
    expectations. Intermediate items with href become anchors; intermediate
    items without href fall back to a plain <li> with aria-current still
    on the trailing crumb.
    """
    if not items:
        return ""
    items = list(items)
    parts = ['<nav class="nv-breadcrumb" aria-label="Breadcrumb"><ol>']
    last = len(items) - 1
    for i, entry in enumerate(items):
        try:
            label, url = entry
        except (TypeError, ValueError):
            # Defensive: a stray string in the list must not 500 the page.
            label, url = str(entry), None
        is_last = (i == last)
        safe_label = html.escape(str(label or ""))
        if url and not is_last:
            parts.append(
                f'<li><a href="{html.escape(str(url))}">{safe_label}</a></li>'
            )
        else:
            parts.append(
                f'<li aria-current="page">{safe_label}</li>'
            )
    parts.append("</ol></nav>")
    return "".join(parts)


def render_breadcrumb_schema(items) -> str:
    """Emit a schema.org BreadcrumbList JSON-LD <script> block for SEO.

    Only items with a non-None href are included (search engines can't
    do anything useful with a crumb that has no URL). Returns "" if no
    items qualify so the caller can safely interpolate either way.
    Relative URLs are absolutised against https://narve.ai.
    """
    if not items:
        return ""
    qualifying = []
    for entry in items:
        try:
            label, url = entry
        except (TypeError, ValueError):
            continue
        if not url:
            continue
        qualifying.append((str(label or ""), str(url)))
    if not qualifying:
        return ""
    payload = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": label,
                "item": (
                    url if url.startswith(("http://", "https://"))
                    else f"https://narve.ai{url if url.startswith('/') else '/' + url}"
                ),
            }
            for i, (label, url) in enumerate(qualifying)
        ],
    }
    return (
        '<script type="application/ld+json">'
        + _json_breadcrumb.dumps(payload, separators=(",", ":"), default=str)
        + "</script>"
    )


def render_page(name: str, request=None, **context) -> HTMLResponse:
    """Tiny templating: load static/<name>.html and do {{ key }} substitution.

    Keys prefixed with ``raw_`` are inserted verbatim (used for pre-escaped
    server-side HTML). All other values are HTML-escaped before insertion.
    For convenience, the well-known keys ``dashboard_cards`` and
    ``billing_rows`` are also treated as raw.
    """
    path = STATIC_DIR / f"{name}.html"
    page = path.read_text()
    # Replace {{ static: <path> }} tokens with content-hashed URLs. This is
    # done before normal key substitution so templates can use concise syntax
    # like `<link rel="stylesheet" href="{{ static: gateway.css }}">` without
    # an entry in the context dict for every asset.
    page = re.sub(
        r"\{\{\s*static:\s*([^}\s]+)\s*\}\}",
        lambda m: static_url(m.group(1)),
        page,
    )
    # ── i18n — detect the display language and expose t(key) in templates.
    # Priority: ?lang= > user pref > lang cookie > Accept-Language > 'en'.
    # Fallback chain in translator guarantees a never-500 render even if the
    # locale file is missing or malformed.
    try:
        from i18n import detect_language as _detect_language
        from i18n import t as _i18n_t
        from i18n import SUPPORTED as _I18N_SUPPORTED
    except Exception:
        _detect_language = None
        _i18n_t = None
        _I18N_SUPPORTED = ["en"]
    if _detect_language is not None and request is not None:
        try:
            _lang = _detect_language(request)
        except Exception:
            _lang = "en"
    else:
        _lang = context.get("lang", "en")
    context.setdefault("lang", _lang)

    # Substitute {{ t("key") }} and {{ t("key", var=val) }} patterns in the
    # template. We do this before the normal {{ key }} pass so translated
    # strings can themselves contain placeholders that the context supplies.
    if _i18n_t is not None:
        def _t_sub(m: "re.Match") -> str:
            raw = m.group(1).strip()
            # raw looks like:  "nav.billing"  or  "feed.X_predictions", count=3
            # Split first ',' outside the quoted key.
            if raw.startswith('"') or raw.startswith("'"):
                quote = raw[0]
                end = raw.find(quote, 1)
                if end == -1:
                    return m.group(0)
                key = raw[1:end]
                rest = raw[end + 1:].lstrip(", ")
            else:
                # bare key form: t(nav.billing)
                comma = raw.find(",")
                key = raw if comma == -1 else raw[:comma].strip()
                rest = "" if comma == -1 else raw[comma + 1:].strip()
            kwargs: dict = {}
            if rest:
                # Minimal kwarg parser: k=value pairs. Quoted values are
                # literals; bare identifiers resolve against the render
                # context dict so ``t("foo", count=dashboard_count)``
                # actually passes the int, not the literal string.
                # Numeric literals get cast. Everything else falls back
                # to the raw token — better to show {name} than to 500
                # the page.
                for pair in re.findall(
                    r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("[^"]*"|\'[^\']*\'|[^,]+)',
                    rest,
                ):
                    k, v = pair
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (
                        v.startswith("'") and v.endswith("'")
                    ):
                        kwargs[k] = v[1:-1]
                    elif v in context:
                        kwargs[k] = context[v]
                    else:
                        try:
                            kwargs[k] = int(v)
                        except ValueError:
                            try:
                                kwargs[k] = float(v)
                            except ValueError:
                                kwargs[k] = v
            try:
                return html.escape(_i18n_t(key, _lang, **kwargs))
            except Exception:
                return html.escape(key)

        page = re.sub(r"\{\{\s*t\(([^)]*)\)\s*\}\}", _t_sub, page)

    # Auto-inject CSRF hidden field
    if request is not None:
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or getattr(request.state, "csrf_token", None) or _generate_csrf_token()
        context["raw_csrf_field"] = f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{html.escape(csrf_token)}">'
    if "raw_csrf_field" not in context:
        context["raw_csrf_field"] = ""
    # Auto-fill empty raw_admin_link and raw_role_badge if not provided
    if "raw_admin_link" not in context:
        context["raw_admin_link"] = ""
    if "raw_nav_role" not in context:
        context["raw_nav_role"] = ""
    if "raw_role_badge" not in context:
        context["raw_role_badge"] = ""
    if "raw_sitemap" not in context:
        context["raw_sitemap"] = ""
    # Breadcrumb auto-injection: caller passes `breadcrumb=[(label, href|None), ...]`
    # and we render both the visible <nav> trail and the schema.org JSON-LD,
    # exposing them as raw_ keys so templates can interpolate explicitly. The
    # JSON-LD is also auto-injected into <head> later in this function when
    # the template didn't place it inline.
    _bc_items = context.pop("breadcrumb", None)
    if _bc_items:
        if "raw_breadcrumb" not in context:
            context["raw_breadcrumb"] = render_breadcrumb(_bc_items)
        if "raw_breadcrumb_schema" not in context:
            context["raw_breadcrumb_schema"] = render_breadcrumb_schema(_bc_items)
    if "raw_breadcrumb" not in context:
        context["raw_breadcrumb"] = ""
    if "raw_breadcrumb_schema" not in context:
        context["raw_breadcrumb_schema"] = ""
    # Auto-inject sitemap if _user context has admin flag
    if context.get("_is_admin") and not context["raw_sitemap"]:
        context["raw_sitemap"] = _sitemap_html()
    context.pop("_is_admin", None)
    raw_keys = {"dashboard_cards", "billing_rows"}
    for key, value in context.items():
        placeholder = "{{ " + key + " }}"
        if key in raw_keys or key.startswith("raw_"):
            page = page.replace(placeholder, str(value))
        else:
            page = page.replace(placeholder, html.escape(str(value)))
    # Auto-inject CSRF hidden field into all <form method="post"> tags
    csrf_field = context.get("raw_csrf_field", "")
    if csrf_field:
        page = re.sub(
            r'(<form[^>]*method="post"[^>]*>)',
            r'\1' + csrf_field,
            page
        )
    # Auto-inject skeleton CSS (Feature 4) + skeleton JS library + the shared
    # states.css (error-state / error-card / empty-state). Pages that don't
    # use them ignore the <link>/<script>; pages that need data loaders can
    # call `window.narveSkel.show(...)` without wiring anything, and pages
    # with empty/error states just add the BEM classes.
    skel_injection = (
        '<link rel="stylesheet" href="/_gateway_static/skeletons.css">\n'
        '<link rel="stylesheet" href="/_gateway_static/states.css">\n'
        '<link rel="stylesheet" href="/_gateway_static/lang-switcher.css">\n'
        '<link rel="stylesheet" href="/_gateway_static/changelog_widget.css">\n'
        '<link rel="stylesheet" href="/_gateway_static/explain_popover.css">\n'
        '<script src="/_gateway_static/skeletons.js" defer></script>\n'
        '<script src="/_gateway_static/i18n-client.js" defer></script>\n'
        '<script src="/_gateway_static/lang-switcher.js" defer></script>\n'
        '<script src="/_gateway_static/changelog_widget.js" defer></script>\n'
        '<script src="/_gateway_static/explain_popover.js" defer></script>'
    )
    if "skeletons.js" not in page:
        lower = page.lower()
        head_idx = lower.rfind("</head>")
        if head_idx != -1:
            page = page[:head_idx] + skel_injection + "\n" + page[head_idx:]

    # ── i18n switcher mount — inject a tiny container directly above the
    # sidebar-user block on every page that has one. lang-switcher.js picks
    # up `#lang-switcher-mount` on DOMContentLoaded. Pages without a
    # sidebar silently skip — window.SUPPORTED_LANGS stays available so
    # a future placement (topbar, modal) can mount elsewhere.
    if "lang-switcher-mount" not in page and len(_I18N_SUPPORTED) > 1:
        m = re.search(
            r'<(?:a|div|button)\b[^>]*class="[^"]*\bsidebar-user\b[^"]*"[^>]*>',
            page,
        )
        if m:
            mount_html = '<div id="lang-switcher-mount"></div>\n          '
            page = page[:m.start()] + mount_html + page[m.start():]

    # ── i18n: set <html lang="..."> and expose window.LANG for client JS
    #    (Intl.NumberFormat / Intl.DateTimeFormat read it for locale-aware
    #    formatting). Templates that already hardcoded `lang="en"` get
    #    overwritten so the user's switcher actually takes effect.
    if _lang:
        page = re.sub(
            r'(<html\b[^>]*?)(\s+lang="[^"]*")?(\s*[^>]*>)',
            lambda m: f'{m.group(1)} lang="{html.escape(_lang)}"{m.group(3)}',
            page,
            count=1,
        )
        window_lang_js = (
            f'<script>window.LANG={html.escape(repr(_lang))};'
            f'window.SUPPORTED_LANGS={html.escape(repr(list(_I18N_SUPPORTED)))}'
            f';</script>'
        )
        # Locale blob for client-side window.t(). We inline ONLY the current
        # locale (not every language) so payload stays small. Resolving
        # each entry through _resolve_locale_entry unwraps the
        # {"text":...,"_machine":true} wrapper shape — the client only
        # needs the display string.
        try:
            from i18n.translator import load_locale as _load_locale
            from i18n.translator import _resolve as _resolve_locale_entry
            _raw_locale = _load_locale(_lang)
            _flat_locale = {}
            for _k, _v in _raw_locale.items():
                _resolved = _resolve_locale_entry(_v)
                if _resolved is not None:
                    _flat_locale[_k] = _resolved
            _locale_json = json.dumps(_flat_locale, ensure_ascii=False, separators=(",", ":"))
            # Minimal escape: only `</` inside a <script> needs breaking.
            _locale_json_safe = _locale_json.replace("</", "<\\/")
            locale_blob_tag = (
                f'<script type="application/json" id="__NARVE_I18N__">'
                f'{_locale_json_safe}'
                f'</script>'
            )
        except Exception:
            locale_blob_tag = ""
        if "window.LANG=" not in page:
            body_idx = page.lower().find("<body")
            if body_idx != -1:
                gt = page.find(">", body_idx)
                if gt != -1:
                    page = page[: gt + 1] + window_lang_js + locale_blob_tag + page[gt + 1:]
    # ⌘K command palette. Mounts itself on first keypress — single script
    # handles modal DOM, FTS search, click logging, command mode. Inject
    # into every rendered page so the hotkey is uniformly available.
    cmdp_injection = '<script src="/_gateway_static/js/command-palette.js" defer></script>'
    if "command-palette.js" not in page:
        lower = page.lower()
        head_idx = lower.rfind("</head>")
        if head_idx != -1:
            page = page[:head_idx] + cmdp_injection + "\n" + page[head_idx:]
    # Forensic watermark overlay + anti-capture JS — only on authenticated
    # pages (user is resolvable from the request). See gateway/watermark.py.
    try:
        page = _inject_watermark_layer(page, request)
    except Exception as _wm_exc:
        log.warning("watermark inject failed: %s", _wm_exc)
    # Impersonation banner — inject after <body> so the admin always sees
    # they're viewing-as. Banner HTML handles its own padding.
    imp_state = getattr(getattr(request, "state", None), "impersonation", None) if request else None
    if imp_state and "narve-impersonation-banner" not in page:
        try:
            import impersonation as _imp
            banner = _imp.banner_html(
                target_display=_imp.display_name_for(imp_state.get("target_row")),
                admin_email=imp_state.get("admin_email", ""),
                started_at=imp_state.get("started_at", 0),
                csrf_field=context.get("raw_csrf_field", "") if context else "",
            )
            page = re.sub(
                r"(<body[^>]*>)",
                lambda m: m.group(1) + "\n" + banner,
                page, count=1,
            )
        except Exception as _exc:
            log.warning("impersonation banner inject failed: %s", _exc)
    _seo_obj = context.pop("seo", None)
    if _seo_obj is not None and "narve-seo-head" not in page:
        from seo import build_seo_head as _build_seo_head
        _seo_block = _build_seo_head(_seo_obj)
        _head_idx = page.lower().rfind("</head>")
        if _head_idx != -1:
            page = re.sub(r"<title>[^<]*</title>\s*", "", page, count=1)
            _head_idx = page.lower().rfind("</head>")
            page = page[:_head_idx] + _seo_block + page[_head_idx:]
    # Auto-inject the breadcrumb JSON-LD into <head> if the caller passed
    # `breadcrumb=` but the template didn't interpolate
    # `{{ raw_breadcrumb_schema }}`. We never inject twice — `application/ld+json`
    # blocks are content-equivalent so a single block is enough for crawlers.
    _bc_schema = context.get("raw_breadcrumb_schema") or ""
    if _bc_schema and 'application/ld+json' in _bc_schema and _bc_schema not in page:
        _head_idx = page.lower().rfind("</head>")
        if _head_idx != -1:
            page = page[:_head_idx] + _bc_schema + page[_head_idx:]
    return HTMLResponse(page)


# ── Canonical base-template wrapping (foundation bundle) ─────────────────────
#
# render_with_base() wraps a page body through static/_base.html so every
# migrated page gets the same <head>, OG tags, canonical URL, skip-link,
# toast region, and script loading order. Pages opt in by calling this
# function instead of render_page; the two can coexist during the
# 99-page migration so a regression in one migrated page doesn't break
# the 94 still on the legacy path.
#
# The inner template is a fragment — ONLY the <main> body content, no
# <head>, no <body>, no <!DOCTYPE>. Existing templates stay unchanged;
# new templates follow the fragment convention. During migration, the
# caller can pass raw_header / raw_footer inline if a page needs
# surrounding chrome the base doesn't know about yet.


def _default_lang_theme(request) -> tuple[str, str]:
    """Best-effort lang + theme for the base template.

    Both have a cookie + localStorage fallback in the inline theme-init
    script — we just seed the SSR value so the first paint doesn't
    flash. 'light' is the product default.
    """
    lang = "en"
    theme = "light"
    if request is not None:
        lang = (request.query_params.get("lang") or
                request.cookies.get("narve-lang") or
                "en")
        theme = (request.cookies.get("narve-theme") or
                 request.cookies.get("betyc-theme") or
                 "light")
    return lang, theme


def render_with_base(
    template_name: str,
    *,
    request=None,
    title: str = "narve.ai",
    meta_description: str = (
        "Prediction market intelligence for serious traders."
    ),
    og_title: Optional[str] = None,
    og_type: str = "website",
    og_image: str = "/og/default",
    canonical_url: Optional[str] = None,
    schema_jsonld: str = "",
    page_head: str = "",
    page_scripts: str = "",
    header: str = "",
    footer: str = "",
    noindex: bool = False,
    **context,
) -> HTMLResponse:
    """Render `template_name` as a page body, wrap through _base.html.

    `template_name` should resolve to static/<name>.html containing
    ONLY the body fragment. Legacy full-page templates should keep
    calling `render_page()` and will be migrated one at a time.

    Every keyword beyond the explicit set is forwarded into the inner
    template's own {{ key }} substitution — same semantics as
    render_page, including the raw_/static:/t() support.
    """
    # Resolve the inner body first so its own substitutions run.
    inner_response = render_page(template_name, request=request, **context)
    inner_body = inner_response.body.decode("utf-8") if inner_response.body else ""

    lang, theme = _default_lang_theme(request)

    if canonical_url is None:
        base = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")
        path = request.url.path if request is not None else "/"
        canonical_url = f"{base}{path}"

    robots_meta = (
        '<meta name="robots" content="noindex, nofollow">'
        if noindex else ""
    )

    base_ctx = {
        "title": title,
        "meta_description": meta_description,
        "og_title": og_title or title,
        "og_type": og_type,
        "og_image": og_image,
        "canonical_url": canonical_url,
        "lang": lang,
        "theme": theme,
        "raw_robots": robots_meta,
        "raw_schema_jsonld": schema_jsonld,
        "raw_page_head": page_head,
        "raw_page_scripts": page_scripts,
        "raw_header": header,
        "raw_footer": footer,
        "raw_content": inner_body,
    }
    # Re-use render_page for the base wrapping so {{ static: }} /
    # {{ t(…) }} / raw_ semantics are consistent across the whole tree.
    return render_page("_base", request=request, **base_ctx)


def render_empty(
    *,
    title: str,
    body: str,
    actions: Optional[list[dict]] = None,
    icon_svg: str = "",
) -> str:
    """Render the shared empty-state partial to an HTML string.

    Callers: `actions=[{"label": "Browse sources", "href": "/sources",
    "primary": True}, ...]`. Returns the HTML ready to drop into a page
    template — pair with a `raw_empty_state` placeholder.
    """
    parts: list[str] = []
    for action in (actions or []):
        cls = "nv-empty__action"
        if action.get("primary"):
            cls += " nv-empty__action--primary"
        href = html.escape(action.get("href", "#"))
        label = html.escape(action.get("label", ""))
        parts.append(f'<a class="{cls}" href="{href}">{label}</a>')
    actions_html = "".join(parts)

    ctx = {
        "empty_title": title,
        "empty_body": body,
        "raw_empty_actions": actions_html,
        "raw_icon_svg": icon_svg,
    }

    path = STATIC_DIR / "_partials" / "empty_state.html"
    try:
        page = path.read_text()
    except FileNotFoundError:
        # Fallback: inline the same structure so a missing partial on a
        # half-deployed server never 500s. Matches the .nv-empty CSS
        # class names so styling still applies.
        return (
            f'<div class="nv-empty" role="status">'
            f'<h2 class="nv-empty__title">{html.escape(title)}</h2>'
            f'<p class="nv-empty__body">{html.escape(body)}</p>'
            f'<div class="nv-empty__actions">{actions_html}</div>'
            f'</div>'
        )
    # Run the same raw_/escape substitution render_page uses.
    raw_keys = {k for k in ctx if k.startswith("raw_")}
    for key, value in ctx.items():
        placeholder = "{{ " + key + " }}"
        if key in raw_keys:
            page = page.replace(placeholder, str(value))
        else:
            page = page.replace(placeholder, html.escape(str(value)))
    return page


def _role_badge(user: dict) -> str:
    """Return a small role badge span for the nav bar."""
    level = user.get("is_admin") or 0
    if level >= 2:
        return '<span style="font-size:10px;font-weight:600;padding:3px 8px;border-radius:10px;background:rgba(245,158,11,0.12);color:#f59e0b;margin-left:6px">SUPER ADMIN</span>'
    elif level == 1:
        return '<span style="font-size:10px;font-weight:600;padding:3px 8px;border-radius:10px;background:var(--accent-light);color:var(--accent);margin-left:6px">ADMIN</span>'
    return ""


def _sitemap_html() -> str:
    """Build the Dev — Sitemap button + modal HTML for admin users."""
    routes = [
        ("Public", [
            ("/", "Pre-release page", "Public"),
            ("/gate", "Site access gate", "Public"),
        ]),
        ("Landing & Auth", [
            ("/landing", "Marketing landing page", "Gate"),
            ("/pricing", "Plan pricing", "Gate"),
            ("/enquire", "Enterprise contact", "Gate"),
            ("/subscribe", "Checkout flow", "Gate"),
            ("/support", "Support ticket", "Gate"),
            ("/contact", "Contact form", "Gate"),
            ("/register", "Create account", "Gate"),
            ("/login", "Sign in", "Gate"),
            ("/forgot-password", "Reset password", "Gate"),
        ]),
        ("Dashboards", [
            ("/dashboards", "Dashboard hub", "Logged in"),
            ("/billing", "Manage subscriptions", "Logged in"),
            ("/profile", "User profile", "Logged in"),
            ("/settings", "Default dashboard", "Logged in"),
        ] + [
            (f"https://{cfg['subdomain']}.{DOMAIN}", cfg["display_name"], "Subscription")
            for k, cfg in DASHBOARDS.items()
        ]),
        ("Admin", [
            ("/admin", "Admin panel", "Admin"),
            ("/admin/users/bulk", "Bulk user actions", "Admin"),
            ("/admin/security/bulk-fetches", "Bulk-fetch leaderboard", "Admin"),
            ("/admin/security/forensics", "Leak forensics tool", "Admin"),
        ]),
        ("API", [
            ("/api/newsletter", "Newsletter signup", "Public"),
            ("/api/enquire", "Submit enquiry", "Gate"),
            ("/api/subscribe", "Checkout API", "Gate"),
            ("/api/support-ticket", "Support ticket API", "Gate"),
        ]),
    ]
    rows = ""
    for section, items in routes:
        rows += f'<div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.08em;padding:12px 0 6px;border-bottom:1px solid var(--border);margin-top:8px">{section}</div>'
        for path, desc, access in items:
            color = {"Public": "var(--green)", "Gate": "var(--accent)", "Logged in": "var(--text-secondary)", "Subscription": "var(--amber)", "Admin": "var(--red)"}.get(access, "var(--text-muted)")
            rows += (
                f'<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;font-size:13px">'
                f'<div><a href="{html.escape(path)}" style="color:var(--accent);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;text-decoration:none" '
                f'onmouseover="this.style.textDecoration=\'underline\'" onmouseout="this.style.textDecoration=\'none\'">{html.escape(path)}</a>'
                f'<span style="color:var(--text-muted);margin-left:8px">{html.escape(desc)}</span></div>'
                f'<span style="font-size:10px;font-weight:600;color:{color}">{access}</span></div>'
            )

    return (
        '<div id="sitemap-btn" onclick="document.getElementById(\'sitemap-modal\').style.display=\'flex\'" '
        'style="position:fixed;bottom:20px;left:20px;background:#f3f4f6;border:1px solid #e5e7eb;'
        'border-radius:999px;padding:6px 14px;font-size:0.75rem;font-weight:500;color:#374151;'
        'cursor:pointer;z-index:9998;transition:opacity 0.15s">'
        'Dev &mdash; Sitemap</div>'
        '<div id="sitemap-modal" onclick="if(event.target===this)this.style.display=\'none\'" '
        'style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;'
        'align-items:center;justify-content:center;padding:24px">'
        '<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;'
        'max-width:480px;width:100%;max-height:70vh;overflow-y:auto;padding:24px;position:relative">'
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
        '<div style="font-size:18px;font-weight:500;color:var(--text-primary);font-family:Jost,sans-serif">Site Map</div>'
        '<button onclick="document.getElementById(\'sitemap-modal\').style.display=\'none\'" '
        'style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:20px">&times;</button></div>'
        f'{rows}'
        '</div></div>'
        '<script>document.addEventListener("keydown",function(e){if(e.key==="Escape")document.getElementById("sitemap-modal").style.display="none"});</script>'
    )


# ── Health check ─────────────────────────────────────────────────────────────
# Public endpoint used by Cloudflare Health Checks, load balancers, and the
# uptime monitor in RUNBOOK.md. Returns structured JSON describing the status
# of every critical dependency.
#
# Status semantics:
#   ok       → all checks passed, app is fully functional
#   degraded → some non-critical checks failed but app still serves requests
#              (returned with HTTP 200 so it doesn't trigger LB removal)
#   error    → a critical dependency is down (returned with HTTP 503)


def _check_database() -> tuple[str, Optional[str]]:
    """Cheap liveness ping on the SQLite auth.db. Returns (status, err)."""
    try:
        with db.conn() as c:
            row = c.execute("SELECT 1").fetchone()
            if row and row[0] == 1:
                return "ok", None
            return "error", "unexpected result from SELECT 1"
    except Exception as exc:  # pragma: no cover - defensive
        return "error", str(exc)[:200]


def _check_static_dir() -> tuple[str, Optional[str]]:
    """Verify the static directory is mounted and readable."""
    try:
        if STATIC_DIR.exists() and STATIC_DIR.is_dir():
            return "ok", None
        return "error", f"static dir missing: {STATIC_DIR}"
    except Exception as exc:
        return "error", str(exc)[:200]


def _check_dashboards() -> tuple[str, Optional[str]]:
    """Report whether the configured dashboards have target ports defined.

    We don't actively probe each downstream — that would turn the health check
    into a fan-out storm under load. We just verify the config is loaded.
    """
    if not DASHBOARDS:
        return "error", "no dashboards configured"
    return "ok", None


def _check_email_dry_run() -> str:
    """Report whether email is in dry-run mode (staging) or live (prod)."""
    if os.environ.get("EMAIL_DRY_RUN", "").lower() in ("1", "true", "yes", "on"):
        return "dry_run"
    smtp_user = os.environ.get("SMTP_USER", "")
    return "ok" if smtp_user else "unconfigured"


def _check_encryption_key() -> tuple[str, Optional[str]]:
    """Verify the Fernet key is set when encryption-sensitive features are active."""
    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if not key:
        return ("error", "CREDENTIALS_ENCRYPTION_KEY not set") if IS_PRODUCTION else ("unconfigured", None)
    return "ok", None


def _check_site_access_token() -> tuple[str, Optional[str]]:
    """In production the gate token must be set and long enough."""
    if not IS_PRODUCTION:
        return "ok", None
    if not SITE_ACCESS_TOKEN:
        return "error", "SITE_ACCESS_TOKEN not set"
    if len(SITE_ACCESS_TOKEN) < 32:
        return "error", f"SITE_ACCESS_TOKEN too short ({len(SITE_ACCESS_TOKEN)} chars)"
    return "ok", None


def _check_redis() -> tuple[str, Optional[str]]:
    """Cheap PING on the configured Redis URL. Disabled when REDIS_URL unset."""
    if not _REDIS_URL:
        return "disabled", None
    try:
        if _redis_client is None:
            return "error", "redis client not initialized"
        # Re-use the module-level client; it was configured with a 1s socket timeout.
        ok = _redis_client.ping()
        return ("ok", None) if ok else ("error", "ping returned falsey")
    except Exception as exc:
        return "error", str(exc)[:200]


def _check_scheduler() -> tuple[str, Optional[str]]:
    """Report whether the in-process scheduler is running.

    Returns ``disabled`` when the worker is intentionally turned off via the
    ``NARVE_SKIP_SCHEDULER`` environment variable (used in tests and the
    `--web-only` deploy mode).
    """
    if os.environ.get("NARVE_SKIP_SCHEDULER", "").strip():
        return "disabled", None
    try:
        sched = globals().get("_scheduler") or globals().get("scheduler")
        if sched is None:
            return "disabled", None
        running = getattr(sched, "running", None)
        if running is None and hasattr(sched, "state"):
            running = bool(sched.state)
        return ("ok", None) if running else ("error", "scheduler not running")
    except Exception as exc:
        return "error", str(exc)[:200]


def _check_subproducts() -> tuple[str, dict]:
    """Probe each subproduct dashboard. Slow — only called in deep mode."""
    summary: dict = {"summary": "", "down": [], "slow": []}
    try:
        total = 0
        up = 0
        import urllib.request as _ur
        for slug, cfg in (DASHBOARDS or {}).items():
            total += 1
            target = cfg.get("upstream") if isinstance(cfg, dict) else None
            if not target:
                up += 1
                continue
            try:
                with _ur.urlopen(target, timeout=1.0) as r:  # nosec
                    if 200 <= getattr(r, "status", 200) < 500:
                        up += 1
                    else:
                        summary["down"].append(slug)
            except Exception:
                summary["down"].append(slug)
        summary["summary"] = f"{up}/{total} up"
        status = "ok" if not summary["down"] else "error"
        return status, summary
    except Exception as exc:
        summary["summary"] = f"probe error: {str(exc)[:100]}"
        return "error", summary


def _is_truthy_query(v: Optional[str]) -> bool:
    """Whether a query-string param represents "on/true".

    The bare presence of a flag (``?deep``) parses to an empty string in
    Starlette; we treat that as truthy. Anything explicit is parsed
    case-insensitively against a small allow-list.
    """
    if v is None:
        return False
    if v == "":
        return True
    return v.strip().lower() in ("1", "true", "yes", "on")


@app.get("/health", include_in_schema=False)
@app.get("/health/deep", include_in_schema=False)
async def health_check(request: Request = None):
    """Structured health report. Exposed publicly, no auth, no rate limit.

    Deep mode (``/health/deep`` or ``/health?deep=...``) additionally probes
    Redis + every subproduct dashboard. Subproduct failures are non-critical
    so a downed dashboard never trips the load balancer.
    """
    import datetime as _dt

    # Decide deep-mode once: explicit path alias or a truthy query param.
    deep = False
    if request is not None:
        if request.url.path.endswith("/deep"):
            deep = True
        else:
            qp = request.query_params
            if "deep" in qp and _is_truthy_query(qp.get("deep")):
                deep = True

    checks: dict = {}
    errors: list[str] = []
    deep_meta: dict = {}

    db_status, db_err = _check_database()
    checks["database"] = db_status
    # Tests assert ``db`` is the canonical key and ``database`` a legacy alias.
    checks["db"] = db_status
    if db_err:
        errors.append(f"database: {db_err}")

    static_status, static_err = _check_static_dir()
    checks["static"] = static_status
    if static_err:
        errors.append(f"static: {static_err}")

    dash_status, dash_err = _check_dashboards()
    checks["dashboards"] = dash_status
    if dash_err:
        errors.append(f"dashboards: {dash_err}")

    enc_status, enc_err = _check_encryption_key()
    checks["encryption"] = enc_status
    if enc_err:
        errors.append(f"encryption: {enc_err}")

    gate_status, gate_err = _check_site_access_token()
    checks["gate"] = gate_status
    if gate_err:
        errors.append(f"gate: {gate_err}")

    sched_status, sched_err = _check_scheduler()
    checks["scheduler"] = sched_status
    if sched_err:
        errors.append(f"scheduler: {sched_err}")

    checks["email"] = _check_email_dry_run()

    if deep:
        redis_status, redis_err = _check_redis()
        checks["redis"] = redis_status
        if redis_err:
            errors.append(f"redis: {redis_err}")

        sub_status, sub_meta = _check_subproducts()
        checks["subproducts"] = sub_status
        deep_meta["subproducts"] = sub_meta

    # Critical checks — any error here downgrades the whole report to "error"
    # and returns HTTP 503. Non-critical warnings only flip to "degraded".
    # ``subproducts`` is intentionally non-critical: the LB should not pull
    # the gateway out of rotation because a downstream dashboard is wobbly.
    critical = {"database", "gate"}
    critical_failed = any(
        checks[k] == "error" for k in critical if k in checks
    )
    any_error = any(v == "error" for v in checks.values())

    if critical_failed:
        status = "error"
        http_status = 503
    elif any_error:
        status = "degraded"
        http_status = 200
    else:
        status = "ok"
        http_status = 200

    payload = {
        "status": status,
        "service": APP_SERVICE_NAME,
        "version": APP_VERSION,
        "environment": APP_ENVIRONMENT,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "git_sha": APP_GIT_SHA,
        "deployed_at": APP_DEPLOYED_AT,
        "checks": checks,
    }
    if deep_meta:
        payload["deep"] = deep_meta
    if errors and not IS_PRODUCTION:
        # Only reveal the specific failure strings outside production.
        payload["errors"] = errors

    return JSONResponse(
        payload,
        status_code=http_status,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ── Apex routes (login / signup / my dashboards / billing) ────────────────────


@app.get("/", response_class=HTMLResponse)
async def prerelease_page(request: Request):
    """Public pre-release page — no auth, no gate cookie needed.

    On a known sub-brand subdomain:
      - unauthenticated visitor → branded subproduct landing page
      - authenticated visitor   → reverse-proxy to the backend dashboard
    """
    sub = get_subdomain(request)
    if sub:
        from subproduct import SUBPRODUCTS as _SP
        if sub in _SP and current_user(request) is None:
            return _render_subproduct_landing(request, sub)
        return await proxy_request(request, "/")
    return render_page("prerelease", request=request)


def _render_subproduct_landing(request: Request, slug: str) -> HTMLResponse:
    """Build and return the subproduct marketing page for *slug*.

    Stats are pulled from what's cheap today. Missing stats render as em-
    dashes rather than breaking the template — the page has to work on day
    zero before any of the subproduct-specific pipelines are wired up.
    """
    from subproduct import SUBPRODUCTS as _SP, landing_context, DASHBOARD_KEY_FOR_SLUG
    cfg = _SP[slug]
    stats: dict[str, object] = {}
    dashboard_key = DASHBOARD_KEY_FOR_SLUG[slug]
    try:
        if hasattr(db, "count_active_dashboard_subscriptions"):
            stats["subscribers"] = db.count_active_dashboard_subscriptions(dashboard_key)
    except Exception:
        pass
    if slug == "traders":
        try:
            with db.conn() as c:
                row = c.execute(
                    "SELECT COUNT(DISTINCT wallet_address) AS n FROM whale_positions"
                ).fetchone()
            stats["wallets"] = int(row["n"]) if row else 0
        except Exception:
            pass

    ctx = landing_context(slug, stats)

    floating_html = "".join(
        f"<span>{html.escape(str(n))}</span>" for n in ctx["floating_numbers"]
    )
    # Stat pills: split each format string into literal label fragments
    # and {placeholder} numeric values so the template can render labels
    # in Inter and numbers in Geist Mono. Walking the catalogue template
    # (not the formatted output) lets us know which spans came from
    # `{...}` versus surrounding label text.
    pills_html = _format_stat_pills(_SP[slug]["stat_pills"], stats)

    # Tabs: each tab listed in SUBPRODUCTS[slug]["tabs"] becomes both a
    # nav button and a paired panel. Panels carry a one-line description
    # so the section is never empty before the per-tab content ships.
    tab_buttons_html, tab_panels_html = _format_subproduct_tabs(
        ctx["tabs"], ctx["name"]
    )

    # Bundle-math figures for the Pro price card. Summed from the live
    # catalogue so the page never lies if a subproduct's price changes.
    bundle_sum_usd = sum(float(v["price_usd"]) for v in _SP.values())
    bundle_sum_gbp = sum(float(v["price_gbp"]) for v in _SP.values())
    bundle_save_gbp = max(0.0, bundle_sum_gbp - 180.0)

    # Per-slug accent from gateway/config.json (mapped via dashboard_key).
    # This is the ONLY hue on the page — applied to the 10x10 hero dot via
    # the inline --sp-accent CSS var on <body>. Fallback to neutral so a
    # missing config entry never breaks the page.
    accent_hex = (DASHBOARDS.get(dashboard_key) or {}).get("accent", "#000000")

    # Cross-link discovery bar — five other subproducts (excluding the
    # current slug) picked at random per-request so repeat visitors see
    # different neighbours over time. Cap is min(5, available) so the page
    # still renders if the catalogue ever shrinks below 6 entries. All
    # interpolated values pass through html.escape; the resulting HTML
    # lands in the template via `{{ raw_other_subproducts }}`.
    import random as _random
    other_slugs = [s for s in _SP if s != slug]
    _random.shuffle(other_slugs)
    other_picks = other_slugs[: min(5, len(other_slugs))]
    other_links_html = "".join(
        (
            f'<a class="sp-other-link" href="https://{html.escape(s)}.narve.ai/">'
            f'<span class="sp-other-name">{html.escape(_SP[s]["name"])}</span>'
            f'<span class="sp-other-tagline">{html.escape(_SP[s]["tagline"])}</span>'
            f'</a>'
        )
        for s in other_picks
    )

    return render_page(
        "subproduct_landing",
        request=request,
        subproduct_slug=ctx["slug"],
        subproduct_slug_upper=ctx["slug"].upper(),
        subproduct_name=ctx["name"],
        subproduct_tagline=ctx["tagline"],
        subproduct_hero_sub=ctx["hero_sub"],
        raw_hero_headline=_format_hero_headline(ctx["hero_headline"]),
        subproduct_price_usd=f"{ctx['price_usd']:.2f}",
        subproduct_price_gbp=f"{ctx['price_gbp']:.2f}",
        subproduct_dashboard_key=dashboard_key,
        subproduct_animation_style=ctx.get("animation_style", "drift"),
        subproduct_accent=accent_hex,
        subproduct_count=str(len(_SP)),
        bundle_sum_usd=f"{bundle_sum_usd:.2f}",
        bundle_sum_gbp=f"{bundle_sum_gbp:.2f}",
        bundle_save_gbp=f"{bundle_save_gbp:.2f}",
        raw_floating_numbers=floating_html,
        raw_stat_pills=pills_html,
        raw_tab_buttons=tab_buttons_html,
        raw_tab_panels=tab_panels_html,
        raw_other_subproducts=other_links_html,
    )


def _format_stat_pills(templates: list, stats: dict) -> str:
    """Render the stat-pill row with typographic separation of labels and
    numbers. Inter for the surrounding label text, Geist Mono for any
    value coming from a {placeholder}. Missing stats fall back to an
    em-dash so the page never 500s on a dry catalogue.
    """
    import string
    parts: list[str] = []
    for tpl in templates:
        # Build piecewise HTML by walking literal/field-name pairs.
        chunks: list[str] = ['<span class="sp-pill">']
        for literal, field_name, _, _ in string.Formatter().parse(tpl):
            if literal:
                chunks.append(
                    f'<span class="sp-pill-label">{html.escape(literal)}</span>'
                )
            if field_name:
                value = stats.get(field_name, "—")
                chunks.append(
                    f'<span class="sp-pill-num">{html.escape(str(value))}</span>'
                )
        chunks.append("</span>")
        parts.append("".join(chunks))
    return "".join(parts)


def _format_subproduct_tabs(tabs: list, product_name: str) -> tuple[str, str]:
    """Render tab buttons + matching panels for the subproduct landing.

    No per-tab content yet — each panel carries a placeholder card so the
    section reads as intentional ("Coming soon — preview the [tab]") rather
    than empty. Returns ``(buttons_html, panels_html)``.
    """
    buttons: list[str] = []
    panels: list[str] = []
    for i, label in enumerate(tabs):
        safe = html.escape(str(label))
        tab_id = f"sp-tab-{i}"
        panel_id = f"sp-panel-{i}"
        buttons.append(
            f'<button type="button" class="sp-tab" role="tab" '
            f'id="{tab_id}" aria-controls="{panel_id}" '
            f'aria-selected="{"true" if i == 0 else "false"}" '
            f'tabindex="{0 if i == 0 else -1}">{safe}</button>'
        )
        hidden_attr = "" if i == 0 else " hidden"
        panels.append(
            f'<div class="sp-tab-panel" id="{panel_id}" role="tabpanel" '
            f'aria-labelledby="{tab_id}"{hidden_attr}>'
            f'<div class="sp-tab-card">'
            f'<h3>{safe}</h3>'
            f'<p>The {safe.lower()} view inside {html.escape(product_name)} — '
            f'live data, dense tables, no clutter. Sign in to open.</p>'
            f'<span class="sp-tab-meta">Available with access</span>'
            f'</div></div>'
        )
    return "".join(buttons), "".join(panels)


def _format_hero_headline(text: str) -> str:
    """Turn a "First line / Second line" string into safe HTML with a <br>.

    The slash stays visible in the source copy but renders on two display
    lines — matches the main landing's big serif headline style.
    """
    if "/" in text:
        first, _, second = text.partition("/")
        return f"{html.escape(first.strip())} <br>{html.escape(second.strip())}"
    return html.escape(text)


def _render_landing() -> HTMLResponse:
    """Public landing page — shown to unauthenticated visitors at apex."""
    # Build feature cards from the configured dashboards so marketing copy
    # always matches what's actually live.
    card_html_parts = []
    for _key, cfg in DASHBOARDS.items():
        card_html_parts.append(f"""
        <div class="landing-dash" style="--accent: {cfg['accent']}">
          <div class="landing-dash-dot"></div>
          <div class="landing-dash-title">{html.escape(cfg['display_name'])}</div>
          <div class="landing-dash-desc">{html.escape(cfg['description'])}</div>
          <div class="landing-dash-price">${cfg['monthly_cents']/100:.0f}/mo</div>
        </div>
        """)
    return render_page(
        "landing",
        dashboard_count=str(len(DASHBOARDS)),
        dashboard_cards="".join(card_html_parts),
    )


# ── SEO: robots.txt / sitemap.xml / /narve ─────────────────────────────
#
# Minimal search-engine-facing endpoints. Paths are already whitelisted in
# _PUBLIC_PATHS so GateMiddleware lets them through.


@app.get("/robots.txt")
async def seo_robots_txt(request: Request):
    """Static robots.txt — allow indexing of public pages, block auth/admin/API.

    On a sub-brand subdomain (sports.narve.ai, crypto.narve.ai, …) we emit
    a minimal subdomain-scoped robots.txt that points at that subdomain's
    own sitemap. Each sub-brand is its own Google property.
    """
    sub = get_subdomain(request)
    if sub:
        from subproduct import SUBPRODUCTS as _SP
        if sub in _SP:
            body = (
                "User-agent: *\n"
                "Allow: /\n"
                "Disallow: /admin/\n"
                "Disallow: /api/\n"
                "Disallow: /auth/\n"
                "Disallow: /dashboard/\n"
                "Disallow: /gate\n"
                f"Sitemap: https://{sub}.narve.ai/sitemap.xml\n"
            )
            return Response(body, media_type="text/plain; charset=utf-8")

    apex = _request_apex(request) or DOMAIN
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /pricing\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /dpa\n"
        "Allow: /about\n"
        "Allow: /how-it-works\n"
        "Allow: /methodology\n"
        "Allow: /faq\n"
        "Allow: /narve\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /dashboards\n"
        "Disallow: /dashboard/\n"
        "Disallow: /login\n"
        "Disallow: /signup\n"
        "Disallow: /register\n"
        "Disallow: /settings/\n"
        "Disallow: /embed/\n"
        "Disallow: /invite/\n"
        f"Sitemap: https://{apex}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain; charset=utf-8")


# Public pages included in the apex sitemap. Priorities are hand-tuned to
# match crawl-importance: homepage highest, legal pages lowest, source/
# calendar pages in between since they change frequently.
#
# NOTE: sub-brand subdomains (sports.narve.ai, crypto.narve.ai, …) are
# DELIBERATELY excluded from this list. Each sub-brand is registered as
# its own Google property and serves its own subdomain-scoped sitemap
# from the same /sitemap.xml route below (see the get_subdomain() branch).
# Do not add subdomain root URLs here — that would cross-link properties
# and dilute the per-subdomain canonical signals.
#
# There is no static gateway/static/sitemap.xml; the dynamic route below
# is the single source of truth (StaticFiles is mounted at
# /_gateway_static/, so a file at that path would not be served anyway).
_SITEMAP_ENTRIES = [
    ("/",               "weekly",  "1.0"),
    ("/landing",        "weekly",  "0.9"),
    ("/pricing",        "monthly", "0.8"),
    ("/about",          "monthly", "0.8"),
    ("/how-it-works",   "monthly", "0.8"),
    ("/methodology",    "monthly", "0.7"),
    ("/faq",            "monthly", "0.7"),
    ("/changelog",      "weekly",  "0.7"),
    ("/narve",          "monthly", "0.7"),
    ("/calendar",       "hourly",  "0.7"),
    ("/terms",          "yearly",  "0.3"),
    ("/privacy",        "yearly",  "0.3"),
    ("/dpa",            "yearly",  "0.3"),
]


@app.get("/sitemap.xml")
async def seo_sitemap_xml(request: Request):
    """Auto-generated sitemap. Called on every crawl; cheap enough to render live.

    Sub-brand subdomains (sports.narve.ai, crypto.narve.ai, …) return a
    minimal sitemap canonical to the subdomain itself. The sub-brand
    landing page is the only public URL on these hosts, and treating each
    subdomain as its own Google property requires they not link back to
    the apex sitemap.
    """
    import datetime as _dt
    sub = get_subdomain(request)
    if sub:
        from subproduct import SUBPRODUCTS as _SP
        if sub in _SP:
            today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            base = f"https://{sub}.narve.ai"
            parts = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
                f"<url><loc>{base}/</loc>"
                f"<lastmod>{today}</lastmod>"
                f"<changefreq>weekly</changefreq>"
                f"<priority>1.0</priority></url>",
            ]
            # Per-subproduct extra public pages — pulled from the
            # `sitemap_pages` field on SUBPRODUCTS[sub] so each sub-brand
            # owns its own crawl surface. Field is a list of
            # (path, changefreq, priority) tuples; an absent or empty list
            # leaves the sitemap as just the subdomain root. Anything added
            # here MUST resolve to a 200 on this subdomain — gated or
            # proxy-only paths will create soft-404s in Search Console.
            for path, freq, priority in _SP[sub].get("sitemap_pages", ()):
                parts.append(
                    f"<url><loc>{base}{path}</loc>"
                    f"<lastmod>{today}</lastmod>"
                    f"<changefreq>{freq}</changefreq>"
                    f"<priority>{priority}</priority></url>"
                )
            parts.append("</urlset>")
            return Response("".join(parts), media_type="application/xml; charset=utf-8")

    apex = _request_apex(request) or DOMAIN
    today = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for path, freq, priority in _SITEMAP_ENTRIES:
        parts.append(
            f"<url><loc>https://{apex}{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )
    # Dynamic source profile pages — only sources whose credibility has
    # been unlocked (>=10 predictions resolved) are crawl-worthy. Unrated
    # sources show a sparse "not enough signal yet" stub, so we keep them
    # out of the sitemap to avoid Google indexing thin content.
    try:
        with db.conn() as _c:
            rated = _c.execute(
                "SELECT source_handle FROM source_credibility "
                "WHERE accuracy_unlocked = 1 "
                "ORDER BY global_credibility DESC LIMIT 5000",
            ).fetchall()
        for row in rated:
            handle = row[0] if not isinstance(row, dict) else row.get("source_handle")
            if not handle:
                continue
            parts.append(
                f"<url><loc>https://{apex}/sources/{handle}</loc>"
                f"<lastmod>{today}</lastmod>"
                f"<changefreq>weekly</changefreq>"
                f"<priority>0.6</priority></url>"
            )
    except Exception:
        pass
    parts.append('</urlset>')
    return Response("".join(parts), media_type="application/xml; charset=utf-8")


@app.get("/narve", response_class=HTMLResponse)
async def seo_narve_page(request: Request):
    """Brand-query landing page. URL+H1+title all contain "narve" so Google's
    brand-disambiguation signals point here for the query "narve"."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/narve")
    return render_page("narve-brand", request=request)


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Marketing landing page — the old homepage, now at /landing."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/landing")
    return _render_landing()


# ── Vulnerability disclosure (RFC 9116) ──────────────────────────────────────
#
# Publishes a contact address for security researchers so reports reach the
# right inbox instead of getting lost in /support. Expires is required and
# should be refreshed annually. The route is in _PUBLIC_PATHS so the gate
# doesn't swallow it.
_SECURITY_TXT_CONTACT = os.environ.get("SECURITY_TXT_CONTACT", "mailto:security@narve.ai")
_SECURITY_TXT_EXPIRES = os.environ.get("SECURITY_TXT_EXPIRES", "2027-04-08T00:00:00Z")


@app.get("/.well-known/security.txt")
async def security_txt(request: Request):
    body = (
        f"Contact: {_SECURITY_TXT_CONTACT}\n"
        f"Expires: {_SECURITY_TXT_EXPIRES}\n"
        "Preferred-Languages: en\n"
        "Policy: https://narve.ai/security\n"
    )
    return Response(content=body, media_type="text/plain; charset=utf-8")


# ── Site-wide access gate ────────────────────────────────────────────────────


@app.get("/gate", response_class=HTMLResponse)
async def gate_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    # Already past the gate? Go to landing.
    if has_gate_access(request):
        return RedirectResponse("/landing", status_code=302)
    return render_page("gate", request=request, error="")


@app.post("/gate")
async def gate_submit(request: Request, token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    if _auth_rate_limited(_get_client_ip(request)):
        return RATE_LIMITED_RESPONSE
    token = _bounded(token, FIELD_MAX["invite_token"], "token")
    if not token:
        return render_page("gate", request=request, error="Invalid token.")
    if not SITE_ACCESS_TOKEN:
        return render_page("gate", request=request, error="Gate not configured. Contact admin.")
    if not hmac.compare_digest(token, SITE_ACCESS_TOKEN):
        return render_page("gate", request=request, error="Invalid token.")
    # Correct — set gate cookie and redirect to landing
    response = RedirectResponse("/landing", status_code=302)
    set_gate_cookie(response, request)
    return response


# Invite-token entry (/invite, /token) removed 2026-05-15 — the
# perimeter is now /gate (SITE_ACCESS_TOKEN). Anyone past /gate may
# create an account directly at /register.


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Standalone email + password sign-in page.

    The /gate perimeter (SITE_ACCESS_TOKEN) protects the apex. Anyone
    past the gate may attempt to sign in here directly — no invite
    token, no pre-flight cookie. Already-authenticated visitors are
    bounced to /dashboards.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")

    # Already logged in? Straight to dashboard.
    from auth.guards import read_hardened_session
    if read_hardened_session(request) or current_user(request):
        return RedirectResponse("/dashboards", status_code=302)

    query_success = request.query_params.get("reset")
    success_html = ""
    if query_success == "success":
        success_html = "Password updated. Please sign in with your new password."
    return render_page(
        "login",
        request=request,
        error="",
        email_hint="",
        raw_success=success_html,
    )


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
):
    """Server-rendered form fallback for /login.

    Mirrors the password-verification path of the JSON ``/auth/login``
    endpoint (defined in server_features.py) — same per-IP + per-email
    rate limits, same ``db.verify_password`` check, same hardened
    session cookie. JS-enabled clients continue to use /auth/login via
    fetch; this handler exists so JS-disabled browsers and curl-style
    clients have a working flow as well.

    CSRF: enforced by the global CSRFMiddleware (form ``_csrf`` field).
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")

    ip = _get_client_ip(request)
    if _auth_rate_limited(ip):
        return RATE_LIMITED_RESPONSE
    if _is_rate_limited(f"{ip}:login-auth", limit=10, window=300):
        return render_page(
            "login", request=request,
            error="Too many attempts. Try again in a few minutes.",
            email_hint="", raw_success="",
        )

    email = _bounded(email, FIELD_MAX["email"], "email").lower()
    if len(password) > FIELD_MAX["password"]:
        return render_page(
            "login", request=request,
            error="Invalid email or password.",
            email_hint="", raw_success="",
        )
    if not email or not is_valid_email(email) or not password:
        return render_page(
            "login", request=request,
            error="Invalid email or password.",
            email_hint=email, raw_success="",
        )

    # Per-email rate limit (credential-stuffing defence across rotating IPs).
    if _is_rate_limited(f"email:{email}:login", limit=5, window=600):
        return render_page(
            "login", request=request,
            error="Too many attempts for this account. Try again later.",
            email_hint=email, raw_success="",
        )

    user = db.get_user_by_email(email)
    # Generic error message either way — don't leak whether the email exists.
    if not user:
        return render_page(
            "login", request=request,
            error="Invalid email or password.",
            email_hint=email, raw_success="",
        )
    if user["suspended"]:
        return RedirectResponse("/suspended", status_code=302)
    if not db.verify_password(password, user["password_hash"], user["password_salt"]):
        log.info("login.failure: user_id=%d ip=%s", user["id"], ip)
        return render_page(
            "login", request=request,
            error="Invalid email or password.",
            email_hint=email, raw_success="",
        )

    # Opportunistic PBKDF2 iteration upgrade — mirrors /auth/login.
    try:
        if db.password_needs_rehash(password, user["password_hash"], user["password_salt"]):
            new_hash, new_salt = db._hash_password(password)
            with db.conn() as c:
                c.execute(
                    "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                    (new_hash, new_salt, user["id"]),
                )
            log.info("login: upgraded PBKDF2 iterations for user_id=%d", user["id"])
    except Exception as exc:
        log.warning("login: rehash-on-login failed for user_id=%d: %s", user["id"], exc)

    # Issue BOTH the legacy session and the hardened narve_session cookie,
    # matching server_features._issue_hardened_session.
    legacy_token = db.create_session(user["id"])
    try:
        db.mark_session_two_fa_verified(legacy_token)
    except Exception:
        pass
    ua = request.headers.get("user-agent", "")[:256]
    raw_hardened = db.create_user_session(
        user["id"], ip_address=ip, user_agent=ua, legacy_token=legacy_token,
    )

    response = RedirectResponse("/dashboards", status_code=302)
    set_session_cookie(response, legacy_token, request)
    try:
        from auth.cookies import set_session_cookie_hardened, clear_pending_token_cookie
        set_session_cookie_hardened(response, raw_hardened, request)
        clear_pending_token_cookie(response, request)
    except Exception:
        log.exception("login: hardened-session cookie issuance failed")
    # Rotate CSRF on successful login.
    try:
        _set_csrf_cookie(response, _generate_csrf_token(), request)
    except Exception:
        pass
    log.info("login: user_id=%d success ip=%s (form-post)", user["id"], ip)
    return response


# ── Two-factor authentication ────────────────────────────────────────────────


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")
    return render_page("forgot-password", request=request, error="", success="")


@app.post("/forgot-password")
async def forgot_password_submit(request: Request, email: str = Form("")):
    """Legacy form-post entry to the email-link reset flow.

    The old token-gated reset path (which required an invite_token to
    set a new password inline) was removed alongside the invite-token
    system on 2026-05-15. This handler now mirrors the JSON
    ``/auth/forgot-password`` endpoint: rate-limited, never reveals
    whether the email is registered, and triggers a password-reset
    email if a matching account exists. The actual email sender comes
    from ``server_features`` so the two routes stay in lockstep.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")

    ip = _get_client_ip(request)
    if _auth_rate_limited(ip):
        return RATE_LIMITED_RESPONSE

    email = _bounded(email, FIELD_MAX["email"], "email").lower()
    generic_success = (
        "If an account with that email exists, a password-reset link "
        "has been sent. Check your inbox."
    )

    # Per-IP cap — mirrors /auth/forgot-password.
    if _is_rate_limited(f"{ip}:forgot-password", limit=3, window=3600):
        return render_page("forgot-password", request=request, error="", success=generic_success)

    if not email or not is_valid_email(email):
        return render_page("forgot-password", request=request, error="", success=generic_success)

    # Per-email cap (hashed key so raw email never persists).
    import hashlib as _h
    email_key = _h.sha256(email.encode()).hexdigest()[:24]
    if _is_rate_limited(f"forgot-password:{email_key}", limit=3, window=3600):
        return render_page("forgot-password", request=request, error="", success=generic_success)

    user = db.get_user_by_email(email)
    if user and not user["suspended"]:
        try:
            from server_features import _hash_reset_token, _APP_URL, enqueue_email
            raw = secrets.token_urlsafe(32)
            token_hash = _hash_reset_token(raw)
            now = int(time.time())
            with db.conn() as c:
                c.execute(
                    "INSERT INTO password_resets (user_id, token, token_hash, created_at, expires_at, used) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (user["id"], raw[:32], token_hash, now, now + 3600),
                )
            reset_url = f"{_APP_URL}/reset-password?token={raw}"
            try:
                await enqueue_email(
                    to=email,
                    template="password_reset",
                    context={
                        "reset_url": reset_url,
                        "display_name": user["username"] or email.split("@")[0],
                    },
                    tags=["password_reset", "transactional"],
                )
            except Exception as exc:
                log.warning("forgot-password: email enqueue failed: %s", exc)
        except Exception:
            log.exception("forgot-password: reset-token issuance failed for user_id=%d", user["id"])

    return render_page("forgot-password", request=request, error="", success=generic_success)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Legacy alias — account creation now happens at /register."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    return RedirectResponse("/register", status_code=302)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.post("/signup")
async def signup_submit(request: Request):
    """Legacy POST /signup — replaced by POST /auth/register (JSON)."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    return RedirectResponse("/register", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/logout")
    # Capture the user BEFORE deleting the session so we can audit admin logout
    user = current_user(request)
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.delete_session(token)
    # Also revoke the hardened session cookie if present.
    try:
        from auth.cookies import SESSION_COOKIE as _HARDENED_COOKIE
        from auth.cookies import clear_session_cookie_hardened, clear_pending_token_cookie
        hardened_raw = request.cookies.get(_HARDENED_COOKIE)
        if hardened_raw:
            db.revoke_user_session_by_token(hardened_raw)
    except Exception:
        pass
    try:
        if user and user.get("is_admin") and not user.get("_dev_bypass"):
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user.get("user_id"),
                admin_email=user.get("email"),
                action=_audit.AuditAction.ADMIN_LOGOUT,
                request=request,
            )
    except Exception:
        pass
    response = RedirectResponse("/login", status_code=302)
    try:
        clear_session_cookie_hardened(response, request)
        clear_pending_token_cookie(response, request)
    except Exception:
        pass
    # Rotate CSRF token on logout so the next session on this browser starts
    # with a fresh secret. Without this, a token captured earlier remains
    # valid until its TTL expires and could be replayed by the next user
    # signing in on the same machine.
    _set_csrf_cookie(response, _generate_csrf_token(), request)
    clear_session_cookie(response, request)
    return response


def _subscription_pause_status(user_id: int, now_ts: int) -> dict:
    """Return {paused: bool, until_ts: int|None, until_str: str|None} for
    the subscription-pause check. Safe to call before migration 094 has
    landed — absent column/rows = not paused."""
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT subscription_paused_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except Exception:
        return {"paused": False, "until_ts": None, "until_str": None}
    if not row or not row["subscription_paused_until"]:
        return {"paused": False, "until_ts": None, "until_str": None}
    until_str = row["subscription_paused_until"]
    try:
        with db.conn() as c:
            epoch_row = c.execute(
                "SELECT CAST(strftime('%s', ?) AS INTEGER) AS e",
                (until_str,),
            ).fetchone()
        until_ts = int(epoch_row["e"] if epoch_row and epoch_row["e"] else 0)
    except Exception:
        until_ts = 0
    if until_ts <= now_ts:
        # Pause window expired — clear the column so subsequent calls are
        # fast-pathed. Re-enter the billing portal to resubscribe.
        try:
            with db.conn() as c:
                c.execute(
                    "UPDATE users SET subscription_paused_until = NULL WHERE id = ?",
                    (user_id,),
                )
        except Exception:
            pass
        return {"paused": False, "until_ts": None, "until_str": None}
    return {"paused": True, "until_ts": until_ts, "until_str": until_str}


@app.get("/dashboards", response_class=HTMLResponse)
async def my_dashboards(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/dashboards")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Pause gate — paused users can log in and manage billing, but the
    # dashboard cards are soft-locked with a single "Paused until X,
    # resume now?" banner at the top.
    pause = _subscription_pause_status(user["user_id"], int(time.time()))
    if pause["paused"]:
        import datetime as _dt_pause
        until_str = _dt_pause.datetime.utcfromtimestamp(pause["until_ts"]).strftime("%B %d, %Y")
        body = (
            '<div style="max-width:560px;margin:80px auto;padding:32px;'
            'background:var(--bg-raised);border:1px solid var(--border);'
            'border-radius:12px;text-align:center">'
            '<h1 style="margin:0 0 12px;font-family:var(--font-display);font-size:24px">'
            'Subscription paused</h1>'
            f'<p style="margin:0 0 24px;color:var(--text-secondary)">'
            f'Your subscription is paused until <strong>{html.escape(until_str)}</strong>. '
            'Resume anytime to regain access to your dashboards.</p>'
            '<form method="post" action="/settings/billing/resume" style="display:inline">'
            '<button class="btn btn-primary" type="submit">Resume now</button>'
            '</form>'
            ' <a href="/settings/billing" class="btn btn-outline" style="margin-left:8px">Manage billing</a>'
            '</div>'
        )
        return HTMLResponse(body)

    # Perf audit #3: cache the DB-heavy subscription read per user (60s).
    # The subs dict drives the dashboard card grid + plan-info; the page-frame
    # (sidebar, badges, links) is rebuilt per request below so personalisation
    # stays intact. Defensive import so a broken cache layer doesn't break the
    # page — the un-cached path is identical to the original handler.
    user_id = user["user_id"]
    is_admin_user = bool(user.get("is_admin"))
    now = int(time.time())
    try:
        from cache import cache as _async_cache

        async def _build_dashboards_data() -> dict:
            rows = [dict(r) for r in db.list_subscriptions(user_id)]
            return {"subs_list": rows}

        _cached = await _async_cache.get_or_set(
            f"dashboards:user:{user_id}", _build_dashboards_data, ttl_seconds=60,
        )
        subs = {s["dashboard_key"]: s for s in _cached["subs_list"]}
    except Exception:
        subs = {s["dashboard_key"]: dict(s) for s in db.list_subscriptions(user_id)}
    pinfo = _user_plan_info(user, subs, now)
    local_mode = is_local_host(request)
    # Dashboard cards link to <sub>.<apex> on whichever apex the user is
    # currently browsing, so a visitor on narve.ai stays on narve.ai.
    apex = _request_apex(request) or DOMAIN
    cards_html = []
    for key, cfg in DASHBOARDS.items():
        has_sub = _is_sub_active(subs.get(key), is_admin_user)
        # Pro plan or admin unlocks everything
        if pinfo["plan"] == "pro" or is_admin_user:
            has_sub = True
        active_badge = (
            '<span class="badge badge-active">Active</span>' if has_sub
            else '<span class="badge badge-locked">Locked</span>'
        )
        if has_sub:
            if local_mode:
                open_url = f"http://localhost:{cfg['target']}"
            else:
                open_url = f"https://{cfg['subdomain']}.{apex}"
            card_href = open_url
            card_attrs = ' target="_blank" rel="noopener"'
            card_class = "dash-card"
            action_label = "Open ↗"
        else:
            card_href = f"/billing?dashboard={key}"
            card_attrs = ""
            card_class = "dash-card dash-card--locked"
            action_label = "Unlock →"

        cards_html.append(f"""
        <a class="{card_class}" href="{card_href}"{card_attrs} style="--accent: {cfg['accent']}">
          <div class="dash-card-head">
            <span class="dash-accent-dot" aria-hidden="true"></span>
            {active_badge}
          </div>
          <h3 class="dash-card-title">{cfg['display_name']}</h3>
          <p class="dash-card-desc">{cfg['description']}</p>
          <div class="dash-card-foot">
            <span class="dash-card-price">&pound;{cfg['monthly_cents']/100:.0f}/mo &middot; &pound;{cfg['annual_cents']/100:.0f}/yr</span>
            <span class="dash-card-action">{action_label}</span>
          </div>
        </a>
        """)

    # Credits badge — monochrome per narve-design skill: no green/amber
    # status hue, no absolute positioning. The new layout puts the badge
    # inline as `.page-actions` inside the flex `.page-header`, which
    # narve-redesign.css aligns to the right on desktop and stacks below
    # the title on mobile so it never overlaps Instrument Serif copy.
    credits_badge = ""
    if is_admin_user:
        credits_badge = '<div class="page-actions"><span class="plan-badge">All Access</span></div>'
    elif pinfo["plan"] == "pro":
        credits_badge = '<div class="page-actions"><span class="plan-badge">Pro · All Unlocked</span></div>'
    elif pinfo["plan"] == "trader":
        used = pinfo["active_count"]
        total = PLAN_DEFS["trader"]["credits"]
        remaining = total - used
        # Differentiation through weight, not hue: depleted credits get
        # `data-state="depleted"` which narve-redesign promotes to bold
        # primary text on a sturdier border.
        state_attr = ' data-state="depleted"' if remaining <= 0 else ''
        credits_badge = (
            f'<div class="page-actions">'
            f'<span class="plan-badge"{state_attr}>Trader · {remaining}/{total} credits</span>'
            f'</div>'
        )
    else:
        credits_badge = (
            '<div class="page-actions">'
            '<span class="plan-badge plan-badge--noplan">No plan · '
            '<a href="/billing">Subscribe</a></span>'
            '</div>'
        )

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    # Signal Search link for Pro users
    signal_link = ""
    if pinfo["plan"] == "pro" or is_admin_user:
        signal_link = '<a href="/signal-search">Signal Search</a>'
    nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="dashboards",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_signal_search_link=signal_link,
        raw_nav_role=nav_role,
    )
    return render_page(
        "dashboards", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        dashboard_cards="".join(cards_html),
        raw_credits_badge=credits_badge,
        raw_signal_search_link=signal_link,
        raw_admin_link=admin_link,
        raw_nav_role=nav_role, _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )


# Plan definitions — Trader gets 3 dashboard credits, Pro gets all
PLAN_DEFS = {
    "trader": {"label": "Trader", "credits": 3, "monthly": 75, "annual": 765, "monthly_usd": 99, "annual_usd": 999},
    "pro": {"label": "Pro", "credits": len(DASHBOARDS), "monthly": 180, "annual": 1836, "monthly_usd": 229, "annual_usd": 1999},
}
TRADING_ADDON = {"label": "Trading Access", "monthly": 25, "annual": 255, "monthly_usd": 29, "annual_usd": 299}


def _user_plan_info(user: dict, subs: dict, now: int) -> dict:
    """Determine the user's current plan tier and active dashboard count."""
    if not isinstance(subs, dict):
        subs = {}
    is_admin = bool(user.get("is_admin"))
    active_keys = []
    plan_name = None
    interval = None
    expires_at = None
    downgrading = False

    # Check if any dashboard sub is marked as downgrading
    for s in subs.values():
        if s and (s["plan"] or "").startswith("pro_downgrading"):
            downgrading = True
            break

    # Check __plan__ sentinel first (Trader plan marker)
    plan_sub = subs.get("__plan__")
    if plan_sub and plan_sub["status"] == "active" and (not plan_sub["expires_at"] or plan_sub["expires_at"] > now):
        raw = plan_sub["plan"] or ""
        if raw.startswith("trader"):
            plan_name = "trader"
        elif raw.startswith("pro"):
            plan_name = "pro"
        if "_annual" in raw:
            interval = "annual"
        elif "_monthly" in raw:
            interval = "monthly"
        expires_at = plan_sub["expires_at"]

    for key in DASHBOARDS:
        s = subs.get(key)
        if _is_sub_active(s, is_admin):
            active_keys.append(key)
            # Infer plan from dashboard subs — Pro always wins over Trader
            if s:
                raw = s["plan"] or ""
                if raw.startswith("pro") and plan_name != "pro":
                    plan_name = "pro"
                    if "_annual" in raw:
                        interval = "annual"
                    elif "_monthly" in raw:
                        interval = "monthly"
                    expires_at = s["expires_at"]
                elif raw.startswith("trader") and not plan_name:
                    plan_name = "trader"
                    if "_annual" in raw:
                        interval = "annual"
                    elif "_monthly" in raw:
                        interval = "monthly"
                    expires_at = s["expires_at"]
    return {
        "plan": plan_name,
        "interval": interval,
        "active_keys": active_keys,
        "active_count": len(active_keys),
        "expires_at": expires_at,
        "is_admin": is_admin,
        "downgrading": downgrading,
    }


def _build_plan_card(pinfo: dict) -> str:
    """Build the plan summary card HTML for the billing page."""
    import datetime as _dt
    plan = pinfo["plan"]
    is_admin = pinfo["is_admin"]

    if is_admin and not plan:
        return (
            '<div class="billing-plan-card">'
            '<div class="billing-plan-header">'
            '<span class="billing-plan-name">Admin Access</span>'
            '<span class="billing-plan-badge billing-plan-badge-admin">ADMIN</span>'
            '</div>'
            '<div class="billing-plan-desc">Full access to all dashboards via admin privileges.</div>'
            '</div>'
        )

    if not plan:
        return (
            '<div class="billing-plan-card">'
            '<div class="billing-plan-header">'
            '<span class="billing-plan-name">No Active Plan</span>'
            '</div>'
            '<div class="billing-plan-desc">You don\'t have an active subscription. Choose a plan below to unlock dashboards.</div>'
            '<div class="billing-upgrade-row">'
            '<form method="post" action="/billing/subscribe">'
            '<input type="hidden" name="plan" value="trader"><input type="hidden" name="interval" value="monthly">'
            '<button type="submit" class="billing-upgrade-btn billing-upgrade-primary">Subscribe to Trader — &pound;75/$99/mo</button>'
            '</form>'
            '<form method="post" action="/billing/subscribe">'
            '<input type="hidden" name="plan" value="pro"><input type="hidden" name="interval" value="monthly">'
            '<button type="submit" class="billing-upgrade-btn billing-upgrade-outline">Subscribe to Pro — &pound;180/$229/mo</button>'
            '</form>'
            '<a href="/enquire" class="billing-upgrade-btn" style="line-height:24px;background:transparent;border:1px solid var(--text-muted);color:var(--text-secondary)">Enterprise — Contact Sales</a>'
            '</div>'
            '</div>'
        )

    pdef = PLAN_DEFS.get(plan, PLAN_DEFS["trader"])
    interval = pinfo["interval"] or "monthly"
    price = pdef["annual"] if interval == "annual" else pdef["monthly"]
    period = "/yr" if interval == "annual" else "/mo"
    credits_label = "All dashboards" if plan == "pro" else f'{pdef["credits"]} dashboard credits'
    used = pinfo["active_count"]
    total = pdef["credits"]

    expires_str = ""
    if pinfo["expires_at"]:
        expires_str = f' &middot; Renews {_dt.datetime.fromtimestamp(pinfo["expires_at"], tz=_dt.timezone.utc).strftime("%d %b %Y")}'

    upgrade_row = ""
    if plan == "trader":
        upgrade_row = (
            '<div class="billing-upgrade-row">'
            '<form method="post" action="/billing/subscribe">'
            '<input type="hidden" name="plan" value="pro"><input type="hidden" name="interval" value="monthly">'
            '<button type="submit" class="billing-upgrade-btn billing-upgrade-primary">Upgrade to Pro — &pound;180/$229/mo</button>'
            '</form>'
            '<a href="/enquire" class="billing-upgrade-btn billing-upgrade-outline" style="line-height:24px">Upgrade to Enterprise — Contact Sales</a>'
            '</div>'
        )
    elif plan == "pro":
        downgrade_btn = ""
        if pinfo.get("downgrading"):
            import datetime as _dtx
            end_str = _dtx.datetime.fromtimestamp(pinfo["expires_at"], tz=_dtx.timezone.utc).strftime("%d %b %Y") if pinfo["expires_at"] else "end of period"
            downgrade_btn = (
                f'<div style="font-size:13px;color:var(--amber);background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2);'
                f'border-radius:var(--radius-sm);padding:10px 14px;margin-top:8px">'
                f'Downgrading to Trader on <strong>{end_str}</strong>. You keep Pro access until then.</div>'
            )
        else:
            downgrade_btn = (
                '<form method="post" action="/billing/subscribe">'
                '<input type="hidden" name="plan" value="trader"><input type="hidden" name="interval" value="monthly">'
                '<button type="submit" class="billing-upgrade-btn billing-upgrade-danger" '
                'onclick="return confirm(\'Downgrade to Trader? You\\\'ll keep Pro access until the end of your billing period, then switch to 3 dashboard credits.\')">Downgrade to Trader</button>'
                '</form>'
            )
        upgrade_row = (
            '<div class="billing-upgrade-row">'
            '<a href="/enquire" class="billing-upgrade-btn billing-upgrade-outline" style="line-height:24px">Upgrade to Enterprise — Contact Sales</a>'
            f'{downgrade_btn}'
            '</div>'
        )

    credits_bar = ""
    if plan == "trader":
        pct = min(100, int(used / total * 100)) if total else 0
        bar_color = "var(--green)" if used < total else "var(--amber)"
        credits_bar = (
            f'<div style="margin-top:16px">'
            f'<div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:6px">'
            f'<span>Credits used</span><span>{used} / {total}</span></div>'
            f'<div style="height:6px;background:var(--surface-hover);border-radius:3px;overflow:hidden">'
            f'<div style="height:100%;width:{pct}%;background:{bar_color};border-radius:3px;transition:width 0.3s"></div>'
            f'</div></div>'
        )

    return (
        f'<div class="billing-plan-card">'
        f'<div class="billing-plan-header">'
        f'<span class="billing-plan-name">{pdef["label"]} Plan</span>'
        f'<span class="billing-plan-badge billing-plan-badge-active">ACTIVE</span>'
        f'</div>'
        f'<div class="billing-plan-desc">{credits_label}{expires_str}</div>'
        f'<div class="billing-plan-price">&pound;{price}{period}</div>'
        f'{credits_bar}'
        f'{upgrade_row}'
        f'</div>'
    )


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, dashboard: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        if dashboard and dashboard in DASHBOARDS:
            forwarded_path = "/billing?" + urlencode({"dashboard": dashboard})
        else:
            forwarded_path = "/billing"
        return await proxy_request(request, forwarded_path)
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if dashboard and dashboard not in DASHBOARDS:
        dashboard = None

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin_user = bool(user.get("is_admin"))
    now = int(time.time())

    pinfo = _user_plan_info(user, subs, now)
    plan_card = _build_plan_card(pinfo)

    rows_html = []
    for key, cfg in DASHBOARDS.items():
        s = subs.get(key)
        is_active = _is_sub_active(s, is_admin_user)
        if is_admin_user and not s:
            status_label = '<span style="color:var(--green)">Active (admin)</span>'
        elif is_active:
            status_label = '<span style="color:var(--green)">Active</span>'
        elif s and s["status"] == "active" and s["expires_at"] and s["expires_at"] <= now:
            status_label = '<span style="color:var(--amber)">Expired — renew below</span>'
        elif s and s["status"] == "cancelled":
            status_label = '<span style="color:var(--red)">Cancelled</span>'
        else:
            status_label = '<span style="color:var(--text-muted)">Not subscribed</span>'

        # For Trader plan: show Add button only (credits can't be removed)
        action_btns = ""
        if is_admin_user:
            action_btns = ""  # admins have full access already
        elif pinfo["plan"] == "pro":
            if is_active:
                action_btns = '<span style="font-size:12px;color:var(--green)">Included in Pro</span>'
            else:
                action_btns = '<span style="font-size:12px;color:var(--green)">Included in Pro</span>'
        elif pinfo["plan"] == "trader":
            if is_active:
                action_btns = '<span style="font-size:12px;color:var(--green)">Credit used</span>'
            elif pinfo["active_count"] < PLAN_DEFS["trader"]["credits"]:
                action_btns = (
                    f'<form method="post" action="/billing">'
                    f'<button type="submit" name="action" value="sub:{key}:monthly" class="btn btn-primary" style="--accent:{cfg["accent"]}">Add</button>'
                    f'</form>'
                )
            else:
                action_btns = '<span style="font-size:12px;color:var(--text-muted)">No credits left</span>'
        else:
            # No plan — individual subscribe buttons
            action_btns = (
                f'<form method="post" action="/billing">'
                f'<button type="submit" name="action" value="sub:{key}:monthly" class="btn btn-primary" style="--accent:{cfg["accent"]}">Subscribe</button>'
                f'</form>'
            )

        highlight = ' style="outline: 2px solid var(--accent); outline-offset: 2px;"' if dashboard == key else ""
        rows_html.append(f"""
        <div class="billing-row" data-key="{key}"{highlight}>
          <div class="billing-row-main">
            <div class="billing-row-accent" style="background:{cfg['accent']}"></div>
            <div>
              <div class="billing-row-title">{cfg['display_name']}</div>
              <div class="billing-row-desc">{cfg['description']}</div>
            </div>
          </div>
          <div class="billing-row-status">{status_label}</div>
          <div class="billing-row-actions">{action_btns}</div>
        </div>
        """)

    # Dynamic access description
    if is_admin_user:
        access_desc = "Full access to all dashboards via admin privileges."
    elif pinfo["plan"] == "pro":
        access_desc = "All dashboards are included with your Pro subscription."
    elif pinfo["plan"] == "trader":
        remaining = max(0, PLAN_DEFS["trader"]["credits"] - pinfo["active_count"])
        access_desc = f'You have <strong>{remaining}</strong> of <strong>{PLAN_DEFS["trader"]["credits"]}</strong> dashboard credits remaining. Use the Add button to choose which dashboards to unlock.'
    else:
        access_desc = "Subscribe to a plan above to unlock dashboards."

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="billing",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )
    return render_page(
        "billing", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        raw_plan_card=plan_card,
        raw_access_desc=access_desc,
        billing_rows="".join(rows_html),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role, _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )


@app.post("/billing")
async def billing_action(request: Request, action: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/billing")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    parts = action.split(":")
    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    now = int(time.time())
    pinfo = _user_plan_info(user, subs, now)

    if parts[0] == "sub" and len(parts) == 3:
        _, key, plan = parts
        if key in DASHBOARDS and plan in ("monthly", "annual"):
            # For Trader: check credit limit before adding
            if pinfo["plan"] == "trader":
                if pinfo["active_count"] >= PLAN_DEFS["trader"]["credits"]:
                    return RedirectResponse("/billing", status_code=302)
            # Get duration from the plan sentinel if trader
            duration = 30 if plan == "monthly" else 365
            plan_prefix = pinfo["plan"] or "standalone"
            db.upsert_subscription(
                user_id=user["user_id"],
                dashboard_key=key,
                plan=f"{plan_prefix}_{plan}",
                duration_days=duration,
                source=f"billing_{plan_prefix}",
            )
    # Cancel only allowed for non-trader plans (trader credits are permanent)
    elif parts[0] == "cancel" and len(parts) == 2:
        _, key = parts
        if key in DASHBOARDS and pinfo["plan"] != "trader":
            db.cancel_subscription(user["user_id"], key)

    return RedirectResponse("/billing", status_code=302)


@app.post("/billing/subscribe")
async def billing_subscribe(request: Request, plan: str = Form(""), interval: str = Form("monthly")):
    """Subscribe the logged-in user — Trader gets 3 credits, Pro gets all.

    AUDIT (CRIT, audit_tier_change.md): every state-change branch in
    this handler must call ``ttl_invalidate.on_subscription_change``
    AND ``subproduct_access.invalidate_user`` before returning, so the
    next request observes the new tier instead of a 60s stale
    dashboards/settings/sidebar payload + a 5min stale subproduct
    gate. The canonical helpers (``db.upsert_subscription`` /
    ``db.cancel_subscription``) bust both caches themselves now, but
    this route also issues raw ``DELETE`` / ``UPDATE`` / ``INSERT OR
    REPLACE`` SQL that bypasses those helpers — so we fire the bust
    at the bottom of the route as well, defensively, to cover every
    state-change path through this route in one place.
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if plan not in ("trader", "pro"):
        return RedirectResponse("/billing", status_code=302)
    uid = user["user_id"]
    duration = 30 if interval == "monthly" else 365
    if plan == "pro":
        # Pro unlocks everything — clear old trader sentinel + old subs
        with db.conn() as c:
            c.execute("DELETE FROM subscriptions WHERE user_id = ? AND dashboard_key = '__plan__'", (uid,))
        # Subscribe to ALL dashboards as pro
        for key in DASHBOARDS:
            db.upsert_subscription(
                user_id=uid,
                dashboard_key=key,
                plan=f"pro_{interval}",
                duration_days=duration,
                source="billing_pro",
            )
    else:
        # Trader plan — check if downgrading from Pro
        subs = {s["dashboard_key"]: s for s in db.list_subscriptions(uid)}
        now = int(time.time())
        current_pinfo = _user_plan_info(user, subs, now)

        if current_pinfo["plan"] == "pro" and current_pinfo["expires_at"]:
            # Downgrade: keep Pro access until current period ends, then switch
            # Mark all Pro subs with a "downgrading" flag in plan name
            with db.conn() as c:
                c.execute(
                    "UPDATE subscriptions SET plan = 'pro_downgrading' "
                    "WHERE user_id = ? AND dashboard_key != '__plan__' AND status = 'active'",
                    (uid,),
                )
            # Create Trader sentinel starting when Pro expires
            pro_end = current_pinfo["expires_at"]
            trader_duration = 30 if interval == "monthly" else 365
            with db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO subscriptions "
                    "(user_id, dashboard_key, plan, status, started_at, expires_at, source) "
                    "VALUES (?, '__plan__', ?, 'active', ?, ?, 'downgrade')",
                    (uid, f"trader_{interval}", pro_end, pro_end + trader_duration * 86400),
                )
            log.info("User %s scheduled downgrade from Pro to Trader at %d", user.get("username", user["email"]), pro_end)
        else:
            # Fresh Trader subscription
            db.upsert_subscription(
                user_id=uid,
                dashboard_key="__plan__",
                plan=f"trader_{interval}",
                duration_days=duration,
                source="billing_trader",
            )
    # AUDIT (CRIT, audit_tier_change.md): bust the per-user feed/best-
    # bets sync TTL cache AND the in-process subproduct access verdict
    # cache. Both helpers are wrapped so a missing/broken cache module
    # never masks the underlying write — same pattern as the Stripe
    # webhook positive branches and the queries/subscriptions.py
    # canonical helpers. The raw DELETE/UPDATE/INSERT OR REPLACE SQL
    # above bypasses ``db.upsert_subscription``'s built-in bust, so
    # this catch-all bust is required even though the route also
    # calls the canonical helper in most branches.
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_subscription_change(uid)
    except Exception:
        log.exception("ttl_invalidate.on_subscription_change failed (user=%s)", uid)
    try:
        from subproduct_access import invalidate_user
        invalidate_user(uid)
    except Exception:
        log.exception("subproduct_access.invalidate_user failed (user=%s)", uid)
    log.info("User %s subscribed to %s (%s)", user.get("username", user["email"]), plan, interval)
    return RedirectResponse("/billing", status_code=302)


# ── Preview / product page ─────────────────────────────────────────────────


@app.get("/preview/{dashboard_key}", response_class=HTMLResponse)
async def preview_page(request: Request, dashboard_key: str):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/preview/{dashboard_key}")

    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if dashboard_key not in DASHBOARDS:
        return RedirectResponse("/dashboards", status_code=302)

    cfg = DASHBOARDS[dashboard_key]
    preview = DASHBOARD_PREVIEWS.get(dashboard_key, {})

    # If the user already has an active subscription, redirect to the dashboard.
    is_admin_user = bool(user.get("is_admin"))
    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    if _is_sub_active(subs.get(dashboard_key), is_admin_user):
        return RedirectResponse("/dashboards", status_code=302)

    # Build feature cards HTML
    features_html_parts = []
    for feat in preview.get("features", []):
        features_html_parts.append(
            f'<div class="preview-feature-card">'
            f'<div class="preview-feature-icon">{feat["icon"]}</div>'
            f'<div class="preview-feature-title">{html.escape(feat["title"])}</div>'
            f'<div class="preview-feature-desc">{html.escape(feat["desc"])}</div>'
            f'</div>'
        )

    # Build includes list HTML
    check_svg = (
        '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/>'
        '</svg>'
    )
    includes_html_parts = []
    for item in preview.get("includes", []):
        includes_html_parts.append(
            f'<li class="preview-includes-item">'
            f'<span class="preview-includes-check">{check_svg}</span>'
            f'{html.escape(item)}'
            f'</li>'
        )

    monthly_price = f"${cfg['monthly_cents'] / 100:.2f}"
    annual_price = f"${cfg['annual_cents'] / 100:.2f}"
    # Calculate annual savings percentage vs paying monthly for 12 months
    monthly_total = cfg["monthly_cents"] * 12
    savings_pct = round((1 - cfg["annual_cents"] / monthly_total) * 100) if monthly_total > 0 else 0

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )

    return render_page(
        "preview", request=request,
        dashboard_name=cfg["display_name"],
        dashboard_key=dashboard_key,
        tagline=preview.get("tagline", cfg["description"]),
        monthly_price=monthly_price,
        annual_price=annual_price,
        annual_savings=str(savings_pct),
        accent=cfg["accent"],
        username=user.get("username", user["email"]),
        raw_features_html="".join(features_html_parts),
        raw_includes_html="".join(includes_html_parts),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role, _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )


# ── Profile page ────────────────────────────────────────────────────────────


def _profile_context(user: dict, banner: str = "") -> dict:
    import datetime as _dt
    db_user = db.get_user_by_id(user["user_id"])
    joined = _dt.datetime.fromtimestamp(db_user["created_at"], tz=_dt.timezone.utc).strftime("%b %d, %Y UTC") if db_user else "—"
    role_badge = ""
    if user.get("is_super_admin"):
        role_badge = '<span class="profile-meta-item" style="background:rgba(245,158,11,0.12);color:var(--amber)">Super Admin</span>'
    elif user.get("is_admin"):
        role_badge = '<span class="profile-meta-item" style="background:var(--accent-light);color:var(--accent)">Admin</span>'
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    avatar = user.get("username", "?")[0].upper()

    # User's public collections — shown as a section on the profile page
    # so the owner has a canonical surface to point people at, separate
    # from the /collections dashboard view they use for editing.
    import html as _html
    collections_html = ""
    try:
        from queries import collections as _coll
        public_cols = _coll.list_public_by_owner(user["user_id"], limit=12)
        if public_cols:
            cards = []
            for c in public_cols:
                title = _html.escape(c.get("title") or "Untitled")
                desc = _html.escape((c.get("description") or "").strip()[:120])
                items = c.get("item_count") or 0
                followers = c.get("follower_count") or 0
                featured_chip = (
                    '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                    'background:var(--text-primary);color:var(--bg-base);font-size:10px;'
                    'text-transform:uppercase;letter-spacing:0.08em;margin-left:8px">Featured</span>'
                    if c.get("is_featured") else ""
                )
                cards.append(
                    f'<a href="/collections/{c["id"]}" '
                    f'style="display:block;padding:16px;border:1px solid var(--border);'
                    f'border-radius:10px;text-decoration:none;color:inherit;background:var(--bg-surface,var(--bg-base))">'
                    f'<div style="font-weight:600;font-size:15px">{title}{featured_chip}</div>'
                    f'<div style="color:var(--text-secondary,var(--text-muted));font-size:12px;'
                    f'line-height:1.45;margin-top:6px;min-height:32px">{desc}</div>'
                    f'<div style="color:var(--text-tertiary,var(--text-muted));font-size:11px;'
                    f'text-transform:uppercase;letter-spacing:0.08em;margin-top:10px">'
                    f'{items} items · {followers} followers</div>'
                    f'</a>'
                )
            collections_html = (
                '<div class="settings-card" style="margin-top:24px">'
                '<div class="settings-section">'
                '<div class="settings-section-title">Public collections</div>'
                '<div class="settings-section-desc">'
                'Boards you\'ve made public — anyone with the link can view them.'
                '</div>'
                '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:14px">'
                + "".join(cards) +
                '</div>'
                '<div style="margin-top:14px"><a href="/collections" style="font-size:12px;color:var(--text-secondary)">Manage all collections →</a></div>'
                '</div></div>'
            )
    except Exception:
        # Collections module optional — profile still renders without it.
        log.exception("profile: public collections section failed")

    nav_role = _role_badge(user)
    sidebar_html = render_sidebar(
        None,
        active="",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )
    return {
        "username": user.get("username", user["email"]),
        "email": user["email"],
        "avatar_letter": avatar,
        "joined": joined,
        "raw_role_badge": role_badge,
        "raw_nav_role": nav_role,
        "raw_admin_link": admin_link,
        "raw_banner": banner,
        "raw_collections_section": collections_html,
        "_is_admin": user.get("is_admin"),
        "raw_sidebar": sidebar_html,
    }


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return render_page("profile", request=request, **_profile_context(user))


@app.post("/account/delete")
async def account_self_delete(
    request: Request,
    confirm_email: str = Form(""),
    confirm_password: str = Form(""),
):
    """User-initiated account deletion (GDPR Art. 17 — Right to Erasure).

    Requires the user to re-type their email + password to defuse accidental
    clicks and confirm ownership.

    AUDIT 2026-05-15 — this route previously called ``cascade_delete_user``
    immediately, which diverged from the JSON sibling ``/api/account/delete``
    (server_features.py:531) which flips a 30-day soft-flag and waits for
    the ``process_scheduled_deletions`` cron to anonymise. The split meant
    the form variant skipped the recovery window, the deletion-confirmation
    email, and the audit-friendly schedule. Now both routes set the same
    30-day soft-flag, revoke sessions, cancel active subscriptions, and
    rely on the daily cron (or the explicit super-admin
    ``/admin/users/{id}/delete`` route) for the final hard delete.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/account/delete")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Refuse while impersonating — deletion must be initiated by the real user.
    try:
        from impersonation import is_impersonating  # type: ignore
        if is_impersonating(request):
            raise HTTPException(status_code=403, detail="Cannot delete an account while impersonating")
    except ImportError:
        pass

    # Per-user rate cap to stop accidental-or-malicious submit loops.
    if _is_rate_limited(f"account-delete:{user['user_id']}", 3, 3600):
        raise HTTPException(status_code=429, detail="Too many attempts")

    db_user = db.get_user_by_id(user["user_id"])
    if not db_user:
        return RedirectResponse("/logout", status_code=302)

    typed_email = (confirm_email or "").strip().lower()
    stored_email = (db_user["email"] or "").strip().lower()
    if not typed_email or typed_email != stored_email:
        raise HTTPException(status_code=400, detail="Email confirmation does not match")
    if not confirm_password or not db.verify_password(
        confirm_password, db_user["password_hash"], db_user["password_salt"]
    ):
        raise HTTPException(status_code=401, detail="Password is incorrect")

    user_id = db_user["id"]
    email = db_user["email"]
    now = int(time.time())
    deletion_scheduled_for = now + 30 * 86400
    log.info(
        "account.delete: user_id=%d email=%s initiated soft-delete (30-day window)",
        user_id, email,
    )

    with db.conn() as c:
        # Soft-flag — anonymisation happens in process_scheduled_deletions.
        c.execute(
            "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ?, "
            "deletion_cancelled_at = NULL, jwt_invalidated_before = ? WHERE id = ?",
            (now, deletion_scheduled_for, now, user_id),
        )
        # Cancel any active subscriptions to stop further Stripe charges
        # during the recovery window. The user can un-cancel via
        # /api/account/delete/cancel within the 30-day grace period.
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        # Revoke every existing session — JWT invalidation cutoff plus
        # row-level deletion together stop outstanding cookies from working.
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # Send deletion-confirmation email when templating + queue are available.
    try:
        import datetime as _dt
        from jobs.email_jobs import enqueue_email
        await enqueue_email(
            to=email,
            template="account_deletion_confirmation",
            context={
                "display_name": db_user["username"] or email.split("@")[0],
                "deletion_date": _dt.datetime.fromtimestamp(deletion_scheduled_for).strftime("%B %d, %Y"),
            },
            tags=["account_deletion", "transactional"],
        )
    except Exception as e:
        log.warning("account.delete: deletion-confirmation enqueue failed: %s", e)

    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user_id, admin_email=email,
            action=_audit.AuditAction.USER_DELETE_COMPLETED,
            target_type="user", target_id=user_id,
            target_description="self-delete (scheduled, 30-day window)",
            before={"email": email},
            after={"deletion_scheduled_for": deletion_scheduled_for},
            request=request,
        )
    except Exception:
        pass

    log.info(
        "account.delete: user_id=%d scheduled_for=%d", user_id, deletion_scheduled_for,
    )

    response = RedirectResponse("/", status_code=302)
    try:
        clear_session_cookie(response, request)
    except Exception:
        pass
    return response


@app.post("/profile/password")
async def profile_change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), confirm_password: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile/password")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # AUDIT 2026-05-14 — 5 password-change attempts per hour per user.
    # Stops a compromised session from brute-forcing current_password
    # (the only gate between session hijack and account take-over).
    if _is_rate_limited(f"profile-password:{user['user_id']}", 5, 3600):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Try again in an hour.",
        )

    db_user = db.get_user_by_id(user["user_id"])
    if not db_user:
        return RedirectResponse("/login", status_code=302)

    err_banner = lambda msg: f'<div class="notice notice-error" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--red)">{html.escape(msg)}</div>'
    ok_banner = lambda msg: f'<div class="notice notice-success" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--green)">{html.escape(msg)}</div>'

    if (
        len(current_password) > FIELD_MAX["password"]
        or len(new_password) > FIELD_MAX["password"]
        or len(confirm_password) > FIELD_MAX["password"]
    ):
        return render_page("profile", request=request, **_profile_context(user, err_banner("Password is too long.")))

    if not db.verify_password(current_password, db_user["password_hash"], db_user["password_salt"]):
        return render_page("profile", request=request, **_profile_context(user, err_banner("Current password is incorrect.")))
    if new_password != confirm_password:
        return render_page("profile", request=request, **_profile_context(user, err_banner("New passwords don't match.")))
    if len(new_password) < 12:
        return render_page("profile", request=request, **_profile_context(user, err_banner("Password must be at least 12 characters.")))
    if not re.search(r"[A-Z]", new_password) or not re.search(r"[a-z]", new_password) or not re.search(r"[0-9]", new_password) or not re.search(r"[^A-Za-z0-9]", new_password):
        return render_page("profile", request=request, **_profile_context(user, err_banner("Password must include uppercase, lowercase, number, and special character.")))

    pwd_hash, salt = db._hash_password(new_password)
    with db.conn() as c:
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwd_hash, salt, user["user_id"]))
    # Revoke every session except the actor's current one so any compromised
    # cookie elsewhere cannot survive the voluntary password change.
    try:
        db.revoke_all_user_sessions(user["user_id"])
    except Exception as exc:
        log.error("Failed to revoke hardened sessions after password change for user_id=%d: %s", user["user_id"], exc)

    log.info("User %s changed their password", user.get("username", user["email"]))
    return render_page("profile", request=request, **_profile_context(user, ok_banner("Password changed successfully.")))


# ── Password reset ─────────────────────────────────────────────────────────


def _validate_password(password: str) -> Optional[str]:
    """Return an error message if the password is invalid, else None."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if len(password) > 256:
        return "Password is too long."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return "Password must contain at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain at least one special character."
    return None


def _reset_token_hash(raw: str) -> str:
    """SHA-256 hash the reset token for at-rest storage (Feature 2).

    Matches server_features._hash_reset_token so either code path can
    validate tokens produced by the other.
    """
    import hashlib as _h
    return _h.sha256(raw.encode()).hexdigest()


# Per-deployment salt so the same raw IP hashes to a different value
# across deploys — protects against rainbow-table attacks if the analytics
# DB is exfiltrated. MUST be set per-deploy in production (random, ≥32
# chars); a literal in source code is not protection because anyone with
# read access to a leaked DB can precompute SHA-256 over IPv4. In dev /
# tests we fall back to a constant so the helper stays deterministic
# without environment setup — the startup check logs a WARNING whenever
# that dev fallback is in use, and production refuses to boot without
# IP_HASH_SALT in the environment.
_IP_HASH_SALT_DEV_FALLBACK = "narve.ai/analytics/dev-only-not-secret"
_IP_HASH_SALT_ENV = os.environ.get("IP_HASH_SALT", "")
_IP_HASH_SALT = _IP_HASH_SALT_ENV or _IP_HASH_SALT_DEV_FALLBACK


def _hash_ip(raw_ip: str) -> str:
    """Return a SHA-256 hex digest of the salted client IP.

    Used as the `ip_hash` column on analytics_events so we can count unique
    visitors without ever persisting the raw IP. The salt is a constant per
    deployment, so the same visitor reliably hashes to the same value within
    a deployment but cannot be correlated across deployments — and the
    output is never reversible to the original IP.
    """
    if not raw_ip:
        return ""
    import hashlib as _h
    return _h.sha256(f"{_IP_HASH_SALT}:{raw_ip}".encode()).hexdigest()


# Master switch for server-side analytics. Defaults to enabled; flip to
# "false"/"0" to keep the endpoint live (and 204) but skip DB writes.
_ANALYTICS_ENABLED = os.environ.get("ANALYTICS_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off", ""
}

# event_type guard: ≤ 64 chars, alphanumeric + underscore. Mirrors the
# slice() in static/analytics.js but enforces the charset server-side so a
# malicious client cannot inject odd payloads into our reporting tables.
_ANALYTICS_EVENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Per-principal rate limit on /api/analytics/event. The global middleware
# already caps at 600 req/min/IP for *everything*, but an attacker who
# only hits analytics could still fill engagement tables at that rate.
# Tighten to 60/min per user (or per IP for anonymous traffic) — well
# above the worst real-world page (one auto-track on load + a few user
# actions) and matches the user-spec hardening bar.
_ANALYTICS_RATE_LIMIT = int(os.environ.get("ANALYTICS_RATE_LIMIT_PER_MIN", "60"))
_ANALYTICS_RATE_WINDOW = 60


@app.post("/api/analytics/event")
async def api_analytics_event(request: Request):
    """Record a single anonymous analytics event.

    Public endpoint — landing/dashboards visitors are typically not
    signed in. We resolve user_id best-effort from the session cookie so
    authenticated traffic gets attributed when possible.

    Hardening (security audit, 2026-05-14):
    * Per-principal rate limit (60/min by default; tunable via
      ``ANALYTICS_RATE_LIMIT_PER_MIN``).
    * Body size cap (4 KB) + per-field length caps.
    * ``properties`` size cap and PII scrub via ``queries.analytics``.

    Analytics MUST NEVER 500: we wrap every step in try/except and return
    204 on internal failure. Validation errors (bad event_type, malformed
    JSON, oversized properties) still return 400/422 because those are
    caller bugs we want surfaced. Rate-limit returns 429.

    TODO(perf): the DB write is still synchronous on the request path.
    At very high event rates we should push to a background task / queue
    so the beacon response never blocks on disk I/O. See the
    fire-and-forget pattern in engagement.py for reference.
    """
    # Resolve the rate-limit principal first so authenticated users get
    # their own bucket (one user behind NAT can't be DoSed by a noisy
    # neighbour, and we still throttle anon traffic per source IP).
    try:
        _rl_ip = _get_client_ip(request)
    except Exception:
        _rl_ip = "unknown"
    _rl_user: Optional[int] = None
    try:
        _rl_session = current_user(request)
        if _rl_session:
            _rl_user = _rl_session.get("user_id")
    except Exception:
        _rl_user = None
    _rl_key = (
        f"analytics:user:{_rl_user}" if _rl_user is not None
        else f"analytics:ip:{_rl_ip}"
    )
    if _is_rate_limited(_rl_key, _ANALYTICS_RATE_LIMIT, _ANALYTICS_RATE_WINDOW):
        # 429 — explicit signal so the client backs off rather than
        # retrying. Body intentionally empty (this is a beacon endpoint).
        return Response(status_code=429)

    # Hard cap on body size — sendBeacon payloads from analytics.js are
    # small (a few hundred bytes); anything larger is abuse.
    try:
        raw = await request.body()
    except Exception:
        return Response(status_code=204)
    if len(raw) > 4096:
        return Response(status_code=400)

    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            return Response(status_code=400)
    except (ValueError, _JSONDecodeError):
        return Response(status_code=400)

    event_type = str(payload.get("event_type") or "").strip()
    if not _ANALYTICS_EVENT_RE.match(event_type):
        return Response(status_code=400)

    # Honour the kill switch AFTER validating shape so misbehaving clients
    # still get a clear 400 in dev when ANALYTICS_ENABLED is off.
    if not _ANALYTICS_ENABLED:
        return Response(status_code=204)

    # Properties: PII scrub + size cap. Imported lazily so the analytics
    # helper module isn't required for the rest of the gateway to import.
    from queries import analytics as _analytics_q
    raw_properties = payload.get("properties")
    if raw_properties is not None and not isinstance(raw_properties, dict):
        raw_properties = None
    scrubbed = _analytics_q.scrub_properties(raw_properties)
    if _analytics_q.properties_too_large(scrubbed):
        # 422 — caller sent a valid-shape body but the content is too
        # big to accept. Distinguished from the 400 "body cap" case so
        # client-side debugging can tell the two apart.
        return Response(status_code=422)

    try:
        page = (str(payload.get("page") or "") or None)
        if page is not None:
            page = page[:512]
        referrer = (str(payload.get("referrer") or "") or None)
        if referrer is not None:
            referrer = referrer[:512]
        ua_cat = (str(payload.get("user_agent_category") or "") or None)
        if ua_cat is not None:
            ua_cat = ua_cat[:32]
        session_id = payload.get("session_id")
        if session_id is not None:
            session_id = str(session_id)[:128]
        properties = scrubbed

        # user_id resolved above for the rate-limit key — reuse so we
        # don't pay for current_user() twice on the hot path.
        user_id: Optional[int] = _rl_user

        # Hash the client IP via the salted helper. Honour
        # X-Forwarded-For when behind the Cloudflare/Tunnel front so the
        # hash reflects the visitor, not the loopback proxy.
        raw_ip = ""
        try:
            xff = request.headers.get("x-forwarded-for") or ""
            if xff:
                raw_ip = xff.split(",", 1)[0].strip()
            if not raw_ip:
                raw_ip = (request.client.host if request.client else "") or ""
        except Exception:
            raw_ip = ""
        ip_hash = _hash_ip(raw_ip)

        db.record_analytics_event(
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            page=page,
            referrer=referrer,
            ip_hash=ip_hash,
            user_agent_category=ua_cat,
            properties=properties,
        )
    except Exception:
        # Analytics MUST NEVER 500. Log and swallow so beacon callers
        # don't see a failure that might trigger client-side retries.
        log.warning("analytics.event_record_failed", exc_info=True)
        return Response(status_code=204)

    return Response(status_code=204)


def _lookup_reset(token: str):
    """Find a non-used, non-expired reset row by raw token.

    Checks `token_hash` first (Feature 2 hardening) and falls back to the
    legacy plaintext `token` column so old outstanding links still work
    during the rollover window.
    """
    if not token:
        return None
    th = _reset_token_hash(token)
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM password_resets WHERE token_hash = ? AND used = 0 "
            "AND (invalidated IS NULL OR invalidated = 0) AND expires_at > ?",
            (th, now),
        ).fetchone()
        if row:
            return row
        return c.execute(
            "SELECT * FROM password_resets WHERE token = ? AND used = 0 AND expires_at > ?",
            (token, now),
        ).fetchone()


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/reset-password")

    token = _bounded(token, FIELD_MAX["reset_token"], "token")
    reset = _lookup_reset(token)
    if not reset:
        return render_page(
            "forgot-password", request=request,
            error="This reset link is invalid or has expired. Please request a new one.",
            raw_success="",
        )
    return render_page("reset-password", request=request, token=token, error="", raw_success="")


@app.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/reset-password")

    if _auth_rate_limited(_get_client_ip(request)):
        return RATE_LIMITED_RESPONSE
    token = _bounded(token, FIELD_MAX["reset_token"], "token")
    if len(new_password) > FIELD_MAX["password"] or len(confirm_password) > FIELD_MAX["password"]:
        return render_page("reset-password", request=request, token=token, error="Password is too long.", raw_success="")
    reset = _lookup_reset(token)
    if not reset:
        return render_page(
            "forgot-password", request=request,
            error="This reset link is invalid or has expired. Please request a new one.",
            raw_success="",
        )

    if new_password != confirm_password:
        return render_page("reset-password", request=request, token=token, error="Passwords don't match.", raw_success="")

    pwd_err = _validate_password(new_password)
    if pwd_err:
        return render_page("reset-password", request=request, token=token, error=pwd_err, raw_success="")

    # Atomically claim the reset row by id. Race-safe under concurrent clicks.
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE password_resets SET used = 1, used_from_ip = ? WHERE id = ? AND used = 0",
            (_get_client_ip(request), reset["id"]),
        )
        if cur.rowcount == 0:
            return render_page(
                "forgot-password", request=request,
                error="This reset link has already been used.",
                raw_success="",
            )

    pwd_hash, salt = db._hash_password(new_password)
    with db.conn() as c:
        # Also bump jwt_invalidated_before so any already-issued session cookie
        # from before the reset is rejected by the middleware (Feature 2).
        c.execute(
            "UPDATE users SET password_hash = ?, password_salt = ?, jwt_invalidated_before = ? WHERE id = ?",
            (pwd_hash, salt, now, reset["user_id"]),
        )

    # Revoke all existing sessions for this user — email reset is the
    # recovery flow for compromised credentials, so any active cookie
    # elsewhere must be killed. User lands on /login and signs in fresh.
    try:
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (reset["user_id"],))
        db.revoke_all_user_sessions(reset["user_id"])
    except Exception as exc:
        log.error("Failed to revoke sessions after reset for user_id=%d: %s", reset["user_id"], exc)

    user = db.get_user_by_id(reset["user_id"])
    log.info("Password reset completed for user %s", user["email"] if user else reset["user_id"])

    # Redirect to login with a success indicator.
    return render_page(
        "login", request=request,
        error="",
        email_hint="",
        raw_success='<div class="auth-success">Password reset successfully. You can now sign in.</div>',
    )


# ── Enquiry page + API ───────────────────────────────────────────────────────


# Public marketing + pre-release routes (/enquire, /pricing, /subscribe,
# /support, /suspended, /api/newsletter*) live in public_routes.py. They
# are registered alongside the other extracted modules below.

# ── Admin panel ──────────────────────────────────────────────────────────────


def _require_admin_user(request: Request, *, page: bool = False):
    """Return the current user dict if admin.

    If *page* is True, returns None (caller should render 403 page) or a
    RedirectResponse (caller should return it directly) when 2FA is required.
    If *page* is False, raises HTTPException(403) for POST/API routes or
    303 for 2FA redirects.

    For state-changing admin requests (POST/PUT/PATCH/DELETE), additionally
    enforces a per-admin-email rate limit so a compromised admin credential
    cannot be used to mass-mutate users/grants/tokens. Keying on the admin
    email (not IP) defends against an attacker rotating IPs via VPN.
    """
    # Use the real admin during impersonation so /admin stays reachable.
    user = _real_admin_user(request) or current_user(request)
    if not user or not user.get("is_admin"):
        if page:
            return None
        raise HTTPException(status_code=403, detail="Admin access required")

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # 30 mutations per 5 minutes per admin. Generous for normal panel
        # work; tight enough to bound damage from a stolen credential.
        key = f"admin_mut:{user.get('email') or user.get('user_id')}"
        if _is_rate_limited(key, 30, 300):
            log.warning("Admin rate limit tripped for %s on %s", user.get("email"), request.url.path)
            raise HTTPException(status_code=429, detail="Too many admin actions. Slow down.")
    return user


def _denied_response(request: Request) -> Response:
    """Return the 403 page for non-admin users, or redirect to gate."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    resp = render_page("403", request=request)
    resp.status_code = 403
    return resp


def _build_admin_context(
    new_token_str: str = "",
    caller_level: int = 1,
    tokens_before: Optional[int] = None,
    users_before: Optional[int] = None,
) -> dict:
    """Build the template context for the admin page.

    Invite-token surface was removed 2026-05-15. ``new_token_str`` and
    ``tokens_before`` remain as no-op parameters so stale callers don't
    crash; only ``users_before`` is still a live cursor.
    """
    users = db.list_all_users(before_id=users_before)
    token_rows: list = []

    # User rows HTML — with checkboxes and full management
    import datetime as _dt
    is_super = caller_level >= 2
    user_rows = []
    dash_opts = "".join(
        f'<option value="{k}">{html.escape(cfg["display_name"])}</option>'
        for k, cfg in DASHBOARDS.items()
    )
    sel_style = 'style="padding:6px 10px;font-size:11px;background:#1e2130;color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-xs);appearance:auto"'

    for u in users:
        ulevel = u["is_admin"] or 0
        badges = ""
        if ulevel >= 2:
            badges += '<span class="badge" style="background:rgba(245,158,11,0.12);color:var(--amber)">SUPER ADMIN</span> '
        elif ulevel == 1:
            badges += '<span class="badge" style="background:var(--accent-light);color:var(--accent)">ADMIN</span> '
        if u["suspended"]:
            badges += '<span class="badge" style="background:var(--red-bg);color:var(--red)">SUSPENDED</span> '
        joined = _dt.datetime.fromtimestamp(u["created_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        uname = html.escape(u["username"] or u["email"].split("@")[0])
        email_esc = html.escape(u["email"])

        # Determine if caller can manage this user
        can_manage = False
        if is_super:
            can_manage = True  # super admin can manage everyone
        elif caller_level == 1 and ulevel == 0:
            can_manage = True  # regular admin can only manage regular users

        actions = ""
        detail_extra = ""

        if can_manage:
            # Role management
            if is_super:
                role_opts = (
                    f'<option value="0" {"selected" if ulevel == 0 else ""}>User</option>'
                    f'<option value="1" {"selected" if ulevel == 1 else ""}>Admin</option>'
                    f'<option value="2" {"selected" if ulevel == 2 else ""}>Super Admin</option>'
                )
                actions += (
                    f'<form method="post" action="/admin/users/{u["id"]}/role" onclick="event.stopPropagation()" '
                    f'onsubmit="return confirm(\'Change role for {uname}?\')" style="display:flex;gap:6px;align-items:center">'
                    f'<select name="level" {sel_style}>{role_opts}</select>'
                    f'<button class="btn btn-primary-outline" style="font-size:11px">Set Role</button></form>'
                )
            else:
                # Regular admin: promote/demote regular users only
                if ulevel == 0:
                    actions += f'<form method="post" action="/admin/users/{u["id"]}/promote" onsubmit="return confirm(\'Promote {uname} to admin?\')"><button class="btn btn-primary-outline" style="font-size:11px">Promote to Admin</button></form>'
                elif ulevel == 1:
                    actions += f'<form method="post" action="/admin/users/{u["id"]}/demote" onsubmit="return confirm(\'Demote {uname}?\')"><button class="btn btn-danger" style="font-size:11px">Demote to User</button></form>'

            # Suspend/unsuspend
            if not u["suspended"]:
                actions += f'<form method="post" action="/admin/users/{u["id"]}/suspend" onsubmit="return confirm(\'Suspend {uname}?\')"><button class="btn btn-danger" style="font-size:11px">Suspend</button></form>'
            else:
                actions += f'<form method="post" action="/admin/users/{u["id"]}/unsuspend"><button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Unsuspend</button></form>'

            # Change email (admin+)
            detail_extra += (
                f'<form method="post" action="/admin/users/{u["id"]}/email" onclick="event.stopPropagation()" '
                f'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                f'<input name="new_email" type="email" placeholder="New email" {sel_style} style="padding:6px 10px;font-size:11px;background:#1e2130;color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-xs);flex:1">'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Change Email</button></form>'
            )

            # Invite-token revoke / generate buttons removed 2026-05-15.

            # Impersonate (admin+) — prompts for reason, then POSTs.
            detail_extra += (
                f'<form method="post" action="/admin/users/{u["id"]}/impersonate" onclick="event.stopPropagation()" '
                f'onsubmit="var r=prompt(\'Reason for impersonating {uname} (min 4 chars):\'); '
                f'if(!r||r.trim().length<4){{return false;}} '
                f'this.reason.value=r.trim(); return true;" '
                f'style="margin-top:8px">'
                f'<input type="hidden" name="reason" value="">'
                f'<button class="btn btn-primary-outline" style="font-size:11px;color:#f59e0b;border-color:#f59e0b">View as user</button></form>'
            )

            # Trading add-on toggle (admin+)
            trading_status = db.get_trading_addon_status(u["id"])
            if trading_status["active"]:
                detail_extra += (
                    f'<div style="display:flex;align-items:center;gap:8px;margin-top:8px">'
                    f'<span style="font-size:12px;color:var(--green);font-weight:600">Trading Add-on: Active</span>'
                    f'<form method="post" action="/admin/users/{u["id"]}/trading-addon" onclick="event.stopPropagation()">'
                    f'<input type="hidden" name="active" value="0">'
                    f'<button class="btn btn-danger" style="font-size:11px">Deactivate</button></form>'
                    f'</div>'
                )
            else:
                detail_extra += (
                    f'<div style="display:flex;align-items:center;gap:8px;margin-top:8px">'
                    f'<span style="font-size:12px;color:var(--text-muted)">Trading Add-on: Inactive</span>'
                    f'<form method="post" action="/admin/users/{u["id"]}/trading-addon" onclick="event.stopPropagation()">'
                    f'<input type="hidden" name="active" value="1">'
                    f'<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Activate</button></form>'
                    f'</div>'
                )

            # Grant subscription (super admin only)
            if is_super:
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/grant" onclick="event.stopPropagation()" '
                    f'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                    f'<select name="dashboard_key" {sel_style}>{dash_opts}</select>'
                    f'<select name="plan" {sel_style}><option value="monthly">Monthly</option><option value="annual">Annual</option></select>'
                    f'<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Grant Free</button></form>'
                )

            # Delete user (super admin only)
            if is_super:
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/delete" onclick="event.stopPropagation()" '
                    f'onsubmit="return confirm(\'Permanently delete {uname}? This removes their account, sessions, and subscriptions. This cannot be undone.\')" '
                    f'style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">'
                    f'<button class="btn btn-danger" style="font-size:11px">Delete User Permanently</button></form>'
                )
        else:
            actions = '<span style="font-size:12px;color:var(--text-muted)">Insufficient permissions</span>'

        can_select = can_manage
        checkbox = f'<input type="checkbox" name="user_ids" value="{u["id"]}" class="user-check" style="width:18px;height:18px;accent-color:var(--accent);cursor:pointer;flex-shrink:0;margin-right:12px">' if can_select else '<span style="width:18px;margin-right:12px;flex-shrink:0"></span>'
        user_rows.append(
            f'<div class="admin-row" style="align-items:flex-start">'
            f'{checkbox}'
            f'<div class="admin-row-info" style="cursor:pointer" onclick="toggleUserDetail(this)">'
            f'<div class="admin-row-main"><span style="font-weight:600">{uname}</span> {badges}</div>'
            f'<div class="admin-row-meta">{email_esc} &middot; Joined {joined}</div>'
            f'<div class="user-detail" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">'
            f'<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;padding:10px;background:var(--surface-hover);border-radius:var(--radius-xs)">'
            f'<strong>Username:</strong> {uname} &middot; '
            f'<strong>Email:</strong> {email_esc} &middot; '
            f'<strong>Hash:</strong> <code style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--bg)">...{html.escape(u["password_hash"][-8:])}</code>'
            f'</div>'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap">{actions}</div>'
            f'{detail_extra}'
            f'</div></div></div>'
        )

    # Stats — invite-token counters removed 2026-05-15. The admin
    # template's Users panel is now the default and no longer slots in
    # the token banner.
    total_users = len(users)
    stat_cards = (
        f'<div class="stat-card"><div class="stat-label">Total Users</div><div class="stat-value">{total_users}</div></div>'
    )

    # ``new_token_banner`` is permanently empty — kept for template
    # back-compat until admin.html is updated by the design agent.
    new_token_banner = ""

    # Revenue tab only for super admins (level >= 2)
    if caller_level >= 2:
        revenue_tab = '<button class="admin-tab" onclick="switchTab(\'revenue\')">Revenue</button>'
        revenue_content = _build_revenue_content()
    else:
        revenue_tab = ""
        revenue_content = '<div style="text-align:center;padding:48px 0;color:var(--text-muted)">Super admin access required.</div>'

    # Perf audit #5: cursor-pagination 'Load more' anchor for users.
    # The token cursor was removed alongside the invite-token surface.
    if users and len(users) >= 100:
        last_user_id = int(users[-1]["id"])
        user_rows.append(
            f'<div class="admin-row" style="justify-content:center">'
            f'<a href="/admin?users_before={last_user_id}#panel-users" '
            f'class="btn btn-primary-outline" style="font-size:12px">Load more users</a></div>'
        )

    # XSS invariant (AUDIT #5 MED #4): every `raw_*` key here is built
    # server-side from either (a) a whitelist of static strings or
    # (b) the output of a helper that html.escape's its inputs before
    # joining. No untrusted request body, URL param, or DB column with
    # user-controlled content lands here without escaping. If you add a
    # new raw_ key below, uphold this — or drop the raw_ prefix so
    # render_page escapes it for you.
    # ``raw_token_rows`` / ``raw_new_token_banner`` were dropped when
    # admin.html lost the Tokens tab. Local ``token_rows`` /
    # ``new_token_banner`` are retained but unused so any reflective
    # diff is obvious.
    _ = token_rows, new_token_banner
    return {
        "raw_user_rows": "".join(user_rows),
        "raw_stat_cards": stat_cards,
        "raw_enquiry_rows": _build_enquiry_rows(),
        "raw_revenue_tab": revenue_tab,
        "raw_revenue_content": revenue_content,
    }


def _build_enquiry_rows() -> str:
    enquiries = db.list_enquiries()
    if not enquiries:
        return '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No enquiries yet.</div></div></div>'
    import datetime as _dt
    rows = []
    for e in enquiries:
        read_badge = "" if e["read"] else '<span class="badge" style="background:var(--accent-light);color:var(--accent)">NEW</span> '
        ts = _dt.datetime.fromtimestamp(e["created_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        mark_btn = ""
        if not e["read"]:
            mark_btn = (
                f'<form method="post" action="/admin/enquiries/{e["id"]}/read">'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Mark Read</button></form>'
            )
        # Create-token button removed 2026-05-15 — admins no longer mint
        # invite tokens. Enquirers register through /register directly.
        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">{read_badge}<span style="font-weight:600">{html.escape(e["email"])}</span>'
            f' <span class="badge" style="background:var(--surface-hover);color:var(--text-secondary)">{html.escape(e["job_title"])}</span></div>'
            f'<div style="font-size:13px;color:var(--text-secondary);margin:8px 0;line-height:1.5">{html.escape(e["message"][:300])}</div>'
            f'<div class="admin-row-meta">{ts}</div>'
            f'</div>'
            f'<div class="admin-row-actions" style="display:flex;gap:6px">{mark_btn}</div></div>'
        )
    return "".join(rows)


def _build_revenue_content() -> str:
    import datetime as _dt
    stats = db.get_revenue_stats()
    # Recent-activity section below renders the last page of subs
    # (cap 20 in the loop). MRR/ARR no longer iterate this list — see
    # the perf audit #5 note below — so it's display-only here.
    subs = db.list_all_subscriptions(limit=20)
    now = int(time.time())

    # Perf audit #5: MRR is computed from the SQL-aggregated
    # per_dashboard×plan counts in `stats` rather than iterating every
    # row of `list_all_subscriptions()` (which is now paginated and
    # would under-report MRR once active_subs > page_size). Same math
    # (config price × active count per plan), O(active_dashboards)
    # instead of O(subs).
    mrr_cents = 0
    for row in stats["per_dashboard"]:
        cfg = DASHBOARDS.get(row["dashboard_key"])
        if not cfg:
            continue
        cnt = int(row["cnt"] or 0)
        if row["plan"] == "monthly":
            mrr_cents += cfg["monthly_cents"] * cnt
        elif row["plan"] == "annual":
            mrr_cents += (cfg["annual_cents"] // 12) * cnt

    mrr = mrr_cents / 100
    arr = mrr * 12

    # Summary cards
    out = (
        f'<div class="stat-grid" style="margin-bottom:32px">'
        f'<div class="stat-card"><div class="stat-label">Monthly Recurring Revenue</div>'
        f'<div class="stat-value" style="color:var(--green)">${mrr:,.2f}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Annual Run Rate</div>'
        f'<div class="stat-value" style="color:var(--green)">${arr:,.2f}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Active Subscriptions</div>'
        f'<div class="stat-value">{stats["active"]}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Cancelled</div>'
        f'<div class="stat-value" style="color:var(--red)">{stats["cancelled"]}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Expired</div>'
        f'<div class="stat-value" style="color:var(--amber)">{stats["expired"]}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Total All Time</div>'
        f'<div class="stat-value">{stats["total"]}</div></div>'
        f'</div>'
    )

    # Per-dashboard breakdown
    dashboard_rows = {}
    for row in stats["per_dashboard"]:
        key = row["dashboard_key"]
        if key not in dashboard_rows:
            dashboard_rows[key] = {"monthly": 0, "annual": 0}
        dashboard_rows[key][row["plan"]] = row["cnt"]

    if dashboard_rows:
        out += (
            '<div style="margin-bottom:24px">'
            '<div style="font-size:15px;font-weight:600;color:var(--text-primary);margin-bottom:16px">Revenue by Dashboard</div>'
            '<div class="admin-list">'
        )
        for key, counts in dashboard_rows.items():
            cfg = DASHBOARDS.get(key, {})
            name = cfg.get("display_name", key)
            accent = cfg.get("accent", "var(--accent)")
            mo_price = cfg.get("monthly_cents", 0) / 100
            yr_price = cfg.get("annual_cents", 0) / 100
            mo_rev = counts["monthly"] * mo_price
            yr_rev = counts["annual"] * (yr_price / 12)
            dash_mrr = mo_rev + yr_rev
            out += (
                f'<div class="admin-row">'
                f'<div class="admin-row-info">'
                f'<div class="admin-row-main">'
                f'<span style="width:8px;height:8px;border-radius:50%;background:{accent};flex-shrink:0"></span>'
                f'<span style="font-weight:600">{html.escape(name)}</span>'
                f'<span class="badge" style="background:var(--surface-hover);color:var(--text-secondary)">${mo_price:.0f}/mo &middot; ${yr_price:.0f}/yr</span>'
                f'</div>'
                f'<div class="admin-row-meta">'
                f'{counts["monthly"]} monthly &middot; {counts["annual"]} annual'
                f'</div></div>'
                f'<div style="text-align:right;margin-left:16px">'
                f'<div style="font-size:18px;font-weight:700;color:var(--green)">${dash_mrr:,.2f}<span style="font-size:11px;font-weight:400;color:var(--text-muted)">/mo</span></div>'
                f'</div></div>'
            )
        out += '</div></div>'

    # Recent subscription activity
    if subs:
        out += (
            '<div>'
            '<div style="font-size:15px;font-weight:600;color:var(--text-primary);margin-bottom:16px">Recent Activity</div>'
            '<div class="admin-list">'
        )
        for s in subs[:20]:
            cfg = DASHBOARDS.get(s["dashboard_key"], {})
            name = cfg.get("display_name", s["dashboard_key"])
            accent = cfg.get("accent", "var(--accent)")
            ts = _dt.datetime.fromtimestamp(s["started_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
            status = s["status"]
            is_expired = s["expires_at"] and s["expires_at"] <= now
            if status == "active" and not is_expired:
                status_badge = '<span class="badge" style="background:var(--green-bg);color:var(--green)">Active</span>'
            elif status == "cancelled":
                status_badge = '<span class="badge" style="background:var(--red-bg);color:var(--red)">Cancelled</span>'
            else:
                status_badge = '<span class="badge" style="background:var(--surface-hover);color:var(--amber)">Expired</span>'
            plan_label = s["plan"].title()
            user_label = html.escape(s["username"] or s["email"])
            out += (
                f'<div class="admin-row">'
                f'<div class="admin-row-info">'
                f'<div class="admin-row-main">'
                f'<span style="width:6px;height:6px;border-radius:50%;background:{accent};flex-shrink:0"></span>'
                f'<span style="font-weight:500">{html.escape(name)}</span>'
                f'{status_badge}'
                f'<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">{plan_label}</span>'
                f'</div>'
                f'<div class="admin-row-meta">{user_label} &middot; {ts}</div>'
                f'</div></div>'
            )
        out += '</div></div>'
    else:
        out += '<div style="text-align:center;padding:48px 0;color:var(--text-muted)">No subscriptions yet.</div>'

    return out


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    tokens_before: Optional[int] = None,
    users_before: Optional[int] = None,
):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    if isinstance(user, Response):
        return user  # 2FA setup or verification redirect
    # Perf audit #5: pass cursor params through; FastAPI's int parser
    # rejects non-numeric values with a 422 before they hit the DB, so
    # the query layer always sees ints or None.
    ctx = _build_admin_context(
        caller_level=user.get("admin_level", 1),
        tokens_before=tokens_before,
        users_before=users_before,
    )
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"), **ctx)


# /admin/tokens/{generate,revoke} removed 2026-05-15 alongside the
# invite-token entry point. Admins no longer mint invite tokens; new
# accounts go through /register directly behind the /gate perimeter.


@app.post("/admin/users/{user_id}/promote")
async def admin_promote(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    db.set_user_admin(user_id, True)
    after = _audit.snapshot_user(user_id)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_PROMOTE_ADMIN,
            target_type="user", target_id=user_id,
            target_description=(before or {}).get("email") if before else None,
            before=before, after=after, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/demote")
async def admin_demote(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    db.set_user_admin(user_id, False)
    after = _audit.snapshot_user(user_id)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_DEMOTE_ADMIN,
            target_type="user", target_id=user_id,
            target_description=(before or {}).get("email") if before else None,
            before=before, after=after, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    db.set_user_suspended(user_id, True)
    after = _audit.snapshot_user(user_id)
    log.info("Admin %s suspended user id=%d", admin.get("email"), user_id)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_SUSPEND,
            target_type="user", target_id=user_id,
            target_description=(before or {}).get("email") if before else None,
            before=before, after=after, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    db.set_user_suspended(user_id, False)
    after = _audit.snapshot_user(user_id)
    log.info("Admin %s unsuspended user id=%d", admin.get("email"), user_id)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_UNSUSPEND,
            target_type="user", target_id=user_id,
            target_description=(before or {}).get("email") if before else None,
            before=before, after=after, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/enquiries/{enquiry_id}/read")
async def admin_mark_enquiry_read(request: Request, enquiry_id: int):
    _require_admin_user(request)
    db.mark_enquiry_read(enquiry_id)
    return RedirectResponse("/admin", status_code=302)


# /admin/enquiries/{id}/create-token removed 2026-05-15 — invite-token
# minting is no longer part of the auth flow.


# ── Admin: Logs section ───────────────────────────────────────────────────
#
# Three endpoints back the admin "Logs" tab. All read from the in-memory ring
# buffer populated by logging_config.configure_logging() so queries are cheap
# and do not hit disk.


def _parse_log_query(request: Request) -> dict:
    """Extract common log-filter params from query string."""
    try:
        limit = int(request.query_params.get("limit", "50") or 50)
    except ValueError:
        limit = 50
    return {
        "level": (request.query_params.get("level") or "").upper() or None,
        "service": request.query_params.get("service") or None,
        "q": request.query_params.get("q") or None,
        "limit": max(1, min(limit, 500)),
    }


@app.get("/admin/logs/live")
async def admin_logs_live(request: Request):
    """Return the most recent structured log records from the ring buffer.

    Query params:
      level   INFO|WARNING|ERROR — minimum level (default: all)
      service app|scraper|worker|all — filter by service name
      q       substring search inside the JSON payload
      limit   1-500 (default 50)
    """
    admin = _require_admin_user(request)
    if _is_rate_limited(f"admin_logs_live:{admin['email']}", 120, 60):
        return JSONResponse(
            {"error": "Log tail polled too frequently."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    params = _parse_log_query(request)
    records = _log_ring_buffer.snapshot(
        level=params["level"],
        service=params["service"],
        contains=params["q"],
        limit=params["limit"],
    )
    return JSONResponse({
        "records": records,
        "count": len(records),
        "capacity": _log_ring_buffer.capacity,
        "logtail_configured": is_logtail_configured(),
        "service": _LOG_SERVICE_NAME,
    })


@app.get("/admin/logs/errors")
async def admin_logs_errors(request: Request):
    """Return ERROR-level records grouped by (logger, message)."""
    admin = _require_admin_user(request)
    if _is_rate_limited(f"admin_logs_errors:{admin['email']}", 60, 60):
        return JSONResponse(
            {"error": "Error log polled too frequently."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    records = _log_ring_buffer.snapshot(level="ERROR", limit=500)

    grouped: dict = {}
    for rec in records:
        logger_name = rec.get("logger", "unknown")
        msg = (rec.get("message") or "")[:200]
        key = (logger_name, msg)
        if key not in grouped:
            grouped[key] = {
                "logger": logger_name,
                "message": msg,
                "service": rec.get("service"),
                "count": 0,
                "first_seen": rec.get("timestamp"),
                "last_seen": rec.get("timestamp"),
                "sample": rec,
            }
        g = grouped[key]
        g["count"] += 1
        ts = rec.get("timestamp")
        if ts:
            if not g["first_seen"] or ts < g["first_seen"]:
                g["first_seen"] = ts
            if not g["last_seen"] or ts > g["last_seen"]:
                g["last_seen"] = ts

    groups = sorted(grouped.values(),
                    key=lambda g: g["last_seen"] or "",
                    reverse=True)
    return JSONResponse({
        "groups": groups,
        "total_errors": sum(g["count"] for g in groups),
        "distinct_errors": len(groups),
    })


@app.get("/admin/logs/search")
async def admin_logs_search(request: Request):
    """Free-text substring search over the ring buffer.

    For richer queries (regex, multi-day retention) use BetterStack directly.
    """
    admin = _require_admin_user(request)
    if _is_rate_limited(f"admin_logs_search:{admin['email']}", 30, 60):
        return JSONResponse(
            {"error": "Log search rate limit reached."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    params = _parse_log_query(request)
    try:
        limit = int(request.query_params.get("limit", "100") or 100)
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    records = _log_ring_buffer.snapshot(
        level=params["level"],
        service=params["service"],
        contains=params["q"],
        limit=limit,
    )
    return JSONResponse({
        "records": records,
        "count": len(records),
        "query": params["q"] or "",
        "logtail_configured": is_logtail_configured(),
    })


def _can_manage_user(admin: dict, target_user_id: int) -> bool:
    """Check if admin can manage the target user based on role hierarchy."""
    target = db.get_user_by_id(target_user_id)
    if not target:
        return False
    target_level = target["is_admin"] or 0
    caller_level = admin.get("admin_level", 0)
    if caller_level >= 2:
        return True  # super admin manages everyone including other super admins
    if caller_level == 1 and target_level == 0:
        return True  # admin manages regular users only
    return False


def _require_super_admin(request: Request) -> dict:
    user = _require_admin_user(request)
    if user.get("admin_level", 0) < 2:
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


@app.post("/admin/users/{user_id}/role")
async def admin_set_role(request: Request, user_id: int, level: int = Form(0)):
    admin = _require_super_admin(request)
    if level < 0 or level > 2:
        raise HTTPException(status_code=400, detail="Invalid role level")
    # Self-demotion lockout: a super-admin who drops their own level
    # below their current one can lock themselves (and the install, if
    # they're the sole super-admin) out of every super-admin route.
    # ``set_user_role`` revokes all sessions on a role change as well,
    # which would compound the lockout. Promotion of self is allowed.
    if user_id == admin["user_id"] and level < int(admin.get("admin_level") or 0):
        raise HTTPException(
            status_code=400,
            detail="Refusing to self-demote: change another super-admin's role first",
        )
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    db.set_user_role(user_id, level)
    after = _audit.snapshot_user(user_id)
    log.info("Super admin %s set user %d role to %d", admin["email"], user_id, level)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_ROLE_CHANGE,
            target_type="user", target_id=user_id,
            target_description=(before or {}).get("email"),
            before=before, after=after, request=request,
            notes=f"level={level}",
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/email")
async def admin_change_email(request: Request, user_id: int, new_email: str = Form("")):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    new_email = new_email.strip().lower()
    if not new_email or not EMAIL_RE.match(new_email):
        raise HTTPException(status_code=400, detail="Invalid email")
    existing = db.get_user_by_email(new_email)
    if existing and existing["id"] != user_id:
        raise HTTPException(status_code=400, detail="Email already in use")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    with db.conn() as c:
        c.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, user_id))
    # Auth material changed — invalidate every outstanding session for the
    # affected account so the new email cannot be exercised on a stale cookie.
    try:
        db.revoke_all_user_sessions(user_id)
    except Exception as exc:
        log.error("Failed to revoke sessions after admin email change for user_id=%d: %s", user_id, exc)
    after = _audit.snapshot_user(user_id)
    log.info("Super admin %s changed email for user %d to %s", admin["email"], user_id, new_email)
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_EMAIL_CHANGE,
            target_type="user", target_id=user_id,
            target_description=new_email,
            before=before, after=after, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


# /admin/users/{id}/revoke-token and /new-token removed 2026-05-15
# along with the rest of the invite-token surface. The invite_tokens
# table is retained read-only for the audit log; nothing in this file
# writes to it any more.


@app.post("/admin/users/{user_id}/grant")
async def admin_grant_subscription(request: Request, user_id: int, dashboard_key: str = Form(""), plan: str = Form("monthly")):
    admin = _require_super_admin(request)
    if dashboard_key not in DASHBOARDS:
        raise HTTPException(status_code=400, detail="Invalid dashboard")
    duration = 30 if plan == "monthly" else 365
    db.upsert_subscription(
        user_id=user_id,
        dashboard_key=dashboard_key,
        plan=plan,
        duration_days=duration,
        source="admin_grant",
    )
    log.info("Super admin %s granted %s (%s) to user id=%d", admin["email"], dashboard_key, plan, user_id)
    try:
        from security import audit as _audit
        target_user = db.get_user_by_id(user_id)
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_GIFT_SUBSCRIPTION,
            target_type="user", target_id=user_id,
            target_description=target_user["email"] if target_user else None,
            after={"dashboard_key": dashboard_key, "plan": plan, "duration_days": duration},
            request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/trading-addon")
async def admin_toggle_trading_addon(request: Request, user_id: int, active: int = Form(0)):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    # Default to 30 days from now when activating
    period_end = int(time.time()) + 30 * 86400 if active else None
    db.set_trading_addon(user_id, bool(active), period_end)
    log.info("Admin %s set trading_addon=%s for user id=%d", admin["email"], bool(active), user_id)
    try:
        from security import audit as _audit
        target_user = db.get_user_by_id(user_id)
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_TRADING_ADDON,
            target_type="user", target_id=user_id,
            target_description=target_user["email"] if target_user else None,
            after={"active": bool(active), "period_end": period_end},
            request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, user_id: int):
    admin = _require_super_admin(request)
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Cannot delete another super admin
    target_level = user["is_admin"] or 0
    if target_level >= 2:
        raise HTTPException(status_code=403, detail="Cannot delete a super admin")
    from security import audit as _audit
    before = _audit.snapshot_user(user_id)
    # GDPR Art. 17: cascade across every user-scoped table — the inline
    # 3-table DELETE used to leave orphans in analytics_events, gifts,
    # email_send_log, push_subscriptions, etc. ``cascade_delete_user``
    # enumerates ``sqlite_master`` and deletes every row tied to the
    # user. Revoke hardened sessions first so any outstanding cookie
    # stops working mid-cascade.
    try:
        db.revoke_all_user_sessions(user_id)
    except Exception:
        pass
    deleted = db.cascade_delete_user(user_id)
    log.info(
        "Super admin %s deleted user id=%d (%s); cascade=%s",
        admin["email"], user_id, user["email"], deleted,
    )
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_DELETE_COMPLETED,
            target_type="user", target_id=user_id,
            target_description=user["email"],
            before=before, after=None, request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/bulk")
async def admin_bulk_users(request: Request):
    admin = _require_admin_user(request)
    # Rate limit admin bulk operations: 10 per admin per 5 minutes
    if _is_rate_limited(f"admin_bulk:{admin['email']}", 10):
        return RedirectResponse("/admin", status_code=302)
    form = await request.form()
    action = form.get("bulk_action", "")
    # form.getlist returns Union[str, UploadFile]; user_ids must be string
    # digits only — guarding the type prevents an attacker uploading a file
    # named "user_ids" from crashing the handler with AttributeError on
    # .isdigit().
    user_ids = [
        int(uid) for uid in form.getlist("user_ids")
        if isinstance(uid, str) and uid.isdigit() and int(uid) != 1
    ]
    if not user_ids or not action:
        return RedirectResponse("/admin", status_code=302)
    for uid in user_ids:
        if not _can_manage_user(admin, uid):
            continue
        if action == "promote":
            db.set_user_admin(uid, True)
        elif action == "demote":
            db.set_user_admin(uid, False)
        elif action == "suspend":
            db.set_user_suspended(uid, True)
        elif action == "unsuspend":
            db.set_user_suspended(uid, False)
        elif action == "delete" and (admin.get("admin_level") or 0) >= 2:
            target = db.get_user_by_id(uid)
            if target and (target["is_admin"] or 0) < 2:
                # GDPR Art. 17: same cascade as ``admin_delete_user`` —
                # the hand-rolled 3-table DELETE left orphans in every
                # user-scoped table without a hard FK CASCADE.
                try:
                    db.revoke_all_user_sessions(uid)
                except Exception:
                    pass
                db.cascade_delete_user(uid)
    log.info("Admin %s bulk %s %d users: %s", admin["email"], action, len(user_ids), user_ids)
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.USER_BULK_ACTION,
            target_type="user", target_id=None,
            target_description=f"{len(user_ids)} users",
            after={"action": action, "user_ids": user_ids},
            request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


# ── Admin: Impersonation / feature flags / email templates ──────────────
#
# Registered from admin_routes.py — see that module for the handlers.

try:
    import admin_routes as _admin_routes  # noqa: E402
    _admin_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("admin_routes.register failed: %s", _exc)


# ── /api/changelog + /api/changelog/seen ────────────────────────────────
#
# Powers the "What's new" widget on /dashboards. Parses CHANGELOG.md
# at the repo root, persists per-user "seen" state in changelog_seen
# (migration 170).

try:
    import changelog_routes as _changelog_routes  # noqa: E402
    _changelog_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("changelog_routes.register failed: %s", _exc)


# ── Data export (GDPR) + user predictions ──────────────────────────────

try:
    import export_routes as _export_routes  # noqa: E402
    _export_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("export_routes.register failed: %s", _exc)

try:
    import user_prediction_routes as _user_pred_routes  # noqa: E402
    _user_pred_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("user_prediction_routes.register failed: %s", _exc)


# ── Public SEO content pages ────────────────────────────────────────────
#
# /about, /how-it-works, /methodology, /faq, /team, /press, /changelog.
# Handlers live in seo_routes.py. The paths are added to _PUBLIC_PATHS
# above so GateMiddleware lets anonymous crawlers through.

try:
    import seo_routes as _seo_routes  # noqa: E402
    _seo_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("seo_routes.register failed: %s", _exc)


# ── Post-token-gate first-run experience ─────────────────────────────────
#
# /onboarding (5-step tour), /api/first-week/goals + goal-mark POSTs,
# /api/feed/sample for empty-dashboard sample data, and /admin/onboarding
# metrics page. Handlers live in onboarding_routes.py.

try:
    import onboarding_routes as _onboarding_routes  # noqa: E402
    _onboarding_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("onboarding_routes.register failed: %s", _exc)


# ── Scenario + correlation matrix (Pro) ──────────────────────────────────
#
# /tools/scenario, /tools/correlations, /api/scenario/* — conditional
# probability + Pearson heatmap. Handlers in scenarios_routes.py.

try:
    import scenarios_routes as _scenarios_routes  # noqa: E402
    _scenarios_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("scenarios_routes.register failed: %s", _exc)


# ── Public developer API v1 + API-key + webhook settings pages ────────
#
# /api/public/v1/* — Bearer-authenticated JSON endpoints (api_public/).
# /settings/api-keys, /settings/webhooks, /admin/webhooks — session pages.
# /api/docs — static developer docs.

try:
    import api_public  # noqa: E402
    app.include_router(api_public.router)
except Exception as _exc:  # pragma: no cover
    log.exception("api_public router mount failed: %s", _exc)

try:
    import api_keys_routes as _api_keys_routes  # noqa: E402
    _api_keys_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("api_keys_routes.register failed: %s", _exc)

try:
    import webhooks_routes as _webhooks_routes  # noqa: E402
    _webhooks_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("webhooks_routes.register failed: %s", _exc)

# Bridge realtime hub broadcasts into external webhook subscribers (no-op
# if the hub doesn't expose register_after_broadcast yet).
try:
    import webhooks as _webhooks_mod  # noqa: E402
    _webhooks_mod.register_with_hub()
except Exception as _exc:  # pragma: no cover
    log.exception("webhooks.register_with_hub failed: %s", _exc)


@app.get("/api/docs", response_class=HTMLResponse, include_in_schema=False)
async def api_docs_page(request: Request):
    """Human-readable developer reference for the narve.ai public API.

    Documents every public-facing endpoint group: Public (no auth),
    User-scoped (session), Subscribed (Pro / add-on), Embed (X-API-Key),
    and Subproducts (subdomain HMAC SSO). Anonymous; no auth required.
    The companion machine-readable OpenAPI schema lives at
    /api/openapi.json.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/docs")
    return render_page(
        "api_docs", request=request,
        breadcrumb=[
            ("narve.ai", "/dashboards"),
            ("API", None),
        ],
    )


# ── Topics (Pro-tier saved search topics) ──────────────────────────────

try:
    import topics_routes as _topics_routes  # noqa: E402
    _topics_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("topics_routes.register failed: %s", _exc)


# ── Markets, Kelly, user bankroll ──────────────────────────────────────
#
# Routes live in market_routes.py. The POLY_CLIENT / KALSHI_CLIENT
# singletons and _get_market_connections helper stay in server.py (used
# by the shutdown handler and the dashboard switcher snippet).

try:
    import market_routes as _market_routes  # noqa: E402
    _market_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("market_routes.register failed: %s", _exc)


# ── Public profile (/u/{handle}) + opt-in settings + follow graph ──
#
# Routes live in profile_routes.py. /u/ is whitelisted in
# _PUBLIC_PREFIXES so anonymous visitors and crawlers can reach
# opted-in profile pages.

try:
    import profile_routes as _profile_routes  # noqa: E402
    _profile_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("profile_routes.register failed: %s", _exc)


# ── Public marketing + pre-release pages ───────────────────────────────
#
# /enquire, /pricing, /subscribe, /support, /suspended, /api/newsletter*
# live in public_routes.py.

try:
    import public_routes as _public_routes  # noqa: E402
    _public_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("public_routes.register failed: %s", _exc)


# ── Intelligence: credibility / backtests / retrospective / probability ───
# ── / environmental impact ────────────────────────────────────────────────

try:
    import intelligence_routes as _intelligence_routes  # noqa: E402
    _intelligence_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("intelligence_routes.register failed: %s", _exc)


# ── Realtime WebSocket hub ────────────────────────────────────────────────
#
# /ws + /admin/realtime + /admin/realtime/stats. Must be registered before
# the catch-all websocket_proxy at the bottom of this file — the module
# inserts its WebSocket route at position 0 in app.router.routes for that
# reason. Broadcast sites (see hub.broadcast) live in the write paths of
# predictions, market snapshots, notifications, credibility, forensics.

try:
    import realtime as _realtime  # noqa: E402
    _realtime.register(app)
except Exception as _exc:  # pragma: no cover
    log.exception("realtime.register failed: %s", _exc)


# ── Admin: Audit log ─────────────────────────────────────────────────────────
#
# Forensic record of every administrative action. This is the primary
# investigation surface when a credential rotation or trace-watermark
# alert fires — so the filter bar, suspicious-pattern card, expandable
# row JSON, and CSV export all need to be discoverable on a single screen.
#
# Filters honoured (all optional, all combinable):
#   action            — one of audit.AuditAction
#   admin_id          — exact numeric id
#   admin_email       — case-insensitive substring (autocompleted from
#                       distinct admin_emails in audit_log via datalist)
#   target_type       — exact match against audit_log.target_type
#   target_user_id    — exact match against audit_log.target_id
#   from, to          — YYYY-MM-DD; converted to inclusive unix bounds
#   range             — "today" | "24h" | "7d" | "30d" quick chip; if
#                       present overrides from/to so the chip is a
#                       single-click "snap to now"
#   before_id         — cursor for pagination (matches the existing
#                       list_all_users cursor pattern)


def _audit_log_querystring(params, *, drop: tuple = (), **overrides) -> str:
    """Build a `?…` querystring preserving the current filter set, dropping
    any keys in `drop`, and applying `overrides`. Used to render the
    "next page" cursor link and the chip-active CSS classes.
    """
    qs: dict[str, str] = {}
    try:
        for k, v in params.items():
            if k in drop:
                continue
            qs[k] = str(v)
    except Exception:
        pass
    for k, v in overrides.items():
        if v is None:
            qs.pop(k, None)
        else:
            qs[k] = str(v)
    if not qs:
        return ""
    from urllib.parse import urlencode as _urlencode
    return "?" + _urlencode(qs, doseq=False)


@app.get("/admin/audit-log", response_class=HTMLResponse)
async def admin_audit_log_page(request: Request):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    if isinstance(user, Response):
        return user  # 2FA redirect
    from security import audit as _audit

    filters = _audit.filter_to_search_kwargs(request.query_params)

    # Cursor pagination. before_id ⇒ "next page" link the previous render
    # emitted; the page itself doesn't track an absolute "page N" — the
    # link the user clicks contains the next cursor.
    try:
        before_id = int(request.query_params.get("before_id") or "0") or None
    except ValueError:
        before_id = None

    rows, next_cursor, total = db.search_audit_log(filters, limit=50, before_id=before_id)
    stats = db.get_audit_stats(filters)
    known_admin_emails = db.list_audit_admin_emails(limit=50)

    import datetime as _dt
    import json as _json

    def _fmt_ts(ts: int) -> str:
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _pretty_json(raw: str) -> str:
        if not raw:
            return ""
        try:
            return _json.dumps(_json.loads(raw), indent=2, sort_keys=True)
        except Exception:
            return raw

    def _render_row(r):
        ts_str = _fmt_ts(r["timestamp"])
        action = r["action"]
        label = _audit.ACTION_LABELS.get(action, action)
        email = html.escape(r["admin_email"] or "—")
        target_id = r["target_id"] or ""
        target_desc = r["target_description"] or ""
        # Render target as "<desc> · <type>:<id>" so the admin sees both
        # the human label and the canonical id at a glance.
        if target_desc and target_id:
            target_html = (
                f'<span>{html.escape(target_desc)}</span>'
                f'<span style="display:block;font-family:var(--font-mono);font-size:11px;color:var(--text-tertiary)">'
                f'{html.escape(r["target_type"] or "")}:{html.escape(target_id)}</span>'
            )
        elif target_desc:
            target_html = html.escape(target_desc)
        elif target_id:
            target_html = (
                f'<span style="font-family:var(--font-mono);font-size:12px">'
                f'{html.escape(r["target_type"] or "")}:{html.escape(target_id)}</span>'
            )
        else:
            target_html = '<span style="color:var(--text-tertiary)">—</span>'

        ip = html.escape(r["ip_address"] or "—")
        notes = (r["notes"] or "").strip()
        details_preview = ""
        if notes:
            short = notes if len(notes) <= 80 else notes[:77] + "…"
            details_preview = html.escape(short)
        elif r["target_description"]:
            details_preview = html.escape(r["target_description"][:80])

        before_pretty = _pretty_json(r["before_state"] or "")
        after_pretty = _pretty_json(r["after_state"] or "")

        ua = html.escape(r["user_agent"] or "—")
        req_id = html.escape(r["request_id"] or "—")
        expand_id = f"row-{int(r['id'])}"

        expanded_html = (
            f'<div class="audit-expand-grid">'
            f'<div class="audit-expand-meta">'
            f'<div><span class="audit-expand-key">User agent</span>'
            f'<span class="audit-expand-val" style="font-family:var(--font-ui)">{ua}</span></div>'
            f'<div><span class="audit-expand-key">Request id</span>'
            f'<span class="audit-expand-val">{req_id}</span></div>'
            f'<div><span class="audit-expand-key">Notes</span>'
            f'<span class="audit-expand-val" style="font-family:var(--font-body)">{html.escape(notes or "—")}</span></div>'
            f'</div>'
        )
        if before_pretty:
            expanded_html += (
                f'<div class="audit-expand-block">'
                f'<div class="audit-expand-key">Before</div>'
                f'<pre class="audit-json">{html.escape(before_pretty)}</pre></div>'
            )
        if after_pretty:
            expanded_html += (
                f'<div class="audit-expand-block">'
                f'<div class="audit-expand-key">After</div>'
                f'<pre class="audit-json">{html.escape(after_pretty)}</pre></div>'
            )
        expanded_html += '</div>'

        _empty_details_cell = (
            "<span style=\"color:var(--text-tertiary)\">—</span>"
        )
        details_cell = details_preview or _empty_details_cell
        return (
            f'<tr class="audit-row" data-expand-target="{expand_id}" tabindex="0">'
            f'<td class="audit-ts">{ts_str}</td>'
            f'<td>{email}</td>'
            f'<td><span class="badge">{html.escape(label)}</span></td>'
            f'<td>{target_html}</td>'
            f'<td class="audit-ip">{ip}</td>'
            f'<td class="audit-details-preview">{details_cell}</td>'
            f'</tr>'
            f'<tr class="audit-expand" id="{expand_id}" hidden>'
            f'<td colspan="6">{expanded_html}</td>'
            f'</tr>'
        )

    table_rows = "".join(_render_row(r) for r in rows) or (
        '<tr><td colspan="6" style="text-align:center;color:var(--text-tertiary);padding:24px">'
        'No audit entries match your filters.</td></tr>'
    )

    # ── Filter bar ─────────────────────────────────────────────────────
    action_value = request.query_params.get("action") or ""
    action_opts = "<option value=''>All actions</option>" + "".join(
        f'<option value="{a}"{" selected" if action_value == a else ""}>'
        f'{html.escape(_audit.ACTION_LABELS.get(a, a))}</option>'
        for a in sorted(_audit.ALL_ACTIONS)
    )
    admin_email_value = request.query_params.get("admin_email") or ""
    target_user_value = request.query_params.get("target_user_id") or ""
    target_type_value = request.query_params.get("target_type") or ""
    from_value = request.query_params.get("from") or ""
    to_value = request.query_params.get("to") or ""
    current_range = (request.query_params.get("range") or "").lower()

    admin_email_datalist = "".join(
        f'<option value="{html.escape(e)}">' for e in known_admin_emails
    )

    def _chip(slug: str, label: str) -> str:
        is_active = current_range == slug
        href = _audit_log_querystring(
            request.query_params,
            drop=("before_id", "from", "to"),
            range=(None if is_active else slug),
        ) or "/admin/audit-log"
        if href.startswith("?"):
            href = "/admin/audit-log" + href
        cls = "audit-chip" + (" audit-chip--on" if is_active else "")
        return f'<a class="{cls}" href="{html.escape(href)}">{html.escape(label)}</a>'

    chips_html = (
        f'{_chip("today", "Today")}'
        f'{_chip("24h", "Last 24h")}'
        f'{_chip("7d", "Last 7d")}'
        f'{_chip("30d", "Last 30d")}'
    )

    filters_html = (
        '<form method="get" action="/admin/audit-log" class="audit-filters">'
        '<div class="audit-filters-row">'
        f'<label class="audit-field"><span>Action</span>'
        f'<select name="action">{action_opts}</select></label>'
        f'<label class="audit-field"><span>Admin email</span>'
        f'<input type="text" name="admin_email" list="audit-admin-emails" '
        f'value="{html.escape(admin_email_value)}" placeholder="@narve.ai" autocomplete="off"></label>'
        f'<datalist id="audit-admin-emails">{admin_email_datalist}</datalist>'
        f'<label class="audit-field"><span>Target user id</span>'
        f'<input type="text" name="target_user_id" inputmode="numeric" '
        f'value="{html.escape(str(target_user_value))}" placeholder="e.g. 4012"></label>'
        f'<label class="audit-field"><span>Target type</span>'
        f'<input type="text" name="target_type" value="{html.escape(target_type_value)}" '
        f'placeholder="user / token / …"></label>'
        f'<label class="audit-field"><span>From</span>'
        f'<input type="date" name="from" value="{html.escape(from_value)}"></label>'
        f'<label class="audit-field"><span>To</span>'
        f'<input type="date" name="to" value="{html.escape(to_value)}"></label>'
        '<div class="audit-filters-actions">'
        '<button type="submit" class="btn">Apply filters</button>'
        '<a href="/admin/audit-log" class="btn btn--ghost">Reset</a>'
        '</div></div>'
        f'<div class="audit-chips" role="group" aria-label="Quick ranges">{chips_html}</div>'
        '</form>'
    )

    # ── Stats card ─────────────────────────────────────────────────────
    suspicious = stats.get("suspicious") or []
    if suspicious:
        suspicious_html = (
            '<div class="audit-flags">'
            '<div class="audit-flags-title">Suspicious patterns</div>'
            + "".join(
                f'<div class="audit-flag">'
                f'<span class="audit-flag-mark">!</span>'
                f'<span class="audit-flag-text">{html.escape(s["label"])} — '
                f'<strong>{html.escape(s["admin_email"])}</strong> · {s["count"]} hits '
                f'(threshold {s["threshold"]}) on '
                f'<span class="audit-mono">{html.escape(s["action"])}</span></span>'
                f'</div>'
                for s in suspicious
            )
            + '</div>'
        )
    else:
        suspicious_html = (
            '<div class="audit-flags audit-flags--clean">'
            '<div class="audit-flags-title">Suspicious patterns</div>'
            '<div class="audit-flag-clean">No threshold breaches in the filtered range.</div>'
            '</div>'
        )

    def _stat_list(items):
        if not items:
            return '<li class="audit-stat-empty">No data in range</li>'
        return "".join(
            f'<li><span class="audit-stat-name">{html.escape(str(name))}</span>'
            f'<span class="audit-stat-count audit-mono">{count}</span></li>'
            for name, count in items
        )

    top_action_labels = [
        (_audit.ACTION_LABELS.get(a, a), n) for a, n in stats.get("top_actions", [])
    ]
    stats_html = (
        '<div class="audit-stats">'
        '<div class="audit-stat-card">'
        '<div class="audit-stat-label">Events in range</div>'
        f'<div class="audit-stat-value audit-mono">{stats["total"]:,}</div>'
        '</div>'
        '<div class="audit-stat-card">'
        '<div class="audit-stat-label">Top actions</div>'
        f'<ul class="audit-stat-list">{_stat_list(top_action_labels)}</ul>'
        '</div>'
        '<div class="audit-stat-card">'
        '<div class="audit-stat-label">Top admins by event count</div>'
        f'<ul class="audit-stat-list">{_stat_list(stats.get("top_admins", []))}</ul>'
        '</div>'
        f'<div class="audit-stat-card audit-stat-card--flags">{suspicious_html}</div>'
        '</div>'
    )

    # ── Cursor pagination ──────────────────────────────────────────────
    pagination_bits: list[str] = [
        f'<span class="audit-page-summary">'
        f'Showing {len(rows)} of <span class="audit-mono">{total:,}</span> '
        f'matching event{"s" if total != 1 else ""}</span>'
    ]
    if before_id:
        first_href = _audit_log_querystring(request.query_params, drop=("before_id",))
        pagination_bits.append(
            f'<a class="audit-page-link" href="/admin/audit-log{first_href}">'
            '&larr; Newest</a>'
        )
    if next_cursor:
        next_href = _audit_log_querystring(
            request.query_params, drop=("before_id",), before_id=next_cursor
        )
        pagination_bits.append(
            f'<a class="audit-page-link" href="/admin/audit-log{next_href}">'
            'Older &rarr;</a>'
        )
    pagination_html = (
        '<div class="audit-pagination">' + " ".join(pagination_bits) + '</div>'
    )

    # ── CSV export link, preserves filters ─────────────────────────────
    csv_qs = _audit_log_querystring(request.query_params, drop=("before_id",))
    csv_link = f'/admin/audit-log/export.csv{csv_qs}'

    body = (
        '<div class="audit-page">'
        '<header class="audit-hero">'
        '<h2 class="audit-hero-title">Audit log</h2>'
        '<p class="audit-hero-sub">'
        'Append-only forensic record of every administrative action. '
        'Filter by admin, target user, or action; click a row for the full '
        'before / after payload, request id, and user agent.'
        '</p>'
        f'<a class="btn audit-csv-btn" href="{html.escape(csv_link)}">Export CSV</a>'
        '</header>'
        f'{stats_html}'
        f'{filters_html}'
        '<div class="audit-table-wrap">'
        '<table class="audit-table">'
        '<thead><tr>'
        '<th scope="col">Timestamp</th>'
        '<th scope="col">Admin</th>'
        '<th scope="col">Action</th>'
        '<th scope="col">Target</th>'
        '<th scope="col">IP</th>'
        '<th scope="col">Details</th>'
        '</tr></thead>'
        f'<tbody>{table_rows}</tbody>'
        '</table></div>'
        f'{pagination_html}'
        '<p class="audit-footer-note">'
        'Audit log is append-only. Entries cannot be deleted or edited. '
        'Suspicious-pattern flags are computed per-admin within the filtered range.'
        '</p>'
        '</div>'
    )

    return render_page(
        "audit_log",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_body=body,
        total_entries=str(total),
    )


@app.get("/admin/audit-log/export.csv")
async def admin_audit_log_csv(request: Request):
    """Streaming CSV export of audit_log, honouring the same filters as
    /admin/audit-log. Rate-limited per-admin to bound damage from a
    compromised credential — the streaming generator pages through the
    table 500 rows at a time so an unbounded query can't OOM the gateway.
    """
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    if isinstance(user, Response):
        return user

    # Per-admin rate limit: 6 CSV exports per 5 minutes. Generous for
    # genuine investigation work; tight enough that a stolen credential
    # can't be used to mass-exfiltrate the audit log.
    key = f"audit_csv:{user.get('email') or user.get('user_id')}"
    if _is_rate_limited(key, 6, 300):
        log.warning(
            "audit CSV export rate limit tripped for %s", user.get("email"),
        )
        raise HTTPException(status_code=429, detail="Too many CSV exports. Slow down.")

    from security import audit as _audit
    from fastapi.responses import StreamingResponse as _StreamingResponse

    filters = _audit.filter_to_search_kwargs(request.query_params)

    try:
        _audit.log_action(
            admin_user_id=user.get("user_id"),
            admin_email=user.get("email"),
            action="audit.csv_export",
            target_type="audit_log",
            request=request,
            notes=f"filters={filters}",
        )
    except Exception:
        pass  # never block the export on a log-write failure

    return _StreamingResponse(
        db.export_audit_csv_stream(filters),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="narve-audit-log.csv"',
            "Cache-Control": "no-store",
        },
    )


# ── Subproducts admin (MRR per sub-brand) ─────────────────────────────────────
#
# Rolls up active subscriptions on the per-dashboard ``subscriptions`` table,
# scoped to the thirteen sub-brand dashboard_keys in ``subproduct.SUBPRODUCTS``.
# A separate hero summarises the main-apex narve.ai Pro bundle so admins can
# see how many customers take the all-in subscription vs how many stack
# individual sub-products.
#
# All aggregates are SQL-side via ``queries/subscriptions.py``:
#   - ``get_active_subscription_counts_by_dashboard`` (active subscribers)
#   - ``get_mrr_by_dashboard`` (MRR in cents per subproduct)
#   - ``get_churn_rate`` (rolling 7-day churn)
#   - ``get_new_signups`` (rolling 30-day new signups)
#   - ``get_signups_daily_series`` (90-day sparkline)


@app.get("/admin/subproducts", response_class=HTMLResponse)
async def admin_subproducts_page(request: Request):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    from subproduct import SUBPRODUCTS as _SP, DASHBOARD_KEY_FOR_SLUG

    counts = db.get_active_subscription_counts_by_dashboard()
    mrr_by_dk = db.get_mrr_by_dashboard()

    # Pro tier rollup — main-apex bundle marker (dashboard_key="__plan__").
    # Pro price is £180/mo per the platform plan in DASHBOARDS["pro"].
    pro_active = int(counts.get("__plan__", 0))
    pro_mrr_gbp = pro_active * 180

    # Symmetric chart range across all subproducts so visual heights are
    # comparable. Capped at 1 so a brand-new product still renders bars.
    series_by_slug: dict[str, list[int]] = {}
    series_max = 1
    for slug in _SP.keys():
        dk = DASHBOARD_KEY_FOR_SLUG[slug]
        series = db.get_signups_daily_series(window_days=90, dashboard_key=dk)
        series_by_slug[slug] = series
        if series:
            series_max = max(series_max, max(series))

    rows_html: list[str] = []
    total_active = 0
    total_mrr_cents = 0
    for slug, cfg in _SP.items():
        dk = DASHBOARD_KEY_FOR_SLUG[slug]
        active = int(counts.get(dk, 0))
        mrr_cents = int(mrr_by_dk.get(dk, 0))
        churn = db.get_churn_rate(window_days=7, dashboard_key=dk)
        new_signups = db.get_new_signups(window_days=30, dashboard_key=dk)
        total_active += active
        total_mrr_cents += mrr_cents

        # 90-day signup sparkline — monochrome bars; height encodes count.
        bars = []
        series = series_by_slug.get(slug, [])
        for v in series:
            pct = 0 if series_max <= 0 else max(4, int(round((v / series_max) * 100)))
            bars.append(
                f'<span class="sp-bar" style="height:{pct}%" title="{v} new"></span>'
            )
        chart_html = (
            '<div class="sp-chart" aria-label="90-day new signups, monochrome bars">'
            + "".join(bars)
            + '</div>'
        )

        deep_link = f"https://{html.escape(slug)}.narve.ai/"
        churn_pct = f"{churn * 100:.1f}%"

        rows_html.append(
            '<article class="sp-card">'
            '<header class="sp-card-head">'
            f'<h3 class="sp-name">{html.escape(cfg["name"])}</h3>'
            f'<a class="sp-deep" href="{deep_link}" target="_blank" rel="noopener" '
            f'aria-label="Open {html.escape(cfg["name"])}">'
            f'<span class="sp-slug">{html.escape(slug)}.narve.ai</span>'
            '<span aria-hidden="true"> &rarr;</span>'
            '</a>'
            '</header>'
            '<div class="sp-grid">'
            '<div class="sp-stat">'
            '<div class="sp-stat-label">Active subscribers</div>'
            f'<div class="sp-stat-value">{active}</div>'
            '</div>'
            '<div class="sp-stat">'
            '<div class="sp-stat-label">MRR</div>'
            f'<div class="sp-stat-value">${mrr_cents / 100:,.2f}</div>'
            '</div>'
            '<div class="sp-stat">'
            '<div class="sp-stat-label">7-day churn</div>'
            f'<div class="sp-stat-value">{churn_pct}</div>'
            '</div>'
            '<div class="sp-stat">'
            '<div class="sp-stat-label">30-day new</div>'
            f'<div class="sp-stat-value">{new_signups}</div>'
            '</div>'
            '</div>'
            '<div class="sp-chart-wrap">'
            '<div class="sp-chart-label">90-day new signups</div>'
            f'{chart_html}'
            '</div>'
            '</article>'
        )

    sp_subs_total = total_active  # individual subproduct subscribers
    if (sp_subs_total + pro_active) > 0:
        mix_pro_pct = pro_active * 100.0 / (sp_subs_total + pro_active)
    else:
        mix_pro_pct = 0.0
    pro_vs_individual = f"{pro_active} : {sp_subs_total}"

    pro_rollup = (
        '<section class="sp-pro">'
        '<div class="sp-pro-head">'
        '<h2 class="sp-pro-title">narve.ai Pro</h2>'
        '<p class="sp-pro-sub">Bundle subscribers have access to every sub-product. '
        'Per-product totals below count individual subscriptions only.</p>'
        '</div>'
        '<div class="sp-pro-grid">'
        '<div class="sp-stat">'
        '<div class="sp-stat-label">Active Pro subs</div>'
        f'<div class="sp-stat-value">{pro_active}</div>'
        '</div>'
        '<div class="sp-stat">'
        '<div class="sp-stat-label">Pro MRR</div>'
        f'<div class="sp-stat-value">&pound;{pro_mrr_gbp:,}</div>'
        '</div>'
        '<div class="sp-stat">'
        '<div class="sp-stat-label">Pro vs individual</div>'
        f'<div class="sp-stat-value">{pro_vs_individual}</div>'
        f'<div class="sp-stat-foot">{mix_pro_pct:.1f}% Pro share</div>'
        '</div>'
        '<div class="sp-stat">'
        '<div class="sp-stat-label">Subproduct MRR (sum)</div>'
        f'<div class="sp-stat-value">${total_mrr_cents / 100:,.2f}</div>'
        '</div>'
        '</div>'
        '</section>'
    )

    body = (
        '<div class="sp-page">'
        '<header class="sp-page-head">'
        '<h1 class="sp-hero">Subproducts</h1>'
        '<p class="sp-hero-sub">13 sub-brands. Active subscriptions, MRR, '
        '7-day churn, 30-day new signups, and a 90-day signup sparkline '
        'for each. Pro bundle subscribers are tallied above and excluded '
        'from the per-product totals.</p>'
        '</header>'
        f'{pro_rollup}'
        '<section class="sp-list">'
        + "".join(rows_html) +
        '</section>'
        '</div>'
    )

    return render_page(
        "ai_usage",  # re-uses the existing admin shell template
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_body=body,
    )


# ── Settings ──────────────────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Perf audit #3: cache the bundle of DB reads that drives /settings (60s).
    # `subs` powers the default-dashboard <select>; the rest (market connections,
    # bankroll, plan info, trading-addon, env prefs) is rebuilt downstream from
    # cached primitives. HTML composition still runs per request so the sidebar,
    # density, and saved-banner stay personalised.
    user_id = user["user_id"]
    is_admin = bool(user.get("is_admin"))
    try:
        from cache import cache as _async_cache

        async def _build_settings_data() -> dict:
            return {
                "current_pref": db.get_default_dashboard(user_id) or "",
                "subs_list": [dict(r) for r in db.list_subscriptions(user_id)],
                "market_conns": _get_market_connections(user_id),
                "bankroll": db.get_user_bankroll(user_id),
                "trading_status": db.get_trading_addon_status(user_id),
                "env_prefs": db.get_user_env_preferences(user_id),
            }

        _cached = await _async_cache.get_or_set(
            f"settings:user:{user_id}", _build_settings_data, ttl_seconds=60,
        )
        current_pref = _cached["current_pref"]
        subs = {s["dashboard_key"]: s for s in _cached["subs_list"]}
        _cached_market_conns = _cached["market_conns"]
        _cached_bankroll = _cached["bankroll"]
        _cached_trading_status = _cached["trading_status"]
        _cached_env_prefs = _cached["env_prefs"]
    except Exception:
        current_pref = db.get_default_dashboard(user_id) or ""
        subs = {s["dashboard_key"]: dict(s) for s in db.list_subscriptions(user_id)}
        _cached_market_conns = _get_market_connections(user_id)
        _cached_bankroll = db.get_user_bankroll(user_id)
        _cached_trading_status = db.get_trading_addon_status(user_id)
        _cached_env_prefs = db.get_user_env_preferences(user_id)

    option_html = ['<option value="">Always show the dashboards hub</option>']
    for key, cfg in DASHBOARDS.items():
        has_access = _is_sub_active(subs.get(key), is_admin)
        if not has_access:
            continue
        selected = " selected" if key == current_pref else ""
        option_html.append(
            f'<option value="{html.escape(key)}"{selected}>'
            f'{html.escape(cfg["display_name"])}</option>'
        )

    saved_banner = ""
    if saved == "1":
        saved_banner = (
            '<div class="notice notice-success">'
            '<strong>Saved.</strong> Your landing preference has been updated.'
            '</div>'
        )

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""

    # Connected market accounts section
    market_conns = _cached_market_conns
    mc_html = (
        '<div class="settings-card" style="margin-top:24px">'
        '<div class="settings-section">'
        '<div class="settings-section-title">Connected Accounts</div>'
        '<div class="settings-section-desc">Connect your Polymarket wallet and Kalshi account to trade directly from any dashboard.</div>'
    )

    def _conn_row(label: str, sub: str, actions: str, with_border: bool = True) -> str:
        bd = ";border-bottom:1px solid var(--border)" if with_border else ""
        return (
            f'<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 0{bd}">'
            f'<div><div style="font-weight:600;font-size:14px">{label}</div>'
            f'<div style="font-size:12px;color:var(--text-muted)">{sub}</div></div>'
            f'<div>{actions}</div></div>'
        )

    def _disconnect_form(src: str, label: str = "Disconnect") -> str:
        confirm = f"{label} {src.capitalize()} account?"
        return (
            f'<form method="post" action="/settings/disconnect/{src}" style="display:inline">'
            f'<button type="submit" class="btn btn-danger" style="font-size:12px" '
            f'onclick="return confirm(\'{confirm}\')">{label}</button>'
            f'</form>'
        )

    kalshi = market_conns["kalshi"]
    k_status = kalshi.get("status") or "disconnected"
    if k_status == "active":
        sub = f'Member: {html.escape(kalshi.get("member_id") or "")}'
        mc_html += _conn_row("Kalshi", sub, _disconnect_form("kalshi"))
    elif k_status == "expired":
        sub = (
            'Session expired — reconnect to resume sync. '
            f'Member: {html.escape(kalshi.get("member_id") or "")}'
        )
        actions = (
            '<span style="font-size:12px;color:var(--text-muted);margin-right:8px">'
            'Reconnect from Markets tab</span>'
            + _disconnect_form("kalshi", "Forget")
        )
        mc_html += _conn_row("Kalshi", sub, actions)
    else:
        mc_html += _conn_row(
            "Kalshi", "Not connected",
            '<span style="font-size:12px;color:var(--text-muted)">'
            "Connect from any dashboard's Markets tab</span>",
        )

    poly = market_conns["polymarket"]
    p_status = poly.get("status") or "disconnected"
    addr = poly.get("address") or ""
    addr_display = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr
    if p_status == "active":
        mc_html += _conn_row(
            "Polymarket", f"Wallet: {html.escape(addr_display)}",
            _disconnect_form("polymarket"), with_border=False,
        )
    elif p_status == "expired":
        actions = (
            '<span style="font-size:12px;color:var(--text-muted);margin-right:8px">'
            'Reconnect from Markets tab</span>'
            + _disconnect_form("polymarket", "Forget")
        )
        mc_html += _conn_row(
            "Polymarket", f"Wallet: {html.escape(addr_display)} (disconnected)",
            actions, with_border=False,
        )
    else:
        mc_html += _conn_row(
            "Polymarket", "Not connected",
            '<span style="font-size:12px;color:var(--text-muted)">'
            "Connect from any dashboard's Markets tab</span>",
            with_border=False,
        )
    mc_html += '</div></div>'

    # Bankroll & Kelly fraction preferences
    br_info = _cached_bankroll
    bankroll_val = "" if br_info["bankroll"] is None else f'{br_info["bankroll"]:.2f}'
    kelly_opts: list[str] = []
    for _val, _label in (
        ("1.0", "Full Kelly"),
        ("0.5", "Half Kelly (recommended)"),
        ("0.25", "Quarter Kelly (conservative)"),
    ):
        sel = " selected" if abs(br_info["kelly_fraction"] - float(_val)) < 1e-6 else ""
        kelly_opts.append(f'<option value="{_val}"{sel}>{_label}</option>')
    mc_html += (
        '<div class="settings-card" style="margin-top:24px">'
        '<div class="settings-section">'
        '<div class="settings-section-title">Bet sizing</div>'
        '<div class="settings-section-desc">Your bankroll powers the Kelly calculator shown on every market. We never move funds — these numbers are reference only.</div>'
        '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px">'
        '<label class="settings-label" style="flex:1;min-width:200px">'
        '<span style="display:block;font-size:13px;color:var(--text-secondary);margin-bottom:6px">Bankroll (USD)</span>'
        f'<input id="bankroll-input" type="number" min="0" step="1" class="settings-select" '
        f'placeholder="e.g. 10000" value="{html.escape(bankroll_val)}" style="width:100%">'
        '</label>'
        '<label class="settings-label" style="flex:1;min-width:200px">'
        '<span style="display:block;font-size:13px;color:var(--text-secondary);margin-bottom:6px">Default Kelly fraction</span>'
        f'<select id="kelly-fraction-input" class="settings-select" style="width:100%">{"".join(kelly_opts)}</select>'
        '</label>'
        '</div>'
        '<div style="margin-top:14px">'
        '<button id="bankroll-save" type="button" class="btn btn-primary">Save</button>'
        '<span id="bankroll-save-msg" style="margin-left:12px;font-size:13px;color:var(--text-secondary)"></span>'
        '</div>'
        '</div></div>'
        '<script>'
        '(function(){'
        'var btn=document.getElementById("bankroll-save");'
        'var brInput=document.getElementById("bankroll-input");'
        'var kfInput=document.getElementById("kelly-fraction-input");'
        'var msg=document.getElementById("bankroll-save-msg");'
        'if(!btn||!brInput||!kfInput)return;'
        'btn.addEventListener("click", async function(){'
        'msg.textContent="Saving…";'
        'var body={bankroll:brInput.value===""?null:parseFloat(brInput.value),'
        'kelly_fraction:parseFloat(kfInput.value)};'
        'try{'
        'var r=await fetch("/api/v1/user/bankroll",{method:"PATCH",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});'
        'var d=await r.json();'
        'if(!r.ok){msg.textContent=d.error||("HTTP "+r.status);return;}'
        'msg.textContent="Saved.";'
        'setTimeout(function(){msg.textContent="";},2000);'
        '}catch(e){msg.textContent="Network error.";}'
        '});'
        '})();'
        '</script>'
    )

    # Billing / subscription section — reuses the cached `subs` dict from
    # earlier in the handler; second `list_subscriptions` round-trip removed.
    now_ts = int(time.time())
    pinfo = _user_plan_info(user, subs, now_ts)
    trading_status = _cached_trading_status

    billing_html = (
        '<div class="settings-card" style="margin-top:24px">'
        '<div class="settings-section">'
        '<div class="settings-section-title">Subscription</div>'
        f'<div style="display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">'
        f'<div><div style="font-weight:600;font-size:14px">Plan</div>'
        f'<div style="font-size:12px;color:var(--text-muted)">{(pinfo["plan"] or "none").title()}</div></div>'
        f'<span style="font-size:12px;color:var(--green);font-weight:600">Active</span>'
        f'</div>'
        f'<div style="display:flex;justify-content:space-between;padding:12px 0">'
        f'<div><div style="font-weight:600;font-size:14px">Trading Access</div>'
    )
    if trading_status["active"]:
        # TODO: Replace /enquire with Stripe add-on checkout when payments configured
        billing_html += (
            '<div style="font-size:12px;color:var(--green)">Active</div></div>'
            '<a href="/enquire" style="font-size:12px;color:var(--text-muted);text-decoration:none">Contact to manage</a>'
        )
    else:
        billing_html += (
            '<div style="font-size:12px;color:var(--text-muted)">Not active</div></div>'
            # TODO: Replace /enquire with Stripe add-on checkout when payments configured
            '<a href="/enquire" style="font-size:12px;color:var(--accent);text-decoration:none;font-weight:600">Add &pound;25/mo</a>'
        )
    billing_html += '</div></div></div>'


    sessions_html = (
        '<div class="settings-card" style="margin-top:24px">'
        '<div class="settings-section">'
        '<div class="settings-section-title">Active sessions</div>'
        '<div class="settings-section-desc">Every device where your account is signed in.</div>'
        '<div id="sessions-list" style="margin-top:12px;font-size:13px;color:var(--text-secondary)">Loading sessions…</div>'
        '<div style="padding:16px 0 0">'
        '<button type="button" id="sign-out-others-btn" class="btn">Sign out all other sessions</button>'
        '</div>'
        '</div></div>'
        '<script>'
        '(function(){'
        'var list=document.getElementById("sessions-list");'
        'var btn=document.getElementById("sign-out-others-btn");'
        'function csrf(){var m=document.cookie.match(/(?:^|;\\\\s*)_csrf=([^;]*)/);return m?decodeURIComponent(m[1]):"";}'
        'function load(){fetch("/api/auth/sessions").then(function(r){return r.json();}).then(function(d){'
        'if(!d.sessions||!d.sessions.length){list.textContent="No active sessions.";return;}'
        'list.innerHTML=d.sessions.map(function(s){'
        'var label=(s.browser||"Unknown")+" \u00b7 "+(s.os||"Unknown");'
        'var cur=s.is_current?"<span style=\\"margin-left:8px;padding:2px 8px;border-radius:9999px;background:var(--interactive-ghost);color:var(--text-primary);font-size:11px\\">Current</span>":"";'
        'var last=new Date(s.last_active_at*1000).toLocaleString();'
        'var act=s.is_current?"":("<button class=\\"btn btn-ghost\\" onclick=\\"revokeSession("+s.id+")\\">Revoke</button>");'
        'return "<div style=\\"display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)\\">"+'
        '"<div><div style=\\"color:var(--text-primary);font-weight:500\\">"+label+cur+"</div>"+'
        '"<div style=\\"font-size:11px;color:var(--text-tertiary);margin-top:2px\\">Last active: "+last+"</div></div>"+'
        '"<div>"+act+"</div></div>";'
        '}).join("");}).catch(function(){list.textContent="Failed to load sessions.";});}'
        'window.revokeSession=function(id){if(!confirm("Revoke this session?"))return;'
        'fetch("/api/auth/sessions/"+id,{method:"DELETE",headers:{"x-csrf-token":csrf()}}).then(load);};'
        'btn.addEventListener("click",function(){if(!confirm("Sign out of every other session?"))return;'
        'fetch("/api/auth/sessions",{method:"DELETE",headers:{"x-csrf-token":csrf()}}).then(load);});'
        'load();'
        '})();'
        '</script>'
    )

    # Environmental impact preferences (Feature 008)
    env_prefs = _cached_env_prefs
    env_show_checked = "checked" if env_prefs.get("show") else ""
    _env_unit = env_prefs.get("unit", "co2_mt")
    env_unit_flags = {
        "env_unit_co2_mt": "selected" if _env_unit == "co2_mt" else "",
        "env_unit_trees": "selected" if _env_unit == "trees" else "",
        "env_unit_cars": "selected" if _env_unit == "cars" else "",
        "env_unit_homes": "selected" if _env_unit == "homes" else "",
        "env_unit_flights": "selected" if _env_unit == "flights" else "",
    }

    nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="settings",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )
    return render_page(
        "settings", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        raw_options="".join(option_html),
        raw_saved_banner=saved_banner,
        raw_market_connections=mc_html,
        raw_billing_section=billing_html,
        raw_security_section=sessions_html,
        raw_admin_link=admin_link,
        raw_nav_role=nav_role, _is_admin=user.get("is_admin"),
        env_show_checked=env_show_checked,
        raw_sidebar=_sidebar,
        **env_unit_flags,
    )


@app.post("/settings/disconnect/{source}")
async def settings_disconnect_market(request: Request, source: str):
    """Disconnect a market account from settings page."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/settings/disconnect/{source}")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if source in ("polymarket", "kalshi"):
        db.disconnect_market_credential(user["user_id"], source)
        db.delete_user_positions(user["user_id"], platform=source)
        log.info("User %s disconnected %s from settings", user.get("username"), source)
        # Market connection state lives in the settings cache — bust it so the
        # post-redirect render reflects the disconnect immediately.
        try:
            from cache import cache as _async_cache
            await _async_cache.delete(f"settings:user:{user['user_id']}")
        except Exception:
            pass
    return RedirectResponse("/settings", status_code=302)


@app.get("/settings/integrations", response_class=HTMLResponse)
async def settings_integrations_page(request: Request):
    """Dedicated settings surface for Polymarket wallet + Kalshi token + bankroll.

    The page is read-only on the server — all state lives behind the
    market_routes JSON endpoints (/api/markets/connections,
    /api/user/bankroll). The template is a shell; settings_integrations.js
    hydrates it on load. Auth is required (redirect to /login), but the
    Trading Add-on check happens client-side via the JSON endpoints'
    403 response — that way users without the add-on still see the page
    and a clear "add-on required" toast rather than a 403 wall.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings/integrations")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    _username = user.get("username", user["email"])
    _admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    _nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="settings",
        username=_username,
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
    )
    return render_page(
        "settings_integrations",
        request=request,
        email=user["email"],
        username=_username,
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
        _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )



@app.get("/settings/trading-addon", response_class=HTMLResponse)
async def settings_trading_addon_page(request: Request):
    """Dedicated settings surface for the Trading Add-on."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings/trading-addon")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    _username = user.get("username", user["email"])
    _admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    _nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="settings",
        username=_username,
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
    )
    return render_page(
        "settings_trading_addon",
        request=request,
        email=user["email"],
        username=_username,
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
        _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )


@app.get("/api/trading-addon/config")
async def api_trading_addon_config_get(request: Request):
    """Return the user's add-on subscription status + saved settings."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    user_id = user["user_id"]
    is_admin = bool(user.get("is_admin"))
    status = db.get_trading_addon_status(user_id)
    active = bool(status.get("active")) or is_admin
    return JSONResponse({
        "active": active,
        "period_end": status.get("period_end"),
        "config": db.get_trading_addon_settings(user_id),
    })


@app.patch("/api/trading-addon/config")
async def api_trading_addon_config_patch(request: Request):
    """Upsert the user's trading-addon config; validates bounds."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    user_id = user["user_id"]
    is_admin = bool(user.get("is_admin"))
    if not (is_admin or db.has_trading_addon(user_id)):
        return JSONResponse(
            {"error": "Trading add-on required to save these settings."},
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    payload = {}

    if "kelly_fraction" in body:
        try:
            kf = float(body["kelly_fraction"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid kelly_fraction"}, status_code=400)
        if not any(abs(kf - v) < 1e-6 for v in (1.0, 0.5, 0.25)):
            return JSONResponse(
                {"error": "kelly_fraction must be 1.0, 0.5, or 0.25"},
                status_code=400,
            )
        payload["kelly_fraction"] = kf

    if "max_cap_pct" in body:
        try:
            mc = int(body["max_cap_pct"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid max_cap_pct"}, status_code=400)
        if not (1 <= mc <= 25):
            return JSONResponse(
                {"error": "max_cap_pct must be between 1 and 25"},
                status_code=400,
            )
        payload["max_cap_pct"] = mc

    if "auto_execute" in body:
        payload["auto_execute"] = bool(body["auto_execute"])

    if "auto_execute_min_ev" in body:
        raw = body["auto_execute_min_ev"]
        if raw is None:
            payload["auto_execute_min_ev"] = None
        else:
            try:
                ev = float(raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "Invalid auto_execute_min_ev"}, status_code=400,
                )
            if not (1 <= ev <= 50):
                return JSONResponse(
                    {"error": "auto_execute_min_ev must be between 1 and 50"},
                    status_code=400,
                )
            payload["auto_execute_min_ev"] = ev

    if "daily_cap" in body:
        raw = body["daily_cap"]
        if raw is None:
            payload["daily_cap"] = None
        else:
            try:
                dc = float(raw)
            except (TypeError, ValueError):
                return JSONResponse({"error": "Invalid daily_cap"}, status_code=400)
            if dc < 0 or dc > 1_000_000_000:
                return JSONResponse(
                    {"error": "daily_cap must be 0 or a positive number"},
                    status_code=400,
                )
            payload["daily_cap"] = dc

    if "daily_cap_currency" in body:
        cur = str(body["daily_cap_currency"] or "").upper().strip()
        if cur not in ("USD", "GBP"):
            return JSONResponse(
                {"error": "daily_cap_currency must be USD or GBP"},
                status_code=400,
            )
        payload["daily_cap_currency"] = cur

    if "max_position_size" in body:
        raw = body["max_position_size"]
        if raw is None:
            payload["max_position_size"] = None
        else:
            try:
                ps = float(raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "Invalid max_position_size"}, status_code=400,
                )
            if ps < 0 or ps > 1_000_000_000:
                return JSONResponse(
                    {"error": "max_position_size must be 0 or a positive number"},
                    status_code=400,
                )
            payload["max_position_size"] = ps

    if "cooldown_minutes" in body:
        raw = body["cooldown_minutes"]
        if raw is None:
            payload["cooldown_minutes"] = None
        else:
            try:
                cd = int(raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "Invalid cooldown_minutes"}, status_code=400,
                )
            if not (0 <= cd <= 1440):
                return JSONResponse(
                    {"error": "cooldown_minutes must be between 0 and 1440"},
                    status_code=400,
                )
            payload["cooldown_minutes"] = cd

    updated = db.upsert_trading_addon_settings(user_id, **payload)
    return JSONResponse(updated)



@app.post("/settings")
async def settings_save(
    request: Request,
    default_dashboard: str = Form(""),
    env_show: str = Form(""),
    env_unit: str = Form("co2_mt"),
):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Blank → clear preference. Otherwise must be a real dashboard key the
    # user has access to (admin bypasses the subscription check).
    key: Optional[str] = default_dashboard.strip() or None
    if key is not None:
        if key not in DASHBOARDS:
            return RedirectResponse("/settings", status_code=302)
        if not user.get("is_admin") and not db.has_active_subscription(user["user_id"], key):
            return RedirectResponse("/settings", status_code=302)

    db.set_default_dashboard(user["user_id"], key)

    # Environmental impact preferences (Feature 008). The checkbox sends
    # env_show=1 when ticked and is absent from the form data when unticked,
    # so any non-empty value is treated as True. Bad units silently fall
    # back to the default rather than 400-ing the whole settings POST.
    show = bool(env_show.strip())
    unit = env_unit.strip().lower() or "co2_mt"
    if unit not in db.ENV_VALID_UNITS:
        unit = "co2_mt"
    try:
        db.set_user_env_preferences(user["user_id"], show=show, unit=unit)
    except Exception as exc:
        log.warning("settings_save: env prefs failed for %d: %s", user["user_id"], exc)

    # Bust the user-scoped settings cache so the redirect lands on fresh data.
    # Cache layer hiccup mustn't block the save — swallow and rely on TTL.
    try:
        from cache import cache as _async_cache
        await _async_cache.delete(f"settings:user:{user['user_id']}")
    except Exception as exc:
        log.debug("settings_save: cache invalidation failed: %s", exc)

    return RedirectResponse("/settings?saved=1", status_code=302)


# ── Markets API (unified Polymarket + Kalshi) ────────────────────────────────
# These routes are handled by the gateway on ALL hosts (including subdomains).
# trade.js calls them from within each dashboard.

from backend.markets.polymarket_client import PolymarketClient
from backend.markets.kalshi_client import KalshiClient
from backend.markets import unified_markets
from backend.markets.portfolio_aggregator import get_combined_portfolio, get_combined_orders
from backend.markets.portfolio_signals import enrich_positions, signal_for_position
from backend.markets.encryption import encrypt_token, decrypt_token

POLY_CLIENT = PolymarketClient(
    gamma_base=os.environ.get("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"),
    clob_base=os.environ.get("POLYMARKET_CLOB_API", "https://clob.polymarket.com"),
)
KALSHI_CLIENT = KalshiClient(
    base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
    service_email=os.environ.get("KALSHI_SERVICE_EMAIL") or None,
    service_password=os.environ.get("KALSHI_SERVICE_PASSWORD") or None,
)
MARKETS_CACHE_TTL = max(60, min(3600, int(os.environ.get("MARKETS_CACHE_TTL", "300"))))


def _require_markets_user(request: Request) -> dict:
    """Require authenticated user with active Trading Add-on for markets access."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    # Admin bypasses all checks
    if user.get("is_admin"):
        return user
    # Require trading add-on (separate from base subscription)
    if not db.has_trading_addon(user["user_id"]):
        raise HTTPException(status_code=403, detail="Trading Add-on required. Contact us to add trading access.")
    return user


def _get_market_connections(user_id: int) -> dict:
    """Get user's market platform connection status.

    An inactive row (is_active=0 — typically a Kalshi token that 401'd)
    surfaces as connected=False with status='expired' so the UI can show
    a reconnect prompt instead of silently dropping the row."""
    creds = db.get_all_market_credentials(user_id)
    result = {
        "polymarket": {"connected": False, "status": "disconnected", "address": None, "last_synced_at": None},
        "kalshi": {"connected": False, "status": "disconnected", "member_id": None, "balance": None, "last_synced_at": None},
    }
    for c in creds:
        is_active = bool(c["is_active"]) if "is_active" in c.keys() else True
        last_synced_at = c["last_used_at"]
        if c["source"] == "polymarket" and c["polymarket_wallet_address"]:
            result["polymarket"]["address"] = c["polymarket_wallet_address"]
            result["polymarket"]["last_synced_at"] = last_synced_at
            if is_active:
                result["polymarket"]["connected"] = True
                result["polymarket"]["status"] = "active"
            else:
                result["polymarket"]["status"] = "expired"
        elif c["source"] == "kalshi" and c["kalshi_token"]:
            result["kalshi"]["member_id"] = c["kalshi_member_id"]
            result["kalshi"]["last_synced_at"] = last_synced_at
            if is_active:
                result["kalshi"]["connected"] = True
                result["kalshi"]["status"] = "active"
            else:
                result["kalshi"]["status"] = "expired"
    return result


# Registered below alongside the other extracted modules.
# Routes for /api/markets/*, /api/kelly/*, /api/user/bankroll live in market_routes.py.

# ── Switcher injection ────────────────────────────────────────────────────────


def _switcher_snippet(dashboard_key: str, user_id: int, apex: str = "") -> str:
    """Build the <script> tags that configure and load the dashboard switcher."""
    items = []
    for k, c in DASHBOARDS.items():
        if db.has_active_subscription(user_id, k):
            items.append({
                "key": k,
                "subdomain": c["subdomain"],
                "display_name": c["display_name"],
                "accent": c["accent"],
            })
    # Get username for the header bar
    user_row = db.get_user_by_id(user_id)
    username = user_row["username"] if user_row and "username" in user_row.keys() else ""

    # Determine plan tier for Markets tab gating
    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user_id)}
    user_dict = {
        "user_id": user_id,
        "is_admin": bool(user_row["is_admin"]) if user_row else False,
    }
    pinfo = _user_plan_info(user_dict, subs, int(time.time()))
    plan_tier = pinfo["plan"] or "none"
    has_markets_access = db.has_trading_addon(user_id)

    # Get market connections
    connections = _get_market_connections(user_id)

    # If the current dashboard_key maps to a sub-brand subproduct, publish
    # its slug + name so the switcher can render the "narve.ai / <slug>"
    # wordmark. Pure passthrough — the switcher decides whether to render it.
    from subproduct import SUBPRODUCTS as _SP
    subproduct_meta = None
    for _slug, _cfg in _SP.items():
        if _cfg.get("dashboard_key") == dashboard_key:
            subproduct_meta = {
                "slug": _slug,
                "name": _cfg["name"],
                "tagline": _cfg["tagline"],
            }
            break

    cfg_json = json.dumps({
        "dashboards": items,
        "current": dashboard_key,
        "domain": apex or DOMAIN,
        "username": username,
        "markets": {
            "enabled": has_markets_access,
            "plan": plan_tier,
            "connections": connections,
        },
        "subproduct": subproduct_meta,
    })
    return (
        f'<script>window.__hbSwitcher={cfg_json};</script>'
        f'<script src="/_gateway_static/switcher.js" defer></script>'
        f'<script src="/_gateway_static/trade.js" defer></script>'
    )


def _inject_switcher(content: bytes, content_type: str, key: str, user_id: int, apex: str = "") -> bytes:
    """Inject the switcher into HTML responses (before </body>)."""
    if "text/html" not in (content_type or ""):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    snippet = _switcher_snippet(key, user_id, apex=apex)
    # Case-insensitive replace; inject once before </body>
    lower = text.lower()
    idx = lower.rfind("</body>")
    if idx != -1:
        text = text[:idx] + snippet + text[idx:]
    else:
        text += snippet
    return text.encode("utf-8")


# ── Reverse proxy for dashboard subdomains ────────────────────────────────────


async def proxy_request(request: Request, forced_path: Optional[str] = None) -> Response:
    """Reverse-proxy the current request to the backend matching its subdomain."""
    # Route everything back to the apex the visitor actually came from
    # (habbig.com / narve.ai / …). Falling back to DOMAIN only protects
    # against entirely unknown hosts.
    apex = _request_apex(request) or DOMAIN
    sub = get_subdomain(request)
    key = SUBDOMAIN_TO_KEY.get(sub)
    if not key:
        # Unknown subdomain — redirect to apex.
        return RedirectResponse(f"https://{apex}/", status_code=302)

    dash_cfg = DASHBOARDS[key]

    # 1. Require login.
    user = current_user(request)
    if not user:
        return RedirectResponse(f"https://{apex}/gate", status_code=302)

    # 2. Require active subscription — but mirror the /dashboards hub logic
    # so admins and Pro/__plan__ subscribers don't get bounced to /billing
    # for dashboards that the hub already showed as "Active".
    is_admin = bool(user.get("is_admin"))
    has_access = is_admin or db.has_active_subscription(user["user_id"], key)
    if not has_access:
        # Pro plan (via __plan__ sentinel) unlocks every dashboard.
        try:
            _plan_subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
            _plan = _plan_subs.get("__plan__")
            if _plan and _plan["status"] == "active":
                _exp = _plan["expires_at"]
                if (not _exp or _exp > int(time.time())):
                    _raw = _plan["plan"] or ""
                    if _raw.startswith("pro"):
                        has_access = True
        except Exception:
            pass
    if not has_access:
        return RedirectResponse(
            f"https://{apex}/billing?dashboard={key}",
            status_code=302,
        )

    # 3. Forward the request.
    target_port = dash_cfg["target"]
    path = forced_path if forced_path is not None else request.url.path
    query = request.url.query
    upstream_url = f"http://127.0.0.1:{target_port}{path}"
    if query:
        upstream_url += f"?{query}"

    # Strip hop-by-hop headers; also strip any client-supplied X-Gateway-*
    # headers so a malicious client can't forge upstream identity.
    hop_by_hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host",
    }
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in hop_by_hop and not k.lower().startswith("x-gateway-")
    }
    fwd_headers["X-Gateway-User-Id"] = str(user["user_id"])
    fwd_headers["X-Gateway-User-Email"] = user["email"]
    # Shared secret lets downstream dashboards trust the identity headers
    # without relying on peer-IP checks (uvicorn's default proxy_headers=True
    # rewrites request.client.host from X-Forwarded-For, so IP-based trust
    # is unreliable). The secret lives only in gateway/.env.production and is
    # loaded into the same EnvironmentFile each dashboard service reads.
    #
    # Fail-closed: refuse to proxy when GATEWAY_SSO_SECRET is empty. Previously
    # we silently omitted X-Gateway-Secret, and if the downstream side was also
    # unset hmac.compare_digest("", "") would return True and accept
    # unauthenticated traffic. This guard applies in dev too (PRODUCTION=0
    # would otherwise still hit the compare path on the downstream side).
    _sso_secret = GATEWAY_SSO_SECRET or os.environ.get("GATEWAY_SSO_SECRET", "")
    if not _sso_secret:
        log.error(
            "proxy_request refusing to forward %s — GATEWAY_SSO_SECRET is unset",
            key,
        )
        return Response(
            content=b"Gateway misconfigured: SSO secret is unset",
            status_code=401,
            media_type="text/plain",
        )
    fwd_headers["X-Gateway-Secret"] = _sso_secret
    fwd_headers["X-Forwarded-Host"] = request.headers.get("host", "")
    fwd_headers["X-Forwarded-Proto"] = request.url.scheme

    body = await request.body()

    try:
        upstream = await HTTP_CLIENT.request(
            request.method,
            upstream_url,
            headers=fwd_headers,
            content=body,
            follow_redirects=False,
        )
    except httpx.ConnectError:
        return HTMLResponse(
            f"<h1>{html.escape(dash_cfg['display_name'])} is offline</h1>"
            f"<p>The backend on port {target_port} isn't responding. "
            f"Try <code>./start_dashboards.sh restart</code>.</p>",
            status_code=502,
        )
    except httpx.RequestError as e:
        log.exception("Upstream error for %s: %s", upstream_url, e)
        return HTMLResponse(
            f"<h1>Upstream error</h1><p>{html.escape(str(e))}</p>",
            status_code=502,
        )

    # Relay response; strip hop-by-hop headers AND content-encoding/length
    # from upstream because httpx already transparently decompresses gzip/br
    # responses. Leaving the upstream Content-Encoding+Content-Length pair in
    # the response causes uvicorn to error with "Response content longer than
    # Content-Length" when the decoded body is bigger than the compressed
    # header value — which silently produces empty 200s for every JSON API
    # served by an upstream that gzips by default (e.g. Flask + werkzeug).
    _drop_response_headers = hop_by_hop | {"content-encoding", "content-length"}
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _drop_response_headers
    }

    # Inject dashboard switcher into HTML responses. Pass apex so the
    # switcher builds subdomain URLs for whichever apex the user came from.
    body = _inject_switcher(
        upstream.content,
        upstream.headers.get("content-type", ""),
        key,
        user["user_id"],
        apex=apex,
    )
    # Always set the Content-Length to the actual decoded body length —
    # whether or not we injected. Starlette/uvicorn would compute it from
    # the body anyway, but being explicit avoids surprises.
    resp_headers["content-length"] = str(len(body))

    return Response(
        content=body,
        status_code=upstream.status_code,
        headers=resp_headers,
    )



# ── Credibility API ──────────────────────────────────────────────────────────


def _require_authenticated(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _require_pro_user(request: Request) -> dict:
    user = _require_authenticated(request)
    if user.get("is_admin"):
        return user
    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    now = int(time.time())
    pinfo = _user_plan_info(user, subs, now)
    if pinfo["plan"] != "pro":
        raise HTTPException(status_code=403, detail="Pro tier required")
    return user


# Credibility, backtests, retrospective, probability, environmental
# impact routes now live in intelligence_routes.py.

# ── Signal Search page ───────────────────────────────────────────────────────


@app.get("/signal-search", response_class=HTMLResponse)
async def signal_search_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signal-search")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not user.get("is_admin"):
        # Perf audit #3: cache the Pro-tier gate (30s — more dynamic, falls
        # back on miss). Builder returns the access verdict as a small dict so
        # it round-trips JSON safely; expensive part is db.list_subscriptions.
        _ss_user_id = user["user_id"]
        try:
            from cache import cache as _async_cache

            async def _build_signal_access() -> dict:
                _subs = {s["dashboard_key"]: dict(s)
                         for s in db.list_subscriptions(_ss_user_id)}
                _pinfo = _user_plan_info(user, _subs, int(time.time()))
                return {"plan": _pinfo["plan"]}

            _access = await _async_cache.get_or_set(
                f"signal_search:user:{_ss_user_id}",
                _build_signal_access,
                ttl_seconds=30,
            )
            if _access.get("plan") != "pro":
                return RedirectResponse("/billing", status_code=302)
        except Exception:
            subs = {s["dashboard_key"]: s for s in db.list_subscriptions(_ss_user_id)}
            pinfo = _user_plan_info(user, subs, int(time.time()))
            if pinfo["plan"] != "pro":
                return RedirectResponse("/billing", status_code=302)
    try:
        from engagement import log_event
        log_event(user["user_id"], "signal_search")
    except Exception:
        pass
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    role_badge = _role_badge(user)
    username = user.get("username", user["email"])
    # Always inject the Signal Search nav item with class="active" so the
    # current page highlights and the user can still see where they are
    # in the sidebar. The upgradeInjected JS preserves existing classes
    # while adding nav-item, so 'active' survives the upgrade.
    signal_link = '<a href="/signal-search" class="active" aria-current="page">Signal Search</a>'
    try:
        from sidebar import render_sidebar as _render_sidebar
        sidebar_html = _render_sidebar(
            request, active="signal-search",
            username=username,
            raw_admin_link=admin_link,
            raw_signal_search_link=signal_link,
            raw_nav_role=role_badge,
        )
    except Exception:
        sidebar_html = ""

    # Super-admin only: X bearer token panel. Hidden entirely for
    # non-admins (empty string) so the form / endpoint don't leak a
    # signal that an admin surface exists. Metadata is fetched
    # server-side once so the page doesn't have to make an extra
    # round-trip on first paint.
    admin_x_panel = ""
    if user.get("is_admin"):
        try:
            meta = db.system_secret_meta("signal_search.x_bearer") or {}
        except Exception:
            meta = {}
        if meta.get("set_at"):
            import datetime as _dt
            ago = int(time.time()) - int(meta["set_at"])
            days = max(1, ago // 86400) if ago >= 86400 else 0
            if days >= 1:
                state_when = f"{days} day{'s' if days != 1 else ''} ago"
            else:
                hrs = max(1, ago // 3600)
                state_when = f"{hrs} hour{'s' if hrs != 1 else ''} ago"
            who = html.escape(meta.get("set_by_username") or "system")
            length = int(meta.get("length") or 0)
            state = (
                f'Token set {state_when} by <code>{who}</code> '
                f'(length {length})'
            )
        else:
            state = "No token configured."
        # Inline JS so it sits next to the markup it controls.
        admin_x_panel = (
            '<section class="ss-admin-card" id="ss-admin-x">'
            '<span class="ss-admin-badge">Super-admin</span>'
            '<h2 class="ss-card-title">X (Twitter) bearer token</h2>'
            '<p class="ss-card-subtitle" style="margin:4px 0 14px">'
            'Used by the auto-pull worker to fetch posts. Encrypted at rest. '
            'Rotating here takes effect on the next pull cycle.</p>'
            f'<p class="ss-admin-state" id="ss-admin-x-state">{state}</p>'
            '<form class="ss-admin-form" id="ss-admin-x-form" autocomplete="off">'
            '<input type="password" id="ss-admin-x-input" name="token" '
            'placeholder="Paste new bearer token…" '
            'autocomplete="new-password" spellcheck="false">'
            '<button type="submit" class="primary">Save</button>'
            '<button type="button" class="danger" id="ss-admin-x-clear">Clear</button>'
            '</form>'
            '<p class="ss-admin-status" id="ss-admin-x-status" role="status" aria-live="polite"></p>'
            '<script>(function(){'
            'function csrf(){var m=document.cookie.match(/(?:^|;\\s*)_csrf=([^;]*)/);return m?decodeURIComponent(m[1]):"";}'
            'var f=document.getElementById("ss-admin-x-form");'
            'var inp=document.getElementById("ss-admin-x-input");'
            'var st=document.getElementById("ss-admin-x-status");'
            'var sd=document.getElementById("ss-admin-x-state");'
            'function refresh(){fetch("/api/admin/signal-search/x-token",{credentials:"same-origin"}).then(function(r){return r.json();}).then(function(j){if(!j.set){sd.textContent="No token configured.";return;}var t=new Date((j.set_at||0)*1000);sd.textContent="Token set "+t.toLocaleString()+" (length "+(j.length||0)+")";});}'
            'f.addEventListener("submit",function(e){e.preventDefault();st.textContent="Saving…";var fd=new FormData(); fd.append("token",inp.value); fetch("/api/admin/signal-search/x-token",{method:"POST",credentials:"same-origin",headers:{"X-CSRF-Token":csrf()},body:fd}).then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j};});}).then(function(res){if(res.ok){st.textContent="Saved.";st.style.color="var(--text-secondary)";inp.value="";refresh();}else{st.textContent=(res.body&&res.body.error)||"Save failed.";st.style.color="var(--red,#ef4444)";}}).catch(function(){st.textContent="Network error.";st.style.color="var(--red,#ef4444)";});});'
            'document.getElementById("ss-admin-x-clear").addEventListener("click",function(){if(!confirm("Clear the X bearer token? Auto-pull will fail until a new one is set."))return;fetch("/api/admin/signal-search/x-token",{method:"DELETE",credentials:"same-origin",headers:{"X-CSRF-Token":csrf()}}).then(function(r){return r.json();}).then(function(){st.textContent="Cleared.";st.style.color="var(--text-secondary)";refresh();});});'
            '})();</script>'
            '</section>'
        )

    return render_page(
        "signal-search",
        username=username,
        raw_admin_link=admin_link,
        raw_nav_role=role_badge,
        raw_sidebar=sidebar_html,
        raw_admin_x_token_panel=admin_x_panel,
        _is_admin=user.get("is_admin"),
    )



# Register feature routes (Features 1-10). Imported late so every helper
# in this module (current_user, render_page, CSRF, rate-limit, etc.) is
# already defined when server_features.py binds to them. Guarded so the
# main gateway still boots even if the features module is missing or
# broken — crashing the whole server because a feature file is absent
# would be a bad trade.
# ── Developer API v1 (F12) ────────────────────────────────────────────────
try:
    from api_v1 import router as _api_v1_router
    app.include_router(_api_v1_router)
except Exception as _exc:  # pragma: no cover
    log.warning("api_v1 router failed to mount: %s", _exc)

# Unified ⌘K search MUST register BEFORE server_features below, because
# server_features.py defines a legacy `/api/search` with a different
# response shape. FastAPI routes first-match, so the earliest registration
# wins — putting mine first lets the palette endpoint shadow the legacy
# one. (The legacy handler stays in server_features so existing callers
# that import the helper don't break; it's just not reachable via HTTP.)
try:
    import search_routes as _search_routes  # noqa: E402
    import sys as _sr2_sys
    if "search_routes" in _sr2_sys.modules:
        import importlib as _sr2_importlib
        _sr2_importlib.reload(_sr2_sys.modules["search_routes"])
    _search_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.warning("search_routes register failed: %s — continuing without it", _exc)


try:
    import server_features  # noqa: F401,E402
    # If server.py is being re-executed (e.g. via importlib.reload in tests),
    # the cached server_features module still references the OLD `app` and
    # its routes are missing from the new app. Force a reload so the
    # @app.get/@app.post decorators bind to the live FastAPI instance.
    import sys as _sys
    if "server_features" in _sys.modules:
        import importlib as _importlib
        _importlib.reload(_sys.modules["server_features"])
except Exception as _exc:  # pragma: no cover
    log.warning("server_features import failed: %s — continuing without it", _exc)

# Private affiliate program — routes, dashboards, admin panel.
# Same reload-safe pattern as server_features above so pytest's module-
# cache reuse doesn't re-register routes on the OLD `app`.
try:
    import affiliate_routes  # noqa: F401,E402
    import sys as _ar_sys
    if "affiliate_routes" in _ar_sys.modules:
        import importlib as _ar_importlib
        _ar_importlib.reload(_ar_sys.modules["affiliate_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("affiliate_routes import failed: %s — continuing without it", _exc)

# Forecast benchmark feature — /api/v1/forecasts/compare/<slug>,
# /dashboard/models (Pro), /admin/equivalences. Depends on migration 127
# having run (external_forecasts + market_equivalences tables). Defensive
# import so a stale dev DB without the migration doesn't break server start.
try:
    import forecast_routes  # noqa: F401,E402
    import sys as _fr_sys
    if "forecast_routes" in _fr_sys.modules:
        import importlib as _fr_importlib
        _fr_importlib.reload(_fr_sys.modules["forecast_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("forecast_routes import failed: %s — continuing without it", _exc)

# Public status page (/status) + admin incident management (/admin/status).
# Same reload-safe pattern as server_features above so pytest's module-cache
# reuse doesn't re-register routes on the OLD `app`.
try:
    import status_routes  # noqa: F401,E402
    import sys as _sr_sys
    if "status_routes" in _sr_sys.modules:
        import importlib as _sr_importlib
        _sr_importlib.reload(_sr_sys.modules["status_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("status_routes import failed: %s — continuing without it", _exc)

# Community Takes — paid subscribers annotate markets, others vote.
# Mounts /api/v1/markets/{slug}/takes, /api/v1/takes/*, /settings/takes,
# /admin/moderation. Reload-safe for pytest module-cache reuse.
try:
    import take_routes  # noqa: F401,E402
    import sys as _tr_sys
    if "take_routes" in _tr_sys.modules:
        import importlib as _tr_importlib
        _tr_importlib.reload(_tr_sys.modules["take_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("take_routes import failed: %s — continuing without it", _exc)


# Embed widgets (token-gated, domain-locked iframes for partner sites).
# Must register BEFORE the catch-all below so /embed/{widget_id} and
# /api/embeds/* hit embed_routes handlers rather than the subdomain proxy.
try:
    import embed_routes  # noqa: F401,E402
    import sys as _em_sys
    if "embed_routes" in _em_sys.modules:
        import importlib as _em_importlib
        _em_importlib.reload(_em_sys.modules["embed_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("embed_routes import failed: %s — continuing without it", _exc)


# Web Push subscription + delivery (/api/push/*). Registers BEFORE the
# catch-all so the /api/push/* paths hit our handlers. Same reload-safe
# pattern as notification_routes/embed_routes above.
try:
    import push_routes  # noqa: F401,E402
    import sys as _pr_sys
    if "push_routes" in _pr_sys.modules:
        import importlib as _pr_importlib
        _pr_importlib.reload(_pr_sys.modules["push_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("push_routes import failed: %s — continuing without it", _exc)


# Offline shell + /settings/offline. Same reload-safe late-import pattern
# as push_routes above. Must land before the catch-all so /offline and
# /settings/offline hit our handlers.
try:
    import offline_routes  # noqa: F401,E402
    import sys as _or_sys
    if "offline_routes" in _or_sys.modules:
        import importlib as _or_importlib
        _or_importlib.reload(_or_sys.modules["offline_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("offline_routes import failed: %s — continuing without it", _exc)


# Scheduled-job admin UI (/admin/jobs + /admin/api/jobs/*). Registers
# BEFORE the catch-all so the admin routes hit our handlers. Same
# reload-safe pattern as notification_routes / push_routes above.
try:
    import admin_jobs_routes  # noqa: F401,E402
    import sys as _ajr_sys
    if "admin_jobs_routes" in _ajr_sys.modules:
        import importlib as _ajr_importlib
        _ajr_importlib.reload(_ajr_sys.modules["admin_jobs_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_jobs_routes import failed: %s — continuing without it", _exc)


# Service health monitor (/admin/health-monitor + /api/admin/health-monitor).
# Single-pane status board for the gateway + 12 subproducts. Same reload-safe
# side-effect pattern — must sit before the catch-all so /admin/health-monitor
# isn't swallowed as a 404.
try:
    import admin_health_monitor_routes  # noqa: F401,E402
    import sys as _ahm_sys
    if "admin_health_monitor_routes" in _ahm_sys.modules:
        import importlib as _ahm_importlib
        _ahm_importlib.reload(_ahm_sys.modules["admin_health_monitor_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_health_monitor_routes import failed: %s — continuing without it", _exc)


# Anthropic AI cost-alerts dashboard (/admin/cost-alerts + /admin/api/ai-cost/*).
# Surfaces month-to-date spend, the 30-day chart, alert history, and the global
# kill-switch with a super-admin toggle. Same reload-safe side-effect pattern as
# admin_jobs_routes above — must sit before the catch-all so /admin/cost-alerts
# isn't swallowed as a 404.
try:
    import admin_cost_alerts_routes  # noqa: F401,E402
    import sys as _aca_sys
    if "admin_cost_alerts_routes" in _aca_sys.modules:
        import importlib as _aca_importlib
        _aca_importlib.reload(_aca_sys.modules["admin_cost_alerts_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_cost_alerts_routes import failed: %s — continuing without it", _exc)


# Admin email-template test harness (/admin/test-emails + /admin/test-emails/*).
# Preview + send-to-self for every template in email_system/templates/, so we
# can verify a render before pulling the trigger on a live cohort. Same reload-
# safe side-effect pattern as admin_cost_alerts_routes above — must sit before
# the catch-all so /admin/test-emails isn't swallowed as a 404.
try:
    import admin_test_emails_routes  # noqa: F401,E402
    import sys as _ate_sys
    if "admin_test_emails_routes" in _ate_sys.modules:
        import importlib as _ate_importlib
        _ate_importlib.reload(_ate_sys.modules["admin_test_emails_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_test_emails_routes import failed: %s — continuing without it", _exc)


# Outbound email queue + delivery review (/admin/emails + /admin/api/emails*).
# Diagnostic surface for every send_email background job — list, filter,
# inspect, resend. Same reload-safe side-effect pattern as
# admin_test_emails_routes above; must sit before the catch-all so
# /admin/emails isn't swallowed as a 404.
try:
    import admin_emails_routes  # noqa: F401,E402
    import sys as _aer_sys
    if "admin_emails_routes" in _aer_sys.modules:
        import importlib as _aer_importlib
        _aer_importlib.reload(_aer_sys.modules["admin_emails_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_emails_routes import failed: %s — continuing without it", _exc)


# Single-pane external-integration health (/admin/integrations + /api/admin/integrations*).
# Surfaces config + live state for every third-party SaaS (Stripe, Anthropic,
# Polymarket, Kalshi, SMTP, Sentry, BetterStack, Cloudflare). Same reload-safe
# side-effect pattern as admin_cost_alerts_routes above — must sit before the
# catch-all so /admin/integrations isn't swallowed as a 404.
try:
    import admin_integrations_routes  # noqa: F401,E402
    import sys as _ain_sys
    if "admin_integrations_routes" in _ain_sys.modules:
        import importlib as _ain_importlib
        _ain_importlib.reload(_ain_sys.modules["admin_integrations_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("admin_integrations_routes import failed: %s — continuing without it", _exc)


# Billing UI — /settings/billing + /api/v1/billing/*. Same reload-safe pattern
# as status_routes/embed_routes above; billing_routes registers its endpoints
# on server.app via @app.get/@app.post decorators as a side effect of import.
# MUST land before the catch-all so /settings/billing isn't eaten as a 404.
try:
    import billing_routes  # noqa: F401,E402
    import sys as _br_sys
    if "billing_routes" in _br_sys.modules:
        import importlib as _br_importlib
        _br_importlib.reload(_br_sys.modules["billing_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("billing_routes import failed: %s — continuing without it", _exc)


# Stripe webhook — POST /stripe/webhook. Replaces backend/payments/stripe_stub
# (which raises NotImplementedError). IP allowlist + signature verification +
# idempotency live in this module; helpers come from stripe_webhook_hardening.
# Same side-effect-of-import pattern as billing_routes above. MUST land before
# the catch-all so /stripe/webhook isn't 404'd.
try:
    import stripe_webhook_routes  # noqa: F401,E402
    import sys as _swr_sys
    if "stripe_webhook_routes" in _swr_sys.modules:
        import importlib as _swr_importlib
        _swr_importlib.reload(_swr_sys.modules["stripe_webhook_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("stripe_webhook_routes import failed: %s — continuing without it", _exc)


# Engagement / in-app re-engagement prompts. Same reload-safe side-effect
# pattern as billing_routes above — must sit before the catch-all so
# /api/engagement/* resolve on the apex rather than being swallowed.
try:
    import engagement_routes  # noqa: F401,E402
    import sys as _er_sys
    if "engagement_routes" in _er_sys.modules:
        import importlib as _er_importlib
        _er_importlib.reload(_er_sys.modules["engagement_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("engagement_routes import failed: %s — continuing without it", _exc)


# Public feedback + roadmap + admin triage (migration 130). Same
# reload-safe side-effect pattern — must sit before the catch-all or
# /feedback + /admin/feedback get swallowed as 404s.
try:
    import feedback_routes  # noqa: F401,E402
    import sys as _fb_sys
    if "feedback_routes" in _fb_sys.modules:
        import importlib as _fb_importlib
        _fb_importlib.reload(_fb_sys.modules["feedback_routes"])
except Exception as _exc:  # pragma: no cover
    log.warning("feedback_routes import failed: %s — continuing without it", _exc)


# Private referral + leaderboard router. Must sit before the catch-all
# below, same ordering rule as billing_routes / status_routes above —
# otherwise the catch-all swallows /invite/{code}, /settings/referrals,
# /leaderboard, and /api/referrals/me as 404s.
try:
    from routes_referrals import router as _referrals_router  # noqa: E402
    app.include_router(_referrals_router)
except Exception as _exc:  # pragma: no cover
    log.warning("routes_referrals import failed: %s — continuing without it", _exc)


# Share-artifacts + per-user invite-token router. Same ordering rule:
# the /s/m/{token} /s/s/{token} /s/p/{token} public pages + /og/shared/*
# image endpoints + /settings/invites + /tools/card-preview would all
# 404 via the catch-all if mounted below it.
try:
    from routes_sharing import router as _sharing_router  # noqa: E402
    app.include_router(_sharing_router)
except Exception as _exc:  # pragma: no cover
    log.warning("routes_sharing import failed: %s — continuing without it", _exc)


# Public OG card routes — /og/default, /og/pricing, /og/calendar,
# /og/source/{handle}, /og/market/{slug}. Defensive import so a missing
# Pillow / og_cards module doesn't block the rest of the mount graph;
# the base template's og_image reference would then just 404 and
# browsers fall back to no social preview.
try:
    import og_routes as _og_routes  # noqa: E402
    _og_routes.register(app)
except Exception as _exc:  # pragma: no cover
    log.warning("og_routes import failed: %s — continuing without it", _exc)


# Subproduct + portfolio + extension + bot routes. All registered via a
# ``register(app)`` function so server.py stays free of business logic.
# Same defensive try/except pattern as the rest of this section — one
# missing module should never take the whole gateway down.
for _mod_name in (
    "subproduct_signup_routes",
    "subproduct_dashboard_routes",
    "portfolio.routes",
    "extension_routes",
    "bot_routes",
    "security_routes",
    "collections_routes",
    "saved_views_routes",
):
    try:
        _mod = __import__(_mod_name, fromlist=["register"])
        _mod.register(app)
    except Exception as _exc:  # pragma: no cover
        log.warning(
            "%s register failed: %s — continuing without it", _mod_name, _exc,
        )


# Catch-all: anything that isn't an explicit apex route goes through the proxy.
# Excluded from OpenAPI — it's a routing fallback, not a documented endpoint,
# and the wildcard path produces a duplicate operation ID otherwise.
@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def catch_all(request: Request, full_path: str):
    sub = get_subdomain(request)
    if not sub:
        # Apex fallthrough — route through the branded error page so the
        # 404 surface is consistent with every other 404 in the app
        # (search box, curated top-links, JSON envelope for API callers).
        from error_handlers import render_error_page, is_api_request, _json_envelope, get_request_id, slug_for_status
        if is_api_request(request):
            return _json_envelope(
                status=404,
                slug=slug_for_status(404),
                message="Not found.",
                request_id=get_request_id(request),
            )
        return render_error_page(request, status=404)
    return await proxy_request(request)


# ── WebSocket proxy ───────────────────────────────────────────────────────────


@app.websocket("/{full_path:path}")
async def websocket_proxy(ws: WebSocket, full_path: str):
    # Extract subdomain from headers (WebSocket Request doesn't expose it the
    # same way). Iterate ALLOWED_DOMAINS so habbig.com and narve.ai subdomains
    # both resolve correctly.
    host = ws.headers.get("host", "").split(":")[0].lower()
    sub = ""
    matched = False
    for apex in ALLOWED_DOMAINS:
        if host == apex:
            matched = True
            break
        if host.endswith("." + apex):
            sub = host[: -(len(apex) + 1)]
            matched = True
            break
    if not matched and host.endswith(".localhost"):
        sub = host[: -len(".localhost")]

    key = SUBDOMAIN_TO_KEY.get(sub)
    if not key:
        await ws.close(code=1008, reason="Unknown subdomain")
        return

    # Origin check — WebSocket upgrades are NOT protected by the HTTP CSRF
    # middleware (no form body to cover). Without this, a malicious site could
    # open a cross-origin WS to a subdomain, the browser would attach the
    # user's session cookie automatically, and the attacker would hijack the
    # authenticated stream. Validate Origin against the configured apex list.
    if IS_PRODUCTION:
        origin = (ws.headers.get("origin") or "").lower().strip()
        if origin:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(origin)
            origin_host = (parsed.hostname or "").lower()
            allowed = False
            for apex in ALLOWED_DOMAINS:
                if origin_host == apex or origin_host.endswith("." + apex):
                    allowed = True
                    break
            if not allowed:
                # Audit MED FIX (audit_security_dir.md cross-cutting):
                # stamp ``host`` + ``ip_hash`` on every WS Origin denial
                # so the security feed can pivot from a single row to
                # either the requested vhost or the offending client
                # without exposing the raw peer IP. ``_hash_ip`` returns
                # "" when the peer is unknown — the log keeps its shape
                # rather than emitting 64 zeroes, matching the
                # analytics_events.ip_hash column contract.
                log.warning(
                    "ws origin rejected: origin=%s host=%s ip_hash=%s",
                    origin, host, _hash_ip(_get_client_ip(ws)),
                )
                await ws.close(code=1008, reason="Cross-origin upgrade denied")
                return
        else:
            # No Origin header in production is suspicious — browsers always
            # send one for cross-origin or same-origin WS. Reject rather than
            # fail-open, since legitimate clients always include it.
            # Same ``host`` + ``ip_hash`` shape as the cross-origin reject
            # above so both denial branches surface in identical columns
            # for the security feed.
            log.warning(
                "ws missing origin header from host=%s ip_hash=%s",
                host, _hash_ip(_get_client_ip(ws)),
            )
            await ws.close(code=1008, reason="Missing origin")
            return

    # Gate cookie check — the HTTP GateMiddleware enforces this for requests,
    # but WS upgrades bypass HTTP middleware entirely. An attacker with a
    # session cookie but no gate cookie could otherwise open a dashboard WS
    # while the site is still in pre-release.
    if SITE_ACCESS_TOKEN and not _gate_cookie_is_valid(ws.cookies.get(GATE_COOKIE_NAME, "")):
        await ws.close(code=1008, reason="Gate access required")
        return

    # Auth check via cookie (with dev-bypass for localhost).
    token = ws.cookies.get(COOKIE_NAME)
    session = db.get_session(token) if token else None
    user_id = session["user_id"] if session else None
    if not user_id and not IS_PRODUCTION:
        ws_host = ws.headers.get("host", "").split(":")[0].lower()
        if ws_host in ("localhost", "127.0.0.1") or ws_host.endswith(".localhost"):
            user_id = ensure_dev_user()
    if not user_id:
        await ws.close(code=1008, reason="Not authenticated")
        return
    if not db.has_active_subscription(user_id, key):
        await ws.close(code=1008, reason="No active subscription")
        return

    dash_cfg = DASHBOARDS[key]
    if not dash_cfg.get("supports_websocket"):
        await ws.close(code=1008, reason="Dashboard does not support WebSocket")
        return

    target_port = dash_cfg["target"]
    query = ws.url.query
    upstream_url = f"ws://127.0.0.1:{target_port}/{full_path}"
    if query:
        upstream_url += f"?{query}"

    await ws.accept()

    try:
        async with websockets.connect(upstream_url) as upstream_ws:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await ws.receive_text()
                        await upstream_ws.send(msg)
                except WebSocketDisconnect:
                    pass
                except Exception as ex:
                    log.warning("ws client→upstream error for %s: %s", upstream_url, ex)

            async def upstream_to_client():
                try:
                    async for msg in upstream_ws:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception as ex:
                    log.warning("ws upstream→client error for %s: %s", upstream_url, ex)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as e:
        log.warning("WebSocket proxy error for %s: %s", upstream_url, e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # AUDIT #4 MEDIUM #1 — bind loopback by default. Production runs uvicorn
    # via the deploy command which pins `--host 127.0.0.1`; this ``python -m
    # server`` path is dev-only, and 0.0.0.0 was the wrong default for a
    # gateway that's meant to sit behind Cloudflare Tunnel. Override with
    # GATEWAY_HOST= if you actually need LAN-reachable.
    _host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    uvicorn.run(
        "server:app",
        host=_host,
        port=GATEWAY_PORT,
        log_level="info",
    )
