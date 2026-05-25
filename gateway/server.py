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
import html
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque, OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

# Load .env.production before any config reads
_env_file = Path(__file__).parent / ".env.production"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                _v = _v[1:-1]
            os.environ[_k.strip()] = _v

import httpx
import stripe
import websockets
from fastapi import FastAPI, Request, Response, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db
import polymarket_client as poly_client
import kalshi_client as kalshi_client
import trading
from cache import cache
from sse import event_stream, active_connection_count
from poller import Poller
from mark_to_market import MarkToMarketWorker

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from customer_bot import LeadsPoller  # noqa: E402
from customer_bot import store as leads_store  # noqa: E402
from customer_bot.config import topic_by_key  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DOMAIN: str = CONFIG["domain"]
# Allow GATEWAY_PORT env var to override config.json — useful for local dev
# when port 7000 is held by another process (e.g. macOS Control Center).
GATEWAY_PORT: int = int(os.environ.get("GATEWAY_PORT") or CONFIG["gateway_port"])
DASHBOARDS: dict = CONFIG["dashboards"]

# Build reverse lookup: subdomain → dashboard_key
SUBDOMAIN_TO_KEY = {cfg["subdomain"]: key for key, cfg in DASHBOARDS.items()}

# ── Stripe config ──────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logging.getLogger("gateway").warning(
        "STRIPE_SECRET_KEY not set — billing will use placeholder mode (no real payments)"
    )

BUNDLE_PLANS = {
    "trader": {
        "monthly_cents": 4900, "annual_cents": 39900, "name": "betyc Trader",
        "stripe_price_monthly": "price_1TJXulQq4pCmZ5172Svy34cn",
        "stripe_price_annual": "price_1TJXulQq4pCmZ517VPw60dds",
    },
    "pro": {
        "monthly_cents": 14900, "annual_cents": 119900, "name": "betyc Pro",
        "stripe_price_monthly": "price_1TJXumQq4pCmZ517nHAuSv3b",
        "stripe_price_annual": "price_1TJXunQq4pCmZ517pIJRjiDp",
    },
}

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
# Leading dot makes the cookie apply to every subdomain.
# Computed per-request below to support both production (.narve.ai) and
# local testing (*.localhost) — the browser rejects the Domain attribute when
# it doesn't match the actual request host, so we inspect each request.
PROD_COOKIE_DOMAIN = f".{DOMAIN}" if "." in DOMAIN and DOMAIN != "localhost" else None


def cookie_domain_for(request: Request) -> Optional[str]:
    """Return the Domain attribute to use for Set-Cookie for this request.

    Rules:
      * If the request host ends in the configured DOMAIN → use .DOMAIN so the
        cookie applies across subdomains in production.
      * If the request host is localhost or *.localhost → return None so the
        browser stores the cookie for the exact host (works for preview/dev).
      * Otherwise → derive the base domain from the request and set the cookie
        on that, so sessions work across subdomains on any domain.
    """
    host = request.headers.get("host", "").split(":")[0].lower()
    if not host:
        return None
    if host == DOMAIN or host.endswith("." + DOMAIN):
        return PROD_COOKIE_DOMAIN
    # localhost / dev — no domain attribute
    if host in ("localhost", "127.0.0.1") or host.endswith(".localhost"):
        return None
    # Flexible: derive base from request — but only if it matches our configured domain
    _, base, _ = _request_base_domain(request)
    if "." in base and base != "localhost" and base == DOMAIN:
        return f".{base}"
    return None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] gateway: %(message)s",
)
log = logging.getLogger("gateway")

# Simple but defensible email regex (no attempt to RFC 5322; just common cases).
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def is_valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s)) and len(s) <= 254

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Polymarket Gateway", docs_url=None, redoc_url=None, openapi_url=None)

db.init_db()

# Persistent httpx client for upstream proxying (connection pooling).
HTTP_CLIENT: Optional[httpx.AsyncClient] = None

_poller: Optional[Poller] = None
_mtm_worker: Optional[MarkToMarketWorker] = None
_leads_poller: Optional[LeadsPoller] = None
_cleanup_task: Optional[asyncio.Task] = None
_health_task: Optional[asyncio.Task] = None

# ── Upstream health checking / circuit breaker ────────────────────────────────
# Pings each dashboard every 15 s.  If a backend is marked unhealthy, proxy_request
# returns 503 immediately instead of waiting for the 30 s connect timeout.

_HEALTH_CHECK_INTERVAL = 15  # seconds
_HEALTH_CHECK_TIMEOUT = 3.0  # seconds per probe
_upstream_health: dict[str, bool] = {}  # dashboard_key → healthy?


async def _health_check_loop():
    """Periodically probe each upstream dashboard."""
    probe_client = httpx.AsyncClient(timeout=httpx.Timeout(_HEALTH_CHECK_TIMEOUT))
    try:
        while True:
            for key, cfg in DASHBOARDS.items():
                port = cfg["target"]
                try:
                    resp = await probe_client.get(f"http://127.0.0.1:{port}/")
                    _upstream_health[key] = resp.status_code < 500
                except Exception:
                    _upstream_health[key] = False
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
    finally:
        await probe_client.aclose()


def is_upstream_healthy(dashboard_key: str) -> bool:
    """Return whether a backend is considered healthy (default True if unknown)."""
    return _upstream_health.get(dashboard_key, True)


async def _periodic_cleanup():
    """Purge expired sessions and password resets every 10 minutes."""
    while True:
        await asyncio.sleep(600)
        try:
            sessions_purged = db.purge_expired_sessions()
            resets_purged = db.purge_expired_resets()
            if sessions_purged or resets_purged:
                log.info("Cleanup: purged %d expired sessions, %d expired resets", sessions_purged, resets_purged)
        except Exception as e:
            log.warning("Cleanup error: %s", e)


@app.on_event("startup")
async def _startup():
    global HTTP_CLIENT, _cleanup_task, _health_task, _poller, _mtm_worker, _leads_poller
    HTTP_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
    )
    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    _health_task = asyncio.create_task(_health_check_loop())

    # Redis cache + background poller
    if cache.connect():
        _poller = Poller(DASHBOARDS)
        await _poller.start()
        log.info("Redis cache + background poller active")
    else:
        log.warning("Running without Redis — no caching or SSE")

    # Mark-to-market background worker for portfolio positions
    _mtm_worker = MarkToMarketWorker()
    await _mtm_worker.start()

    # Customer-finding bot — find leads on Reddit / HN / Polymarket.
    # Opt out by setting CUSTOMER_BOT_DISABLED=1.
    if not os.environ.get("CUSTOMER_BOT_DISABLED"):
        _leads_poller = LeadsPoller()
        await _leads_poller.start()

    mode = "PRODUCTION" if IS_PRODUCTION else "dev (localhost bypass enabled)"
    log.info("Gateway started on port %d, domain=%s, mode=%s", GATEWAY_PORT, DOMAIN, mode)
    log.info("Dashboards: %s", ", ".join(f"{k}→:{v['target']}" for k, v in DASHBOARDS.items()))
    if IS_PRODUCTION and not os.environ.get("GATEWAY_COOKIE_SECRET"):
        log.warning("PRODUCTION=1 but GATEWAY_COOKIE_SECRET is unset — reserved for future signed-cookie use; not fatal.")
    # Auto-generate first admin invite token if none exist
    tokens = db.list_invite_tokens()
    if not tokens:
        first_token = db.create_invite_token("Auto-generated admin token")
        log.info("=" * 50)
        log.info("  FIRST ADMIN INVITE TOKEN created — retrieve from DB:")
        log.info("  sqlite3 auth.db \"SELECT token FROM invite_tokens LIMIT 1\"")
        log.info("=" * 50)


@app.on_event("shutdown")
async def _shutdown():
    if _leads_poller:
        await _leads_poller.stop()
    if _mtm_worker:
        await _mtm_worker.stop()
    if _poller:
        await _poller.stop()
    if _health_task:
        _health_task.cancel()
    if _cleanup_task:
        _cleanup_task.cancel()
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()


# Static files for apex pages (CSS, JS, images) — with browser cache headers.
_STATIC_CACHE_HEADER = (b"cache-control", b"public, max-age=86400, stale-while-revalidate=3600")


class CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that adds Cache-Control headers to every response."""

    async def __call__(self, scope, receive, send):
        async def send_with_cache(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(_STATIC_CACHE_HEADER)
                message = {**message, "headers": headers}
            await send(message)
        await super().__call__(scope, receive, send_with_cache)


if STATIC_DIR.exists():
    app.mount("/_gateway_static", CachedStaticFiles(directory=str(STATIC_DIR)), name="gateway_static")


# ── Security headers (raw ASGI middleware — faster than BaseHTTPMiddleware) ──

_SECURITY_HEADERS_RAW: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
if IS_PRODUCTION:
    _SECURITY_HEADERS_RAW.append(
        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
    )

_CSP_VALUE = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self' https://*.stripe.com https://*.polymarket.com https://*.kalshi.com",
    "frame-src https://kalshi.com https://*.kalshi.com https://polymarket.com https://*.polymarket.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self' https://checkout.stripe.com",
]).encode()
_SECURITY_HEADERS_RAW.append((b"content-security-policy", _CSP_VALUE))


class SecurityHeadersMiddleware:
    """Pure ASGI middleware — avoids the overhead of Starlette's BaseHTTPMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_SECURITY_HEADERS_RAW)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

# GZip compression — compresses HTML/JSON/CSS responses > 500 bytes.
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)


# ── Rate limiting ────────────────────────────────────────────────────────────

_rate_store: dict[str, deque] = defaultdict(deque)
_RATE_WINDOW = 300
_RATE_MAX_LOGIN = 10
_RATE_MAX_SIGNUP = 5
_RATE_MAX_FORGOT = 3
# Hard cap on rate-store size to prevent IP-rotation memory DoS. When over the
# cap, _is_rate_limited fails closed (returns True for new keys) until the
# periodic cleanup prunes stale entries.
_RATE_STORE_MAX = 10_000
_rate_last_cleanup = 0.0


def _rate_cleanup():
    global _rate_last_cleanup
    now = time.time()
    if now - _rate_last_cleanup < 60:
        return
    _rate_last_cleanup = now
    cutoff = now - _RATE_WINDOW
    stale = [k for k, v in _rate_store.items() if not v or v[-1] < cutoff]
    for k in stale:
        del _rate_store[k]


def _is_rate_limited(ip: str, endpoint: str, limit: int) -> bool:
    _rate_cleanup()
    now = time.time()
    key = f"{ip}:{endpoint}"
    # Fail closed if the store has hit its hard cap and this would be a new
    # key — protects against IP-rotation memory exhaustion.
    if key not in _rate_store and len(_rate_store) >= _RATE_STORE_MAX:
        return True
    timestamps = _rate_store[key]
    cutoff = now - _RATE_WINDOW
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= limit:
        return True
    timestamps.append(now)
    return False


def _get_client_ip(request: Request) -> str:
    # In production, only trust Cloudflare's cf-connecting-ip header.
    # It is set and overwritten by Cloudflare, not spoofable by clients.
    if IS_PRODUCTION:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        # Fallback to direct connection IP (not xff, which is spoofable)
        return request.client.host if request.client else "unknown"
    # In dev, allow X-Forwarded-For for local proxy setups
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


RATE_LIMITED_RESPONSE = HTMLResponse(
    "<h1>Too many requests</h1>"
    "<p>You've made too many attempts. Please wait a few minutes and try again.</p>",
    status_code=429,
)


# ── CSRF protection ──────────────────────────────────────────────────────────
# Per-session CSRF tokens stored in-memory. Keyed by session token (from cookie).
# For unauthenticated forms (login, signup, gate, forgot-password, reset-password)
# we key by a temporary CSRF cookie instead.

_CSRF_COOKIE = "pm_csrf"
_csrf_tokens: dict[str, tuple[str, float]] = {}  # session_key → (token, created_at)
_CSRF_TOKEN_MAX = 5000
_CSRF_TOKEN_TTL = 3600  # seconds


def _evict_csrf_tokens() -> None:
    """Evict expired tokens first, then oldest if still over capacity."""
    now = time.time()
    expired = [k for k, (_, ts) in _csrf_tokens.items() if now - ts > _CSRF_TOKEN_TTL]
    for k in expired:
        del _csrf_tokens[k]
    if len(_csrf_tokens) >= _CSRF_TOKEN_MAX:
        to_delete = list(_csrf_tokens.keys())[:1000]
        for k in to_delete:
            del _csrf_tokens[k]


def _get_csrf_token(request: Request) -> str:
    """Return the CSRF token for the current request, creating one if needed."""
    # Prefer session token, fall back to CSRF cookie
    session_key = request.cookies.get(COOKIE_NAME) or request.cookies.get(_CSRF_COOKIE)
    if session_key and session_key in _csrf_tokens:
        token, ts = _csrf_tokens[session_key]
        if time.time() - ts <= _CSRF_TOKEN_TTL:
            return token
        # Token expired — fall through to generate a new one
        del _csrf_tokens[session_key]
    # Generate new token
    token = secrets.token_urlsafe(32)
    if session_key:
        _evict_csrf_tokens()
        _csrf_tokens[session_key] = (token, time.time())
    return token


def _set_csrf_cookie_if_needed(response: Response, request: Request, csrf_token: str) -> None:
    """Set a CSRF cookie for unauthenticated users so token validation works."""
    session_key = request.cookies.get(COOKIE_NAME)
    if session_key:
        # Already have a session — CSRF is keyed by session, no extra cookie needed
        if session_key not in _csrf_tokens:
            _evict_csrf_tokens()
            _csrf_tokens[session_key] = (csrf_token, time.time())
        return
    # No session — use or create CSRF cookie
    csrf_cookie = request.cookies.get(_CSRF_COOKIE)
    if not csrf_cookie:
        csrf_cookie = secrets.token_urlsafe(24)
        response.set_cookie(
            key=_CSRF_COOKIE,
            value=csrf_cookie,
            max_age=3600,
            httponly=True,
            samesite="lax",
            secure=IS_PRODUCTION,
            path="/",
        )
    _evict_csrf_tokens()
    _csrf_tokens[csrf_cookie] = (csrf_token, time.time())


def _validate_csrf(request: Request, form_token: str) -> bool:
    """Validate a CSRF token from form data against the stored token."""
    session_key = request.cookies.get(COOKIE_NAME) or request.cookies.get(_CSRF_COOKIE)
    if not session_key:
        return False
    entry = _csrf_tokens.get(session_key)
    if not entry:
        return False
    expected, ts = entry
    if time.time() - ts > _CSRF_TOKEN_TTL:
        del _csrf_tokens[session_key]
        return False
    return _hmac_module.compare_digest(expected, form_token)


def _csrf_error() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Invalid request</h1>"
        "<p>Your session may have expired. Please go back and try again.</p>",
        status_code=403,
    )


def _csrf_error_json() -> JSONResponse:
    return JSONResponse({"error": "Invalid or missing CSRF token"}, status_code=403)


def _validate_csrf_header(request: Request) -> bool:
    """Validate CSRF for JSON API endpoints using the X-CSRF-Token header."""
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        return False
    return _validate_csrf(request, token)


import hmac as _hmac_module  # stdlib hmac for CSRF compare (avoids name collision)


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_subdomain(request: Request) -> Optional[str]:
    """Extract the subdomain portion of the Host header.

    Examples:
        yourdomain.tld        → ""    (apex)
        crypto.yourdomain.tld → "crypto"
        localhost             → ""
        crypto.localhost      → "crypto"
    """
    host = request.headers.get("host", "").split(":")[0].lower()
    if not host or host == "localhost":
        return ""
    # Strip the configured base domain
    if host == DOMAIN:
        return ""
    if host.endswith("." + DOMAIN):
        return host[: -(len(DOMAIN) + 1)]
    # Localhost subdomain testing: crypto.localhost → "crypto"
    if host.endswith(".localhost"):
        return host[: -len(".localhost")]
    # Flexible matching: recognise known dashboard subdomains on any base domain
    # (e.g. world.narve.ai when config says narve.ai).
    for sub in SUBDOMAIN_TO_KEY:
        if host.startswith(sub + "."):
            return sub
    # Unknown host — treat as apex
    return ""


def _request_base_domain(request: Request) -> tuple[str, str, str]:
    """Derive (scheme, base_domain, port_suffix) from the live request.

    Used so that generated links match the domain the user is actually on,
    even if it differs from the configured DOMAIN (e.g. narve.ai vs narve.ai).
    """
    host_raw = request.headers.get("host", DOMAIN)
    if ":" in host_raw:
        host_no_port, port = host_raw.rsplit(":", 1)
        port_suffix = ":" + port
    else:
        host_no_port = host_raw
        port_suffix = ""

    base = host_no_port.lower()
    # Strip a known subdomain prefix to get the apex
    for sub in SUBDOMAIN_TO_KEY:
        prefix = sub + "."
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    else:
        # Unknown subdomain — strip the first label as a fallback
        dot_idx = base.find(".")
        if dot_idx > 0 and dot_idx < len(base) - 1:
            base = base[dot_idx + 1:]

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return scheme, base, port_suffix


