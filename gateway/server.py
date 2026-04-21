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
import hmac
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, Response, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db

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

app = FastAPI(title="Polymarket Gateway", docs_url=None, redoc_url=None, openapi_url=None)

# Application metadata for /health and RUNBOOK tooling.
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
APP_ENVIRONMENT = os.environ.get("ENVIRONMENT", "production" if IS_PRODUCTION else "dev")
APP_START_TIME = time.time()

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

# Persistent httpx client for upstream proxying (connection pooling).
HTTP_CLIENT: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup():
    global HTTP_CLIENT
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
    if IS_PRODUCTION and not SITE_ACCESS_TOKEN:
        log.error("FATAL: PRODUCTION=1 but SITE_ACCESS_TOKEN is unset — refusing to start.")
        raise RuntimeError("SITE_ACCESS_TOKEN must be set in production")
    if IS_PRODUCTION and SITE_ACCESS_TOKEN and len(SITE_ACCESS_TOKEN) < 32:
        log.error("FATAL: SITE_ACCESS_TOKEN is too short (%d chars) — refusing to start.", len(SITE_ACCESS_TOKEN))
        raise RuntimeError("SITE_ACCESS_TOKEN must be at least 32 characters")
    # Auto-generate first admin invite token if none exist
    tokens = db.list_invite_tokens()
    if not tokens:
        first_token = db.create_invite_token("Auto-generated admin token")
        log.info("=" * 50)
        log.info("  FIRST ADMIN INVITE TOKEN: %s... (query DB for full value)", first_token[:12])
        log.info("=" * 50)

    # Run versioned migrations before anything else hits the DB.
    try:
        import migrations as _migrations
        _migrations.upgrade_to_head()
    except Exception as e:
        log.exception("migration upgrade failed at startup: %s", e)

    # If any user has TOTP enabled, the Fernet encryption key MUST be
    # configured — otherwise we can't decrypt existing secrets and those
    # admins would be locked out. Fail fast with a clear error.
    try:
        with db.conn() as _c:
            _totp_row = _c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE totp_enabled = 1"
            ).fetchone()
            _totp_users = int(_totp_row["n"] if _totp_row else 0)
        if _totp_users > 0 and not os.environ.get("CREDENTIALS_ENCRYPTION_KEY"):
            log.error(
                "FATAL: %d users have TOTP enabled but CREDENTIALS_ENCRYPTION_KEY "
                "is unset. Existing TOTP secrets cannot be decrypted. Refusing to start.",
                _totp_users,
            )
            if IS_PRODUCTION:
                raise RuntimeError(
                    "CREDENTIALS_ENCRYPTION_KEY required: existing TOTP secrets cannot be decrypted"
                )
    except RuntimeError:
        raise
    except Exception as e:
        log.warning("startup totp/encryption-key check failed: %s", e)

    # Start the background job queue (in-process by default).
    try:
        from jobs import start_worker as _start_worker
        await _start_worker()
    except Exception as e:
        log.exception("job queue start failed: %s", e)


@app.on_event("shutdown")
async def _shutdown():
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
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
if IS_PRODUCTION:
    SECURITY_HEADERS["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Reject oversized requests
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY:
            return JSONResponse({"error": "Request too large"}, status_code=413)
        # Embed widgets must render inside partner iframes, so /embed/*
        # opts out of the blanket X-Frame-Options: DENY and uses a
        # per-widget frame-ancestors CSP set by the route handler.
        is_embed = request.url.path.startswith("/embed/")
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            if is_embed and header == "X-Frame-Options":
                continue
            response.headers[header] = value
        # Honour a CSP already set by the route handler (embed routes set
        # their own with partner-specific frame-ancestors). Otherwise
        # install the strict site default — or the embed-safe default
        # for /embed/* error pages.
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                EMBED_CSP_DEFAULT if is_embed else CSP
            )
        # Prevent Cloudflare from caching HTML responses on the main site.
        # Embed responses get their own Cache-Control set by the handler
        # (short max-age so a sub lapse propagates quickly).
        ct = response.headers.get("content-type", "")
        if "text/html" in ct and not is_embed:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


app.add_middleware(SecurityHeadersMiddleware)

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

# Routes that skip CSRF validation (public GET-only, static files, proxied)
_CSRF_SKIP_PREFIXES = ("/_gateway_static", "/ws")

# POST endpoints exempt from CSRF because they have no user session to anchor
# a CSRF token to (called from public unauthenticated pages). These are still
# protected by per-IP rate limiting + email format validation.
_CSRF_EXEMPT_POSTS = frozenset({
    "/api/newsletter",
    # Invite-token bootstrap endpoint (token-first auth flow). Called from
    # /token before any session exists to anchor a CSRF token against.
    # Still protected by per-IP rate limiting (10 attempts / minute).
    "/auth/validate-token",
    # Public status page subscribe/unsubscribe. Called from the unauthenticated
    # /status page (no session to anchor CSRF to). Email format is validated
    # and the endpoint is read-only for unknown addresses, so bot noise is
    # bounded — no privileged state change is possible from a forgery.
    "/api/status/subscribe",
    "/api/status/unsubscribe",
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



# Note: /token, /register, /login, /auth/validate-token etc. are handled
# by server_features.py (token-first flow at lines 1135+). Don't add stubs
# here — they'd shadow the real handlers via FastAPI's first-match routing.


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
    - On POST requests: validates the submitted token (form field or header)
      matches the cookie value.
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

        # Exempt public POST endpoints that don't have a session to anchor to
        if request.method == "POST" and path in _CSRF_EXEMPT_POSTS:
            return await call_next(request)
        # Prefix-matched variants for dynamic path segments (e.g. invite/{code}).
        if request.method == "POST" and any(
            path.startswith(p) for p in _CSRF_EXEMPT_POST_PREFIXES
        ):
            return await call_next(request)

        if request.method == "POST":
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

            # Origin/Referer check as secondary defense. Compare against the
            # request's Host header rather than the configured DOMAIN — that
            # way a multi-domain front (habbig.com + narve.ai) still validates
            # cleanly without hardcoding each alias. Cross-origin POSTs are
            # rejected; same-origin POSTs (including subdomains sharing the
            # same apex) pass through.
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
                return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

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
    "/token", "/register", "/login", "/invite", "/signup",
    "/auth/validate-token", "/auth/register", "/auth/login", "/auth/logout",
    "/auth/forgot-password", "/auth/reset-password",
    "/forgot-password", "/reset-password",
    # Legal + marketing
    "/terms", "/privacy", "/dpa",
    "/unsubscribe",
    # Public API endpoints called from the prerelease page
    "/api/newsletter", "/api/newsletter/position",
    "/sitemap.xml", "/robots.txt",
    "/favicon.ico",
    "/.well-known/security.txt",
    # Public status page (incidents, uptime, component health, RSS, subscribe)
    "/status", "/status/feed.xml", "/status/unsubscribe",
    "/api/status", "/api/status/subscribe", "/api/status/unsubscribe",
    # PWA: fetched by browsers/OS installers before any session exists
    "/manifest.json", "/sw.js",
})
_PUBLIC_PREFIXES = ("/_gateway_static", "/sources/", "/auth/")


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
    """Admin "view as" — see impersonation.py for details."""
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


# ── Logging context middleware ───────────────────────────────────────────────
#
# Attaches a short request_id and (when resolvable) the user_id to every log
# record emitted during the request. Added LAST so it sits at the top of the
# middleware stack — that guarantees the context is set before any other
# middleware or handler logs.

import uuid as _uuid


class LoggingContextMiddleware(BaseHTTPMiddleware):
    """Attach request_id (and best-effort user_id) to logging context."""

    async def dispatch(self, request, call_next):
        request_id = _uuid.uuid4().hex[:8]
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
    # Auto-inject skeleton CSS (Feature 4) + skeleton JS library. Pages that
    # don't use them ignore the <link>/<script>; pages that need data loaders
    # can call `window.narveSkel.show(...)` without wiring anything.
    skel_injection = (
        '<link rel="stylesheet" href="/_gateway_static/skeletons.css">\n'
        '<script src="/_gateway_static/skeletons.js" defer></script>'
    )
    if "skeletons.js" not in page:
        lower = page.lower()
        head_idx = lower.rfind("</head>")
        if head_idx != -1:
            page = page[:head_idx] + skel_injection + "\n" + page[head_idx:]
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
    return HTMLResponse(page)


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
            ("/token", "Invite token gate (entry point)", "Public"),
            ("/register", "Create account (requires pending_token)", "Gated"),
            ("/login", "Sign in (requires pending_token)", "Gated"),
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
            ("/admin/tokens/generate", "Generate token", "Admin"),
            ("/admin/users/bulk", "Bulk user actions", "Admin"),
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


@app.get("/health", include_in_schema=False)
async def health_check():
    """Structured health report. Exposed publicly, no auth, no rate limit."""
    import datetime as _dt

    checks: dict = {}
    errors: list[str] = []

    db_status, db_err = _check_database()
    checks["database"] = db_status
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

    checks["email"] = _check_email_dry_run()

    # Critical checks — any error here downgrades the whole report to "error"
    # and returns HTTP 503. Non-critical warnings only flip to "degraded".
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
        "version": APP_VERSION,
        "environment": APP_ENVIRONMENT,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "checks": checks,
    }
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
    pills_html = "".join(
        f'<span class="pill">{html.escape(p)}</span>' for p in ctx["stat_pills"]
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
        raw_floating_numbers=floating_html,
        raw_stat_pills=pills_html,
    )


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
    """Static robots.txt — allow indexing of public pages, block auth/admin/API."""
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
        "Disallow: /token\n"
        "Disallow: /login\n"
        "Disallow: /signup\n"
        "Disallow: /register\n"
        "Disallow: /settings/\n"
        "Disallow: /embed/\n"
        "Disallow: /invite/\n"
        f"Sitemap: https://{apex}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain; charset=utf-8")


# Public pages included in the sitemap. Priorities are hand-tuned to match
# crawl-importance: homepage highest, legal pages lowest, source/calendar
# pages in between since they change frequently.
_SITEMAP_ENTRIES = [
    ("/",               "weekly",  "1.0"),
    ("/landing",        "weekly",  "0.9"),
    ("/pricing",        "monthly", "0.8"),
    ("/about",          "monthly", "0.8"),
    ("/how-it-works",   "monthly", "0.8"),
    ("/methodology",    "monthly", "0.7"),
    ("/faq",            "monthly", "0.7"),
    ("/narve",          "monthly", "0.7"),
    ("/calendar",       "hourly",  "0.7"),
    ("/terms",          "yearly",  "0.3"),
    ("/privacy",        "yearly",  "0.3"),
    ("/dpa",            "yearly",  "0.3"),
]


@app.get("/sitemap.xml")
async def seo_sitemap_xml(request: Request):
    """Auto-generated sitemap. Called on every crawl; cheap enough to render live."""
    import datetime as _dt
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


# ── Invite token entry (old gate, moved here) ───────────────────────────────


@app.get("/invite", response_class=HTMLResponse)
async def invite_page(request: Request):
    """Legacy alias — the invite-token entry point is now /token."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/invite")
    return RedirectResponse("/token", status_code=302)


@app.post("/invite")
async def invite_submit(request: Request, token: str = Form("")):
    """Legacy alias — POST /invite is replaced by POST /auth/validate-token."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/invite")
    return RedirectResponse("/token", status_code=302)