def is_local_host(request: Request) -> bool:
    """True if the request comes from localhost or *.localhost (dev mode).

    Always returns False in production (PRODUCTION=1) regardless of host,
    so a misconfigured reverse proxy can't accidentally trigger the dev
    bypass on the live server.

    Checks BOTH the Host header AND the actual client IP to prevent
    spoofed Host headers from granting dev-bypass access.
    """
    if IS_PRODUCTION:
        return False
    # Verify the actual connection comes from loopback, not just the header.
    client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return False
    host = request.headers.get("host", "").split(":")[0].lower()
    return host == "localhost" or host.endswith(".localhost") or host == "127.0.0.1"


DEV_USER_EMAIL = "dev@local"
DEV_USER_PASSWORD = secrets.token_urlsafe(24)  # random on each startup; unused for login


def ensure_dev_user() -> int:
    """Create a dev user (if missing) and grant it every dashboard for free.
    Used only in local/dev mode to skip signup when previewing on localhost.
    """
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


# ── Session cache ──────────────────────────────────────────────────────────────
# Avoids a DB round-trip on every proxied request.  Entries expire after 60 s
# so permission changes (suspend, role update) propagate within a minute.

_SESSION_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_SESSION_CACHE_TTL = 60  # seconds
_SESSION_CACHE_MAX = 500


def _get_cached_session(token: str) -> Optional[dict]:
    """Look up a session token, returning a cached result when possible."""
    now = time.time()
    entry = _SESSION_CACHE.get(token)
    if entry and now - entry[0] < _SESSION_CACHE_TTL:
        _SESSION_CACHE.move_to_end(token)
        return entry[1]
    session = db.get_session(token)
    if not session:
        _SESSION_CACHE.pop(token, None)
        return None
    # Check if user is suspended
    if session["suspended"]:
        _SESSION_CACHE.pop(token, None)
        return None
    admin_level = session["is_admin"] or 0
    result = {
        "user_id": session["user_id"],
        "username": session["username"],
        "email": session["email"],
        "is_admin": bool(admin_level),
        "is_super_admin": admin_level >= 2,
        "admin_level": admin_level,
    }
    # Evict LRU entry if cache is full
    if len(_SESSION_CACHE) >= _SESSION_CACHE_MAX:
        _SESSION_CACHE.popitem(last=False)
    _SESSION_CACHE[token] = (now, result)
    return result


def invalidate_session_cache(token: str) -> None:
    """Remove a token from the session cache (call on logout)."""
    _SESSION_CACHE.pop(token, None)


def flush_session_cache() -> None:
    """Clear the entire session cache (call on suspend/password reset)."""
    _SESSION_CACHE.clear()


# ── Subscription cache ────────────────────────────────────────────────────────
# Caches has_active_subscription results per (user_id, dashboard_key).
# TTL is 120 s — subscriptions change far less often than sessions.

_SUB_CACHE: OrderedDict[tuple[int, str], tuple[float, bool]] = OrderedDict()
_SUB_CACHE_TTL = 120  # seconds
_SUB_CACHE_MAX = 1000


def cached_has_subscription(user_id: int, dashboard_key: str) -> bool:
    """Cached wrapper around db.has_active_subscription."""
    now = time.time()
    cache_key = (user_id, dashboard_key)
    entry = _SUB_CACHE.get(cache_key)
    if entry and now - entry[0] < _SUB_CACHE_TTL:
        _SUB_CACHE.move_to_end(cache_key)
        return entry[1]
    result = db.has_active_subscription(user_id, dashboard_key)
    if len(_SUB_CACHE) >= _SUB_CACHE_MAX:
        _SUB_CACHE.popitem(last=False)
    _SUB_CACHE[cache_key] = (now, result)
    return result


def cached_active_dashboard_keys(user_id: int) -> list[str]:
    """Return list of dashboard keys the user has active access to (cached)."""
    return [k for k in DASHBOARDS if cached_has_subscription(user_id, k)]


def invalidate_sub_cache_for_user(user_id: int) -> None:
    """Remove all subscription cache entries for a user."""
    stale = [k for k in _SUB_CACHE if k[0] == user_id]
    for k in stale:
        del _SUB_CACHE[k]


def flush_sub_cache() -> None:
    """Clear the entire subscription cache."""
    _SUB_CACHE.clear()


def current_user(request: Request) -> Optional[dict]:
    """Return a dict describing the current session user, or None.

    Always returns a plain dict so callers can use
    ``.get()`` and ``["key"]`` uniformly. Keys:
        user_id, email, is_admin, _dev_bypass (optional)
    """
    token = request.cookies.get(COOKIE_NAME)
    if token:
        cached = _get_cached_session(token)
        if cached:
            return cached
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


class StripeProviderError(Exception):
    """Raised when Stripe is unavailable or returns an unrecoverable error."""


def _get_or_create_stripe_customer(user_id: str, email: str) -> str:
    """Find an existing Stripe customer by email, or create a new one.

    Wraps Stripe SDK calls in try/except so callers can present a user-friendly
    error rather than 500-ing the checkout flow on transient Stripe issues.
    """
    try:
        existing = stripe.Customer.list(email=email, limit=1)
        if getattr(existing, "data", None):
            return existing.data[0].id
        customer = stripe.Customer.create(email=email, metadata={"user_id": str(user_id)})
        return customer.id
    except Exception as exc:
        # stripe.error.* all subclass Exception; we don't want to import the
        # specific class here because the SDK version may not expose every type.
        log.error("Stripe customer lookup/create failed for %s: %s", email, exc)
        raise StripeProviderError("Payment provider unavailable. Please try again in a moment.") from exc


def _stripe_base_url(request: Request) -> str:
    """Return the base URL for Stripe success/cancel redirects."""
    if IS_PRODUCTION:
        return f"https://{DOMAIN}"
    return str(request.base_url).rstrip("/")


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


def _get_superuser_key_from_request(request: Request) -> Optional[str]:
    """Extract superuser key from URL query param or Authorization header."""
    # Check URL query parameter
    if "superuser_key" in request.query_params:
        return request.query_params["superuser_key"]
    # Check Authorization header (Bearer token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def render_page(name: str, request: Optional[Request] = None, **context) -> HTMLResponse:
    """Tiny templating: load static/<name>.html and do {{ key }} substitution.

    Keys prefixed with ``raw_`` are inserted verbatim (used for pre-escaped
    server-side HTML). All other values are HTML-escaped before insertion.
    For convenience, the well-known keys ``dashboard_cards`` and
    ``billing_rows`` are also treated as raw.

    When ``request`` is provided, a CSRF token is auto-generated and made
    available as ``{{ csrf_token }}`` in the template.  A CSRF cookie is
    also set on the response for unauthenticated pages.
    """
    path = STATIC_DIR / f"{name}.html"
    page = path.read_text()
    # Auto-fill empty raw_admin_link if not provided (prevents {{ raw_admin_link }} showing)
    if "raw_admin_link" not in context:
        context["raw_admin_link"] = ""
    # Auto-generate CSRF token when request is available
    csrf_token = ""
    if request is not None:
        csrf_token = _get_csrf_token(request)
    context.setdefault("csrf_token", csrf_token)
    raw_keys = {"dashboard_cards", "billing_rows"}
    for key, value in context.items():
        placeholder = "{{ " + key + " }}"
        if key in raw_keys or key.startswith("raw_"):
            page = page.replace(placeholder, str(value))
        else:
            page = page.replace(placeholder, html.escape(str(value)))
    resp = HTMLResponse(page)
    # Set CSRF cookie for unauthenticated users
    if request is not None and csrf_token:
        _set_csrf_cookie_if_needed(resp, request, csrf_token)
    return resp


# ── Apex routes (login / signup / my dashboards / billing) ────────────────────


@app.get("/", response_class=HTMLResponse)
async def apex_root(request: Request):
    sub = get_subdomain(request)
    if sub:
        # Subdomain request — delegate to the proxy handler below.
        return await proxy_request(request, "/")

    # "Coming soon" teaser — always show the landing page at apex,
    # regardless of login status.  Logged-in users reach their
    # dashboards via /dashboards in the nav.
    return _render_landing()


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


@app.get("/dummy", response_class=HTMLResponse)
async def dummy_page():
    """Staging preview — no auth required."""
    path = STATIC_DIR / "dummy" / "index.html"
    return HTMLResponse(path.read_text())


@app.get("/gate", response_class=HTMLResponse)
async def gate_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    return render_page("gate", request=request, error="")


@app.post("/gate")
async def gate_submit(request: Request, token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    token = token.strip()
    if not token:
        return render_page("gate", request=request, error="Please enter an invite token.")
    invite = db.get_invite_token(token)
    if not invite or invite["status"] == "revoked":
        return render_page("gate", request=request, error="Invalid or revoked token.")
    if invite["status"] == "claimed":
        return RedirectResponse(f"/login?{urlencode({'token': invite['token']})}", status_code=303)
    # Unclaimed — PRG redirect to the signup page with token in URL
    return RedirectResponse(f"/signup?{urlencode({'token': invite['token']})}", status_code=303)


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
async def login_page(request: Request, token: str = ""):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")
    user = current_user(request)
    if user and not user.get("_dev_bypass"):
        # Real logged-in user → skip to their default dashboard (or the hub).
        default_key = db.get_default_dashboard(user["user_id"])
        if default_key and default_key in DASHBOARDS:
            dash_cfg = DASHBOARDS[default_key]
            if is_local_host(request):
                return RedirectResponse(f"http://localhost:{dash_cfg['target']}", status_code=302)
            s, b, p = _request_base_domain(request)
            return RedirectResponse(f"{s}://{dash_cfg['subdomain']}.{b}{p}/", status_code=302)
        return RedirectResponse("/dashboards", status_code=302)
    # If a claimed invite token is provided (from /gate redirect), show token section
    token = token.strip()
    token_section = ""
    footer_link = '<a href="/gate">Have an invite token? Use it here</a>'
    if token:
        invite = db.get_invite_token(token)
        if invite and invite["status"] == "claimed":
            email_hint = db.mask_email(invite["claimed_by_email"] or "")
            token_section = _login_token_section(invite["token"], email_hint)
            footer_link = '<a href="/gate">Use a different token</a>'
    return render_page(
        "login", request=request, error="",
        raw_token_section=token_section,
        raw_footer_link=footer_link,
        raw_success="",
    )


@app.post("/login")
async def login_submit(request: Request, identifier: str = Form(""), password: str = Form(...), invite_token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")

    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "login", _RATE_MAX_LOGIN):
        return RATE_LIMITED_RESPONSE

    invite_token = invite_token.strip()
    identifier = identifier.strip()

    def _render_login_error(msg: str):
        """Re-render login page with error, preserving mode (token vs standalone)."""
        if invite_token:
            invite = db.get_invite_token(invite_token)
            email_hint = db.mask_email(invite["claimed_by_email"] or "") if invite else ""
            return render_page(
                "login", request=request, error=msg,
                raw_token_section=_login_token_section(invite_token, email_hint),
                raw_footer_link='<a href="/gate">Use a different token</a>',
                raw_success="",
            )
        return render_page(
            "login", request=request, error=msg,
            raw_token_section="",
            raw_footer_link='<a href="/gate">Have an invite token? Use it here</a>',
            raw_success="",
        )

    if not identifier:
        return _render_login_error("Please enter your username or email.")

    user = db.get_user_by_email_or_username(identifier)
    if not user:
        log.warning("Failed login: account not found for identifier=%s ip=%s", identifier, request.client.host if request.client else "unknown")
        return _render_login_error("Invalid email or password.")

    # If token provided (gate flow), enforce token-to-user binding
    if invite_token:
        invite = db.get_invite_token(invite_token)
        if not invite or invite["status"] != "claimed":
            return render_page("gate", request=request, error="Invalid or expired token. Please enter your invite token again.")
        if invite["claimed_by_user_id"] != user["id"]:
            return _render_login_error("This token does not belong to that account.")

    if user["suspended"]:
        return _render_login_error("This account has been suspended.")
    if not db.verify_user_password(user["email"], password):
        log.warning("Failed login: wrong password for user=%s ip=%s", user["username"] or user["email"], request.client.host if request.client else "unknown")
        return _render_login_error("Invalid email or password.")

    token = db.create_session(user["id"])
    # Honour the user's default-dashboard preference (set in /settings).
    default_key = db.get_default_dashboard(user["id"])
    if default_key and default_key in DASHBOARDS:
        dash_cfg = DASHBOARDS[default_key]
        if is_local_host(request):
            dest = f"http://localhost:{dash_cfg['target']}"
        else:
            s, b, p = _request_base_domain(request)
            # Validate that the base domain matches the configured domain
            # to prevent open redirects via forged Host headers.
            if b.rstrip(".") == DOMAIN.rstrip("."):
                dest = f"{s}://{dash_cfg['subdomain']}.{b}{p}/"
            else:
                dest = "/dashboards"
    else:
        dest = "/dashboards"
    response = RedirectResponse(dest, status_code=302)
    set_session_cookie(response, token, request)
    return response


# Old forgot-password handlers removed -- using email-based reset flow below (see /forgot-password)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, token: str = ""):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    token = token.strip()
    if not token:
        return RedirectResponse("/gate", status_code=302)
    invite = db.get_invite_token(token)
    if not invite or invite["status"] != "unclaimed":
        return RedirectResponse("/gate", status_code=302)
    # Valid unclaimed token — render the signup form
    target_email = ""
    try:
        target_email = invite["target_email"] or ""
    except (IndexError, KeyError):
        pass
    return render_page("signup", request=request, error="", invite_token=invite["token"], email=target_email)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.post("/signup")
async def signup_submit(request: Request, username: str = Form(""), email: str = Form(...), password: str = Form(...), invite_token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")

    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "signup", _RATE_MAX_SIGNUP):
        return render_page("signup", request=request, error="Too many signup attempts. Please try again later.", invite_token=invite_token, email=email)

    invite_token = invite_token.strip()
    invite = db.get_invite_token(invite_token) if invite_token else None
    if not invite or invite["status"] != "unclaimed":
        return render_page("gate", request=request, error="Invalid or already used invite token. Please enter a valid token.")

    username = username.strip()
    email = (email or "").lower().strip()

    if not username or not USERNAME_RE.match(username):
        return render_page("signup", request=request, error="Username must be 3\u201320 characters: letters, numbers, underscores only.", invite_token=invite_token, email=email)
    if db.get_user_by_username(username):
        return render_page("signup", request=request, error="An account with these details already exists. Please try different credentials.", invite_token=invite_token, email=email)
    if not is_valid_email(email):
        return render_page("signup", request=request, error="Enter a valid email address.", invite_token=invite_token, email=email)
    if len(password) < 12:
        return render_page("signup", request=request, error="Password must be at least 12 characters.", invite_token=invite_token, email=email)
    if len(password) > 256:
        return render_page("signup", request=request, error="Password is too long.", invite_token=invite_token, email=email)
    if not re.search(r"[A-Z]", password):
        return render_page("signup", request=request, error="Password must contain at least one uppercase letter.", invite_token=invite_token, email=email)
    if not re.search(r"[a-z]", password):
        return render_page("signup", request=request, error="Password must contain at least one lowercase letter.", invite_token=invite_token, email=email)
    if not re.search(r"[0-9]", password):
        return render_page("signup", request=request, error="Password must contain at least one number.", invite_token=invite_token, email=email)
    if not re.search(r"[^A-Za-z0-9]", password):
        return render_page("signup", request=request, error="Password must contain at least one special character.", invite_token=invite_token, email=email)
    if db.get_user_by_email(email):
        return render_page("signup", request=request, error="An account with these details already exists. Please try different credentials.", invite_token=invite_token, email=email)
    user_id = db.create_user(email, password, username=username)
    if not db.claim_invite_token(invite_token, user_id, email):
        db.delete_user(user_id)
        return render_page("gate", request=request, error="This token was just claimed by someone else. Please use a different token.")
    token = db.create_session(user_id)
    response = RedirectResponse("/dashboards", status_code=302)
    set_session_cookie(response, token, request)
    return response


@app.post("/logout")
async def logout(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/logout")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.delete_session(token)
        invalidate_session_cache(token)
    response = RedirectResponse("/gate", status_code=302)
    clear_session_cookie(response, request)
    return response


@app.get("/logout")
async def logout_get(request: Request):
    """Fallback GET handler — redirect to login so old bookmarks still work."""
    return RedirectResponse("/gate", status_code=302)


@app.get("/dashboards", response_class=HTMLResponse)
async def my_dashboards(request: Request, hub: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/dashboards")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    # If user set a default dashboard, redirect there (skip with ?hub=1).
    if hub != "1":
        default_key = db.get_default_dashboard(user["user_id"])
        if default_key and default_key in DASHBOARDS:
            dash_cfg = DASHBOARDS[default_key]
            if is_local_host(request):
                return RedirectResponse(f"http://localhost:{dash_cfg['target']}", status_code=302)
            scheme, base, port_suffix = _request_base_domain(request)
            return RedirectResponse(f"{scheme}://{dash_cfg['subdomain']}.{base}{port_suffix}/", status_code=302)

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin_user = bool(user.get("is_admin"))
    local_mode = is_local_host(request)

    # ── Build subscription summary ──────────────────────────────────
    active_subs = []
    total_monthly = 0
    for key, cfg in DASHBOARDS.items():
        if _is_sub_active(subs.get(key), is_admin_user):
            sub_record = subs.get(key)
            plan = sub_record["plan"] if sub_record else ""
            if "annual" in plan:
                price_label = f"${cfg['annual_cents']/100:.2f}/yr"
                total_monthly += cfg["annual_cents"] / 12
            else:
                price_label = f"${cfg['monthly_cents']/100:.2f}/mo"
                total_monthly += cfg["monthly_cents"]
            active_subs.append({"name": cfg["display_name"], "accent": cfg["accent"], "price": price_label})

    if active_subs:
        pills = "".join(
            f'<span class="summary-pill" style="--accent:{s["accent"]}">'
            f'<span class="summary-pill-dot" style="background:{s["accent"]}"></span>'
            f'{html.escape(s["name"])} <span class="summary-pill-price">{s["price"]}</span>'
            f'</span>'
            for s in active_subs
        )
        summary_html = (
            f'<div class="sub-summary">'
            f'<div class="sub-summary-head">'
            f'<div class="sub-summary-label">Your active plan</div>'
            f'<div class="sub-summary-total">${total_monthly/100:.2f}<span>/mo equiv.</span></div>'
            f'</div>'
            f'<div class="sub-summary-pills">{pills}</div>'
            f'<a class="sub-summary-link" href="/billing">Manage billing &rarr;</a>'
            f'</div>'
        )
    else:
        summary_html = (
            '<div class="sub-summary sub-summary-empty">'
            '<div class="sub-summary-head">'
            '<div class="sub-summary-label">No active subscriptions</div>'
            '</div>'
            '<p class="sub-summary-hint">Pick a dashboard below to see what it offers, or '
            '<a href="/billing">view plans</a> to save.</p>'
            '</div>'
        )

    # ── Build dashboard cards with feature highlights ───────────────
    req_scheme, req_base, req_port = _request_base_domain(request)
    cards_html = []
    for key, cfg in DASHBOARDS.items():
        has_sub = _is_sub_active(subs.get(key), is_admin_user)
        active_badge = (
            '<span class="badge badge-active">Active</span>' if has_sub
            else '<span class="badge badge-locked">Locked</span>'
        )
        if has_sub:
            if local_mode:
                open_url = f"http://localhost:{cfg['target']}"
            else:
                open_url = f"{req_scheme}://{cfg['subdomain']}.{req_base}{req_port}"
            cta = f'<a class="card-cta cta-open" href="{open_url}" target="_blank" rel="noopener">Open →</a>'
        else:
            cta = f'<a class="card-cta cta-sub" href="/preview/{key}">Learn More</a>'

        # Top 3 features as highlights
        preview = DASHBOARD_PREVIEWS.get(key, {})
        features = preview.get("features", [])[:3]
        highlights_html = ""
        if features:
            items = "".join(
                f'<li class="dash-highlight-item">'
                f'<span class="dash-highlight-icon">{f["icon"]}</span>'
                f'{html.escape(f["title"])}'
                f'</li>'
                for f in features
            )
            highlights_html = f'<ul class="dash-highlights">{items}</ul>'

        cards_html.append(f"""
        <div class="dash-card" style="--accent: {cfg['accent']}">
          <div class="dash-card-head">
            <div class="dash-accent-dot"></div>
            {active_badge}
          </div>
          <div class="dash-card-title">{cfg['display_name']}</div>
          <div class="dash-card-desc">{cfg['description']}</div>
          {highlights_html}
          <div class="dash-card-price">${cfg['monthly_cents']/100:.2f}/mo · ${cfg['annual_cents']/100:.2f}/yr</div>
          <div class="dash-card-foot">{cta}</div>
        </div>
        """)

    # ── Build onboarding tour steps ─────────────────────────────────
    tour_steps_html = []
    for i, (key, cfg) in enumerate(DASHBOARDS.items()):
        preview = DASHBOARD_PREVIEWS.get(key, {})
        tagline = html.escape(preview.get("tagline", cfg["description"]))
        features = preview.get("features", [])[:4]
        feat_html = "".join(
            f'<div class="tour-feat">'
            f'<span class="tour-feat-icon">{f["icon"]}</span>'
            f'<div><strong>{html.escape(f["title"])}</strong><br>'
            f'<span class="tour-feat-desc">{html.escape(f["desc"])}</span></div>'
            f'</div>'
            for f in features
        )
        includes = preview.get("includes", [])[:4]
        inc_html = "".join(f'<li>{html.escape(item)}</li>' for item in includes)
        price_mo = f"${cfg['monthly_cents']/100:.2f}"
        price_yr = f"${cfg['annual_cents']/100:.2f}"

        tour_steps_html.append(
            f'<div class="tour-step" data-step="{i + 2}">'
            f'<div class="tour-step-accent" style="background:{cfg["accent"]}"></div>'
            f'<h2 class="tour-step-title">'
            f'<span class="tour-dot" style="background:{cfg["accent"]}"></span>'
            f'{html.escape(cfg["display_name"])}'
            f'</h2>'
            f'<p class="tour-step-tagline">{tagline}</p>'
            f'<div class="tour-feats">{feat_html}</div>'
            f'<div class="tour-includes"><h4>Included</h4><ul>{inc_html}</ul></div>'
            f'<div class="tour-price">{price_mo}/mo or {price_yr}/yr</div>'
            f'</div>'
        )

    total_steps = len(DASHBOARDS) + 2  # welcome + each dashboard + finish
    tour_html = "".join(tour_steps_html)

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    return render_page(
        "dashboards", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        dashboard_cards="".join(cards_html),
        raw_admin_link=admin_link,
        raw_sub_summary=summary_html,
        raw_tour_steps=tour_html,
        tour_total_steps=str(total_steps),
        raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request),
    )


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, dashboard: Optional[str] = None, payment: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        # Safely forward the validated dashboard key via urlencode to prevent
        # query string injection from user input.
        if dashboard and dashboard in DASHBOARDS:
            forwarded_path = "/billing?" + urlencode({"dashboard": dashboard})
        else:
            forwarded_path = "/billing"
        return await proxy_request(request, forwarded_path)
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    if dashboard and dashboard not in DASHBOARDS:
        dashboard = None

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin_user = bool(user.get("is_admin"))
    now = int(time.time())
    csrf_token = _get_csrf_token(request)
    csrf_hidden = f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token)}">'
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
        monthly_btn = (
            f'<button type="submit" name="action" value="sub:{key}:monthly" class="btn btn-primary" style="--accent:{cfg["accent"]}">Monthly ${cfg["monthly_cents"]/100:.2f}</button>'
        )
        annual_btn = (
            f'<button type="submit" name="action" value="sub:{key}:annual" class="btn btn-primary-outline" style="--accent:{cfg["accent"]}">Annual ${cfg["annual_cents"]/100:.2f}</button>'
        )
        cancel_btn = (
            f'<button type="submit" name="action" value="cancel:{key}" class="btn btn-danger">Cancel</button>'
            if is_active and not is_admin_user else ""
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
          <div class="billing-row-actions">
            <form method="post" action="/billing">
              {csrf_hidden}
              {monthly_btn}
              {annual_btn}
              {cancel_btn}
            </form>
          </div>
        </div>
        """)

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    banner = ""
    if payment == "success":
        banner = (
            '<div style="background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.3);'
            'border-radius:var(--radius-sm);padding:14px 18px;margin-bottom:20px;'
            'color:#10b981;font-size:14px;font-weight:500">'
            'Payment successful! Your subscription is now active.'
            '</div>'
        )
    return render_page(
        "billing", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        billing_rows="".join(rows_html),
        raw_admin_link=admin_link,
        raw_banner=banner,
        raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request),
    )


@app.post("/billing")
async def billing_action(request: Request, action: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/billing")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    parts = action.split(":")

    # ── Subscribe to a single dashboard ────────────────────────────────────
    if parts[0] == "sub" and len(parts) == 3:
        _, key, plan = parts
        if key in DASHBOARDS and plan in ("monthly", "annual"):
            if not STRIPE_SECRET_KEY:
                # Dev/fallback: placeholder mode (no real payment)
                duration = 30 if plan == "monthly" else 365
                db.upsert_subscription(
                    user_id=user["user_id"],
                    dashboard_key=key,
                    plan=plan,
                    duration_days=duration,
                    source="placeholder",
                )
                flush_sub_cache()
                return RedirectResponse("/billing", status_code=302)

            # Create Stripe Checkout Session
            cfg = DASHBOARDS[key]
            price_key = "stripe_price_monthly" if plan == "monthly" else "stripe_price_annual"
            stripe_price_id = cfg.get(price_key)
            if not stripe_price_id:
                log.error("No Stripe price ID configured for %s %s", key, plan)
                return RedirectResponse("/billing", status_code=302)

            try:
                customer_id = _get_or_create_stripe_customer(user["user_id"], user["email"])
            except StripeProviderError:
                return RedirectResponse("/billing?error=stripe_unavailable", status_code=302)
            base = _stripe_base_url(request)

            try:
                session = stripe.checkout.Session.create(
                    customer=customer_id,
                    mode="subscription",
                    line_items=[{"price": stripe_price_id, "quantity": 1}],
                    metadata={
                        "user_id": str(user["user_id"]),
                        "dashboard_key": key,
                        "plan": plan,
                        "type": "dashboard",
                    },
                    success_url=base + "/stripe/success?session_id={CHECKOUT_SESSION_ID}",
                    cancel_url=base + f"/billing?dashboard={key}",
                )
            except Exception as exc:
                log.error("Stripe checkout creation failed for user %s: %s", user.get("user_id"), exc)
                return RedirectResponse("/billing?error=stripe_unavailable", status_code=302)
            return RedirectResponse(session.url, status_code=303)

    # ── Cancel a dashboard subscription ────────────────────────────────────
    elif parts[0] == "cancel" and len(parts) == 2:
        _, key = parts
        if key in DASHBOARDS:
            # Cancel via Stripe API if a Stripe subscription exists
            if STRIPE_SECRET_KEY:
                subs = db.list_subscriptions(user["user_id"])
                for s in subs:
                    if s["dashboard_key"] == key and s["stripe_sub_id"]:
                        try:
                            stripe.Subscription.cancel(s["stripe_sub_id"])
                        except Exception:
                            log.warning("Stripe cancel failed for %s, cancelling locally", s["stripe_sub_id"])
            db.cancel_subscription(user["user_id"], key)
            flush_sub_cache()

    return RedirectResponse("/billing", status_code=302)


@app.post("/billing/subscribe")
async def billing_subscribe(request: Request, plan: str = Form(""), interval: str = Form("monthly")):
    """Subscribe the logged-in user to a bundle plan (trader/pro)."""
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    if plan not in ("trader", "pro"):
        return RedirectResponse("/billing", status_code=302)
    if interval not in ("monthly", "annual"):
        return RedirectResponse("/billing", status_code=302)

    if not STRIPE_SECRET_KEY:
        # Dev/fallback: placeholder mode
        duration = 30 if interval == "monthly" else 365
        for key in DASHBOARDS:
            db.upsert_subscription(
                user_id=user["user_id"],
                dashboard_key=key,
                plan=f"{plan}_{interval}",
                duration_days=duration,
                source=f"billing_{plan}",
            )
        flush_sub_cache()
        log.info("User %s subscribed to %s (%s) — placeholder mode", user.get("username", user["email"]), plan, interval)
        return RedirectResponse("/billing", status_code=302)

    # Create Stripe Checkout Session for the bundle
    bundle = BUNDLE_PLANS[plan]
    price_key = "stripe_price_monthly" if interval == "monthly" else "stripe_price_annual"
    stripe_price_id = bundle.get(price_key)
    if not stripe_price_id:
        log.error("No Stripe price ID configured for bundle %s %s", plan, interval)
        return RedirectResponse("/billing", status_code=302)

    try:
        customer_id = _get_or_create_stripe_customer(user["user_id"], user["email"])
    except StripeProviderError:
        return RedirectResponse("/billing?error=stripe_unavailable", status_code=302)
    base = _stripe_base_url(request)

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": stripe_price_id, "quantity": 1}],
            metadata={
                "user_id": str(user["user_id"]),
                "plan_type": plan,
                "interval": interval,
                "type": "bundle",
            },
            success_url=base + "/stripe/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/billing",
        )
    except Exception as exc:
        log.error("Stripe checkout creation failed for bundle %s: %s", plan, exc)
        return RedirectResponse("/billing?error=stripe_unavailable", status_code=302)
    return RedirectResponse(session.url, status_code=303)


# ── Stripe webhook & success ──────────────────────────────────────────────


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events — activates/cancels subscriptions."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        if IS_PRODUCTION:
            log.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET is not set — rejecting")
            raise HTTPException(status_code=500, detail="Webhook secret not configured")
        # Dev-only: skip verification only when explicitly opted in
        if os.getenv("STRIPE_SKIP_VERIFICATION") != "1":
            log.error("Stripe webhook: no secret and STRIPE_SKIP_VERIFICATION!=1 — rejecting")
            raise HTTPException(status_code=400, detail="Webhook verification required")
        # No webhook secret configured — parse payload directly (dev only)
        event = json.loads(payload)

    event_type = event["type"] if isinstance(event, dict) else event.type
    event_id = event.get("id") if isinstance(event, dict) else getattr(event, "id", None)
    data_obj = event["data"]["object"] if isinstance(event, dict) else event.data.object

    # Idempotency: skip events already processed (Stripe retries on network errors).
    if event_id and not db.mark_stripe_event_processed(event_id, event_type):
        log.info("Stripe webhook: skipping duplicate event %s (%s)", event_id, event_type)
        return JSONResponse({"status": "ok", "duplicate": True})

    # ── checkout.session.completed — subscription paid ─────────────────────
    if event_type == "checkout.session.completed":
        meta = data_obj.get("metadata", {}) if isinstance(data_obj, dict) else (data_obj.metadata or {})
        raw_user_id = meta.get("user_id")
        if not raw_user_id:
            log.warning("Stripe webhook: checkout.session.completed without user_id in metadata")
            return JSONResponse({"status": "ignored"})
        try:
            user_id = int(raw_user_id)
        except (ValueError, TypeError):
            log.warning("Invalid user_id in Stripe metadata: %s", raw_user_id)
            return JSONResponse({"ok": True})  # ack webhook, don't retry

        stripe_sub_id = data_obj.get("subscription") if isinstance(data_obj, dict) else data_obj.subscription

        if meta.get("type") == "dashboard":
            # Per-dashboard subscription
            dashboard_key = meta.get("dashboard_key")
            plan = meta.get("plan", "monthly")
            if dashboard_key and dashboard_key in DASHBOARDS:
                duration = 30 if plan == "monthly" else 365
                db.upsert_subscription(
                    user_id=user_id,
                    dashboard_key=dashboard_key,
                    plan=plan,
                    duration_days=duration,
                    source="stripe",
                    stripe_sub_id=stripe_sub_id,
                )
                log.info("Stripe: activated %s (%s) for user %s", dashboard_key, plan, user_id)

        elif meta.get("type") == "bundle":
            # Bundle plan — unlock all dashboards
            plan_type = meta.get("plan_type", "trader")
            interval = meta.get("interval", "monthly")
            if interval not in ("monthly", "annual"):
                interval = "monthly"
            duration = 30 if interval == "monthly" else 365
            for key in DASHBOARDS:
                db.upsert_subscription(
                    user_id=user_id,
                    dashboard_key=key,
                    plan=f"{plan_type}_{interval}",
                    duration_days=duration,
                    source="stripe",
                    stripe_sub_id=stripe_sub_id,
                )
            log.info("Stripe: activated bundle %s (%s) for user %s", plan_type, interval, user_id)

    # ── customer.subscription.deleted — subscription cancelled ─────────────
    elif event_type == "customer.subscription.deleted":
        stripe_sub_id = data_obj.get("id") if isinstance(data_obj, dict) else data_obj.id
        db.cancel_subscription_by_stripe_id(stripe_sub_id)
        log.info("Stripe: cancelled subscription %s", stripe_sub_id)

    # ── invoice.payment_failed — payment issue ────────────────────────────
    elif event_type == "invoice.payment_failed":
        invoice_id = data_obj.get("id") if isinstance(data_obj, dict) else data_obj.id
        log.warning("Stripe: payment failed for invoice %s", invoice_id)

    # Flush subscription cache after any webhook-driven mutation.
    flush_sub_cache()
    return JSONResponse({"status": "ok"})


@app.get("/stripe/success")
async def stripe_success(request: Request, session_id: str = ""):
    """Redirect back to billing with a success banner after Stripe checkout."""
    return RedirectResponse("/billing?payment=success", status_code=302)


# ── Preview / product page ─────────────────────────────────────────────────


@app.get("/preview/{dashboard_key}", response_class=HTMLResponse)
async def preview_page(request: Request, dashboard_key: str):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/preview/{dashboard_key}")

    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

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
        raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request),
    )


# ── Profile page ────────────────────────────────────────────────────────────


def _trading_credentials_context(user_id: int, csrf_token: str, scope: str) -> dict:
    """Build the template vars for the Trading Credentials section.

    ``scope`` is either ``"profile"`` or ``"settings"`` and controls which
    URL the disconnect forms post to. The Polymarket/Kalshi *save* form
    actions are hard-coded in the templates themselves.
    """
    if scope not in ("profile", "settings"):
        raise ValueError(f"Invalid scope: {scope}")

    cred_status = db.has_trading_credentials(user_id)
    connected = '<span class="setup-status on">Connected</span>'
    not_connected = '<span class="setup-status off">Not connected</span>'
    csrf_hidden = f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token)}">'

    def _delete_form(platform: str, label: str) -> str:
        return (
            f'<form method="post" action="/{scope}/trading/{platform}/delete" style="display:inline" '
            f'onsubmit="return confirm(\'Disconnect {label}?\')">{csrf_hidden}<button type="submit" '
            f'class="setup-btn-remove">Disconnect</button></form>'
        )

    return {
        "raw_trading_banner": "",
        "raw_poly_status": connected if cred_status["polymarket"] else not_connected,
        "raw_kalshi_status": connected if cred_status["kalshi"] else not_connected,
        "raw_alpaca_status": connected if cred_status["alpaca"] else not_connected,
        "raw_poly_delete": _delete_form("polymarket", "Polymarket") if cred_status["polymarket"] else "",
        "raw_kalshi_delete": _delete_form("kalshi", "Kalshi") if cred_status["kalshi"] else "",
        "raw_alpaca_delete": _delete_form("alpaca", "Alpaca") if cred_status["alpaca"] else "",
    }


def _profile_context(user: dict, banner: str = "", csrf_token: str = "", request: Request = None) -> dict:
    import datetime as _dt
    db_user = db.get_user_by_id(user["user_id"])
    if db_user:
        ca = db_user["created_at"]
        if isinstance(ca, (int, float)):
            joined = _dt.datetime.fromtimestamp(ca, tz=_dt.timezone.utc).strftime("%b %d, %Y UTC")
        elif isinstance(ca, str):
            try:
                joined = _dt.datetime.fromisoformat(ca.replace("Z", "+00:00")).strftime("%b %d, %Y UTC")
            except ValueError:
                joined = ca
        else:
            joined = str(ca)
    else:
        joined = "\u2014"
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
        "raw_admin_link": admin_link,
        "raw_banner": banner,
        "raw_dashboard_tabs": _build_tab_html(user["user_id"], request=request),
        **_trading_credentials_context(user["user_id"], csrf_token, scope="profile"),
    }


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/portfolio")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    return render_page("portfolio", request=request)


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    return render_page("profile", request=request, **_profile_context(user, csrf_token=_get_csrf_token(request), request=request))


@app.post("/profile/password")
async def profile_change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), confirm_password: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile/password")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    db_user = db.get_user_by_id(user["user_id"])
    if not db_user:
        return RedirectResponse("/gate", status_code=302)

    err_banner = lambda msg: f'<div class="notice notice-error" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--red)">{html.escape(msg)}</div>'
    ok_banner = lambda msg: f'<div class="notice notice-success" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--green)">{html.escape(msg)}</div>'

    _csrf = _get_csrf_token(request)
    if not db.verify_user_password(user["email"], current_password):
        return render_page("profile", request=request, **_profile_context(user, err_banner("Current password is incorrect."), csrf_token=_csrf, request=request))
    if new_password != confirm_password:
        return render_page("profile", request=request, **_profile_context(user, err_banner("New passwords don't match."), csrf_token=_csrf, request=request))
    if len(new_password) < 12 or len(new_password) > 256:
        return render_page("profile", request=request, **_profile_context(user, err_banner("Password must be 12\u2013256 characters."), csrf_token=_csrf, request=request))
    if not re.search(r"[A-Z]", new_password) or not re.search(r"[a-z]", new_password) or not re.search(r"[0-9]", new_password) or not re.search(r"[^A-Za-z0-9]", new_password):
        return render_page("profile", request=request, **_profile_context(user, err_banner("Password must include uppercase, lowercase, number, and special character."), csrf_token=_csrf, request=request))

    db.update_user_password(user["user_id"], new_password)
    # Invalidate all other sessions so a compromised session can't persist
    db.delete_user_sessions(user["user_id"])
    flush_session_cache()
    # Re-create a session for the current user so they stay logged in
    new_token = db.create_session(user["user_id"])
    log.info("User %s changed their password, all sessions invalidated", user.get("username", user["email"]))
    resp = render_page("profile", request=request, **_profile_context(user, ok_banner("Password changed successfully. All other sessions have been signed out."), csrf_token=_get_csrf_token(request)))
    set_session_cookie(resp, new_token, request)
    return resp


# ── Trading credential management (profile forms) ────────────────────────


def _parse_trading_creds(
    platform: str, private_key: str, api_key: str, api_secret: str,
    api_passphrase: str, email: str, password: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Validate trading-credential form input.

    Returns ``(creds_dict, None)`` on success or ``(None, error_message)`` on
    failure. Shared by ``/profile/trading/*`` and ``/settings/trading/*`` so the
    two flows can stay byte-for-byte consistent in what they accept.
    """
    if platform == "polymarket":
        pk = private_key.strip()
        if not pk:
            return None, "Polymarket private key is required."
        return {
            "private_key": pk,
            "api_key": api_key.strip(),
            "api_secret": api_secret.strip(),
            "api_passphrase": api_passphrase.strip(),
        }, None
    if platform == "kalshi":
        ak = api_key.strip()
        em = email.strip()
        pw = password.strip()
        if not ak and not (em and pw):
            return None, "Kalshi API key, or email + password, is required."
        return {"api_key": ak, "email": em, "password": pw}, None
    if platform == "alpaca":
        ak = api_key.strip()
        sec = api_secret.strip()
        if not ak or not sec:
            return None, "Alpaca API key and secret are required."
        return {"api_key": ak, "api_secret": sec, "paper": True}, None
    return None, "Unknown trading platform."


@app.post("/profile/trading/{platform}")
async def profile_save_trading_creds(
    request: Request, platform: str,
    private_key: str = Form(""), api_key: str = Form(""),
    api_secret: str = Form(""), api_passphrase: str = Form(""),
    email: str = Form(""), password: str = Form(""),
):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/profile/trading/{platform}")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    if platform not in ("polymarket", "kalshi", "alpaca"):
        return RedirectResponse("/profile#trading", status_code=302)

    t_err = lambda msg: (
        f'<div class="notice notice-error" style="padding:10px 14px;border-radius:var(--radius-sm);'
        f'font-size:13px;border:1px solid var(--red)">{html.escape(msg)}</div>'
    )
    t_ok = lambda msg: (
        f'<div class="notice notice-success" style="padding:10px 14px;border-radius:var(--radius-sm);'
        f'font-size:13px;border:1px solid var(--green)">{html.escape(msg)}</div>'
    )

    _csrf = _get_csrf_token(request)
    creds, err = _parse_trading_creds(platform, private_key, api_key, api_secret, api_passphrase, email, password)
    if err is not None:
        ctx = _profile_context(user, csrf_token=_csrf, request=request)
        ctx["raw_trading_banner"] = t_err(err)
        return render_page("profile", request=request, **ctx)

    db.save_trading_credentials(user["user_id"], platform, creds)
    log.info("User %s saved %s trading credentials via profile", user.get("username", user["email"]), platform)
    ctx = _profile_context(user, csrf_token=_csrf, request=request)
    platform_label = {"polymarket": "Polymarket", "kalshi": "Kalshi", "alpaca": "Alpaca"}.get(platform, platform)
    ctx["raw_trading_banner"] = t_ok(f"{platform_label} credentials saved and encrypted.")
    return render_page("profile", request=request, **ctx)


@app.post("/profile/trading/{platform}/delete")
async def profile_delete_trading_creds(request: Request, platform: str):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/profile/trading/{platform}/delete")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    if platform in ("polymarket", "kalshi", "alpaca"):
        db.delete_trading_credentials(user["user_id"], platform)
        log.info("User %s deleted %s trading credentials", user.get("username", user["email"]), platform)
    return RedirectResponse("/profile#trading", status_code=302)


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


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")
    return render_page("forgot-password", request=request, error="", raw_success="")


@app.post("/forgot-password")
async def forgot_password_submit(request: Request, email: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/forgot-password")

    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "forgot", _RATE_MAX_FORGOT):
        return RATE_LIMITED_RESPONSE

    email = email.lower().strip()

    # Always show success — don't reveal whether the email exists.
    success_msg = (
        '<div class="auth-success">If an account with that email exists, '
        'we\'ve sent a password reset link. Check your inbox.</div>'
    )

    if not email or not is_valid_email(email):
        return render_page("forgot-password", request=request, error="Please enter a valid email address.", raw_success="")

    user = db.get_user_by_email(email)
    if user:
        reset_token = db.create_password_reset(user["id"])
        # Build the reset link from the configured DOMAIN, not the request Host header.
        # The Host header is attacker-controlled and could be used to phish a victim
        # into clicking a reset link that points to a malicious domain.
        if IS_PRODUCTION:
            reset_link = f"https://{DOMAIN}/reset-password?token={reset_token}"
        else:
            host = request.headers.get("host", DOMAIN).split(",")[0].strip()
            # Allow only localhost or the configured DOMAIN in dev to avoid header injection.
            if host not in (DOMAIN, "localhost", "127.0.0.1") and not host.startswith("localhost:") and not host.startswith("127.0.0.1:"):
                host = DOMAIN
            reset_link = f"{request.url.scheme}://{host}/reset-password?token={reset_token}"
        log.info("Password reset requested for %s", email)

        # Send email if SMTP is configured
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")
        if smtp_user and smtp_pass:
            try:
                import smtplib
                from email.mime.text import MIMEText

                smtp_host = os.environ.get("SMTP_HOST", "localhost")
                try:
                    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
                except (ValueError, TypeError):
                    smtp_port = 587

                body_text = (
                    f"Hi,\n\n"
                    f"A password reset was requested for your betyc account.\n\n"
                    f"Click the link below to set a new password:\n"
                    f"{reset_link}\n\n"
                    f"This link expires in 1 hour.\n\n"
                    f"If you did not request this, you can safely ignore this email.\n"
                )
                msg = MIMEText(body_text)
                msg["Subject"] = "Password Reset \u2014 betyc"
                msg["From"] = smtp_user
                msg["To"] = email

                def _send():
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.sendmail(smtp_user, [email], msg.as_string())

                await asyncio.to_thread(_send)
                log.info("Password reset email sent to %s", email)
            except Exception as exc:
                log.error("Failed to send password reset email: %s", exc)
        else:
            # No SMTP configured — note that a reset was generated, but never log
            # the actual reset_token/reset_link. Logs may be shipped or persisted,
            # and a leaked token would let an attacker reset that user's password.
            log.warning(
                "SMTP not configured. Password reset requested for %s but no email sent. "
                "Configure SMTP_USER/SMTP_PASS to enable reset emails.",
                email,
            )

    return render_page("forgot-password", request=request, error="", raw_success=success_msg)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/reset-password")

    token = token.strip()
    reset = db.get_password_reset(token) if token else None
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

    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    token = token.strip()
    reset = db.get_password_reset(token) if token else None
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

    db.update_user_password(reset["user_id"], new_password)
    db.use_password_reset(token)
    # Kill all existing sessions so a compromised session can't persist.
    # Flush the cache BEFORE deleting from DB so concurrent in-flight requests
    # cannot read a stale session row that races the cache flush.
    flush_session_cache()
    db.delete_user_sessions(reset["user_id"])
    flush_session_cache()

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


# ── Static info pages ─────────────────────────────────────────────────────────


@app.get("/impressum", response_class=HTMLResponse)
async def impressum_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/impressum")
    return render_page("impressum", request=request)


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/support")
    return render_page("support", request=request)


@app.get("/suspended", response_class=HTMLResponse)
async def suspended_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/suspended")
    return render_page("suspended", request=request)


# ── Enquiry page + API ───────────────────────────────────────────────────────


@app.get("/enquire", response_class=HTMLResponse)
async def enquire_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/enquire")
    return render_page("enquire", request=request)


@app.post("/api/enquire")
async def api_enquire(request: Request):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/enquire")
    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "enquire", 3):
        return JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    job_title = (body.get("job_title") or "").strip()
    message = (body.get("message") or "").strip()

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

    # Optional: send email notification if ENQUIRY_EMAIL is set
    enquiry_email = os.environ.get("ENQUIRY_EMAIL")
    if enquiry_email:
        try:
            import smtplib
            from email.mime.text import MIMEText
            smtp_host = os.environ.get("SMTP_HOST", "localhost")
            try:
                smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            except (ValueError, TypeError):
                smtp_port = 587
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASS", "")

            body_text = (
                f"New enquiry from the betyc landing page.\n\n"
                f"Email: {email}\n"
                f"Role: {job_title}\n\n"
                f"Message:\n{message}\n"
            )
            msg = MIMEText(body_text)
            msg["Subject"] = "New Enquiry \u2014 betyc"
            msg["From"] = smtp_user or enquiry_email
            msg["To"] = enquiry_email

            _from = msg["From"]
            _to = enquiry_email
            _msg_str = msg.as_string()

            def _send_enquiry():
                with smtplib.SMTP(smtp_host, smtp_port) as srv:
                    if smtp_user and smtp_pass:
                        srv.starttls()
                        srv.login(smtp_user, smtp_pass)
                    srv.sendmail(_from, [_to], _msg_str)

            await asyncio.to_thread(_send_enquiry)
            log.info("Enquiry notification email sent to %s", enquiry_email)
        except Exception as exc:
            log.error("Failed to send enquiry email: %s", exc)

    return JSONResponse({"success": True})