def _login_token_section(invite_token: str, email_hint: str) -> str:
    """Build the token section HTML for the login page (or empty for standalone login)."""
    if not invite_token:
        return ""
    return (
        f'<input type="hidden" name="invite_token" value="{html.escape(invite_token)}">'
        f'<label for="invite_token_display">Invite Token</label>'
        f'<input id="invite_token_display" type="text" value="{html.escape(invite_token)}" readonly class="token-display">'
        f'<div class="email-hint">{html.escape(email_hint)}</div>'
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Token-first login: requires a valid `pending_token` cookie.

    If the user is already authenticated → go to /dashboard.
    If no pending_token cookie → back to /token.
    If the token is unclaimed → send them to /register instead.
    Otherwise render login.html with the email pre-populated from the
    invite token's linked account.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")

    # Already logged in? Straight to dashboard.
    from auth.guards import read_hardened_session
    if read_hardened_session(request) or current_user(request):
        return RedirectResponse("/dashboards", status_code=302)

    # Must have come through /token
    from auth.cookies import read_pending_token
    raw_token = read_pending_token(request)
    if not raw_token:
        return RedirectResponse("/token", status_code=302)

    invite = db.get_invite_token(raw_token)
    if not invite or invite["status"] == "revoked":
        return RedirectResponse("/token", status_code=302)
    if invite["status"] != "claimed":
        # Unclaimed token → account creation flow
        return RedirectResponse("/register", status_code=302)

    email_hint = db.mask_email(invite["claimed_by_email"] or "") if invite["claimed_by_email"] else ""
    query_success = request.query_params.get("reset")
    success_html = ""
    if query_success == "success":
        success_html = "Password updated. Please sign in with your new password."
    return render_page(
        "login",
        request=request,
        error="",
        email_hint=email_hint,
        raw_success=success_html,
    )


@app.post("/login")
async def login_submit(request: Request):
    """Legacy POST /login form — replaced by POST /auth/login (JSON).

    Any client still posting to this path is routed back to /token so
    they re-enter through the token-first flow.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")
    return RedirectResponse("/token", status_code=302)


# ── Two-factor authentication ────────────────────────────────────────────────


async def _issue_email_otp(user_id: int, email: str, ip: str) -> None:
    """Generate, store, and email a fresh 6-digit OTP to the user.

    Rate-limited to one send per minute per user via the persistent bucket
    (`2fa_send:{user_id}`). If the rate limit has been tripped, this function
    returns silently (the previous code is still valid).
    """
    from security import two_factor as _tf
    if not _tf.can_resend_email_otp(user_id):
        return
    code = _tf.generate_email_otp()
    h, salt = _tf.hash_email_otp(code)
    db.insert_email_otp(user_id, h, salt, ip or "", _tf.EMAIL_OTP_TTL_SECONDS)
    _tf.mark_email_otp_sent(user_id)

    # Fire-and-forget email delivery
    try:
        from email_system.service import EmailService
        svc = EmailService()
        await svc.send_template(
            to=email,
            template="2fa_email_otp",
            context={
                "display_name": email.split("@")[0],
                "code": code,
                "expires_in": "10 minutes",
            },
        )
    except Exception as e:
        log.warning("email OTP send failed for user=%s: %s", email, e)


def _safe_json_2fa_body(body) -> dict:
    """Accept either JSON body or form data for 2FA verify routes."""
    if isinstance(body, dict):
        return body
    return {}


@app.get("/auth/2fa", response_class=HTMLResponse)
async def auth_2fa_page(request: Request):
    """Verification page shown after login for users with 2FA enabled."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if user.get("_dev_bypass"):
        return RedirectResponse("/dashboards", status_code=302)
    status = db.get_user_2fa_status(user["user_id"])
    if not status or not status["two_fa_method"]:
        return RedirectResponse("/auth/2fa/setup", status_code=302)
    # Already verified? Skip the page entirely.
    token = request.cookies.get(COOKIE_NAME) or ""
    if db.session_two_fa_verified(token):
        return RedirectResponse("/dashboards", status_code=302)

    method = status["two_fa_method"]
    masked = db.mask_email(user["email"]) if hasattr(db, "mask_email") else user["email"]
    return render_page(
        "auth_2fa",
        request=request,
        method=method,
        masked_email=masked,
        error="",
    )


@app.get("/auth/2fa/setup", response_class=HTMLResponse)
async def auth_2fa_setup_page(request: Request):
    """First-time 2FA enrollment wizard."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if user.get("_dev_bypass"):
        return RedirectResponse("/dashboards", status_code=302)
    status = db.get_user_2fa_status(user["user_id"])
    already_enabled = bool(status and status["two_fa_method"])
    return render_page(
        "auth_2fa_setup",
        request=request,
        already_enabled="1" if already_enabled else "",
        current_method=(status["two_fa_method"] if status else "") or "",
        user_email=user["email"],
    )


@app.get("/api/auth/2fa/status")
async def api_2fa_status(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    status = db.get_user_2fa_status(user["user_id"])
    method = (status["two_fa_method"] if status else None) or None
    remaining = db.count_remaining_backup_codes(user["user_id"]) if method else 0
    setup_at = (status["totp_setup_at"] if status else None) if method == "totp" else None
    verified_at = status["two_fa_verified_at"] if status else None
    return JSONResponse({
        "enabled": bool(method),
        "method": method,
        "backup_codes_remaining": remaining,
        "setup_at": setup_at,
        "last_verified_at": verified_at,
    })


@app.get("/api/auth/2fa/totp/setup")
async def api_2fa_totp_setup(request: Request):
    """Generate a fresh TOTP secret, stash it on the session, return QR."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    token = request.cookies.get(COOKIE_NAME) or ""
    if not token:
        raise HTTPException(status_code=401, detail="No active session")
    secret = _tf.generate_totp_secret()
    encrypted = _tf.encrypt_totp_secret(secret)
    db.set_pending_totp_secret(token, encrypted)
    uri = _tf.build_totp_uri(secret, user["email"])
    qr = _tf.build_qr_data_uri(uri)
    return JSONResponse({
        "qr_data_uri": qr,
        "manual_entry_key": secret,
        "issuer": _tf.TOTP_ISSUER,
        "account": user["email"],
    })


@app.post("/api/auth/2fa/totp/verify-setup")
async def api_2fa_totp_verify_setup(request: Request, code: str = Form("")):
    """Confirm the 6-digit code, enable TOTP, return backup codes exactly once."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # H2: cap setup-time brute force at 5 attempts per 10min per user.
    if _is_rate_limited(f"2fa-setup:{user['user_id']}", 5, 600):
        return JSONResponse(
            {"error": "Too many setup attempts. Try again in a few minutes."},
            status_code=429,
            headers={"Retry-After": "600"},
        )
    from security import two_factor as _tf
    token = request.cookies.get(COOKIE_NAME) or ""
    encrypted = db.get_pending_totp_secret(token)
    if not encrypted:
        return JSONResponse(
            {"error": "Setup session expired. Please restart."},
            status_code=400,
        )
    secret = _tf.decrypt_totp_secret(encrypted)
    if not _tf.verify_totp_code(secret, code):
        return JSONResponse({"error": "Invalid code. Try again."}, status_code=400)

    # Commit: persist encrypted secret, enable method, generate backup codes.
    db.set_user_2fa_method(user["user_id"], "totp", encrypted)
    db.clear_pending_totp_secret(token)
    codes = _tf.generate_backup_codes()
    hashed = _tf.hash_backup_codes(codes)
    db.store_backup_codes(user["user_id"], hashed)
    db.mark_session_two_fa_verified(token)

    try:
        if user["is_admin"]:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user["user_id"],
                admin_email=user["email"],
                action=_audit.AuditAction.ADMIN_2FA_SETUP,
                request=request,
                notes="totp",
            )
    except Exception:
        pass

    return JSONResponse({
        "success": True,
        "backup_codes": codes,  # plaintext — shown ONCE
        "message": "TOTP enabled. Save these backup codes somewhere safe.",
    })


@app.post("/api/auth/2fa/email/enable")
async def api_2fa_email_enable(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    db.set_user_2fa_method(user["user_id"], "email_otp", None)
    codes = _tf.generate_backup_codes()
    hashed = _tf.hash_backup_codes(codes)
    db.store_backup_codes(user["user_id"], hashed)
    token = request.cookies.get(COOKIE_NAME) or ""
    db.mark_session_two_fa_verified(token)

    try:
        if user["is_admin"]:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user["user_id"],
                admin_email=user["email"],
                action=_audit.AuditAction.ADMIN_2FA_SETUP,
                request=request,
                notes="email_otp",
            )
    except Exception:
        pass

    return JSONResponse({"success": True, "backup_codes": codes, "method": "email_otp"})


@app.post("/api/auth/2fa/verify")
async def api_2fa_verify(
    request: Request,
    code: str = Form(""),
    method: str = Form(""),
):
    """Verify a 2FA code during login. Accepts TOTP, email OTP, or backup code."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    ip = _get_client_ip(request)

    # H2: explicit per-user verification-attempt cap (5 per 10min). Stacks on
    # top of the _tf.is_2fa_locked lockout so a slow drip can't bypass it.
    if _is_rate_limited(f"2fa-verify:{user['user_id']}", 5, 600):
        return JSONResponse(
            {"error": "Too many attempts. Try again in a few minutes."},
            status_code=429,
            headers={"Retry-After": "600"},
        )

    if _tf.is_2fa_locked(user["user_id"], ip):
        return JSONResponse(
            {"error": "Too many attempts. Locked for 15 minutes."},
            status_code=429,
            headers={"Retry-After": "900"},
        )

    status = db.get_user_2fa_status(user["user_id"])
    if not status or not status["two_fa_method"]:
        return JSONResponse({"error": "2FA not enabled"}, status_code=400)

    user_method = status["two_fa_method"]
    success = False
    chosen_method = (method or user_method).strip().lower()

    if chosen_method == "backup_code":
        success = db.consume_backup_code(user["user_id"], code)
    elif chosen_method == "totp" and user_method == "totp":
        encrypted = status["totp_secret"]
        if encrypted:
            secret = _tf.decrypt_totp_secret(encrypted)
            success = _tf.verify_totp_code(secret, code)
    elif chosen_method == "email_otp" and user_method == "email_otp":
        otp_row = db.get_active_email_otp(user["user_id"])
        if otp_row:
            if _tf.verify_email_otp_code(code, otp_row["code_hash"], otp_row["code_salt"]):
                db.mark_email_otp_used(otp_row["id"])
                success = True

    _tf.record_2fa_attempt(user["user_id"], chosen_method, success, ip)
    if success:
        token = request.cookies.get(COOKIE_NAME) or ""
        db.mark_session_two_fa_verified(token)
        return JSONResponse({"verified": True, "redirect": "/dashboards"})

    return JSONResponse({"error": "Invalid code"}, status_code=403)


@app.post("/api/auth/2fa/email/resend")
async def api_2fa_email_resend(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    if not _tf.can_resend_email_otp(user["user_id"]):
        return JSONResponse(
            {"error": "Please wait before requesting another code."},
            status_code=429,
        )
    try:
        await _issue_email_otp(user["user_id"], user["email"], _get_client_ip(request))
    except Exception as e:
        log.warning("email OTP resend failed for user=%s: %s", user["email"], e)
        return JSONResponse({"error": "Failed to send code"}, status_code=500)
    return JSONResponse({"success": True})


@app.post("/api/auth/2fa/disable")
async def api_2fa_disable(request: Request, code: str = Form("")):
    """Disable 2FA. Requires a fresh TOTP/email OTP verification in the same call."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    ip = _get_client_ip(request)
    if _tf.is_2fa_locked(user["user_id"], ip):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    status = db.get_user_2fa_status(user["user_id"])
    if not status or not status["two_fa_method"]:
        return JSONResponse({"error": "2FA not enabled"}, status_code=400)

    method = status["two_fa_method"]
    verified = False
    if method == "totp" and status["totp_secret"]:
        secret = _tf.decrypt_totp_secret(status["totp_secret"])
        verified = _tf.verify_totp_code(secret, code)
    elif method == "email_otp":
        otp_row = db.get_active_email_otp(user["user_id"])
        if otp_row and _tf.verify_email_otp_code(code, otp_row["code_hash"], otp_row["code_salt"]):
            db.mark_email_otp_used(otp_row["id"])
            verified = True

    if not verified:
        _tf.record_2fa_attempt(user["user_id"], method, False, ip)
        return JSONResponse({"error": "Invalid code"}, status_code=403)

    db.disable_user_2fa(user["user_id"])
    _tf.record_2fa_attempt(user["user_id"], method, True, ip)

    try:
        if user["is_admin"]:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user["user_id"],
                admin_email=user["email"],
                action=_audit.AuditAction.ADMIN_2FA_DISABLE,
                request=request,
                notes=f"was_{method}",
            )
    except Exception:
        pass

    return JSONResponse({"success": True})


@app.post("/api/auth/2fa/backup-codes")
async def api_2fa_regenerate_backup_codes(request: Request, code: str = Form("")):
    """Regenerate the 8 backup codes. Requires fresh 2FA verification in this call."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from security import two_factor as _tf
    ip = _get_client_ip(request)
    if _tf.is_2fa_locked(user["user_id"], ip):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    status = db.get_user_2fa_status(user["user_id"])
    if not status or not status["two_fa_method"]:
        return JSONResponse({"error": "2FA not enabled"}, status_code=400)

    method = status["two_fa_method"]
    verified = False
    if method == "totp" and status["totp_secret"]:
        secret = _tf.decrypt_totp_secret(status["totp_secret"])
        verified = _tf.verify_totp_code(secret, code)
    elif method == "email_otp":
        otp_row = db.get_active_email_otp(user["user_id"])
        if otp_row and _tf.verify_email_otp_code(code, otp_row["code_hash"], otp_row["code_salt"]):
            db.mark_email_otp_used(otp_row["id"])
            verified = True

    if not verified:
        _tf.record_2fa_attempt(user["user_id"], method, False, ip)
        return JSONResponse({"error": "Invalid code"}, status_code=403)

    _tf.record_2fa_attempt(user["user_id"], method, True, ip)
    codes = _tf.generate_backup_codes()
    hashed = _tf.hash_backup_codes(codes)
    db.store_backup_codes(user["user_id"], hashed)
    return JSONResponse({"success": True, "backup_codes": codes})


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")
    return render_page("forgot-password", request=request, error="", success="")


@app.post("/forgot-password")
async def forgot_password_submit(request: Request, invite_token: str = Form(""), email: str = Form(""), new_password: str = Form(""), confirm_password: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")

    if _auth_rate_limited(_get_client_ip(request)):
        return RATE_LIMITED_RESPONSE

    invite_token = _bounded(invite_token, FIELD_MAX["invite_token"], "invite_token")
    email = _bounded(email, FIELD_MAX["email"], "email").lower()
    if len(new_password) > FIELD_MAX["password"] or len(confirm_password) > FIELD_MAX["password"]:
        return render_page("forgot-password", request=request, error="Invalid token or email.", success="")

    # Per-email rate limiting (3 reset attempts per email per hour, persistent).
    # Prevents an attacker from spamming reset attempts on a single victim's
    # account from many different IPs. The bucket is shared with any other
    # password-reset endpoint (key: "email:{email}:forgot") so an attacker
    # can't bypass the limit by alternating between endpoints.
    #
    # We only consume the bucket for syntactically-valid emails so random
    # garbage doesn't pollute the rate-limit table.
    ip = _get_client_ip(request)
    # Per-IP cap (H17): prevent email-bombing a victim via rotating accounts
    # on a single attacker IP. 10 attempts per 10 minutes per IP.
    if _is_rate_limited(f"ip:{ip}:forgot", 10, 600):
        log.warning("Password reset rate-limited by IP ip=%s", ip)
        return render_page(
            "forgot-password", request=request,
            error="Too many password reset attempts. Please wait and try again later.",
            success="",
        )
    if email and is_valid_email(email):
        if _is_rate_limited(f"email:{email}:forgot", 3, 3600):
            log.warning(
                "Password reset rate-limited for email=%s ip=%s",
                db.mask_email(email), ip,
            )
            # Generic error: don't reveal whether the rate limit was hit
            # vs. some other validation failure.
            return render_page(
                "forgot-password", request=request,
                error="Too many password reset attempts. Please wait and try again later.",
                success="",
            )

    # Validate token exists and is claimed
    invite = db.get_invite_token(invite_token) if invite_token else None
    if not invite or invite["status"] != "claimed":
        return render_page("forgot-password", request=request, error="Invalid or unclaimed token.", success="")

    # Verify email matches the token's linked account
    if invite["claimed_by_email"] != email:
        log.warning("Password reset: email mismatch for token. Provided: %s", db.mask_email(email))
        return render_page("forgot-password", request=request, error="Email does not match the account linked to this token.", success="")

    # Find the user
    user = db.get_user_by_id(invite["claimed_by_user_id"])
    if not user:
        # L14: don't confirm account existence via the reset flow.
        return render_page(
            "forgot-password", request=request,
            error="",
            success="If that account exists, a password-reset link has been sent.",
        )
    if user["suspended"]:
        return RedirectResponse("/suspended", status_code=302)

    # Validate passwords match
    if new_password != confirm_password:
        return render_page("forgot-password", request=request, error="Passwords don't match.", success="")

    # Validate password strength
    if len(new_password) < 12:
        return render_page("forgot-password", request=request, error="Password must be at least 12 characters.", success="")
    if not re.search(r"[A-Z]", new_password) or not re.search(r"[a-z]", new_password) or not re.search(r"[0-9]", new_password) or not re.search(r"[^A-Za-z0-9]", new_password):
        return render_page("forgot-password", request=request, error="Password must include uppercase, lowercase, number, and special character.", success="")

    # Update password
    pwd_hash, salt = db._hash_password(new_password)
    with db.conn() as c:
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwd_hash, salt, user["id"]))

    # Kill all existing sessions for this user (legacy + hardened).
    with db.conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    try:
        db.revoke_all_user_sessions(user["id"])
    except Exception as exc:
        log.error("Failed to revoke hardened sessions after reset for user_id=%d: %s", user["id"], exc)

    log.info("Password reset for user %s (id=%d) via token", user["username"] or user["email"], user["id"])
    return render_page("forgot-password", request=request, error="", success="Password reset successfully. All sessions have been logged out. You can now sign in with your new password.")


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Legacy alias — registration now happens at /register (requires pending_token)."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    return RedirectResponse("/token", status_code=302)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.post("/signup")
async def signup_submit(request: Request):
    """Legacy POST /signup — replaced by POST /auth/register (JSON, requires pending_token)."""
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    return RedirectResponse("/token", status_code=302)


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
    response = RedirectResponse("/token", status_code=302)
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


@app.get("/dashboards", response_class=HTMLResponse)
async def my_dashboards(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/dashboards")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin_user = bool(user.get("is_admin"))
    now = int(time.time())
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
            cta = f'<a class="card-cta cta-open" href="{open_url}" target="_blank">Open →</a>'
        else:
            cta = f'<a class="card-cta cta-sub" href="/billing?dashboard={key}" style="background:var(--accent);color:white;border-color:var(--accent)">Unlock</a>'

        cards_html.append(f"""
        <div class="dash-card" style="--accent: {cfg['accent']}">
          <div class="dash-card-head">
            <div class="dash-accent-dot"></div>
            {active_badge}
          </div>
          <div class="dash-card-title">{cfg['display_name']}</div>
          <div class="dash-card-desc">{cfg['description']}</div>
          <div class="dash-card-price">&pound;{cfg['monthly_cents']/100:.0f}/mo &middot; &pound;{cfg['annual_cents']/100:.0f}/yr</div>
          <div class="dash-card-foot">{cta}</div>
        </div>
        """)

    # Credits badge
    credits_badge = ""
    if is_admin_user:
        credits_badge = '<div style="position:absolute;top:0;right:0;background:var(--accent-light);color:var(--accent);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px">All Access</div>'
    elif pinfo["plan"] == "pro":
        credits_badge = '<div style="position:absolute;top:0;right:0;background:var(--green-bg);color:var(--green);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px">Pro — All Unlocked</div>'
    elif pinfo["plan"] == "trader":
        used = pinfo["active_count"]
        total = PLAN_DEFS["trader"]["credits"]
        remaining = total - used
        badge_color = "var(--green)" if remaining > 0 else "var(--amber)"
        badge_bg = "var(--green-bg)" if remaining > 0 else "rgba(245,158,11,0.10)"
        credits_badge = f'<div style="position:absolute;top:0;right:0;background:{badge_bg};color:{badge_color};font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px">Trader — {remaining}/{total} credits left</div>'
    else:
        credits_badge = '<div style="position:absolute;top:0;right:0;background:var(--surface-hover);color:var(--text-muted);font-size:12px;font-weight:600;padding:6px 14px;border-radius:20px">No plan — <a href="/billing" style="color:var(--accent)">Subscribe</a></div>'

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    # Signal Search link for Pro users
    signal_link = ""
    if pinfo["plan"] == "pro" or is_admin_user:
        signal_link = '<a href="/signal-search">Signal Search</a>'
    return render_page(
        "dashboards", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        dashboard_cards="".join(cards_html),
        raw_credits_badge=credits_badge,
        raw_signal_search_link=signal_link,
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
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
        return RedirectResponse("/token", status_code=302)

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
    return render_page(
        "billing", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        raw_plan_card=plan_card,
        raw_access_desc=access_desc,
        billing_rows="".join(rows_html),
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
    )


@app.post("/billing")
async def billing_action(request: Request, action: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/billing")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

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
    """Subscribe the logged-in user — Trader gets 3 credits, Pro gets all."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if plan not in ("trader", "pro"):
        return RedirectResponse("/billing", status_code=302)
    duration = 30 if interval == "monthly" else 365
    if plan == "pro":
        # Pro unlocks everything — clear old trader sentinel + old subs
        with db.conn() as c:
            c.execute("DELETE FROM subscriptions WHERE user_id = ? AND dashboard_key = '__plan__'", (user["user_id"],))
        # Subscribe to ALL dashboards as pro
        for key in DASHBOARDS:
            db.upsert_subscription(
                user_id=user["user_id"],
                dashboard_key=key,
                plan=f"pro_{interval}",
                duration_days=duration,
                source="billing_pro",
            )
    else:
        # Trader plan — check if downgrading from Pro
        subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
        now = int(time.time())
        current_pinfo = _user_plan_info(user, subs, now)

        if current_pinfo["plan"] == "pro" and current_pinfo["expires_at"]:
            # Downgrade: keep Pro access until current period ends, then switch
            # Mark all Pro subs with a "downgrading" flag in plan name
            with db.conn() as c:
                c.execute(
                    "UPDATE subscriptions SET plan = 'pro_downgrading' "
                    "WHERE user_id = ? AND dashboard_key != '__plan__' AND status = 'active'",
                    (user["user_id"],),
                )
            # Create Trader sentinel starting when Pro expires
            pro_end = current_pinfo["expires_at"]
            trader_duration = 30 if interval == "monthly" else 365
            with db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO subscriptions "
                    "(user_id, dashboard_key, plan, status, started_at, expires_at, source) "
                    "VALUES (?, '__plan__', ?, 'active', ?, ?, 'downgrade')",
                    (user["user_id"], f"trader_{interval}", pro_end, pro_end + trader_duration * 86400),
                )
            log.info("User %s scheduled downgrade from Pro to Trader at %d", user.get("username", user["email"]), pro_end)
        else:
            # Fresh Trader subscription
            db.upsert_subscription(
                user_id=user["user_id"],
                dashboard_key="__plan__",
                plan=f"trader_{interval}",
                duration_days=duration,
                source="billing_trader",
            )
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
        return RedirectResponse("/token", status_code=302)

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
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
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
    return {
        "username": user.get("username", user["email"]),
        "email": user["email"],
        "avatar_letter": avatar,
        "joined": joined,
        "raw_role_badge": role_badge,
        "raw_nav_role": _role_badge(user),
        "raw_admin_link": admin_link,
        "raw_banner": banner,
        "_is_admin": user.get("is_admin"),
    }


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    return render_page("profile", request=request, **_profile_context(user))


@app.post("/account/delete")
async def account_self_delete(
    request: Request,
    confirm_email: str = Form(""),
    confirm_password: str = Form(""),
):
    """User-initiated account deletion (GDPR Art. 17 — Right to Erasure).

    Requires the user to re-type their email + password to defuse accidental
    clicks and confirm ownership. Cascades across every user-scoped table
    and revokes all sessions before returning.
    """
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/account/delete")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

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
    log.info("account.delete: user_id=%d email=%s initiated self-delete", user_id, email)

    # Revoke sessions first so any outstanding cookie stops working mid-cascade.
    try:
        db.revoke_all_user_sessions(user_id)
    except Exception:
        pass

    deleted = db.cascade_delete_user(user_id)

    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user_id, admin_email=email,
            action=_audit.AuditAction.USER_DELETE_COMPLETED,
            target_type="user", target_id=user_id,
            target_description="self-delete",
            before={"email": email}, after=None, request=request,
        )
    except Exception:
        pass

    log.info("account.delete: user_id=%d cascade=%s", user_id, deleted)

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
        return RedirectResponse("/token", status_code=302)

    db_user = db.get_user_by_id(user["user_id"])
    if not db_user:
        return RedirectResponse("/token", status_code=302)

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
# DB is exfiltrated. Falls back to a fixed value in tests so the helper is
# deterministic without environment setup.
_IP_HASH_SALT = os.environ.get("IP_HASH_SALT", "narve.ai/analytics/v1")


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
        raw_token_section="",
        raw_footer_link='<a href="/gate">Have an invite token? Use it here</a>',
        raw_success='<div class="auth-success">Password reset successfully. You can now sign in.</div>',
    )


# ── Enquiry page + API ───────────────────────────────────────────────────────


@app.get("/enquire", response_class=HTMLResponse)
async def enquire_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/enquire")
    return render_page("enquire", request=request)


@app.post("/api/enquire")
async def api_enquire(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/enquire")
    ip = _get_client_ip(request)
    if _is_rate_limited(f"{ip}:enquire", _RATE_MAX_ENQUIRE):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    job_title = (body.get("job_title") or "").strip()
    message = (body.get("message") or "").strip()

    if len(email) > FIELD_MAX["email"] or len(job_title) > FIELD_MAX["enquiry_name"] or len(message) > FIELD_MAX["enquiry_message"]:
        return JSONResponse({"error": "One or more fields exceed maximum length"}, status_code=400)
    if not email or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if not job_title:
        return JSONResponse({"error": "Please select your role"}, status_code=400)
    if len(message) < 20:
        return JSONResponse({"error": "Please write at least 20 characters"}, status_code=400)
    if len(message) > 500:
        return JSONResponse({"error": "Message is too long (500 characters max)"}, status_code=400)

    db.create_enquiry(email, job_title, message)
    log.info("New enquiry from %s (%s)", email, job_title)

    # Notification email — enqueued via the job queue (Feature 10) so the
    # request returns immediately and failures retry automatically.
    enquiry_email = os.environ.get("ENQUIRY_EMAIL")
    if enquiry_email:
        try:
            from jobs.email_jobs import enqueue_email
            await enqueue_email(
                to=enquiry_email,
                template="enquiry_notification",
                context={
                    "enquiry_email": email,
                    "job_title": job_title,
                    "message": message,
                    "app_url": os.environ.get("APP_URL", "https://narve.ai"),
                },
                tags=["enquiry", "transactional"],
            )
            log.info("Enquiry notification enqueued for %s", enquiry_email)
        except Exception as exc:
            log.error("Failed to enqueue enquiry email: %s", exc)

    return JSONResponse({"success": True})


# ── Pricing / Subscribe / Support / Suspended ────────────────────────────────


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/pricing")
    return render_page("pricing", request=request)


@app.get("/subscribe", response_class=HTMLResponse)
async def subscribe_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/subscribe")
    return render_page("subscribe", request=request)


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/subscribe")
    ip = _get_client_ip(request)
    if _is_rate_limited(f"{ip}:subscribe", _RATE_MAX_SUBSCRIBE):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "").strip()
    interval = (body.get("interval") or "monthly").strip()

    if len(email) > FIELD_MAX["email"] or len(plan) > 32 or len(interval) > 16:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    if not email or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if plan not in ("trader", "pro"):
        return JSONResponse({"error": "Invalid plan"}, status_code=400)
    if interval not in ("monthly", "annual"):
        return JSONResponse({"error": "Invalid interval"}, status_code=400)

    # Generate an invite token for the new subscriber
    token = db.create_invite_token(
        note=f"Subscription: {plan} ({interval})",
        target_email=email,
    )
    log.info("Subscription checkout: %s -> %s (%s), token generated", email, plan, interval)
    return JSONResponse({"token": token})


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/support")
    return render_page("support", request=request)


@app.post("/api/support-ticket")
async def api_support_ticket(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/support-ticket")
    ip = _get_client_ip(request)
    if _is_rate_limited(f"{ip}:support", _RATE_MAX_SUPPORT):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    message = (body.get("message") or "").strip()

    if len(email) > FIELD_MAX["email"]:
        return JSONResponse({"error": "Email too long"}, status_code=400)
    if not email or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    if len(message) < 10:
        return JSONResponse({"error": "Please write at least 10 characters"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "Message is too long (2000 characters max)"}, status_code=400)

    db.create_enquiry(email, "Support Ticket", message)
    log.info("Support ticket from %s", email)
    return JSONResponse({"success": True})


@app.get("/suspended", response_class=HTMLResponse)
async def suspended_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/suspended")
    return render_page("suspended", request=request)


# ── Newsletter signup (pre-release waitlist) ─────────────────────────────
# The /prerelease page's "Notify me" form posts here. We store the email,
# assign a referral code, compute a waitlist position, and return the
# result as JSON so the frontend can show the number + share link.
#
# Rate limiting is layered:
#   - Per-IP:    5 signups per hour    (prevents a single origin spamming)
#   - Per-email: 3 position-checks + 1 new signup per day (the unique index
#                on the email column is the real "you can only sign up once"
#                guard — the per-email rate limit just prevents enumerating
#                positions by repeatedly POSTing different addresses)
#   - Global:    100 signups per hour (soft cap to flag bursts in logs)

_NEWSLETTER_RATE_MAX = 5              # per-IP new signups per hour
_NEWSLETTER_RATE_WINDOW = 3600        # 1 hour window
_NEWSLETTER_EMAIL_RATE_MAX = 5        # per-email attempts per day
_NEWSLETTER_EMAIL_RATE_WINDOW = 86400 # 24 hour window
_NEWSLETTER_GLOBAL_MAX = 100          # global signups per hour (alarm threshold)


async def _read_newsletter_body(request: Request) -> dict:
    """Accept either form-urlencoded OR JSON. The prerelease form posts
    urlencoded (because it's a plain <form> submit rewritten as fetch with
    URLSearchParams), but keep the JSON path so curl tests and API clients
    still work."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        return {
            "email": form.get("email", ""),
            "ref": form.get("ref", ""),
        }
    # Fallback: treat the body as JSON. request.json() raises on non-JSON.
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return {}
        return data
    except (_JSONDecodeError, ValueError, Exception):
        return {}


def _build_share_url(request: Request, referral_code: str) -> str:
    """Build the absolute share URL the frontend displays.

    We want the copied link to land on the same environment the visitor
    came from. Priority:
      1. If the request host is `staging.<apex>` (or any known non-apex
         subdomain we serve the landing page from), keep the full host so
         staging testers don't get bounced into production.
      2. Otherwise, fall back to the matching apex from ALLOWED_DOMAINS.
      3. Last resort, use the canonical DOMAIN.
    """
    host = _request_host(request)
    apex = _request_apex(request)
    if host and apex and host != apex:
        # Preserve explicit subdomains (staging.narve.ai, etc.)
        return f"https://{host}/?ref={referral_code}"
    return f"https://{apex or DOMAIN}/?ref={referral_code}"


@app.post("/api/newsletter")
async def api_newsletter(request: Request):
    body = await _read_newsletter_body(request)

    email = str(body.get("email") or "").strip().lower()
    ref = str(body.get("ref") or "").strip() or None

    # Clean validation, never leak DB details.
    if not email or len(email) > FIELD_MAX["email"] or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)

    ip = _get_client_ip(request)

    # Per-IP rate limit (new signups from the same network).
    if _is_rate_limited(f"{ip}:newsletter", _NEWSLETTER_RATE_MAX, _NEWSLETTER_RATE_WINDOW):
        return JSONResponse(
            {"error": "Too many signup attempts from your network. Try again in an hour."},
            status_code=429,
        )

    # Per-email rate limit (prevents enumerating positions by POSTing
    # different addresses repeatedly, and stops a bad actor from using
    # someone else's email to burn their attempts).
    if _is_rate_limited(
        f"newsletter_email:{email}", _NEWSLETTER_EMAIL_RATE_MAX, _NEWSLETTER_EMAIL_RATE_WINDOW
    ):
        return JSONResponse(
            {"error": "Too many attempts for this email. Try again tomorrow."},
            status_code=429,
        )

    # Global soft cap — doesn't block, just warns loudly so we can react
    # if someone's running a script against us at scale.
    if _is_rate_limited("newsletter_global", _NEWSLETTER_GLOBAL_MAX, _NEWSLETTER_RATE_WINDOW):
        log.warning(
            "newsletter signup global cap hit (>%d/hr) — possible spam run ip=%s",
            _NEWSLETTER_GLOBAL_MAX, ip,
        )

    try:
        result = db.subscribe_newsletter(email, source="prerelease", referred_by=ref)
    except Exception as exc:
        log.exception("subscribe_newsletter failed for email=%s: %s", db.mask_email(email), exc)
        return JSONResponse({"error": "Could not save your signup. Try again."}, status_code=500)

    share_url = _build_share_url(request, result["referral_code"])
    log.info(
        "newsletter signup ip=%s email=%s position=%d is_new=%s ref=%s",
        ip, db.mask_email(email), result["position"],
        result["is_new"], result["referred_by"] or "-",
    )
    return JSONResponse({
        "success": True,
        "is_new": result["is_new"],
        "position": result["position"],
        "referral_code": result["referral_code"],
        "share_url": share_url,
    })


@app.get("/api/newsletter/position")
async def api_newsletter_position(request: Request, email: str = ""):
    """Return the current waitlist position for an existing subscriber.

    Used by the prerelease page when a visitor returns via their own
    share link — we want to show them the current number, not assume
    their browser still has the sessionStorage we set at signup.
    """
    email = (email or "").strip().lower()
    if not email or len(email) > FIELD_MAX["email"] or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Invalid email"}, status_code=400)

    # Same per-email bucket as the signup endpoint so position checks count
    # against the email's daily cap too.
    if _is_rate_limited(
        f"newsletter_email:{email}", _NEWSLETTER_EMAIL_RATE_MAX, _NEWSLETTER_EMAIL_RATE_WINDOW
    ):
        return JSONResponse({"error": "Too many attempts. Try again tomorrow."}, status_code=429)

    result = db.get_newsletter_position(email)
    if not result:
        # Don't reveal whether the email exists — return a generic 404 shape.
        return JSONResponse({"error": "Not found"}, status_code=404)

    share_url = _build_share_url(request, result["referral_code"])
    return JSONResponse({
        "success": True,
        "position": result["position"],
        "referral_code": result["referral_code"],
        "share_url": share_url,
    })


# ── Admin panel ──────────────────────────────────────────────────────────────


def _two_fa_redirect(request: Request, user: dict) -> Optional[Response]:
    """Return a RedirectResponse if the user needs to complete 2FA, else None.

    Rules:
      - Dev bypass (`_dev_bypass=True`) is always allowed — localhost only.
      - If the user has no 2FA method configured, redirect to setup (grace
        period for existing admins from before this feature shipped).
      - If the user has 2FA configured but the current session's
        `two_fa_verified` flag is 0, redirect to the verification page.
    """
    if user.get("_dev_bypass"):
        return None
    try:
        status = db.get_user_2fa_status(user["user_id"])
    except Exception:
        status = None
    if not status or not status["two_fa_method"]:
        return RedirectResponse("/auth/2fa/setup", status_code=303)
    token = request.cookies.get(COOKIE_NAME) or ""
    if not db.session_two_fa_verified(token):
        return RedirectResponse("/auth/2fa", status_code=303)
    return None


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

    # 2FA enforcement: admin accounts must have a method configured AND the
    # current session must be freshly verified. Dev bypass skips this so
    # localhost development keeps working.
    two_fa_redirect = _two_fa_redirect(request, user)
    if two_fa_redirect is not None:
        if page:
            return two_fa_redirect  # caller must detect and return this
        # For API / POST routes, raise a 303 that the browser will follow
        raise HTTPException(
            status_code=303,
            detail="Two-factor verification required",
            headers={"Location": two_fa_redirect.headers.get("location", "/auth/2fa")},
        )

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


def _build_admin_context(new_token_str: str = "", caller_level: int = 1) -> dict:
    """Build the template context for the admin page."""
    tokens = db.list_invite_tokens()
    users = db.list_all_users()

    # Token rows HTML
    token_rows = []
    for t in tokens:
        status = t["status"]
        if status == "unclaimed":
            badge = '<span class="badge badge-active">Active</span>'
        elif status == "claimed":
            badge = '<span class="badge" style="background:var(--green-bg);color:var(--green)">Claimed</span>'
        else:
            badge = '<span class="badge" style="background:var(--red-bg);color:var(--red)">Revoked</span>'
        prefix = html.escape(t["token"][:8]) + "..." + html.escape(t["token"][-4:])
        meta_parts = []
        if t["claimed_by_email"]:
            meta_parts.append(f'User: {html.escape(t["claimed_by_email"])}')
        if t["note"]:
            meta_parts.append(html.escape(t["note"]))
        import datetime as _dt
        meta_parts.append(_dt.datetime.fromtimestamp(t["created_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
        if t["claimed_at"]:
            meta_parts.append(f'Claimed {_dt.datetime.fromtimestamp(t["claimed_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d")}')
        meta = " &middot; ".join(meta_parts)
        revoke_btn = ""
        if status == "unclaimed":
            revoke_btn = (
                f'<form method="post" action="/admin/tokens/revoke">'
                f'<input type="hidden" name="token_id" value="{t["id"]}">'
                f'<button type="submit" class="btn btn-danger">Revoke</button></form>'
            )
        token_rows.append(
            f'<div class="admin-row token-row" data-status="{status}">'
            f'<div class="admin-row-info"><div class="admin-row-main">'
            f'<span class="token-mono">{prefix}</span>{badge}</div>'
            f'<div class="admin-row-meta">{meta}</div></div>'
            f'<div class="admin-row-actions">{revoke_btn}</div></div>'
        )

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

            # Revoke token (admin+)
            if u["invite_token_id"]:
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/revoke-token" onclick="event.stopPropagation()" '
                    f'onsubmit="return confirm(\'Revoke token for {uname}? They will not be able to log in.\')"'
                    f' style="margin-top:8px">'
                    f'<button class="btn btn-danger" style="font-size:11px">Revoke Invite Token</button></form>'
                )

            # Generate new token for user (admin+)
            detail_extra += (
                f'<form method="post" action="/admin/users/{u["id"]}/new-token" onclick="event.stopPropagation()" '
                f'onsubmit="return confirm(\'Generate a new invite token for {uname}?\')" style="margin-top:8px">'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Generate New Token</button></form>'
            )

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

    # Stats
    total_users = len(users)
    active_tokens = sum(1 for t in tokens if t["status"] == "unclaimed")
    claimed_tokens = sum(1 for t in tokens if t["status"] == "claimed")
    revoked_tokens = sum(1 for t in tokens if t["status"] == "revoked")
    stat_cards = (
        f'<div class="stat-card"><div class="stat-label">Total Users</div><div class="stat-value">{total_users}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Active Tokens</div><div class="stat-value" style="color:var(--amber)">{active_tokens}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Claimed Tokens</div><div class="stat-value" style="color:var(--green)">{claimed_tokens}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Revoked Tokens</div><div class="stat-value" style="color:var(--red)">{revoked_tokens}</div></div>'
    )

    # New token banner
    new_token_banner = ""
    if new_token_str:
        new_token_banner = (
            f'<div class="new-token-banner">'
            f'<div style="display:flex;align-items:center;justify-content:space-between">'
            f'<div><div style="font-size:12px;color:var(--green);margin-bottom:4px">New token generated:</div>'
            f'<span class="token-mono">{html.escape(new_token_str)}</span></div>'
            f'<button onclick="copyToken(this)" class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Copy</button>'
            f'</div></div>'
        )

    # Revenue tab only for super admins (level >= 2)
    if caller_level >= 2:
        revenue_tab = '<button class="admin-tab" onclick="switchTab(\'revenue\')">Revenue</button>'
        revenue_content = _build_revenue_content()
    else:
        revenue_tab = ""
        revenue_content = '<div style="text-align:center;padding:48px 0;color:var(--text-muted)">Super admin access required.</div>'

    return {
        "raw_token_rows": "".join(token_rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No tokens yet.</div></div></div>',
        "raw_user_rows": "".join(user_rows),
        "raw_stat_cards": stat_cards,
        "raw_new_token_banner": new_token_banner,
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
        create_token_btn = (
            f'<form method="post" action="/admin/enquiries/{e["id"]}/create-token">'
            f'<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Create Token</button></form>'
        )
        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">{read_badge}<span style="font-weight:600">{html.escape(e["email"])}</span>'
            f' <span class="badge" style="background:var(--surface-hover);color:var(--text-secondary)">{html.escape(e["job_title"])}</span></div>'
            f'<div style="font-size:13px;color:var(--text-secondary);margin:8px 0;line-height:1.5">{html.escape(e["message"][:300])}</div>'
            f'<div class="admin-row-meta">{ts}</div>'
            f'</div>'
            f'<div class="admin-row-actions" style="display:flex;gap:6px">{create_token_btn}{mark_btn}</div></div>'
        )
    return "".join(rows)


def _build_revenue_content() -> str:
    import datetime as _dt
    stats = db.get_revenue_stats()
    subs = db.list_all_subscriptions()
    now = int(time.time())

    # Calculate MRR and ARR from active subscriptions using config prices
    mrr_cents = 0
    for s in subs:
        if s["status"] != "active":
            continue
        if s["expires_at"] and s["expires_at"] <= now:
            continue
        cfg = DASHBOARDS.get(s["dashboard_key"])
        if not cfg:
            continue
        if s["plan"] == "monthly":
            mrr_cents += cfg["monthly_cents"]
        elif s["plan"] == "annual":
            mrr_cents += cfg["annual_cents"] // 12

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
async def admin_page(request: Request):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    if isinstance(user, Response):
        return user  # 2FA setup or verification redirect
    ctx = _build_admin_context(caller_level=user.get("admin_level", 1))
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"), **ctx)


@app.post("/admin/tokens/generate")
async def admin_generate_token(request: Request, note: str = Form(""), target_email: str = Form("")):
    user = _require_admin_user(request)
    new_token = db.create_invite_token(note.strip(), target_email=target_email.strip())
    log.info("Admin %s generated invite token: %s... (target: %s)", user["email"], new_token[:8], target_email.strip() or "none")
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user["user_id"], admin_email=user["email"],
            action=_audit.AuditAction.TOKEN_GENERATE,
            target_type="token", target_id=new_token[:8],
            target_description=(target_email.strip() or note.strip() or None),
            after={"note": note.strip(), "target_email": target_email.strip()},
            request=request,
        )
    except Exception:
        pass
    ctx = _build_admin_context(new_token_str=new_token, caller_level=user.get("admin_level", 1))
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"), **ctx)


@app.post("/admin/tokens/revoke")
async def admin_revoke_token(request: Request, token_id: int = Form(0)):
    user = _require_admin_user(request)
    db.revoke_invite_token(token_id)
    log.info("Admin %s revoked token id=%d", user["email"], token_id)
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user["user_id"], admin_email=user["email"],
            action=_audit.AuditAction.TOKEN_REVOKE,
            target_type="token", target_id=token_id,
            request=request,
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


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


@app.post("/admin/enquiries/{enquiry_id}/create-token")
async def admin_create_token_from_enquiry(request: Request, enquiry_id: int):
    admin = _require_admin_user(request)
    enquiry = db.get_enquiry_by_id(enquiry_id)
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    email = enquiry["email"]
    new_token = db.create_invite_token(
        note=f"From enquiry: {email}",
        target_email=email,
    )
    db.mark_enquiry_read(enquiry_id)
    log.info("Admin %s created token %s... for enquiry %d (%s)", admin["email"], new_token[:8], enquiry_id, email)
    ctx = _build_admin_context(new_token_str=new_token, caller_level=admin.get("admin_level", 1))
    return render_page("admin", request=request, email=admin["email"], username=admin.get("username", admin["email"]), raw_nav_role=_role_badge(admin), _is_admin=admin.get("is_admin"), **ctx)


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


@app.post("/admin/users/{user_id}/revoke-token")
async def admin_revoke_user_token(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    user = db.get_user_by_id(user_id)
    if user and user["invite_token_id"]:
        db.revoke_invite_token(user["invite_token_id"])
    log.info("Super admin %s revoked token for user %d", admin["email"], user_id)
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.TOKEN_REVOKE,
            target_type="user", target_id=user_id,
            target_description=user["email"] if user else None,
            request=request, notes="revoke_from_user",
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/new-token")
async def admin_new_token_for_user(request: Request, user_id: int):
    admin = _require_admin_user(request)
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_token = db.create_invite_token(f"Replacement token for {user['username'] or user['email']}")
    db.claim_invite_token(new_token, user_id, user["email"])
    with db.conn() as c:
        c.execute("UPDATE users SET invite_token_id = (SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?", (new_token, user_id))
    log.info("Super admin %s generated new token %s... for user %d", admin["email"], new_token[:8], user_id)
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action=_audit.AuditAction.TOKEN_GENERATE,
            target_type="user", target_id=user_id,
            target_description=user["email"],
            request=request, notes="replacement_token",
        )
    except Exception:
        pass
    return RedirectResponse("/admin", status_code=302)


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
    with db.conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    log.info("Super admin %s deleted user id=%d (%s)", admin["email"], user_id, user["email"])
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
                with db.conn() as c:
                    c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                    c.execute("DELETE FROM subscriptions WHERE user_id = ?", (uid,))
                    c.execute("DELETE FROM users WHERE id = ?", (uid,))
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


# ── Admin: Audit log ─────────────────────────────────────────────────────────


@app.get("/admin/audit-log", response_class=HTMLResponse)
async def admin_audit_log_page(request: Request):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    if isinstance(user, Response):
        return user  # 2FA redirect
    from security import audit as _audit
    try:
        page = max(1, int(request.query_params.get("page") or "1"))
    except ValueError:
        page = 1
    filters = _audit.filter_to_query_kwargs(request.query_params)
    rows, total = db.query_audit_log(page=page, page_size=50, **filters)

    import datetime as _dt
    import json as _json

    def _render_row(r):
        ts = _dt.datetime.fromtimestamp(r["timestamp"], tz=_dt.timezone.utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        label = _audit.ACTION_LABELS.get(r["action"], r["action"])
        email = html.escape(r["admin_email"] or "—")
        target = html.escape(r["target_description"] or r["target_id"] or r["target_type"] or "—")
        ip = html.escape(r["ip_address"] or "—")
        before = r["before_state"] or ""
        after = r["after_state"] or ""
        details = ""
        if before or after:
            try:
                b_pretty = _json.dumps(_json.loads(before), indent=2) if before else ""
                a_pretty = _json.dumps(_json.loads(after), indent=2) if after else ""
            except Exception:
                b_pretty, a_pretty = before, after
            details = (
                "<details style='margin-top:6px'><summary style='cursor:pointer;color:var(--text-secondary);font-size:11px'>diff</summary>"
                f"<pre style='font-size:11px;color:var(--text-secondary);max-height:300px;overflow:auto'>before: {html.escape(b_pretty)}\n\nafter: {html.escape(a_pretty)}</pre>"
                "</details>"
            )
        return (
            f'<tr>'
            f'<td style="font-family:var(--font-mono);font-size:11px;white-space:nowrap">{ts_str}</td>'
            f'<td>{email}</td>'
            f'<td><span class="badge">{html.escape(label)}</span></td>'
            f'<td>{target}</td>'
            f'<td style="font-family:var(--font-mono);font-size:11px">{ip}</td>'
            f'<td>{details}</td>'
            f'</tr>'
        )

    table_rows = "".join(_render_row(r) for r in rows) or '<tr><td colspan="6" style="text-align:center;color:var(--text-tertiary);padding:24px">No audit entries match your filters.</td></tr>'

    # Filter form
    action_opts = "<option value=''>All actions</option>" + "".join(
        f'<option value="{a}"{" selected" if filters.get("action") == a else ""}>{html.escape(_audit.ACTION_LABELS.get(a, a))}</option>'
        for a in sorted(_audit.ALL_ACTIONS)
    )
    cur_target = filters.get("target_type") or ""
    cur_admin = filters.get("admin_user_id") or ""
    cur_from = request.query_params.get("from") or ""
    cur_to = request.query_params.get("to") or ""

    filters_html = (
        '<form method="get" action="/admin/audit-log" class="audit-filters" '
        'style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:16px">'
        f'<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-secondary)">Action<select name="action" style="padding:6px 10px;background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-primary);border-radius:6px;min-width:180px">{action_opts}</select></label>'
        f'<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-secondary)">Target type<input type="text" name="target_type" value="{html.escape(cur_target)}" placeholder="user / token / …" style="padding:6px 10px;background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-primary);border-radius:6px;width:140px"></label>'
        f'<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-secondary)">Admin ID<input type="text" name="admin_id" value="{html.escape(str(cur_admin))}" style="padding:6px 10px;background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-primary);border-radius:6px;width:80px"></label>'
        f'<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-secondary)">From<input type="date" name="from" value="{html.escape(cur_from)}" style="padding:6px 10px;background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-primary);border-radius:6px"></label>'
        f'<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-secondary)">To<input type="date" name="to" value="{html.escape(cur_to)}" style="padding:6px 10px;background:var(--bg-surface);border:1px solid var(--border-default);color:var(--text-primary);border-radius:6px"></label>'
        '<button type="submit" class="btn">Filter</button>'
        '<a href="/admin/audit-log" class="btn" style="text-decoration:none">Reset</a>'
        f'<a href="/admin/audit-log/export.csv?{request.url.query}" class="btn" style="text-decoration:none">Download CSV</a>'
        '</form>'
    )

    # Pagination
    pages = max(1, (total + 49) // 50)
    qs_base = {k: v for k, v in request.query_params.items() if k != "page"}
    def _link(p):
        qs = dict(qs_base, page=str(p))
        return "/admin/audit-log?" + "&".join(f"{k}={html.escape(v)}" for k, v in qs.items())
    pagination = (
        f'<div style="margin-top:16px;display:flex;gap:12px;align-items:center;color:var(--text-secondary);font-size:12px">'
        f'Page {page} of {pages} &middot; {total} entries'
    )
    if page > 1:
        pagination += f' &middot; <a href="{_link(page-1)}">← Prev</a>'
    if page < pages:
        pagination += f' &middot; <a href="{_link(page+1)}">Next →</a>'
    pagination += '</div>'

    body = (
        '<div style="padding:24px">'
        '<h2 style="font-family:var(--font-display);font-size:22px;margin:0 0 16px">Audit Log</h2>'
        f'{filters_html}'
        '<div style="overflow:auto;border:1px solid var(--border-default);border-radius:8px">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr style="background:var(--bg-surface);color:var(--text-secondary);text-align:left">'
        '<th style="padding:10px 12px">Timestamp</th>'
        '<th style="padding:10px 12px">Admin</th>'
        '<th style="padding:10px 12px">Action</th>'
        '<th style="padding:10px 12px">Target</th>'
        '<th style="padding:10px 12px">IP</th>'
        '<th style="padding:10px 12px">Details</th>'
        '</tr></thead>'
        f'<tbody>{table_rows}</tbody>'
        '</table></div>'
        f'{pagination}'
        '<p style="margin-top:24px;font-size:11px;color:var(--text-tertiary)">Audit log is append-only. Entries cannot be deleted or edited.</p>'
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
    _require_admin_user(request)  # auth side effect; user dict not needed below
    from security import audit as _audit
    filters = _audit.filter_to_query_kwargs(request.query_params)
    csv_text = db.export_audit_log_csv(**filters)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="narve-audit-log.csv"'},
    )


# ── Subproducts admin (MRR per sub-brand) ─────────────────────────────────────
#
# Rolls up active subscriptions on the existing per-dashboard `subscriptions`
# table, scoped to the six sub-brand dashboard_keys in subproduct.SUBPRODUCTS.
# Main-apex narve.ai Pro subscriptions (dashboard_key = "__plan__") count
# once in the "Bundle" row so the admin can see how many customers take the
# all-in subscription vs how many stack individual sub-products.


@app.get("/admin/subproducts", response_class=HTMLResponse)
async def admin_subproducts_page(request: Request):
    user = _require_admin_user(request, page=True)
    if user is None:
        return _denied_response(request)
    from subproduct import SUBPRODUCTS as _SP, DASHBOARD_KEY_FOR_SLUG

    now = int(time.time())
    subs = db.list_all_subscriptions()

    # Active = status=='active' AND (expires_at is null OR in the future).
    def _active(s) -> bool:
        if s["status"] != "active":
            return False
        if s["expires_at"] and s["expires_at"] <= now:
            return False
        return True

    by_key: dict[str, list] = {}
    for s in subs:
        if not _active(s):
            continue
        by_key.setdefault(s["dashboard_key"], []).append(s)

    # Subproduct rows
    rows_html: list[str] = []
    total_active = 0
    total_mrr_cents = 0
    for slug, cfg in _SP.items():
        dk = DASHBOARD_KEY_FOR_SLUG[slug]
        rows = by_key.get(dk, [])
        active = len(rows)
        # Per-product MRR: always the subproduct's monthly USD price — the
        # main-apex DASHBOARDS pricing in config.json tracks the *bundle*
        # tier and doesn't represent this sub-brand's standalone price.
        mrr_cents = int(round(cfg["price_usd"] * 100)) * active
        total_active += active
        total_mrr_cents += mrr_cents
        rows_html.append(
            f'<tr>'
            f'<td><span style="font-weight:500">{html.escape(cfg["name"])}</span>'
            f' <span style="color:var(--text-tertiary);font-family:var(--font-mono);font-size:11px">'
            f'{html.escape(slug)}.narve.ai</span></td>'
            f'<td style="text-align:right">{active}</td>'
            f'<td style="text-align:right;font-family:var(--font-mono)">${mrr_cents/100:,.2f}/mo</td>'
            f'</tr>'
        )

    bundle_rows = [s for s in subs if _active(s) and s["dashboard_key"] == "__plan__"]
    rows_html.append(
        f'<tr style="background:var(--bg-surface)">'
        f'<td><span style="font-weight:500">narve.ai Pro (bundle)</span>'
        f' <span style="color:var(--text-tertiary);font-size:11px">all six sub-products included</span></td>'
        f'<td style="text-align:right">{len(bundle_rows)}</td>'
        f'<td style="text-align:right;font-family:var(--font-mono)">—</td>'
        f'</tr>'
    )

    summary_cards = (
        f'<div class="stat-card"><div class="stat-label">Active subproduct subs</div>'
        f'<div class="stat-value">{total_active}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Subproduct MRR</div>'
        f'<div class="stat-value">${total_mrr_cents/100:,.2f}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Bundle subs</div>'
        f'<div class="stat-value">{len(bundle_rows)}</div></div>'
    )

    body = (
        '<div style="padding:24px">'
        '<h2 style="font-family:var(--font-display);font-size:22px;margin:0 0 16px">Subproducts</h2>'
        '<p style="color:var(--text-secondary);font-size:13px;margin:0 0 20px">'
        'Active subscriptions and MRR for each narve.ai sub-brand. Bundle subscribers '
        '(narve.ai Pro) have access to every sub-product automatically and are not counted '
        'in the per-product totals.'
        '</p>'
        f'<div class="stat-grid" style="margin-bottom:28px">{summary_cards}</div>'
        '<div style="overflow:auto;border:1px solid var(--border-default);border-radius:8px">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr style="background:var(--bg-surface);color:var(--text-secondary);text-align:left">'
        '<th style="padding:10px 12px">Product</th>'
        '<th style="padding:10px 12px;text-align:right">Active subs</th>'
        '<th style="padding:10px 12px;text-align:right">MRR</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table></div>'
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
        return RedirectResponse("/token", status_code=302)

    current_pref = db.get_default_dashboard(user["user_id"]) or ""
    # Subscriptions the user has access to (admins get everything).
    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin = bool(user.get("is_admin"))

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
    market_conns = _get_market_connections(user["user_id"])
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
    br_info = db.get_user_bankroll(user["user_id"])
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

    # Billing / subscription section
    subs_dict = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    now_ts = int(time.time())
    pinfo = _user_plan_info(user, subs_dict, now_ts)
    trading_status = db.get_trading_addon_status(user["user_id"])

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

    # Security (2FA) section
    sec_status = db.get_user_2fa_status(user["user_id"])
    two_fa_method = (sec_status["two_fa_method"] if sec_status else None) or None
    remaining = db.count_remaining_backup_codes(user["user_id"]) if two_fa_method else 0
    last_verified = sec_status["two_fa_verified_at"] if sec_status else None
    import datetime as _dt_sec
    last_verified_str = (
        _dt_sec.datetime.fromtimestamp(last_verified, tz=_dt_sec.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if last_verified else "Never"
    )
    method_label = {
        "totp": "Authenticator app (TOTP)",
        "email_otp": "Email one-time code",
    }.get(two_fa_method or "", "Not configured")

    if two_fa_method:
        sec_body = (
            f'<div style="display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">'
            f'<div><div style="font-weight:600;font-size:14px">Method</div>'
            f'<div style="font-size:12px;color:var(--text-muted)">{html.escape(method_label)}</div></div>'
            f'<span style="font-size:12px;color:var(--green);font-weight:600">Enabled</span></div>'
            f'<div style="display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">'
            f'<div><div style="font-weight:600;font-size:14px">Last verified</div>'
            f'<div style="font-size:12px;color:var(--text-muted)">{last_verified_str}</div></div></div>'
            f'<div style="display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">'
            f'<div><div style="font-weight:600;font-size:14px">Backup codes</div>'
            f'<div style="font-size:12px;color:var(--text-muted)">{remaining} of 8 remaining</div></div></div>'
            '<div style="display:flex;gap:8px;padding:16px 0 0;flex-wrap:wrap">'
            '<a href="/auth/2fa/setup" class="btn">Change method</a>'
            '<a href="/auth/2fa/setup?regen=1" class="btn">Regenerate backup codes</a>'
            '<a href="/auth/2fa/setup?disable=1" class="btn btn-danger">Disable 2FA</a>'
            '</div>'
        )
    else:
        sec_body = (
            '<div style="padding:12px 0;color:var(--text-secondary);font-size:13px;line-height:1.55">'
            'Two-factor authentication adds a second verification step at login. '
            'Admin accounts are required to enable 2FA before accessing the admin panel.'
            '</div>'
            '<div style="padding:16px 0 0"><a href="/auth/2fa/setup" class="btn btn-primary">Enable 2FA</a></div>'
        )

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

    security_html = (
        '<div class="settings-card" style="margin-top:24px">'
        '<div class="settings-section">'
        '<div class="settings-section-title">Security</div>'
        '<div class="settings-section-desc">Two-factor authentication and account security.</div>'
        f'{sec_body}'
        '</div></div>'
        f'{sessions_html}'
    )

    # Environmental impact preferences (Feature 008)
    env_prefs = db.get_user_env_preferences(user["user_id"])
    env_show_checked = "checked" if env_prefs.get("show") else ""
    _env_unit = env_prefs.get("unit", "co2_mt")
    env_unit_flags = {
        "env_unit_co2_mt": "selected" if _env_unit == "co2_mt" else "",
        "env_unit_trees": "selected" if _env_unit == "trees" else "",
        "env_unit_cars": "selected" if _env_unit == "cars" else "",
        "env_unit_homes": "selected" if _env_unit == "homes" else "",
        "env_unit_flights": "selected" if _env_unit == "flights" else "",
    }

    return render_page(
        "settings", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        raw_options="".join(option_html),
        raw_saved_banner=saved_banner,
        raw_market_connections=mc_html,
        raw_billing_section=billing_html,
        raw_security_section=security_html,
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
        env_show_checked=env_show_checked,
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
        return RedirectResponse("/token", status_code=302)
    if source in ("polymarket", "kalshi"):
        db.disconnect_market_credential(user["user_id"], source)
        db.delete_user_positions(user["user_id"], platform=source)
        log.info("User %s disconnected %s from settings", user.get("username"), source)
    return RedirectResponse("/settings", status_code=302)


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
        return RedirectResponse("/token", status_code=302)

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


# Market data (public data, but requires Tier 1+ auth)
@app.get("/api/markets/unified")
async def api_markets_unified(
    request: Request,
    category: str = "",
    search: str = "",
    sort: str = "volume",
    source: str = "",
    page: int = 1,
    limit: int = 20,
    env_relevant: int = 0,
):
    _require_markets_user(request)  # auth + add-on check; user dict not used below
    # Clamp pagination params to prevent division by zero and negative indexing
    if limit < 1 or limit > 100:
        limit = 20
    if page < 1:
        page = 1
    markets = await unified_markets.fetch_unified_markets(
        POLY_CLIENT, KALSHI_CLIENT, cache_ttl=MARKETS_CACHE_TTL,
    )
    filtered = unified_markets.filter_markets(
        markets, category=category, source=source, search=search, sort=sort,
    )
    # env_relevant filter — only return markets that have a cached env analysis
    # marked is_relevant=True. Reads from the cache only; never triggers Claude
    # generation during list pagination.
    env_relevant_ids: set[str] = set()
    if env_relevant:
        try:
            top = db.list_top_environmental_impacts(limit=200)
            env_relevant_ids = {row["market_id"] for row in top}
        except Exception as exc:
            log.warning("env_relevant filter failed, returning unfiltered: %s", exc)
            env_relevant_ids = set()
        if env_relevant_ids:
            filtered = [m for m in filtered if m.id in env_relevant_ids]
    total = len(filtered)
    start = (page - 1) * limit
    page_markets = filtered[start:start + limit]
    market_dicts = [m.to_dict() for m in page_markets]
    # When env_relevant filter is active, decorate each row with a small
    # is_env_relevant flag so downstream UIs can render a leaf badge without
    # a second roundtrip per market.
    if env_relevant_ids:
        for md in market_dicts:
            md["is_env_relevant"] = md.get("id") in env_relevant_ids
    return JSONResponse({
        "markets": market_dicts,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    })


# ── Edge scoring & false consensus (F4, F5) ─────────────────────────────────
# IMPORTANT: These must be registered BEFORE the /{market_id:path} catch-all
# to prevent FastAPI from consuming "top-edge" or "false-consensus" as a
# market_id path parameter.


@app.get("/api/markets/top-edge")
async def api_markets_top_edge(
    request: Request,
    limit: int = 20,
    min_sources: int = 1,
    category: str = "",
):
    """Markets with the largest absolute edge between credibility-weighted
    intelligence and the current market price. The core value proposition
    of narve.ai — "where is the crowd most wrong?"
    """
    _require_authenticated(request)
    limit = max(1, min(50, limit))
    markets = await unified_markets.fetch_unified_markets(
        POLY_CLIENT, KALSHI_CLIENT, cache_ttl=MARKETS_CACHE_TTL,
    )
    active = [m for m in markets if m.status == "active"]
    enriched = unified_markets.enrich_markets_with_intelligence(active)
    with_edge = [
        m for m in enriched
        if m.betyc_ev_score is not None and m.betyc_prediction_count >= min_sources
    ]
    if category:
        with_edge = [m for m in with_edge if m.category == category]
    with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
    return JSONResponse({
        "markets": [m.to_dict() for m in with_edge[:limit]],
        "total": len(with_edge),
    })


@app.get("/api/markets/false-consensus")
async def api_markets_false_consensus(request: Request, limit: int = 20):
    """Markets where a high market price (>80% or <20%) disagrees strongly
    with credibility-weighted intelligence (divergence > 15 points).
    These are the highest-conviction contrarian bets.
    """
    _require_authenticated(request)
    limit = max(1, min(50, limit))
    markets = await unified_markets.fetch_unified_markets(
        POLY_CLIENT, KALSHI_CLIENT, cache_ttl=MARKETS_CACHE_TTL,
    )
    active = [m for m in markets if m.status == "active"]
    enriched = unified_markets.enrich_markets_with_intelligence(active)
    fc_markets = [m for m in enriched if m.false_consensus]
    fc_markets.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
    return JSONResponse({
        "markets": [m.to_dict() for m in fc_markets[:limit]],
        "total": len(fc_markets),
    })


@app.get("/api/markets/unified/{market_id:path}")
async def api_market_detail(request: Request, market_id: str):
    user = _require_markets_user(request)
    market = await unified_markets.fetch_single_market(
        POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    payload = market.to_dict()
    # If the caller is Pro+ AND has env preferences enabled AND a cached
    # env analysis exists, merge it into the response under environmental_impact.
    # This is non-breaking: clients that don't know about the field ignore it,
    # and we never block on Claude generation here — only return cached data.
    try:
        is_pro = bool(user.get("is_admin"))
        if not is_pro:
            _subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
            _pinfo = _user_plan_info(user, _subs, int(time.time()))
            is_pro = _pinfo.get("plan") == "pro"
        if is_pro:
            prefs = db.get_user_env_preferences(user["user_id"])
            if prefs.get("show"):
                cached = db.get_environmental_impact(market_id)
                if cached:
                    from intelligence import environmental as _env
                    env_payload = _env._row_to_payload(cached)
                    env_payload = _env.apply_user_unit_preference(env_payload, prefs.get("unit", "co2_mt"))
                    payload["environmental_impact"] = env_payload
    except Exception as exc:
        log.warning("env merge into market detail failed for %s: %s", market_id, exc)
    return JSONResponse(payload)


@app.get("/api/markets/search")
async def api_markets_search(request: Request, q: str = ""):
    _require_markets_user(request)  # auth + add-on check; user dict not used below
    if not q or len(q) < 2:
        return JSONResponse({"markets": []})
    markets = await unified_markets.fetch_unified_markets(
        POLY_CLIENT, KALSHI_CLIENT, cache_ttl=MARKETS_CACHE_TTL,
    )
    filtered = unified_markets.filter_markets(markets, search=q)
    return JSONResponse({"markets": [m.to_dict() for m in filtered[:20]]})


# Account connections
@app.post("/api/markets/connect/kalshi")
async def api_connect_kalshi(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    email = (body.get("email") or "").strip()
    password = body.get("password", "")
    if not email or not password:
        return JSONResponse({"error": "Email and password are required"}, status_code=400)

    result = await KALSHI_CLIENT.login(email, password)
    if "error" in result:
        status_code = result.get("status_code", 400)
        return JSONResponse({"error": result["error"]}, status_code=status_code)

    # Store encrypted token — NEVER store the password
    encrypted = encrypt_token(result["token"])
    db.upsert_market_credential(
        user["user_id"], "kalshi",
        kalshi_token=encrypted,
        kalshi_member_id=result["member_id"],
    )
    log.info("User %s connected Kalshi account (member: %s)", user.get("username"), result["member_id"])

    # Fetch balance
    balance_data = await KALSHI_CLIENT.get_balance(result["token"])
    balance = float(balance_data.get("balance", 0)) / 100.0 if "error" not in balance_data else None

    return JSONResponse({
        "connected": True,
        "member_id": result["member_id"],
        "balance": balance,
    })


@app.post("/api/markets/connect/polymarket")
async def api_connect_polymarket(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    address = (body.get("wallet_address") or "").strip()
    if not address or len(address) < 10:
        return JSONResponse({"error": "Valid wallet address required"}, status_code=400)
    db.upsert_market_credential(
        user["user_id"], "polymarket",
        polymarket_wallet_address=address,
    )
    log.info("User %s connected Polymarket wallet %s", user.get("username"), address[:10] + "...")
    return JSONResponse({"connected": True, "address": address})


@app.delete("/api/markets/connect/{source}")
async def api_disconnect_market(request: Request, source: str):
    user = _require_markets_user(request)
    if source not in ("polymarket", "kalshi"):
        raise HTTPException(status_code=400, detail="Invalid source")
    db.disconnect_market_credential(user["user_id"], source)
    db.delete_user_positions(user["user_id"], platform=source)
    log.info("User %s disconnected %s", user.get("username"), source)
    return JSONResponse({"disconnected": True})


@app.get("/api/markets/connections")
async def api_market_connections(request: Request):
    user = _require_markets_user(request)
    return JSONResponse(_get_market_connections(user["user_id"]))


# Trading
@app.post("/api/markets/bet/kalshi")
async def api_bet_kalshi(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    ticker = (body.get("ticker") or "").strip()
    side = (body.get("side") or "").strip().lower()
    amount_usd = float(body.get("amount_usd", 0))
    order_type = (body.get("type") or "market").strip().lower()
    price = body.get("price")

    if not ticker:
        return JSONResponse({"error": "Ticker required"}, status_code=400)
    if side not in ("yes", "no"):
        return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
    if amount_usd <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    if amount_usd > 25000:
        return JSONResponse({"error": "Amount exceeds maximum"}, status_code=400)

    cred = db.get_market_credential(user["user_id"], "kalshi")
    if not cred or not cred["kalshi_token"]:
        return JSONResponse({"error": "Connect your Kalshi account first"}, status_code=400)

    token = decrypt_token(cred["kalshi_token"])
    db.update_market_credential_last_used(user["user_id"], "kalshi")

    # Validate balance
    balance_data = await KALSHI_CLIENT.get_balance(token)
    if "error" in balance_data:
        if balance_data.get("error") == "token_expired":
            db.set_market_credential_active(user["user_id"], "kalshi", False)
            return JSONResponse({"error": "Kalshi session expired — please reconnect"}, status_code=401)
        return JSONResponse({"error": balance_data["error"]}, status_code=400)

    balance_cents = balance_data.get("balance", 0)
    if amount_usd * 100 > balance_cents:
        return JSONResponse({"error": f"Insufficient balance (${balance_cents / 100:.2f} available)"}, status_code=400)

    count = max(1, int(amount_usd))  # Kalshi uses contract counts
    # Coerce price to float — client may send int, float, or numeric string.
    # Clamp to Kalshi's valid range (1-99 cents) and reject garbage.
    price_cents = None
    if order_type == "limit" and price is not None:
        try:
            price_float = float(price)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid limit price"}, status_code=400)
        if not (0 < price_float < 1):
            return JSONResponse({"error": "Limit price must be between 0 and 1"}, status_code=400)
        price_cents = max(1, min(99, int(round(price_float * 100))))

    result = await KALSHI_CLIENT.place_order(
        token,
        ticker=ticker,
        side=side,
        order_type=order_type,
        count=count,
        price=price_cents,
    )

    if "error" in result:
        if result.get("error") == "token_expired":
            db.set_market_credential_active(user["user_id"], "kalshi", False)
            return JSONResponse({"error": "Kalshi session expired — please reconnect"}, status_code=401)
        return JSONResponse({"error": result["error"]}, status_code=400)

    # Record in history
    db.record_bet(
        user["user_id"], "kalshi", result.get("order_id", ""),
        f"kalshi:{ticker}", ticker, side, amount_usd,
        price or 0, result.get("status", "submitted"),
    )

    return JSONResponse({
        "order_id": result.get("order_id", ""),
        "status": result.get("status", "submitted"),
        "filled": result.get("filled", 0),
    })


@app.post("/api/markets/bet/polymarket")
async def api_bet_polymarket(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    market_id = (body.get("market_id") or "").strip()
    side = (body.get("side") or "").strip().lower()
    amount_usdc = float(body.get("amount_usdc", 0))
    signed_order = body.get("signed_order")
    owner = (body.get("owner") or "").strip()

    if not market_id:
        return JSONResponse({"error": "Market ID required"}, status_code=400)
    if side not in ("yes", "no"):
        return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
    if amount_usdc <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    if amount_usdc > 100000:
        return JSONResponse({"error": "Amount exceeds maximum"}, status_code=400)
    if not signed_order or not isinstance(signed_order, dict):
        return JSONResponse({"error": "Signed order required (sign with your wallet)"}, status_code=400)

    # Validate signed_order structure — must include all CTF Exchange Order fields
    required_fields = {
        "salt", "maker", "signer", "taker", "tokenId",
        "makerAmount", "takerAmount", "expiration", "nonce",
        "feeRateBps", "side", "signatureType", "signature",
    }
    missing = required_fields - set(signed_order.keys())
    if missing:
        return JSONResponse(
            {"error": f"Signed order missing fields: {', '.join(sorted(missing))}"},
            status_code=400,
        )

    cred = db.get_market_credential(user["user_id"], "polymarket")
    if not cred or not cred["polymarket_wallet_address"]:
        return JSONResponse({"error": "Connect your Polymarket wallet first"}, status_code=400)

    # Security: signer/maker MUST match the connected wallet — prevents user A
    # from submitting orders signed by user B's wallet.
    connected_addr = (cred["polymarket_wallet_address"] or "").lower()
    signer_addr = str(signed_order.get("signer", "")).lower()
    maker_addr = str(signed_order.get("maker", "")).lower()
    if signer_addr != connected_addr or maker_addr != connected_addr:
        log.warning(
            "Polymarket bet rejected: signer/maker %s/%s does not match connected %s for user %s",
            signer_addr[:10], maker_addr[:10], connected_addr[:10], user.get("username"),
        )
        return JSONResponse(
            {"error": "Signed order wallet does not match your connected wallet"},
            status_code=403,
        )

    db.update_market_credential_last_used(user["user_id"], "polymarket")

    # Polymarket CLOB expects {order, owner, orderType} envelope
    clob_payload = {
        "order": signed_order,
        "owner": owner or connected_addr,
        "orderType": body.get("order_type", "GTC"),
    }

    result = await POLY_CLIENT.submit_order(clob_payload)

    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)

    db.record_bet(
        user["user_id"], "polymarket", result.get("orderID", result.get("id", "")),
        market_id, market_id, side, amount_usdc, 0, "submitted",
    )

    return JSONResponse({
        "order_id": result.get("orderID", result.get("id", "")),
        "status": "submitted",
    })


# Polymarket CTF Exchange contract (Polygon mainnet)
# https://github.com/Polymarket/ctf-exchange
POLY_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLY_NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLY_CHAIN_ID = 137
POLY_DOMAIN_NAME = "Polymarket CTF Exchange"
POLY_DOMAIN_VERSION = "1"


@app.get("/api/markets/poly/order-params/{market_id:path}")
async def api_poly_order_params(request: Request, market_id: str):
    """Return the EIP-712 order parameters the client needs to sign a Polymarket order.

    The client uses these to construct an EIP-712 typed data object and sign it
    with eth_signTypedData_v4 via MetaMask. The signed order is then POSTed
    to /api/markets/bet/polymarket for submission to the CLOB.
    """
    user = _require_markets_user(request)

    if not market_id.startswith("poly:"):
        raise HTTPException(status_code=400, detail="Only Polymarket markets supported")

    market = await unified_markets.fetch_single_market(
        POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    if not market.poly_yes_token_id or not market.poly_no_token_id:
        raise HTTPException(
            status_code=400,
            detail="Market missing CLOB token IDs — cannot place orders on this market",
        )

    cred = db.get_market_credential(user["user_id"], "polymarket")
    if not cred or not cred["polymarket_wallet_address"]:
        raise HTTPException(status_code=400, detail="Connect your Polymarket wallet first")

    exchange = POLY_NEG_RISK_EXCHANGE_ADDRESS if market.poly_neg_risk else POLY_EXCHANGE_ADDRESS

    return JSONResponse({
        "market_id": market_id,
        "yes_token_id": market.poly_yes_token_id,
        "no_token_id": market.poly_no_token_id,
        "yes_price": market.yes_price,
        "no_price": market.no_price,
        "neg_risk": market.poly_neg_risk,
        "maker_address": cred["polymarket_wallet_address"],
        "exchange": exchange,
        "chain_id": POLY_CHAIN_ID,
        "domain_name": POLY_DOMAIN_NAME,
        "domain_version": POLY_DOMAIN_VERSION,
        "fee_rate_bps": 0,
    })


async def _build_enriched_portfolio(user_id: int) -> dict:
    """Thin wrapper — routes and background jobs both go through
    portfolio_sync so there's one implementation of signal enrichment,
    persistence, and Kalshi-401 deactivation."""
    from backend.markets.portfolio_sync import sync_user_portfolio
    return await sync_user_portfolio(
        user_id,
        poly_client=POLY_CLIENT,
        kalshi_client=KALSHI_CLIENT,
        unified_markets_module=unified_markets,
        markets_cache_ttl=MARKETS_CACHE_TTL,
    )


@app.get("/api/markets/portfolio")
async def api_markets_portfolio(request: Request):
    user = _require_markets_user(request)
    portfolio = await _build_enriched_portfolio(user["user_id"])
    return JSONResponse(portfolio)


@app.get("/api/markets/orders")
async def api_markets_orders(request: Request):
    user = _require_markets_user(request)
    creds = db.get_all_market_credentials(user["user_id"])

    poly_address = None
    kalshi_token = None
    for c in creds:
        if not c["is_active"]:
            continue
        if c["source"] == "polymarket":
            poly_address = c["polymarket_wallet_address"]
        elif c["source"] == "kalshi" and c["kalshi_token"]:
            kalshi_token = decrypt_token(c["kalshi_token"])

    orders = await get_combined_orders(
        POLY_CLIENT, KALSHI_CLIENT,
        polymarket_address=poly_address,
        kalshi_token=kalshi_token,
    )
    return JSONResponse({"orders": orders})


@app.post("/api/markets/sync")
async def api_markets_sync(request: Request):
    """Force-refresh positions from both exchanges. Rate-limited to 1/min
    per user so the refresh button can't hammer upstream APIs."""
    user = _require_markets_user(request)
    if _is_rate_limited(f"portfolio_sync:{user['user_id']}", 1, 60):
        raise HTTPException(status_code=429, detail="Sync rate limit — try again in a moment")
    portfolio = await _build_enriched_portfolio(user["user_id"])
    return JSONResponse({
        "synced": True,
        "synced_at": int(time.time()),
        "combined_total_usd": portfolio.get("combined_total_usd", 0),
    })


@app.get("/api/markets/stats")
async def api_markets_stats(request: Request):
    """Aggregate portfolio stats for the dashboard header cards."""
    user = _require_markets_user(request)
    stats = db.get_portfolio_stats(user["user_id"])
    return JSONResponse(stats)


# ── Kelly criterion ───────────────────────────────────────────────────────


@app.post("/api/kelly/calculate")
async def api_kelly_calculate(request: Request):
    """Kelly sizing for a specific market.

    Body: { market_id: str, bankroll?: float }
    `market_id` is the unified id (poly:{slug} or kalshi:{ticker}).
    `bankroll` falls back to the user's stored bankroll; returns 400 if
    neither is available. Returns full / half / quarter Kelly so the UI
    can show all three tiers without three round-trips.
    """
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    market_id = (body.get("market_id") or body.get("market_slug") or "").strip()
    if not market_id:
        return JSONResponse({"error": "market_id required"}, status_code=400)

    stored = db.get_user_bankroll(user["user_id"])
    req_bankroll = body.get("bankroll")
    bankroll = float(req_bankroll) if req_bankroll is not None else stored["bankroll"]
    if bankroll is None or bankroll <= 0:
        return JSONResponse(
            {"error": "Set your bankroll first — PATCH /api/user/bankroll"},
            status_code=400,
        )

    market = await unified_markets.fetch_single_market(
        POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    enriched = unified_markets.enrich_markets_with_intelligence([market])
    m = enriched[0] if enriched else market
    if m.betyc_ev_score is None:
        return JSONResponse({
            "market_id": market_id,
            "bankroll": bankroll,
            "has_signal": False,
            "market_yes_price": m.yes_price,
            "narve_yes_probability": None,
            "edge": 0,
            "recommendations": [],
            "message": "No narve.ai signal yet — need predictions before Kelly can size.",
        })

    narve_yes = max(0.0, min(1.0, m.yes_price + m.betyc_ev_score))
    recommendations = []
    for label, frac in (("full", 1.0), ("half", 0.5), ("quarter", 0.25)):
        sizing = unified_markets.compute_kelly_sizing(
            betyc_probability=narve_yes,
            market_yes_price=m.yes_price,
            bankroll=bankroll,
            fraction=frac,
        )
        bet = float(sizing.get("recommended_amount") or 0)
        price = m.yes_price if sizing.get("side") == "YES" else (1 - m.yes_price)
        max_profit = round(bet * ((1 / price) - 1), 2) if price > 0 else 0.0
        max_loss = round(bet, 2)
        recommendations.append({
            "label": label,
            "fraction_of_kelly": frac,
            "side": sizing.get("side"),
            "kelly_full_fraction": sizing.get("kelly_full_fraction"),
            "kelly_adjusted_fraction": sizing.get("kelly_adjusted_fraction"),
            "bet_amount_usd": bet,
            "pct_of_bankroll": round((bet / bankroll) * 100, 4) if bankroll > 0 else 0,
            "max_profit_usd": max_profit,
            "max_loss_usd": max_loss,
        })

    return JSONResponse({
        "market_id": market_id,
        "market_title": m.title,
        "market_yes_price": m.yes_price,
        "narve_yes_probability": round(narve_yes, 4),
        "edge": round(narve_yes - m.yes_price, 4),
        "bankroll": bankroll,
        "has_signal": True,
        "recommendations": recommendations,
    })


# ── User bankroll & Kelly fraction preferences ─────────────────────────────


@app.get("/api/user/bankroll")
async def api_user_bankroll_get(request: Request):
    user = _require_markets_user(request)
    return JSONResponse(db.get_user_bankroll(user["user_id"]))


@app.patch("/api/user/bankroll")
async def api_user_bankroll_set(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    bankroll = body.get("bankroll")
    kelly_fraction = body.get("kelly_fraction")

    if bankroll is not None:
        try:
            bankroll = float(bankroll)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid bankroll"}, status_code=400)
        if bankroll < 0 or bankroll > 1_000_000_000:
            return JSONResponse(
                {"error": "Bankroll must be between 0 and 1,000,000,000"},
                status_code=400,
            )

    if kelly_fraction is not None:
        try:
            kelly_fraction = float(kelly_fraction)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid kelly_fraction"}, status_code=400)
        if not (0 < kelly_fraction <= 1):
            return JSONResponse(
                {"error": "kelly_fraction must be between 0 and 1"},
                status_code=400,
            )

    db.set_user_bankroll(user["user_id"], bankroll=bankroll, kelly_fraction=kelly_fraction)
    return JSONResponse(db.get_user_bankroll(user["user_id"]))


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
        f'<script src="/_gateway_static/switcher.js"></script>'
        f'<script src="/_gateway_static/trade.js"></script>'
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

    # 2. Require active subscription.
    if not db.has_active_subscription(user["user_id"], key):
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
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret:
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

    # Relay response; strip hop-by-hop headers from upstream.
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in hop_by_hop
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
    # Update Content-Length since injection may have changed the body size.
    if body is not upstream.content:
        resp_headers.pop("content-length", None)
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


@app.get("/api/credibility/{source_handle}")
async def api_get_credibility(request: Request, source_handle: str):
    _require_authenticated(request)
    cred = db.get_source_credibility(source_handle)
    if not cred:
        return JSONResponse({"source_handle": source_handle, "global_credibility": None, "status": "unknown"})
    cats = db.get_all_category_credibilities(source_handle)
    snaps = db.get_credibility_snapshots(source_handle, 5)
    return JSONResponse({
        "source_handle": source_handle,
        "global_credibility": cred["global_credibility"],
        "accuracy_unlocked": bool(cred["accuracy_unlocked"]),
        "decay_weighted_accuracy": cred["decay_weighted_accuracy"],
        "total_predictions": cred["total_predictions"],
        "correct_predictions": cred["correct_predictions"],
        "categories": [
            {"category": c["category"], "credibility": c["category_credibility"],
             "prediction_count": c["prediction_count"]}
            for c in cats
        ],
        "snapshots": [{"credibility": s["global_credibility"], "at": s["snapshot_at"]} for s in snaps],
    })


@app.get("/api/credibility/{source_handle}/calibration")
async def api_get_calibration(request: Request, source_handle: str):
    """Calibration data for a source (F9).

    Returns the calibration score and per-bucket data showing how well
    the source's stated probabilities match actual outcomes.
    """
    _require_authenticated(request)
    cal = db.get_source_calibration(source_handle)
    if not cal:
        return JSONResponse({
            "source_handle": source_handle,
            "calibration": None,
            "status": "insufficient_data",
        })
    return JSONResponse({
        "source_handle": source_handle,
        "calibration": cal,
    })


@app.post("/api/credibility/refresh")
async def api_credibility_refresh(request: Request):
    user = _require_pro_user(request)
    # Force-refresh recomputes EVERY source's credibility — expensive. Cap
    # at 2 per 5 minutes per user so a single Pro user cannot DoS the engine.
    if _is_rate_limited(f"cred_refresh:{user['user_id']}", limit=2, window=300):
        return JSONResponse(
            {"error": "Credibility refresh available once every 5 minutes."},
            status_code=429,
            headers={"Retry-After": "300"},
        )
    count = db.recompute_all_credibilities()
    log.info("User %s triggered credibility refresh, recomputed %d sources", user.get("username"), count)
    return JSONResponse({"recomputed": count, "timestamp": int(time.time())})


# ── Backtesting API (F13) ────────────────────────────────────────────────────


@app.post("/api/backtests")
async def api_create_backtest(request: Request):
    """Submit a backtest job. Returns backtest_id to poll for results."""
    user = _require_pro_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    import json as _json
    params = {
        "min_credibility": float(body.get("min_credibility", 0.5)),
        "min_edge": float(body.get("min_edge", 0.05)),
        "category": body.get("category") or None,
        "bet_sizing": body.get("bet_sizing", "flat"),
        "bankroll": float(body.get("bankroll", 10000)),
        "max_bet_pct": float(body.get("max_bet_pct", 0.1)),
    }

    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO backtests (user_id, params, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user["user_id"], _json.dumps(params), now),
        )
        backtest_id = cur.lastrowid

    # Run as a background job
    from jobs import enqueue_job
    await enqueue_job("run_backtest", backtest_id=backtest_id)

    return JSONResponse({"backtest_id": backtest_id, "status": "pending"})


@app.get("/api/backtests/{backtest_id}")
async def api_get_backtest(request: Request, backtest_id: int):
    """Get backtest results. Poll until status=completed."""
    user = _require_pro_user(request)
    import json as _json
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM backtests WHERE id = ? AND user_id = ?",
            (backtest_id, user["user_id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    result = _json.loads(row["result"]) if row["result"] else None
    return JSONResponse({
        "backtest_id": row["id"],
        "status": row["status"],
        "params": _json.loads(row["params"]),
        "result": result,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    })


# ── Retrospective API (F6) ───────────────────────────────────────────────────


@app.get("/api/markets/{market_id:path}/retrospective")
async def api_market_retrospective(request: Request, market_id: str):
    """Get the post-resolution retrospective for a resolved market.

    Returns the Claude-generated analysis of how narve.ai's intelligence
    performed, including which sources called it correctly and which were wrong.
    """
    _require_authenticated(request)
    from intelligence.retrospective import _get_cached
    retro = _get_cached(market_id)
    if not retro:
        return JSONResponse({"retrospective": None, "market_id": market_id})
    return JSONResponse({"retrospective": retro, "market_id": market_id})


# ── Probability API ──────────────────────────────────────────────────────────


@app.get("/api/markets/{market_id:path}/probability")
async def api_market_probability(request: Request, market_id: str):
    _require_authenticated(request)
    predictions = db.get_predictions_for_market(market_id)
    pred_dicts = [
        {
            "source_handle": p["source_handle"],
            "direction": p["direction"],
            "predicted_probability": p["predicted_probability"],
            "global_credibility": p["global_credibility"],
            "category_credibility": p["category_credibility"] if "category_credibility" in p.keys() else None,
            "accuracy_unlocked": bool(p["accuracy_unlocked"]) if p["accuracy_unlocked"] is not None else False,
        }
        for p in predictions
    ]
    result = db.calculate_betyc_probability(pred_dicts)
    market = await unified_markets.fetch_single_market(POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120)
    market_yes = market.yes_price if market else None
    if market_yes is not None and result["betyc_yes_probability"] is not None:
        result["betyc_edge"] = round(result["betyc_yes_probability"] - market_yes, 4)
    result["market_yes_price"] = market_yes
    result["contributing_sources"] = [
        {"handle": p["source_handle"], "credibility": p.get("global_credibility"),
         "predicted_probability": p.get("predicted_probability"),
         "category_credibility": p.get("category_credibility")}
        for p in pred_dicts
    ]
    return JSONResponse(result)


# ── Environmental Impact API (Pro feature) ──────────────────────────────────
#
# Claude-generated CO2 analysis of prediction market outcomes. See
# intelligence/environmental.py and migrations/008_environmental_impact.py.
# Lazy generation, 24h cache, force-refresh capped at 5/day/user.

from intelligence import environmental as _env_module


def _serialize_env_payload(payload: dict, unit: str = "co2_mt") -> dict:
    """Render an env payload for API output, applying the user's unit preference."""
    return _env_module.apply_user_unit_preference(payload, unit)


# IMPORTANT: register /api/markets/environmental/top BEFORE the
# /{market_id:path}/environmental pattern, otherwise the :path converter is
# greedy and would swallow "environmental/top" as a market_id.
@app.get("/api/markets/environmental/top")
async def api_environmental_top(request: Request, limit: int = 20):
    user = _require_pro_user(request)
    limit = max(1, min(50, int(limit)))
    rows = db.list_top_environmental_impacts(limit=limit)
    prefs = db.get_user_env_preferences(user["user_id"])
    unit = prefs.get("unit", "co2_mt")
    impacts = []
    for row in rows:
        payload = _env_module._row_to_payload(row)
        impacts.append(_serialize_env_payload(payload, unit))
    return JSONResponse({"impacts": impacts, "as_of": int(time.time()), "count": len(impacts)})


@app.get("/api/markets/{market_id:path}/environmental")
async def api_market_environmental(request: Request, market_id: str):
    user = _require_pro_user(request)
    market = await unified_markets.fetch_single_market(
        POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    payload = await _env_module.generate_environmental_impact(market, force=False)
    prefs = db.get_user_env_preferences(user["user_id"])
    return JSONResponse(_serialize_env_payload(payload, prefs.get("unit", "co2_mt")))


@app.post("/api/markets/{market_id:path}/environmental/refresh")
async def api_market_environmental_refresh(request: Request, market_id: str):
    user = _require_pro_user(request)
    # Per-user rate limit: 5 force-refreshes per 24h. Stops a curious user
    # from running up the Claude bill exploring the same market repeatedly.
    if _is_rate_limited(f"env_refresh:{user['user_id']}", 5, 86400):
        return JSONResponse(
            {"error": "Force-refresh limit reached (5 per day). The cached analysis is still available via GET."},
            status_code=429,
            headers={"Retry-After": "86400"},
        )
    market = await unified_markets.fetch_single_market(
        POLY_CLIENT, KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    payload = await _env_module.generate_environmental_impact(market, force=True)
    prefs = db.get_user_env_preferences(user["user_id"])
    log.info("Pro user %s force-refreshed env analysis for %s", user.get("email"), market_id)
    return JSONResponse(_serialize_env_payload(payload, prefs.get("unit", "co2_mt")))


@app.patch("/api/user/preferences/environmental")
async def api_user_env_preferences(request: Request):
    user = _require_authenticated(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    show = bool(body.get("show_environmental_impact", True))
    unit = (body.get("preferred_unit") or "co2_mt").strip().lower()
    if unit not in db.ENV_VALID_UNITS:
        return JSONResponse(
            {"error": f"preferred_unit must be one of {sorted(db.ENV_VALID_UNITS)}"},
            status_code=400,
        )
    try:
        db.set_user_env_preferences(user["user_id"], show=show, unit=unit)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "show_environmental_impact": show,
        "preferred_unit": unit,
    })


# ── Signal Search API (Pro feature) ──────────────────────────────────────────


@app.get("/api/topics")
async def api_list_topics(request: Request):
    user = _require_pro_user(request)
    import json as _json
    topics = db.list_topics(user["user_id"])
    return JSONResponse({
        "topics": [
            {"id": t["id"], "name": t["name"],
             "keywords": _json.loads(t["keywords"]) if t["keywords"] else [],
             "schedule_minutes": t["schedule_minutes"],
             "last_pulled_at": t["last_pulled_at"],
             "posts_found_total": t["posts_found_total"],
             "predictions_extracted_total": t["predictions_extracted_total"],
             "is_active": bool(t["is_active"])}
            for t in topics
        ]
    })


@app.post("/api/topics")
async def api_create_topic(request: Request):
    user = _require_pro_user(request)
    count = db.count_user_topics(user["user_id"])
    if count >= 10:
        return JSONResponse({"error": "Maximum 10 topics allowed for Pro tier"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    name = (body.get("name") or "").strip()
    keywords = body.get("keywords", [])
    try:
        schedule = int(body.get("schedule_minutes", 60))
    except (TypeError, ValueError):
        return JSONResponse({"error": "schedule_minutes must be an integer"}, status_code=400)
    if not name:
        return JSONResponse({"error": "Topic name required"}, status_code=400)
    if len(name) > FIELD_MAX["topic_name"]:
        return JSONResponse({"error": f"Topic name exceeds {FIELD_MAX['topic_name']} characters"}, status_code=400)
    if not keywords or not isinstance(keywords, list):
        return JSONResponse({"error": "Keywords required (array)"}, status_code=400)
    if len(keywords) > 20:
        return JSONResponse({"error": "Maximum 20 keywords per topic"}, status_code=400)
    # Coerce, strip, and length-cap each keyword. Drop empties. Reject any
    # non-string element so attackers can't smuggle objects/arrays through.
    cleaned_kw = []
    for k in keywords:
        if not isinstance(k, str):
            return JSONResponse({"error": "Keywords must be strings"}, status_code=400)
        ks = k.strip()
        if not ks:
            continue
        if len(ks) > FIELD_MAX["topic_keyword"]:
            return JSONResponse({"error": f"Keyword exceeds {FIELD_MAX['topic_keyword']} characters"}, status_code=400)
        cleaned_kw.append(ks)
    if not cleaned_kw:
        return JSONResponse({"error": "At least one non-empty keyword is required"}, status_code=400)
    keywords = cleaned_kw
    if schedule not in (30, 60, 360, 1440):
        return JSONResponse({"error": "Schedule must be 30, 60, 360, or 1440 minutes"}, status_code=400)
    topic_id = db.create_topic(user["user_id"], name, keywords, schedule)
    log.info("User %s created topic '%s' (id=%d)", user.get("username"), name, topic_id)
    return JSONResponse({"id": topic_id, "name": name})


@app.delete("/api/topics/{topic_id}")
async def api_delete_topic(request: Request, topic_id: int):
    user = _require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.delete_topic(topic_id)
    return JSONResponse({"deleted": True})


@app.post("/api/topics/{topic_id}/pull")
async def api_topic_pull(request: Request, topic_id: int):
    user = _require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    # Manual topic pulls trigger an upstream scrape — costly. Cap at 2
    # pulls per 30 minutes per (user, topic) pair so a single user cannot
    # spam the scraper or drain Anthropic API credits.
    rl_key = f"topic_pull:{user['user_id']}:{topic_id}"
    if _is_rate_limited(rl_key, limit=2, window=1800):
        return JSONResponse(
            {"error": "Topics can be manually pulled once every 30 minutes."},
            status_code=429,
            headers={"Retry-After": "1800"},
        )
    db.update_topic_pull(topic_id, posts_found=0, predictions_extracted=0)
    return JSONResponse({"pulled": True, "topic_id": topic_id})


@app.get("/api/topics/{topic_id}/predictions")
async def api_topic_predictions(request: Request, topic_id: int):
    user = _require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    preds = db.get_topic_predictions(topic_id)
    return JSONResponse({
        "predictions": [
            {"id": p["id"], "source_handle": p["source_handle"], "content": p["content"],
             "category": p["category"], "direction": p["direction"],
             "predicted_probability": p["predicted_probability"],
             "global_credibility": p["global_credibility"],
             "category_credibility": p["category_credibility"] if "category_credibility" in p.keys() else None,
             "accuracy_unlocked": bool(p["accuracy_unlocked"]) if p["accuracy_unlocked"] is not None else False}
            for p in preds
        ]
    })


@app.get("/api/topics/{topic_id}/analysis")
async def api_topic_analysis(request: Request, topic_id: int):
    user = _require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    analysis = db.get_latest_topic_analysis(topic_id)
    if not analysis:
        return JSONResponse({"analysis": None})
    import json as _json
    return JSONResponse({
        "analysis": {
            "signal_direction": analysis["signal_direction"],
            "summary": analysis["summary"],
            "top_signals": _json.loads(analysis["top_signals"]) if analysis["top_signals"] else [],
            "contradictions": _json.loads(analysis["contradictions"]) if analysis["contradictions"] else [],
            "relevant_markets": _json.loads(analysis["relevant_markets"]) if analysis["relevant_markets"] else [],
            "confidence": analysis["confidence"],
            "confidence_reason": analysis["confidence_reason"],
            "generated_at": analysis["generated_at"],
        }
    })


# ── Signal Search page ───────────────────────────────────────────────────────


@app.get("/signal-search", response_class=HTMLResponse)
async def signal_search_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signal-search")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if not user.get("is_admin"):
        subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
        pinfo = _user_plan_info(user, subs, int(time.time()))
        if pinfo["plan"] != "pro":
            return RedirectResponse("/billing", status_code=302)
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    return render_page(
        "signal-search",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
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


# Private referral + leaderboard router. Must sit before the catch-all
# below, same ordering rule as billing_routes / status_routes above —
# otherwise the catch-all swallows /invite/{code}, /settings/referrals,
# /leaderboard, and /api/referrals/me as 404s.
try:
    from routes_referrals import router as _referrals_router  # noqa: E402
    app.include_router(_referrals_router)
except Exception as _exc:  # pragma: no cover
    log.warning("routes_referrals import failed: %s — continuing without it", _exc)


# Catch-all: anything that isn't an explicit apex route goes through the proxy.
@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def catch_all(request: Request, full_path: str):
    sub = get_subdomain(request)
    if not sub:
        # Apex fallthrough — 404 (escape the path to prevent reflected XSS).
        return HTMLResponse(
            f"<h1>Not found</h1><p>No such page at <code>{html.escape(request.url.path)}</code>.</p>",
            status_code=404,
        )
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
                log.warning("ws origin rejected: origin=%s host=%s", origin, host)
                await ws.close(code=1008, reason="Cross-origin upgrade denied")
                return
        else:
            # No Origin header in production is suspicious — browsers always
            # send one for cross-origin or same-origin WS. Reject rather than
            # fail-open, since legitimate clients always include it.
            log.warning("ws missing origin header from host=%s", host)
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
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=GATEWAY_PORT,
        log_level="info",
    )