@app.post("/api/newsletter")
async def api_newsletter(request: Request):
    """Landing page waitlist signup. Accepts email + optional referral, persists
    as a lightweight enquiry, returns the user's position in the waitlist."""
    # Require XMLHttpRequest header to prevent cross-site form-post CSRF
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/newsletter")
    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "newsletter", 5):
        return JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429)

    # Accept either form-encoded (landing page) or JSON
    email = ""
    ref = ""
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            body = await request.json()
            if isinstance(body, dict):
                email = (body.get("email") or "").strip().lower()
                ref = (body.get("ref") or "").strip()
        else:
            form = await request.form()
            email = (form.get("email") or "").strip().lower()
            ref = (form.get("ref") or "").strip()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    if not email or not EMAIL_RE.match(email):
        return JSONResponse({"error": "Please enter a valid email address"}, status_code=400)
    # Bound the referral string to prevent abuse
    ref = ref[:120]

    try:
        message = f"Newsletter signup{f' (ref: {ref})' if ref else ''}"
        db.create_enquiry(email, "newsletter", message)
    except Exception as exc:
        log.error("Failed to record newsletter signup for %s: %s", email, exc)
        return JSONResponse({"error": "Could not record signup. Try again."}, status_code=500)

    # Position = total enquiries (rough waitlist position; admins can de-dup later)
    try:
        all_enq = db.list_enquiries() or []
        position = len(all_enq)
    except Exception:
        position = 0

    # Build a simple share URL pointing back at the landing page with a ref tag
    try:
        scheme = "https" if IS_PRODUCTION else "http"
        share_url = f"{scheme}://{DOMAIN}/?ref={position}" if DOMAIN else f"/?ref={position}"
    except Exception:
        share_url = "/"

    log.info("Newsletter signup: %s (position=%d)", email, position)
    return JSONResponse({
        "success": True,
        "position": position,
        "share_url": share_url,
    })


# ── Admin panel ──────────────────────────────────────────────────────────────


def _require_admin_user(request: Request) -> dict:
    """Return the current user dict if admin, otherwise raise 403."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _build_admin_context(new_token_str: str = "", new_superuser_key: str = "", caller_level: int = 1, csrf_token: str = "", leads_status: str = "new") -> dict:
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
                f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token)}">'
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
    csrf_hidden = f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token)}">'
    pw_style = 'style="padding:6px 10px;font-size:11px;background:#1e2130;color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-xs);width:140px"'
    pw_field = f'<input name="password" type="password" placeholder="Your password" {pw_style} required>'
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
        # Also escape for JS string context (backslashes and single quotes)
        uname_js = uname.replace("\\", "\\\\").replace("'", "\\'")
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
                    f'onsubmit="return confirm(\'Change role for {uname_js}?\')" style="display:flex;gap:6px;align-items:center">'
                    f'{csrf_hidden}'
                    f'<select name="level" {sel_style}>{role_opts}</select>'
                    f'{pw_field}'
                    f'<button class="btn btn-primary-outline" style="font-size:11px">Set Role</button></form>'
                )
            else:
                # Regular admin: can only demote level-1 admins (promote requires super admin)
                if ulevel == 1:
                    actions += f'<form method="post" action="/admin/users/{u["id"]}/demote" onsubmit="return confirm(\'Demote {uname_js}?\')" style="display:flex;gap:6px;align-items:center">{csrf_hidden}{pw_field}<button class="btn btn-danger" style="font-size:11px">Demote to User</button></form>'

            # Suspend/unsuspend
            if not u["suspended"]:
                actions += f'<form method="post" action="/admin/users/{u["id"]}/suspend" onsubmit="return confirm(\'Suspend {uname_js}?\')" style="display:flex;gap:6px;align-items:center">{csrf_hidden}{pw_field}<button class="btn btn-danger" style="font-size:11px">Suspend</button></form>'
            else:
                actions += f'<form method="post" action="/admin/users/{u["id"]}/unsuspend" onsubmit="return confirm(\'Unsuspend {uname_js}?\')" style="display:flex;gap:6px;align-items:center">{csrf_hidden}{pw_field}<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Unsuspend</button></form>'

            # Change email (admin+)
            detail_extra += (
                f'<form method="post" action="/admin/users/{u["id"]}/email" onclick="event.stopPropagation()" '
                f'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                f'{csrf_hidden}'
                f'<input name="new_email" type="email" placeholder="New email" {sel_style} style="padding:6px 10px;font-size:11px;background:#1e2130;color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-xs);flex:1">'
                f'{pw_field}'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Change Email</button></form>'
            )

            # Revoke token (admin+)
            if u["invite_token_id"]:
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/revoke-token" onclick="event.stopPropagation()" '
                    f'onsubmit="return confirm(\'Revoke token for {uname_js}? They will not be able to log in.\')"'
                    f' style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                    f'{csrf_hidden}{pw_field}'
                    f'<button class="btn btn-danger" style="font-size:11px">Revoke Invite Token</button></form>'
                )

            # Generate new token for user (admin+)
            detail_extra += (
                f'<form method="post" action="/admin/users/{u["id"]}/new-token" onclick="event.stopPropagation()" '
                f'onsubmit="return confirm(\'Generate a new invite token for {uname_js}?\')" '
                f'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                f'{csrf_hidden}{pw_field}'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Generate New Token</button></form>'
            )

            # Grant subscription (super admin only)
            if is_super:
                dash_checks = "".join(
                    f'<label style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-secondary);cursor:pointer">'
                    f'<input type="checkbox" name="dashboard_keys" value="{k}" style="accent-color:var(--green);cursor:pointer">'
                    f'{html.escape(cfg["display_name"])}</label>'
                    for k, cfg in DASHBOARDS.items()
                )
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/grant" onclick="event.stopPropagation()" '
                    f'style="margin-top:8px">'
                    f'{csrf_hidden}'
                    f'<div style="display:flex;flex-wrap:wrap;gap:8px 14px;margin-bottom:8px">{dash_checks}</div>'
                    f'<div style="display:flex;gap:6px;align-items:center">'
                    f'<button type="button" onclick="let c=this.closest(\'form\').querySelectorAll(\'input[type=checkbox]\');let all=Array.from(c).every(x=>x.checked);c.forEach(x=>x.checked=!all)" '
                    f'class="btn btn-primary-outline" style="font-size:11px">Toggle All</button>'
                    f'<select name="plan" {sel_style}><option value="monthly">Monthly</option><option value="annual">Annual</option></select>'
                    f'{pw_field}'
                    f'<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Grant Free</button>'
                    f'</div></form>'
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
            f'<strong>Email:</strong> {email_esc}'
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

    # New superuser key banner
    new_superuser_key_banner = ""
    if new_superuser_key:
        new_superuser_key_banner = (
            f'<div class="new-token-banner" style="border-color:var(--accent)">'
            f'<div style="display:flex;align-items:center;justify-content:space-between">'
            f'<div><div style="font-size:12px;color:var(--accent);margin-bottom:4px">New investor key generated:</div>'
            f'<span class="token-mono">{html.escape(new_superuser_key)}</span></div>'
            f'<button onclick="copyToken(this)" class="btn btn-primary-outline" style="font-size:11px;color:var(--accent);border-color:var(--accent)">Copy</button>'
            f'</div></div>'
        )

    return {
        "raw_token_rows": "".join(token_rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No tokens yet.</div></div></div>',
        "raw_user_rows": "".join(user_rows),
        "raw_stat_cards": stat_cards,
        "raw_new_token_banner": new_token_banner,
        "raw_new_superuser_key_banner": new_superuser_key_banner,
        "raw_key_rows": _build_key_rows(csrf_token=csrf_token),
        "raw_enquiry_rows": _build_enquiry_rows(csrf_token=csrf_token),
        "raw_revenue_content": _build_revenue_content(),
        "raw_leads_panel": _build_leads_panel(csrf_token=csrf_token, status_view=leads_status),
    }


def _build_leads_panel(csrf_token: str = "", status_view: str = "new") -> str:
    """Render the customer-finding-bot leads list for the admin Leads tab.

    Every action button is human-driven: open the source URL in a new tab,
    copy the drafted message to the clipboard, then mark contacted/skip/snooze.
    Nothing sends autonomously.

    `status_view` controls which bucket is rendered (new / contacted /
    snoozed / skipped). Client-side JS filters further by source / dashboard.
    """
    import datetime as _dt

    counts = leads_store.counts_by_status()
    new_n = counts.get("new", 0)
    contacted_n = counts.get("contacted", 0)
    skipped_n = counts.get("skipped", 0)
    snoozed_n = counts.get("snoozed", 0)
    archived_n = counts.get("archived", 0)

    conv = leads_store.conversion_stats()
    replied_n = conv.get("replied", 0)
    signed_up_n = conv.get("signed_up", 0)
    no_reply_n = conv.get("no_reply", 0)
    sample = contacted_n  # outcome rate is over all contacted
    conv_pct = f"{100*signed_up_n/sample:.0f}%" if sample else "—"

    rows = leads_store.list_leads(status=status_view, limit=300)

    # Build source/dashboard filter chips from what's actually present in
    # the visible rows so we don't list empty options.
    sources_present = sorted({r["source"] for r in rows})
    dashboards_present = sorted({r["dashboard_key"] for r in rows})

    csrf_esc = html.escape(csrf_token)

    def _status_tab(label: str, key: str, n: int) -> str:
        active = " style=\"font-weight:600;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:6px\"" if key == status_view else " style=\"color:var(--text-secondary);padding-bottom:6px\""
        return f'<a href="?leads_status={key}#leads"{active}>{html.escape(label)} <span style="color:var(--text-muted)">({n})</span></a>'

    status_bar = (
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:13px">'
        + _status_tab("New", "new", new_n)
        + _status_tab("Contacted", "contacted", contacted_n)
        + _status_tab("Snoozed", "snoozed", snoozed_n)
        + _status_tab("Skipped", "skipped", skipped_n)
        + _status_tab("Archived", "archived", archived_n)
        + '</div>'
    )

    metrics = (
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px;font-size:12px;color:var(--text-muted)">'
        f'  <span>signed-up <strong style="color:var(--green)">{signed_up_n}</strong></span>'
        f'  <span>replied <strong style="color:var(--text-primary)">{replied_n}</strong></span>'
        f'  <span>no-reply <strong style="color:var(--text-primary)">{no_reply_n}</strong></span>'
        f'  <span>conversion <strong style="color:var(--text-primary)">{conv_pct}</strong></span>'
        '  <span style="margin-left:auto">Reddit posts + comments · HN · Polymarket · polls every 30 min, read-only.</span>'
        '</div>'
    )

    # Filter chips (client-side toggles).
    src_chips = ['<button type="button" class="filter-btn active" onclick="leadsFilter(\'source\',\'all\',this)">All sources</button>']
    for s in sources_present:
        src_chips.append(
            f'<button type="button" class="filter-btn" onclick="leadsFilter(\'source\',{json.dumps(s)},this)">{html.escape(s)}</button>'
        )
    dash_chips = ['<button type="button" class="filter-btn active" onclick="leadsFilter(\'dashboard\',\'all\',this)">All dashboards</button>']
    for d in dashboards_present:
        name = DASHBOARDS.get(d, {}).get("display_name", d)
        dash_chips.append(
            f'<button type="button" class="filter-btn" onclick="leadsFilter(\'dashboard\',{json.dumps(d)},this)">{html.escape(name)}</button>'
        )
    chips = (
        '<div class="filter-bar" style="margin-bottom:10px;flex-wrap:wrap">' + "".join(src_chips) + '</div>'
        '<div class="filter-bar" style="margin-bottom:14px;flex-wrap:wrap">' + "".join(dash_chips) + '</div>'
    )

    if not rows:
        body = (
            '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">'
            f'No leads in this view ({html.escape(status_view)}). '
            'The bot polls Reddit (posts + comments), Hacker News, and Polymarket every 30 minutes.'
            '</div></div></div>'
        )
        bulk_bar = ""
    else:
        parts = []
        for r in rows:
            dash_name = DASHBOARDS.get(r["dashboard_key"], {}).get("display_name", r["dashboard_key"])
            posted = ""
            if r["posted_at"]:
                posted = _dt.datetime.fromtimestamp(r["posted_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            source = r["source"]
            source_esc = html.escape(source)
            title = html.escape((r["title"] or "(no title)")[:200])
            snippet = html.escape((r["snippet"] or "")[:400])
            author = html.escape(r["author"] or "anon")
            url = html.escape(r["url"] or "")
            draft = html.escape(r["draft"] or "")
            score = int(r["score"] or 0)
            ref = html.escape(r["ref_code"] or "")
            outcome = r["outcome"] if "outcome" in r.keys() else ""

            csrf_hidden = f'<input type="hidden" name="_csrf_token" value="{csrf_esc}">'

            actions: list[str] = []
            actions.append(
                f'<a href="{url}" target="_blank" rel="noopener noreferrer" class="btn btn-primary-outline" style="font-size:12px">Open source ↗</a>'
            )
            actions.append(
                f'<button type="button" class="btn btn-primary-outline" style="font-size:12px" onclick="copyDraft({r["id"]}, this)">Copy draft</button>'
            )
            if status_view == "new":
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/contacted" style="display:inline">{csrf_hidden}<button type="submit" class="btn btn-primary" style="font-size:12px">Mark contacted</button></form>'
                )
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/snooze" style="display:inline">{csrf_hidden}<input type="hidden" name="days" value="7"><button type="submit" class="btn btn-primary-outline" style="font-size:12px">Snooze 7d</button></form>'
                )
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/skip" style="display:inline">{csrf_hidden}<button type="submit" class="btn btn-danger" style="font-size:12px">Skip</button></form>'
                )
            elif status_view == "contacted":
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/outcome" style="display:inline">{csrf_hidden}<input type="hidden" name="outcome" value="signed_up"><button type="submit" class="btn btn-primary" style="font-size:12px;background:var(--green);border-color:var(--green)">Signed up</button></form>'
                )
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/outcome" style="display:inline">{csrf_hidden}<input type="hidden" name="outcome" value="replied"><button type="submit" class="btn btn-primary-outline" style="font-size:12px">Replied</button></form>'
                )
                actions.append(
                    f'<form method="post" action="/admin/leads/{r["id"]}/outcome" style="display:inline">{csrf_hidden}<input type="hidden" name="outcome" value="no_reply"><button type="submit" class="btn btn-primary-outline" style="font-size:12px">No reply</button></form>'
                )

            outcome_badge = ""
            if outcome:
                colour = "var(--green)" if outcome == "signed_up" else "var(--text-secondary)"
                outcome_badge = f'<span class="badge" style="background:var(--surface);color:{colour}">outcome: {html.escape(outcome)}</span>'

            ref_badge = (
                f'<span class="badge" style="background:var(--surface);color:var(--text-muted);font-family:monospace">ref {ref}</span>'
                if ref else ""
            )

            parts.append(
                f'<div class="admin-row lead-row" data-source="{source_esc}" data-dashboard="{html.escape(r["dashboard_key"])}" style="flex-direction:column;align-items:stretch;gap:10px">'
                f'  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
                f'    <input type="checkbox" name="lead_ids" form="leads-bulk-form" value="{r["id"]}" class="lead-cb" onchange="updateLeadBulkBar()" style="width:16px;height:16px;accent-color:var(--accent);cursor:pointer">'
                f'    <span class="badge" style="background:var(--accent-light);color:var(--accent)">{source_esc}</span>'
                f'    <span class="badge" style="background:var(--surface);color:var(--text-secondary)">→ {html.escape(dash_name)}</span>'
                f'    <span class="badge" style="background:var(--surface);color:var(--text-secondary)">score {score}</span>'
                f'    {ref_badge}'
                f'    {outcome_badge}'
                f'    <span style="font-size:12px;color:var(--text-muted);margin-left:auto">{author} · {posted}</span>'
                f'  </div>'
                f'  <div style="font-weight:500;color:var(--text-primary)">{title}</div>'
                f'  <div style="font-size:13px;color:var(--text-secondary);white-space:pre-wrap">{snippet}</div>'
                f'  <details>'
                f'    <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">Drafted message</summary>'
                f'    <textarea readonly id="draft-{r["id"]}" style="width:100%;min-height:90px;margin-top:8px;background:var(--surface);color:var(--text-primary);border:1px solid var(--border);border-radius:var(--radius-xs);padding:8px;font-size:13px;font-family:inherit">{draft}</textarea>'
                f'  </details>'
                f'  <div style="display:flex;gap:8px;flex-wrap:wrap">' + "".join(actions) + '  </div>'
                f'</div>'
            )
        body = "".join(parts)

        bulk_bar = (
            f'<form id="leads-bulk-form" method="post" action="/admin/leads/bulk" style="margin-bottom:12px">'
            f'  <input type="hidden" name="_csrf_token" value="{csrf_esc}">'
            f'  <input type="hidden" name="return_to" value="{html.escape(status_view)}">'
            f'  <div id="leads-bulk-bar" style="display:none;padding:10px 14px;background:var(--surface);border:1px solid var(--accent);border-radius:var(--radius-sm);align-items:center;gap:10px;flex-wrap:wrap">'
            f'    <span id="leads-bulk-count" style="font-size:13px;font-weight:500">0 selected</span>'
            f'    <label style="font-size:12px;color:var(--text-muted);cursor:pointer;display:inline-flex;align-items:center;gap:6px">'
            f'      <input type="checkbox" id="leads-select-all" onchange="leadsSelectAll(this.checked)" style="width:16px;height:16px;accent-color:var(--accent)"> Select all visible'
            f'    </label>'
            f'    <div style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap">'
            f'      <button type="submit" name="bulk_action" value="contacted" class="btn btn-primary" style="font-size:12px" onclick="return confirm(\'Mark selected leads as contacted?\')">Mark contacted</button>'
            f'      <button type="submit" name="bulk_action" value="skip" class="btn btn-danger" style="font-size:12px" onclick="return confirm(\'Skip selected leads?\')">Skip</button>'
            f'      <button type="submit" name="bulk_action" value="archive" class="btn btn-primary-outline" style="font-size:12px" onclick="return confirm(\'Archive selected leads?\')">Archive</button>'
            f'    </div>'
            f'  </div>'
            f'</form>'
        )

    return (
        status_bar + metrics + chips + bulk_bar +
        '<div class="admin-list lead-list" style="display:flex;flex-direction:column;gap:12px">' + body + '</div>'
    )


def _build_key_rows(csrf_token: str = "") -> str:
    """Generate HTML rows for superuser keys management."""
    keys = db.list_superuser_keys()
    if not keys:
        return '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No investor keys yet.</div></div></div>'

    import datetime as _dt
    rows = []
    now = int(time.time())

    for k in keys:
        # Determine status
        if not k["active"]:
            status = "disabled"
            status_text = "Disabled"
        elif k["expires_at"] and k["expires_at"] <= now:
            status = "expired"
            status_text = "Expired"
        else:
            status = "active"
            status_text = "Active"

        # Format metadata
        dashboards_text = ", ".join(k["dashboards"]) if k["dashboards"] else "All dashboards"
        aspects_text = ", ".join(k["aspects"]) if k["aspects"] else "None"

        expires_text = ""
        if k["expires_at"]:
            expires_dt = _dt.datetime.fromtimestamp(k["expires_at"], tz=_dt.timezone.utc)
            expires_text = f"Expires: {expires_dt.strftime('%Y-%m-%d')}"

        last_used_text = ""
        if k["last_used_at"]:
            used_dt = _dt.datetime.fromtimestamp(k["last_used_at"], tz=_dt.timezone.utc)
            last_used_text = f"Last used: {used_dt.strftime('%Y-%m-%d %H:%M')}"

        created_dt = _dt.datetime.fromtimestamp(k["created_at"], tz=_dt.timezone.utc)
        created_text = f"Created: {created_dt.strftime('%Y-%m-%d')}"

        # Status dot and button
        toggle_btn_text = "Enable" if status == "disabled" else "Disable"
        toggle_btn_color = "var(--green)" if status == "disabled" else "var(--text-secondary)"

        # Build row
        rows.append(
            f'<div class="admin-row key-row" data-status="{status}" data-key-id="{k["id"]}">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">'
            f'<span class="key-status-dot {status}"></span>'
            f'<strong>{html.escape(k["name"])}</strong>'
            f'<span class="badge" style="background:var(--surface-hover);color:var(--text-secondary);font-size:11px">{status_text}</span>'
            f'</div>'
            f'<div class="admin-row-meta">'
            f'<span>Dashboards: {html.escape(dashboards_text)}</span>'
            f'<span>Aspects: {html.escape(aspects_text)}</span>'
            f'<span>{created_text}</span>'
        )
        if expires_text:
            rows.append(f'<span>{expires_text}</span>')
        if last_used_text:
            rows.append(f'<span>{last_used_text}</span>')
        rows.append(
            f'</div>'
            f'</div>'
            f'<div class="admin-row-actions" style="display:flex;gap:6px">'
            f'<button class="btn btn-primary-outline" style="font-size:11px" onclick="toggleKeyStatus({k["id"]}, this)">{toggle_btn_text}</button>'
            f'<button class="btn btn-danger" style="font-size:11px" onclick="deleteKey({k["id"]})">Delete</button>'
            f'</div>'
            f'</div>'
        )

    return "".join(rows)


def _build_enquiry_rows(csrf_token: str = "") -> str:
    enquiries = db.list_enquiries()
    if not enquiries:
        return '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No enquiries yet.</div></div></div>'
    import datetime as _dt
    csrf_hidden = f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token)}">'
    rows = []
    for e in enquiries:
        read_badge = "" if e["read"] else '<span class="badge" style="background:var(--accent-light);color:var(--accent)">NEW</span> '
        ts = _dt.datetime.fromtimestamp(e["created_at"], tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        mark_btn = ""
        if not e["read"]:
            mark_btn = (
                f'<form method="post" action="/admin/enquiries/{e["id"]}/read">'
                f'{csrf_hidden}'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Mark Read</button></form>'
            )
        create_token_btn = (
            f'<form method="post" action="/admin/enquiries/{e["id"]}/create-token">'
            f'{csrf_hidden}'
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
        if "monthly" in s["plan"]:
            mrr_cents += cfg["monthly_cents"]
        elif "annual" in s["plan"]:
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
        plan = row["plan"]
        if "monthly" in plan:
            dashboard_rows[key]["monthly"] += row["cnt"]
        elif "annual" in plan:
            dashboard_rows[key]["annual"] += row["cnt"]

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
    user = _require_admin_user(request)
    csrf_token = _get_csrf_token(request)
    leads_status = request.query_params.get("leads_status", "new")
    if leads_status not in ("new", "contacted", "snoozed", "skipped", "archived"):
        leads_status = "new"
    ctx = _build_admin_context(caller_level=user.get("admin_level", 1), csrf_token=csrf_token, leads_status=leads_status)
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request), **ctx)


@app.post("/admin/tokens/generate")
async def admin_generate_token(request: Request, note: str = Form(""), target_email: str = Form("")):
    user = _require_admin_user(request)
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    new_token = db.create_invite_token(note.strip(), target_email=target_email.strip())
    log.info("Admin %s generated invite token (target: %s)", user["email"], target_email.strip() or "none")
    log.debug("Admin %s generated invite token: %s (target: %s)", user["email"], new_token, target_email.strip() or "none")
    csrf_token = _get_csrf_token(request)
    ctx = _build_admin_context(new_token_str=new_token, caller_level=user.get("admin_level", 1), csrf_token=csrf_token)
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request), **ctx)


@app.post("/admin/tokens/revoke")
async def admin_revoke_token(request: Request, token_id: int = Form(0)):
    user = _require_admin_user(request)
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    db.revoke_invite_token(token_id)
    log.info("Admin %s revoked token id=%d", user["email"], token_id)
    return RedirectResponse("/admin", status_code=302)


# ── Superuser key endpoints (for investor access) ──────────────────────────────

@app.post("/admin/superuser-keys/generate")
async def admin_generate_superuser_key(
    request: Request,
    name: str = Form(""),
    dashboards: str = Form(""),
    expires_in_days: str = Form(""),
    custom_key: str = Form(""),
    aspects: str = Form(""),
):
    user = _require_admin_user(request)
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    dashboards_list = [d.strip() for d in dashboards.split(",") if d.strip()]
    aspects_list = [a.strip() for a in aspects.split(",") if a.strip()]
    expires_days = int(expires_in_days) if expires_in_days else None
    custom_key_str = custom_key.strip() if custom_key else None

    try:
        key = db.create_superuser_key(
            name=name.strip() or "Investor Key",
            dashboards=dashboards_list or None,
            expires_in_days=expires_days,
            custom_key=custom_key_str,
            aspects=aspects_list or None,
        )
    except ValueError as e:
        log.warning("Admin %s failed to create superuser key: %s", user["email"], str(e))
        raise HTTPException(status_code=400, detail=str(e))

    log.info("Admin %s generated superuser key: %s (name=%s, aspects=%s)", user["email"], key, name, ",".join(aspects_list) if aspects_list else "none")
    csrf_token = _get_csrf_token(request)
    ctx = _build_admin_context(new_superuser_key=key, caller_level=user.get("admin_level", 1), csrf_token=csrf_token)
    return render_page("admin", request=request, email=user["email"], username=user.get("username", user["email"]), raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request), **ctx)


@app.get("/admin/api/superuser-keys")
async def admin_list_superuser_keys(request: Request):
    user = _require_admin_user(request)
    keys = db.list_superuser_keys()
    return JSONResponse({"keys": keys})


@app.post("/admin/superuser-keys/{key_id}/revoke")
async def admin_revoke_superuser_key(request: Request, key_id: int):
    user = _require_admin_user(request)
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    success = db.revoke_superuser_key(key_id)
    if success:
        log.info("Admin %s revoked superuser key id=%d", user["email"], key_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/superuser-keys/{key_id}/toggle")
async def admin_toggle_superuser_key(request: Request, key_id: int):
    user = _require_admin_user(request)
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    key_info = db.toggle_superuser_key(key_id)
    if key_info:
        action = "enabled" if key_info["active"] else "disabled"
        log.info("Admin %s %s superuser key id=%d (%s)", user["email"], action, key_id, key_info["name"])
        return JSONResponse({"success": True, "key": key_info})
    return JSONResponse({"success": False, "error": "Key not found"}, status_code=404)


@app.post("/admin/superuser-keys/{key_id}/enable")
async def admin_enable_superuser_key(request: Request, key_id: int):
    user = _require_admin_user(request)
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    success = db.enable_superuser_key(key_id)
    if success:
        log.info("Admin %s enabled superuser key id=%d", user["email"], key_id)
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "Key not found"}, status_code=404)


@app.post("/admin/superuser-keys/{key_id}/disable")
async def admin_disable_superuser_key(request: Request, key_id: int):
    user = _require_admin_user(request)
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()

    success = db.disable_superuser_key(key_id)
    if success:
        log.info("Admin %s disabled superuser key id=%d", user["email"], key_id)
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "Key not found"}, status_code=404)


def _verify_admin_password(request: Request, admin: dict, password: str) -> bool:
    """Verify the admin's password for destructive actions. Returns True if valid."""
    return db.verify_user_password(admin["email"], password)


@app.post("/admin/users/{user_id}/promote")
async def admin_promote(request: Request, user_id: int):
    # Promotion requires super-admin privileges to prevent privilege escalation
    # (a regular admin should not be able to create more admins).
    admin = _require_super_admin(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    db.set_user_admin(user_id, True)
    flush_session_cache()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/demote")
async def admin_demote(request: Request, user_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot demote yourself")
    db.set_user_admin(user_id, False)
    flush_session_cache()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend(request: Request, user_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot suspend yourself")
    db.set_user_suspended(user_id, True)
    flush_session_cache()
    log.info("Admin %s suspended user id=%s", admin.get("email"), user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend(request: Request, user_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    db.set_user_suspended(user_id, False)
    flush_session_cache()
    log.info("Admin %s unsuspended user id=%s", admin.get("email"), user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/enquiries/{enquiry_id}/read")
async def admin_mark_enquiry_read(request: Request, enquiry_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    db.mark_enquiry_read(enquiry_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/enquiries/{enquiry_id}/create-token")
async def admin_create_token_from_enquiry(request: Request, enquiry_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    enquiry = db.get_enquiry_by_id(enquiry_id)
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    email = enquiry["email"]
    new_token = db.create_invite_token(
        note=f"From enquiry: {email}",
        target_email=email,
    )
    db.mark_enquiry_read(enquiry_id)
    log.info("Admin %s created token %s for enquiry %d (%s)", admin["email"], new_token, enquiry_id, email)
    csrf_token = _get_csrf_token(request)
    ctx = _build_admin_context(new_token_str=new_token, caller_level=admin.get("admin_level", 1), csrf_token=csrf_token)
    return render_page("admin", request=request, email=admin["email"], username=admin.get("username", admin["email"]), raw_dashboard_tabs=_build_tab_html(admin["user_id"], request=request), **ctx)


# ── Admin: customer-finding bot leads ────────────────────────────────────────


@app.post("/admin/leads/{lead_id}/contacted")
async def admin_lead_mark_contacted(request: Request, lead_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    if not _validate_csrf(request, form.get("_csrf_token", "")):
        return _csrf_error()
    leads_store.set_status(lead_id, "contacted", note=f"by {admin['email']}")
    return RedirectResponse(url="/admin?leads_status=new#leads", status_code=303)


@app.post("/admin/leads/{lead_id}/skip")
async def admin_lead_skip(request: Request, lead_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    if not _validate_csrf(request, form.get("_csrf_token", "")):
        return _csrf_error()
    leads_store.set_status(lead_id, "skipped", note=f"by {admin['email']}")
    return RedirectResponse(url="/admin?leads_status=new#leads", status_code=303)


@app.post("/admin/leads/{lead_id}/snooze")
async def admin_lead_snooze(request: Request, lead_id: int):
    _require_admin_user(request)
    form = await request.form()
    if not _validate_csrf(request, form.get("_csrf_token", "")):
        return _csrf_error()
    try:
        days = max(1, min(int(form.get("days", "7")), 90))
    except ValueError:
        days = 7
    until = int(time.time()) + days * 86400
    leads_store.snooze(lead_id, until)
    return RedirectResponse(url="/admin#leads", status_code=303)


@app.post("/admin/leads/{lead_id}/outcome")
async def admin_lead_outcome(request: Request, lead_id: int):
    _require_admin_user(request)
    form = await request.form()
    if not _validate_csrf(request, form.get("_csrf_token", "")):
        return _csrf_error()
    outcome = (form.get("outcome") or "").strip()
    if outcome not in ("replied", "signed_up", "no_reply"):
        raise HTTPException(status_code=400, detail="Invalid outcome")
    leads_store.set_outcome(lead_id, outcome)
    return RedirectResponse(url="/admin?leads_status=contacted#leads", status_code=303)


@app.post("/admin/leads/bulk")
async def admin_leads_bulk(request: Request):
    admin = _require_admin_user(request)
    form = await request.form()
    if not _validate_csrf(request, form.get("_csrf_token", "")):
        return _csrf_error()
    action = (form.get("bulk_action") or "").strip()
    if action not in ("skip", "contacted", "archive"):
        raise HTTPException(status_code=400, detail="Invalid bulk action")
    lead_ids: list[int] = []
    for raw_id in form.getlist("lead_ids"):
        try:
            lead_ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if not lead_ids:
        return RedirectResponse(url="/admin?leads_status=new#leads", status_code=303)
    status_map = {"skip": "skipped", "contacted": "contacted", "archive": "archived"}
    n = leads_store.bulk_set_status(lead_ids, status_map[action], note=f"bulk by {admin['email']}")
    log.info("Admin %s bulk-%s %d leads", admin["email"], action, n)
    return_to = (form.get("return_to") or "new").strip()
    if return_to not in ("new", "contacted", "snoozed", "skipped", "archived"):
        return_to = "new"
    return RedirectResponse(url=f"/admin?leads_status={return_to}#leads", status_code=303)


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
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if level not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="Invalid role level")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    db.set_user_role(user_id, level)
    flush_session_cache()
    log.info("Super admin %s set user %s role to %d", admin["email"], user_id, level)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/email")
async def admin_change_email(request: Request, user_id: int, new_email: str = Form("")):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    new_email = new_email.strip().lower()
    if not new_email or not EMAIL_RE.match(new_email):
        raise HTTPException(status_code=400, detail="Invalid email")
    existing = db.get_user_by_email(new_email)
    if existing and existing["id"] != user_id:
        raise HTTPException(status_code=400, detail="Email already in use")
    db.update_user_email(user_id, new_email)
    flush_session_cache()
    log.info("Admin %s changed email for user %s to %s", admin["email"], user_id, new_email)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/revoke-token")
async def admin_revoke_user_token(request: Request, user_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    user = db.get_user_by_id(user_id)
    if user and user["invite_token_id"]:
        db.revoke_invite_token(user["invite_token_id"])
    log.info("Admin %s revoked token for user %s", admin["email"], user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/new-token")
async def admin_new_token_for_user(request: Request, user_id: int):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    if not _can_manage_user(admin, user_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Revoke previous invite token before issuing replacement to prevent stale-token accumulation
    if user.get("invite_token_id"):
        try:
            db.revoke_invite_token(user["invite_token_id"])
        except Exception as e:
            log.warning("Failed to revoke previous token for user %s: %s", user_id, e)
    new_token = db.create_invite_token(f"Replacement token for {user['username'] or user['email']}")
    db.claim_invite_token(new_token, user_id, user["email"])
    db.link_invite_token_to_user(user_id, new_token)
    admin_label = "Super admin" if admin.get("admin_level", 0) >= 2 else "Admin"
    log.info("%s %s generated new token %s for user %s", admin_label, admin["email"], new_token, user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/grant")
async def admin_grant_subscription(request: Request, user_id: int):
    admin = _require_super_admin(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    dashboard_keys = form.getlist("dashboard_keys")
    plan = form.get("plan", "monthly")
    if plan not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if not dashboard_keys:
        raise HTTPException(status_code=400, detail="No dashboards selected")
    duration = 30 if plan == "monthly" else 365
    for dk in dashboard_keys:
        if dk not in DASHBOARDS:
            continue
        db.upsert_subscription(
            user_id=user_id,
            dashboard_key=dk,
            plan=plan,
            duration_days=duration,
            source="admin_grant",
        )
    flush_sub_cache()
    granted = [dk for dk in dashboard_keys if dk in DASHBOARDS]
    log.info("Super admin %s granted %s (%s) to user id=%s", admin["email"], ", ".join(granted), plan, user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/bulk")
async def admin_bulk_users(request: Request):
    admin = _require_admin_user(request)
    form = await request.form()
    csrf_tok = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    pw = form.get("password", "")
    if not _verify_admin_password(request, admin, pw):
        raise HTTPException(status_code=403, detail="Incorrect password — re-authenticate to perform this action")
    action = form.get("bulk_action", "")
    if action not in ("promote", "demote", "suspend", "unsuspend"):
        raise HTTPException(status_code=400, detail="Invalid bulk action")
    # Promotion requires super-admin to prevent privilege escalation
    if action == "promote" and admin.get("admin_level", 0) < 2:
        raise HTTPException(status_code=403, detail="Only super admins can promote users")
    user_ids_raw = [uid for uid in form.getlist("user_ids") if uid]
    if not user_ids_raw:
        return RedirectResponse("/admin", status_code=302)
    user_ids = []
    for raw in user_ids_raw:
        try:
            user_ids.append(int(raw))
        except (ValueError, TypeError):
            continue
    for uid in user_ids:
        if not _can_manage_user(admin, uid):
            continue
        if action == "promote":
            # Super admins promote to level 2, regular admins to level 1
            new_level = 2 if admin.get("admin_level", 0) >= 2 else 1
            db.set_user_role(uid, new_level)
        elif action == "demote":
            db.set_user_admin(uid, False)
        elif action == "suspend":
            db.set_user_suspended(uid, True)
        elif action == "unsuspend":
            db.set_user_suspended(uid, False)
    if action in ("suspend", "unsuspend", "promote", "demote"):
        flush_session_cache()
    log.info("Admin %s bulk %s %d users: %s", admin["email"], action, len(user_ids), user_ids)
    return RedirectResponse("/admin", status_code=302)


# ── Settings ──────────────────────────────────────────────────────────────────


_SETTINGS_BANNER_MESSAGES: dict[str, tuple[str, str]] = {
    # query value → (severity, message)
    "1":                       ("success", "<strong>Saved.</strong> Your landing preference has been updated."),
    "trading_poly":            ("success", "Polymarket credentials saved and encrypted."),
    "trading_kalshi":          ("success", "Kalshi credentials saved and encrypted."),
    "trading_poly_removed":    ("success", "Polymarket credentials removed."),
    "trading_kalshi_removed":  ("success", "Kalshi credentials removed."),
    "err_poly_missing_key":    ("error",   "Polymarket private key is required."),
    "err_kalshi_missing":      ("error",   "Kalshi API key, or email + password, is required."),
}


def _settings_banner_html(saved: Optional[str]) -> str:
    if not saved:
        return ""
    entry = _SETTINGS_BANNER_MESSAGES.get(saved)
    if not entry:
        return ""
    severity, msg = entry
    cls = "notice-success" if severity == "success" else "notice-error"
    return f'<div class="notice {cls}">{msg}</div>'


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

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

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    csrf_token = _get_csrf_token(request)
    return render_page(
        "settings", request=request,
        email=user["email"], username=user.get("username", user["email"]),
        raw_options="".join(option_html),
        raw_saved_banner=_settings_banner_html(saved),
        raw_admin_link=admin_link,
        raw_dashboard_tabs=_build_tab_html(user["user_id"], request=request),
        **_trading_credentials_context(user["user_id"], csrf_token, scope="settings"),
    )


@app.post("/settings")
async def settings_save(request: Request, default_dashboard: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    # CSRF check
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    # Blank → clear preference. Otherwise must be a real dashboard key the
    # user has access to (admin bypasses the subscription check).
    key: Optional[str] = default_dashboard.strip() or None
    if key is not None:
        if key not in DASHBOARDS:
            return RedirectResponse("/settings", status_code=302)
        if not user.get("is_admin") and not cached_has_subscription(user["user_id"], key):
            return RedirectResponse("/settings", status_code=302)

    db.set_default_dashboard(user["user_id"], key)
    return RedirectResponse("/settings?saved=1", status_code=302)


# ── Settings: trading credential management ─────────────────────────────────
# Mirrors /profile/trading/* but redirects back to /settings#trading. Both
# endpoints write to the same encrypted store, so saving in one place reflects
# everywhere immediately.


@app.post("/settings/trading/{platform}")
async def settings_save_trading_creds(
    request: Request, platform: str,
    private_key: str = Form(""), api_key: str = Form(""),
    api_secret: str = Form(""), api_passphrase: str = Form(""),
    email: str = Form(""), password: str = Form(""),
):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/settings/trading/{platform}")
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    if platform not in ("polymarket", "kalshi", "alpaca"):
        return RedirectResponse("/settings#trading", status_code=302)

    creds, err = _parse_trading_creds(platform, private_key, api_key, api_secret, api_passphrase, email, password)
    if err is not None:
        err_markers = {"polymarket": "err_poly_missing_key", "kalshi": "err_kalshi_missing", "alpaca": "err_alpaca_missing"}
        marker = err_markers.get(platform, "err_missing")
        return RedirectResponse(f"/settings?saved={marker}#trading", status_code=302)

    db.save_trading_credentials(user["user_id"], platform, creds)
    log.info("User %s saved %s trading credentials via settings", user.get("username", user["email"]), platform)
    saved_markers = {"polymarket": "trading_poly", "kalshi": "trading_kalshi", "alpaca": "trading_alpaca"}
    saved_marker = saved_markers.get(platform, f"trading_{platform}")
    return RedirectResponse(f"/settings?saved={saved_marker}#trading", status_code=302)


@app.post("/settings/trading/{platform}/delete")
async def settings_delete_trading_creds(request: Request, platform: str):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/settings/trading/{platform}/delete")
    form_data = await request.form()
    csrf_tok = form_data.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_tok):
        return _csrf_error()
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    if platform not in ("polymarket", "kalshi", "alpaca"):
        return RedirectResponse("/settings#trading", status_code=302)
    db.delete_trading_credentials(user["user_id"], platform)
    log.info("User %s deleted %s trading credentials via settings", user.get("username", user["email"]), platform)
    del_markers = {"polymarket": "trading_poly_removed", "kalshi": "trading_kalshi_removed", "alpaca": "trading_alpaca_removed"}
    marker = del_markers.get(platform, f"trading_{platform}_removed")
    return RedirectResponse(f"/settings?saved={marker}#trading", status_code=302)


# ── Trading API ──────────────────────────────────────────────────────────────
# JSON endpoints under /api/trading/* — used by trade.js from any subdomain.
# Because all subdomain traffic goes through the gateway proxy, requests to
# /api/trading/* on any subdomain are caught here before the catch-all.


_RATE_MAX_TRADE = 20  # max trades per 5-minute window


def _trading_user(request: Request) -> Optional[dict]:
    """Authenticate trading API requests. Returns user dict or None."""
    return current_user(request)


@app.get("/api/trading/credentials")
async def trading_credentials_status(request: Request):
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    status = db.has_trading_credentials(user["user_id"])
    return JSONResponse({
        "polymarket": status["polymarket"],
        "kalshi": status["kalshi"],
        "alpaca": status["alpaca"],
    })


@app.post("/api/trading/credentials/{platform}")
async def trading_save_credentials(request: Request, platform: str):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    if not _validate_csrf_header(request):
        return _csrf_error_json()
    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "trading_creds_save", limit=10):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if platform not in ("polymarket", "kalshi", "alpaca"):
        return JSONResponse({"error": "Invalid platform"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    if platform == "polymarket":
        private_key = body.get("private_key", "").strip()
        api_key = body.get("api_key", "").strip()
        api_secret = body.get("api_secret", "").strip()
        api_passphrase = body.get("api_passphrase", "").strip()
        if not private_key:
            return JSONResponse({"error": "Private key is required"}, status_code=400)
        creds = {
            "private_key": private_key,
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
        }
    elif platform == "kalshi":
        email = body.get("email", "").strip()
        password = body.get("password", "").strip()
        api_key = body.get("api_key", "").strip()
        if not api_key and not (email and password):
            return JSONResponse({"error": "API key or email+password required"}, status_code=400)
        creds = {"email": email, "password": password, "api_key": api_key}
    elif platform == "alpaca":
        api_key = body.get("api_key", "").strip()
        api_secret = body.get("api_secret", "").strip()
        paper = body.get("paper", True)
        if not api_key or not api_secret:
            return JSONResponse({"error": "Alpaca API key and secret are required"}, status_code=400)
        creds = {"api_key": api_key, "api_secret": api_secret, "paper": bool(paper)}
    else:
        return JSONResponse({"error": "Invalid platform"}, status_code=400)

    db.save_trading_credentials(user["user_id"], platform, creds)
    log.info("User %s saved %s trading credentials", user.get("username", user["email"]), platform)
    return JSONResponse({"ok": True})


@app.delete("/api/trading/credentials/{platform}")
async def trading_delete_credentials(request: Request, platform: str):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    if not _validate_csrf_header(request):
        return _csrf_error_json()
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if platform not in ("polymarket", "kalshi", "alpaca"):
        return JSONResponse({"error": "Invalid platform"}, status_code=400)
    db.delete_trading_credentials(user["user_id"], platform)
    return JSONResponse({"ok": True})


@app.post("/api/trading/place")
async def trading_place_order(request: Request):
    """Place a trade on Polymarket or Kalshi."""
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    if not _validate_csrf_header(request):
        return _csrf_error_json()
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Hard-block suspended users from placing trades — they should not have any
    # write access to monetary endpoints regardless of credential state.
    if user.get("suspended"):
        return JSONResponse({"error": "Account is suspended."}, status_code=403)

    ip = _get_client_ip(request)
    if _is_rate_limited(ip, "trade", _RATE_MAX_TRADE):
        return JSONResponse({"error": "Too many trade requests. Slow down."}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform = str(body.get("platform", "")).lower()
    slug = str(body.get("slug", "")).strip()
    token_id = str(body.get("token_id", "")).strip()
    side = str(body.get("side", "")).lower()
    action = str(body.get("action", "buy")).lower()
    try:
        amount = float(body.get("amount", 0))
        price = float(body.get("price", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Amount and price must be numbers"}, status_code=400)
    # Bound free-text fields so a malicious client cannot push multi-MB strings
    # into the trading_orders table (which is later returned in /api/trading/orders).
    question = str(body.get("question", ""))[:500]
    source_dashboard = str(body.get("source_dashboard", ""))[:50] or None

    if platform not in ("polymarket", "kalshi", "alpaca"):
        return JSONResponse({"error": "Invalid platform"}, status_code=400)
    if platform == "alpaca":
        # Stock trades: side=buy/sell, slug=symbol, amount=qty, price=limit (0=market)
        if action not in ("buy", "sell"):
            return JSONResponse({"error": "Action must be 'buy' or 'sell'"}, status_code=400)
        if not slug:
            return JSONResponse({"error": "Symbol is required"}, status_code=400)
        if amount <= 0 or amount > 100000:
            return JSONResponse({"error": "Quantity must be 0.01-100,000"}, status_code=400)
        if price < 0:
            return JSONResponse({"error": "Price cannot be negative"}, status_code=400)
        side = action  # For stocks, side == action (buy/sell)
    else:
        if side not in ("yes", "no"):
            return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
        if action not in ("buy", "sell"):
            return JSONResponse({"error": "Action must be 'buy' or 'sell'"}, status_code=400)
        if amount <= 0 or amount > 10000:
            return JSONResponse({"error": "Amount must be $0.01-$10,000"}, status_code=400)
        if price <= 0 or price >= 1:
            return JSONResponse({"error": "Price must be between 0 and 1"}, status_code=400)
    # Subscription gate: trading is a paid feature — require an active sub on at
    # least one dashboard (admins are exempt). This prevents lapsed/free users
    # from continuing to trade after they ever once saved credentials.
    is_admin_user = bool(user.get("is_admin"))
    if not is_admin_user:
        user_subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
        has_any_active = any(_is_sub_active(s, False) for s in user_subs.values())
        if not has_any_active:
            return JSONResponse(
                {"error": "Trading requires an active subscription."},
                status_code=403,
            )

    creds = db.get_trading_credentials(user["user_id"], platform)
    if not creds:
        return JSONResponse({"error": f"No {platform} credentials configured. Add them in your profile."}, status_code=400)

    # Log the order
    order_id = db.create_trading_order(
        user_id=user["user_id"], platform=platform, market_slug=slug,
        market_question=question, side=side, action=action,
        amount=amount, price=price, source_dashboard=source_dashboard,
    )

    try:
        result = await trading.place_order(
            platform, creds,
            slug=slug, token_id=token_id, side=side, action=action,
            amount=amount, price=price,
        )

        db.update_trading_order(order_id,
            status=result.get("status", "error"),
            order_ext_id=result.get("order_id", ""),
            fill_price=result.get("fill_price"),
            fill_amount=result.get("shares"),
            error=result.get("error"),
        )
        log.info(
            "Trade %s: user=%s platform=%s side=%s amount=$%.2f status=%s",
            order_id, user.get("username"), platform, side, amount, result.get("status"),
        )
        return JSONResponse({"ok": True, "order_id": order_id, **result})

    except Exception as e:
        log.exception("Trade execution error for order %s: %s", order_id, e)
        db.update_trading_order(order_id, status="error", error=str(e))
        return JSONResponse({"error": "Trade failed. Check logs for details."}, status_code=500)


# _execute_polymarket_trade and _execute_kalshi_trade have been moved to
# polymarket_client.py and kalshi_client.py respectively, unified behind
# the trading.place_order() abstraction.


@app.get("/api/trading/orders")
async def trading_orders(request: Request):
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    orders = db.get_recent_orders(user["user_id"], limit=30)
    return JSONResponse({"orders": [dict(o) for o in orders]})


# ── Portfolio API ─────────────────────────────────────────────────────────────
# Cross-platform portfolio: positions, P&L, trade history, balances.
# Backed by the user_positions table + live data from trading.py.


@app.get("/api/portfolio/summary")
async def portfolio_summary(request: Request):
    """Aggregate portfolio stats across all platforms."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    summary = db.get_portfolio_summary(user["user_id"])
    by_platform = db.get_portfolio_by_platform(user["user_id"])
    return JSONResponse({"summary": summary, "by_platform": by_platform})


@app.get("/api/portfolio/open")
async def portfolio_open(request: Request):
    """Current open positions with mark-to-market data."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    platform = request.query_params.get("platform")
    positions = db.get_open_positions(user["user_id"], platform=platform)
    result = []
    for p in positions:
        d = dict(p)
        # Compute unrealized P&L from mark price
        mark = d.get("last_mark_price") or d.get("avg_entry_price", 0)
        entry = d.get("avg_entry_price", 0)
        qty = d.get("qty_open", 0)
        d["unrealized_pnl"] = round(qty * (mark - entry), 2)
        result.append(d)
    return JSONResponse({"positions": result})


@app.get("/api/portfolio/closed")
async def portfolio_closed(request: Request):
    """Closed positions with realized P&L."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    platform = request.query_params.get("platform")
    limit = min(int(request.query_params.get("limit", 50)), 200)
    positions = db.get_closed_positions(user["user_id"], platform=platform, limit=limit)
    return JSONResponse({"positions": [dict(p) for p in positions]})


@app.get("/api/portfolio/history")
async def portfolio_history(request: Request):
    """Chronological trade/order history across all platforms."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    limit = min(int(request.query_params.get("limit", 50)), 200)
    orders = db.get_recent_orders(user["user_id"], limit=limit)
    return JSONResponse({"orders": [dict(o) for o in orders]})


@app.get("/api/portfolio/by-dashboard")
async def portfolio_by_dashboard(request: Request):
    """P&L breakdown grouped by source dashboard."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    breakdown = db.get_portfolio_by_dashboard(user["user_id"])
    return JSONResponse({"by_dashboard": breakdown})


@app.get("/api/portfolio/balances")
async def portfolio_balances(request: Request):
    """Live balances from all connected platforms."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    uid = user["user_id"]
    cred_status = db.has_trading_credentials(uid)

    balances = []
    tasks = []
    platforms_to_fetch = []
    for plat, connected in cred_status.items():
        if connected:
            creds = db.get_trading_credentials(uid, plat)
            if creds:
                tasks.append(trading.get_balance(plat, creds))
                platforms_to_fetch.append(plat)

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for plat, result in zip(platforms_to_fetch, results):
            if isinstance(result, Exception):
                balances.append({"platform": plat, "available_usd": 0, "total_usd": 0, "error": str(result)})
            else:
                balances.append(result.to_dict())

    return JSONResponse({"balances": balances})


@app.post("/api/portfolio/sync")
async def portfolio_sync(request: Request):
    """Pull authoritative positions from all connected platforms and sync locally."""
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    if not _validate_csrf_header(request):
        return _csrf_error_json()
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    uid = user["user_id"]
    cred_status = db.has_trading_credentials(uid)
    synced = 0

    for plat, connected in cred_status.items():
        if not connected:
            continue
        creds = db.get_trading_credentials(uid, plat)
        if not creds:
            continue
        try:
            positions = await trading.get_positions(plat, creds)
            for pos in positions:
                db.upsert_position(
                    user_id=uid,
                    platform=pos.platform,
                    external_id=pos.external_id,
                    token_or_side=pos.token_or_side,
                    title=pos.title,
                    qty_open=pos.qty,
                    avg_entry_price=pos.avg_entry_price,
                    realized_pnl=pos.realized_pnl,
                    fees_paid=pos.fees_paid,
                    status=pos.status,
                )
                synced += 1
        except Exception as exc:
            log.warning("Portfolio sync failed for %s/%s: %s", uid, plat, exc)

    # Also rebuild from local order history
    rebuilt = db.rebuild_positions_for_user(uid)
    return JSONResponse({"ok": True, "synced_from_platforms": synced, "rebuilt_from_orders": rebuilt})


# ── Stock broker API ─────────────────────────────────────────────────────────
# Endpoints for the stock dashboard to fetch account info, positions, quotes,
# and place orders through the user's connected broker (BYO-key model).

import alpaca_client as alpaca_api


@app.get("/api/trading/stock/account")
async def stock_account(request: Request):
    """Return broker account summary (cash, buying power, portfolio value)."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected. Add Alpaca credentials in Settings."}, status_code=400)
    data = await alpaca_api.get_account(creds)
    if "error" in data:
        return JSONResponse({"error": data["error"]}, status_code=502)
    return JSONResponse({
        "broker": "alpaca",
        "account_id": data.get("id", ""),
        "status": data.get("status", ""),
        "cash": float(data.get("cash", 0)),
        "portfolio_value": float(data.get("portfolio_value", 0)),
        "buying_power": float(data.get("buying_power", 0)),
        "currency": data.get("currency", "USD"),
        "paper": creds.get("paper", True),
    })


@app.get("/api/trading/stock/positions")
async def stock_positions(request: Request):
    """Return open stock positions."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected"}, status_code=400)
    raw = await alpaca_api.get_positions(creds)
    positions = []
    for p in raw:
        positions.append({
            "symbol": p.get("symbol", ""),
            "qty": float(p.get("qty", 0)),
            "side": p.get("side", "long"),
            "avg_entry": float(p.get("avg_entry_price", 0)),
            "current_price": float(p.get("current_price", 0)),
            "market_value": float(p.get("market_value", 0)),
            "unrealized_pnl": float(p.get("unrealized_pl", 0)),
            "unrealized_pnl_pct": float(p.get("unrealized_plpc", 0)),
            "change_today": float(p.get("change_today", 0)),
        })
    return JSONResponse({"positions": positions})


@app.get("/api/trading/stock/quote")
async def stock_quote(request: Request):
    """Get a stock quote. Query param: ?symbol=AAPL"""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    symbol = request.query_params.get("symbol", "").strip().upper()
    if not symbol or len(symbol) > 10:
        return JSONResponse({"error": "Valid symbol required"}, status_code=400)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected"}, status_code=400)
    data = await alpaca_api.get_quote(creds, symbol)
    if "error" in data:
        return JSONResponse({"error": data["error"]}, status_code=502)
    return JSONResponse(data)


@app.get("/api/trading/stock/orders")
async def stock_orders(request: Request):
    """List broker orders (open or recent closed)."""
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected"}, status_code=400)
    status = request.query_params.get("status", "open")
    if status not in ("open", "closed", "all"):
        status = "open"
    raw = await alpaca_api.get_orders(creds, status=status, limit=30)
    orders = []
    for o in raw:
        orders.append({
            "order_id": o.get("id", ""),
            "symbol": o.get("symbol", ""),
            "side": o.get("side", ""),
            "type": o.get("type", ""),
            "qty": float(o.get("qty") or 0),
            "filled_qty": float(o.get("filled_qty") or 0),
            "filled_avg_price": float(o.get("filled_avg_price") or 0),
            "limit_price": float(o.get("limit_price") or 0) if o.get("limit_price") else None,
            "status": o.get("status", ""),
            "created_at": o.get("created_at", ""),
        })
    return JSONResponse({"orders": orders})


@app.delete("/api/trading/stock/order/{order_id}")
async def stock_cancel_order(request: Request, order_id: str):
    """Cancel a broker order."""
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    if not _validate_csrf_header(request):
        return _csrf_error_json()
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected"}, status_code=400)
    result = await alpaca_api.cancel_order(creds, order_id)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "Cancel failed")}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/api/trading/stock/test")
async def stock_test_connection(request: Request):
    """Test broker connection with saved credentials."""
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Missing required header"}, status_code=403)
    user = _trading_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    creds = db.get_trading_credentials(user["user_id"], "alpaca")
    if not creds:
        return JSONResponse({"error": "No broker connected"}, status_code=400)
    result = await alpaca_api.test_connection(creds)
    return JSONResponse(result)


# ── Switcher injection ────────────────────────────────────────────────────────


def _switcher_snippet(dashboard_key: str, user_id: int, username: str = "", csrf_token: str = "", request: Request = None) -> str:
    """Build the <script> tags that configure and load the dashboard switcher."""
    active_keys = cached_active_dashboard_keys(user_id)
    items = [
        {
            "key": k,
            "subdomain": DASHBOARDS[k]["subdomain"],
            "display_name": DASHBOARDS[k]["display_name"],
            "accent": DASHBOARDS[k]["accent"],
        }
        for k in active_keys
    ]
    # Use the domain the user is actually on so links stay on the right host.
    if request:
        _, effective_domain, _ = _request_base_domain(request)
    else:
        effective_domain = DOMAIN
    # Portfolio link — resolve the apex URL so it works from any subdomain.
    if request:
        scheme, base, port_suffix = _request_base_domain(request)
        local = base == "localhost" or base.endswith(".localhost") or base == "127.0.0.1"
        if local:
            gw_port = CONFIG.get("gateway_port", 7000)
            portfolio_url = f"http://localhost:{gw_port}/portfolio"
        else:
            portfolio_url = f"{scheme}://{base}{port_suffix}/portfolio"
    else:
        portfolio_url = "/portfolio"

    cfg_json = json.dumps({
        "dashboards": items,
        "current": dashboard_key,
        "domain": effective_domain,
        "username": username,
        "csrf_token": csrf_token,
        "portfolio_url": portfolio_url,
    }).replace("</", "<\\/")  # prevent </script> breakout in HTML context
    return (
        f'<script>window.__hbSwitcher={cfg_json};</script>'
        f'<script src="/_gateway_static/switcher.js"></script>'
        f'<script src="/_gateway_static/trade.js"></script>'
    )


# ── Tab HTML for static gateway pages ─────────────────────────────────────────


def _build_tab_html(user_id: int, active_tab: str = "", request: Request = None) -> str:
    """Generate <a class='gw-tab'> links for the gateway page header."""
    active_keys = cached_active_dashboard_keys(user_id)
    # Derive scheme/domain from the live request so tabs work on any host.
    if request:
        scheme, base, port_suffix = _request_base_domain(request)
        local = base == "localhost" or base.endswith(".localhost") or base == "127.0.0.1"
    else:
        local = DOMAIN == "localhost" or "localhost" in DOMAIN
        scheme = "http" if local else "https"
        base = DOMAIN
        port_suffix = ""
    tabs = []
    for k in active_keys:
        d = DASHBOARDS[k]
        cls = "gw-tab active" if k == active_tab else "gw-tab"
        if local:
            gw_port = CONFIG.get("gateway_port", 7000)
            url = f"http://{d['subdomain']}.localhost:{gw_port}/"
        else:
            url = f"{scheme}://{d['subdomain']}.{base}{port_suffix}/"
        tabs.append(
            f'<a class="{cls}" href="{url}" style="--tab-accent:{d["accent"]}">'
            f'<span class="gw-tab-dot" style="background:{d["accent"]}"></span>'
            f'{html.escape(d["display_name"])}'
            f'</a>'
        )
    return "".join(tabs)


# ── SSE script injection ───────────────────────────────────────────────────────

_SSE_SCRIPT_TAG = '<script src="/_gateway_static/sse-client.js" defer></script>'

# Match the *structural* </body> tag — one that is followed only by optional
# whitespace and then </html> (or EOF). This avoids the bug where a string
# literal like `'<div></body></div>'` inside an inline <script> would otherwise
# be picked up by a naive rfind("</body>") search and corrupt the page.
_STRUCTURAL_BODY_CLOSE_RE = re.compile(r"</body\s*>(?=\s*(?:</html\s*>)?\s*\Z)", re.IGNORECASE)


def _find_structural_body_close(text: str) -> int:
    """Return the index of the document's real closing </body>, or -1.

    Falls back to the last raw rfind only when no structural match is found,
    which keeps behaviour backwards compatible with pages missing </html>.
    """
    m = _STRUCTURAL_BODY_CLOSE_RE.search(text)
    if m:
        return m.start()
    return text.lower().rfind("</body>")


def _inject_sse_client(content: bytes) -> bytes:
    """Inject the SSE client script before </body> in HTML responses."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    idx = _find_structural_body_close(text)
    if idx != -1:
        text = text[:idx] + _SSE_SCRIPT_TAG + text[idx:]
    else:
        text += _SSE_SCRIPT_TAG
    return text.encode("utf-8")


def _inject_switcher(content: bytes, content_type: str, key: str, user_id: int, username: str = "", csrf_token: str = "", request: Request = None) -> bytes:
    """Inject theme CSS (before </head>) and switcher+trade JS (before </body>)."""
    if "text/html" not in (content_type or ""):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    # 1. Inject shared theme CSS before </head>
    css_tag = '<link rel="stylesheet" href="/_gateway_static/narve-theme.css">'
    lower = text.lower()
    head_idx = lower.rfind("</head>")
    if head_idx != -1:
        text = text[:head_idx] + css_tag + text[head_idx:]
    else:
        head_open = lower.find("<head")
        if head_open != -1:
            close = lower.find(">", head_open)
            if close != -1:
                text = text[:close + 1] + css_tag + text[close + 1:]

    # 2. Inject JS before the *structural* </body> (not any </body> substring
    # appearing inside inline <script> string literals).
    snippet = _switcher_snippet(key, user_id, username, csrf_token=csrf_token, request=request)
    idx = _find_structural_body_close(text)
    if idx != -1:
        text = text[:idx] + snippet + text[idx:]
    else:
        text += snippet
    return text.encode("utf-8")


# ── Reverse proxy for dashboard subdomains ────────────────────────────────────


async def proxy_request(request: Request, forced_path: Optional[str] = None) -> Response:
    """Reverse-proxy the current request to the backend matching its subdomain."""
    sub = get_subdomain(request)
    key = SUBDOMAIN_TO_KEY.get(sub)

    # Build an apex URL that matches the domain the user is actually on.
    scheme, base, port_suffix = _request_base_domain(request)
    apex = f"{scheme}://{base}{port_suffix}"

    if not key:
        # Unknown subdomain — redirect to apex.
        return RedirectResponse(f"{apex}/", status_code=302)

    dash_cfg = DASHBOARDS[key]

    # Check for superuser key (investor mode)
    superuser_key = _get_superuser_key_from_request(request)
    has_superuser_access = False
    if superuser_key and db.has_superuser_key_access(superuser_key, key):
        has_superuser_access = True

    # 1. Require login (unless superuser key is valid).
    user = current_user(request)
    if not user and not has_superuser_access:
        return RedirectResponse(f"{apex}/gate", status_code=302)

    # 2. Require active subscription (or valid superuser key).
    if user and not has_superuser_access and not cached_has_subscription(user["user_id"], key):
        return RedirectResponse(
            f"{apex}/billing?dashboard={key}",
            status_code=302,
        )

    # 3. Fail fast if backend is known to be down (circuit breaker).
    if not is_upstream_healthy(key):
        return HTMLResponse(
            f"<h1>{html.escape(dash_cfg['display_name'])} is temporarily unavailable</h1>"
            f"<p>The backend is being checked every {_HEALTH_CHECK_INTERVAL}s and will recover automatically.</p>"
            f'<p><a href="javascript:location.reload()">Retry</a></p>',
            status_code=503,
        )

    # 4. Forward the request.
    target_port = dash_cfg["target"]
    path = forced_path if forced_path is not None else request.url.path
    query = request.url.query
    upstream_url = f"http://127.0.0.1:{target_port}{path}"
    if query:
        upstream_url += f"?{query}"

    # Cache-first: serve GET /api/* and /data/* from Redis when available.
    # Never cache /api/auth/* — responses are per-user.
    cache_path = f"{path}?{query}" if query else path
    if request.method == "GET" and (path.startswith("/api") or path.startswith("/data")) and not path.startswith("/api/auth"):
        cached = cache.get_api(key, cache_path)
        if cached:
            cached_body, cached_ct = cached
            return Response(
                content=cached_body,
                status_code=200,
                headers={
                    "content-type": cached_ct,
                    "x-cache": "HIT",
                    "cache-control": "no-store",
                },
            )

    # Strip hop-by-hop headers; also strip any client-supplied X-Gateway-*
    # headers so a malicious client can't forge upstream identity.
    hop_by_hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host",
        "content-encoding", "content-length",
    }
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in hop_by_hop and not k.lower().startswith("x-gateway-")
    }

    # Set user identity headers (if user is logged in or using superuser key)
    if user:
        fwd_headers["X-Gateway-User-Id"] = str(user["user_id"])
        fwd_headers["X-Gateway-User-Email"] = user["email"]
    elif has_superuser_access:
        # For superuser/investor access, use a special identifier
        fwd_headers["X-Gateway-User-Id"] = "superuser"
        fwd_headers["X-Gateway-User-Email"] = "investor@dashboard"

    # Mark investor/superuser access and include aspects
    if has_superuser_access:
        fwd_headers["X-Gateway-Investor-Mode"] = "true"
        key_info = db.validate_superuser_key(superuser_key)
        if key_info and key_info.get("aspects"):
            fwd_headers["X-Gateway-Key-Aspects"] = ",".join(key_info["aspects"])

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

    # Inject dashboard switcher into HTML responses.
    body = _inject_switcher(
        upstream.content,
        upstream.headers.get("content-type", ""),
        key,
        user["user_id"],
        username=user.get("username", ""),
        csrf_token=_get_csrf_token(request),
        request=request,
    )
    # Update Content-Length since injection may have changed the body size.
    if body is not upstream.content:
        resp_headers.pop("content-length", None)
        resp_headers["content-length"] = str(len(body))

    # Prevent browsers from caching proxied API responses so dashboards
    # always show fresh data instead of stale upstream cache headers.
    content_type = upstream.headers.get("content-type", "")
    if "application/json" in content_type or path.startswith("/api") or path.startswith("/data"):
        resp_headers["cache-control"] = "no-store, no-cache, must-revalidate"
        resp_headers["pragma"] = "no-cache"
        resp_headers["x-cache"] = "MISS"
        # Write-through: cache this response for next time.
        # Never cache /api/auth/* — responses are per-user.
        if request.method == "GET" and upstream.status_code == 200 and not path.startswith("/api/auth"):
            cache.set_api(key, cache_path, upstream.content, content_type)

    # Inject SSE client script into HTML responses for live updates.
    if "text/html" in (content_type or ""):
        body = _inject_sse_client(body)
        resp_headers.pop("content-length", None)
        resp_headers["content-length"] = str(len(body))

    return Response(
        content=body,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


# ── SSE stream endpoint ────────────────────────────────────────────────────────

from starlette.responses import StreamingResponse


@app.get("/api/stream")
async def sse_stream(request: Request):
    """Server-Sent Events stream for real-time dashboard updates."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    raw = request.query_params.get("dashboards", "")
    dashboards = [d.strip() for d in raw.split(",") if d.strip()]
    if not dashboards:
        return JSONResponse({"error": "No dashboards specified"}, status_code=400)

    allowed = [
        SUBDOMAIN_TO_KEY.get(d, d) for d in dashboards
        if cached_has_subscription(user["user_id"], SUBDOMAIN_TO_KEY.get(d, d))
        or user.get("is_admin")
    ]
    if not allowed:
        return JSONResponse({"error": "No subscriptions for requested dashboards"}, status_code=403)

    return StreamingResponse(
        event_stream(allowed),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/cache/stats")
async def cache_stats_endpoint(request: Request):
    """Admin-only cache and poller stats."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse({
        "cache": cache.stats(),
        "poller": _poller.stats() if _poller else {"running": False},
        "sse_connections": active_connection_count(),
    })


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

_WS_MAX_MESSAGE_SIZE = 1 * 1024 * 1024  # 1 MB
_WS_CONNECT_TIMEOUT = 5.0  # seconds


@app.websocket("/{full_path:path}")
async def websocket_proxy(ws: WebSocket, full_path: str):
    host = ws.headers.get("host", "").split(":")[0].lower()
    sub = ""
    if host == DOMAIN:
        sub = ""
    elif host.endswith("." + DOMAIN):
        sub = host[: -(len(DOMAIN) + 1)]
    elif host.endswith(".localhost"):
        sub = host[: -len(".localhost")]

    key = SUBDOMAIN_TO_KEY.get(sub)
    if not key:
        await ws.close(code=1008, reason="Unknown subdomain")
        return

    # Auth check via cookie — use session cache instead of raw DB call.
    token = ws.cookies.get(COOKIE_NAME)
    session = _get_cached_session(token) if token else None
    user_id = session["user_id"] if session else None
    if not user_id and not IS_PRODUCTION:
        ws_host = ws.headers.get("host", "").split(":")[0].lower()
        if ws_host in ("localhost", "127.0.0.1") or ws_host.endswith(".localhost"):
            user_id = ensure_dev_user()
    if not user_id:
        await ws.close(code=1008, reason="Not authenticated")
        return
    if not cached_has_subscription(user_id, key):
        await ws.close(code=1008, reason="No active subscription")
        return

    dash_cfg = DASHBOARDS[key]
    if not dash_cfg.get("supports_websocket"):
        await ws.close(code=1008, reason="Dashboard does not support WebSocket")
        return

    # Circuit breaker: fail fast if upstream is down.
    if not is_upstream_healthy(key):
        await ws.close(code=1011, reason="Backend temporarily unavailable")
        return

    target_port = dash_cfg["target"]
    query = ws.url.query
    upstream_url = f"ws://127.0.0.1:{target_port}/{full_path}"
    if query:
        upstream_url += f"?{query}"

    await ws.accept()

    try:
        async with websockets.connect(
            upstream_url,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=10,
            max_size=_WS_MAX_MESSAGE_SIZE,
            open_timeout=_WS_CONNECT_TIMEOUT,
        ) as upstream_ws:
            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive()
                        if data["type"] == "websocket.disconnect":
                            break
                        msg = data.get("text") or data.get("bytes")
                        if msg is None:
                            continue
                        if len(msg) > _WS_MAX_MESSAGE_SIZE:
                            log.warning("WS message too large (%d bytes), dropping", len(msg))
                            continue
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

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())
            done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except asyncio.TimeoutError:
        log.warning("WebSocket upstream connect timeout for %s", upstream_url)
    except Exception as e:
        log.warning("WebSocket proxy error for %s: %s", upstream_url, e)
    finally:
        try:
            if ws.client_state.name != "DISCONNECTED":
                await ws.close()
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # Single worker: the in-memory rate limiter and CSRF token store are
    # not shared across processes, so multiple workers would allow trivial
    # bypasses. For a small application fronted by Cloudflare this is fine;
    # if horizontal scaling is ever needed, move rate limiting and CSRF
    # storage to Redis first.
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=GATEWAY_PORT,
        log_level="info",
        workers=1,
    )
