import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

from database import Database
from aggregators import (
    PolymarketAggregator,
    KalshiAggregator,
    PredictItAggregator,
    PollingAggregator,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("midterm-dashboard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8051"))
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)
SESSION_COOKIE = "midterm_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
DATA_REFRESH_INTERVAL = 300  # 5 minutes
DIVERGENCE_INTERVAL = 300  # 5 minutes
SESSION_CLEANUP_INTERVAL = 3600  # 1 hour

# Rate-limit thresholds (requests per minute)
RATE_LIMITS = {"free": 60, "premium": 120, "admin": 0}  # 0 = unlimited

# Tier hierarchy for access checks
TIER_RANK = {"free": 0, "premium": 1, "admin": 2}

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    email: EmailStr
    password: str
    display_name: Optional[str] = None


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class AlertBody(BaseModel):
    race_key: str
    threshold: Optional[float] = None
    direction: Optional[str] = "any"  # "up", "down", "any"


class TierUpdateBody(BaseModel):
    tier: str


class SettingsBody(BaseModel):
    theme: Optional[str] = None  # "light" | "dark" | "system"
    accentColor: Optional[str] = None  # hex color e.g. "#3b82f6"
    contrast: Optional[str] = None  # "normal" | "high"
    density: Optional[str] = None  # "comfortable" | "compact"
    chartStyle: Optional[str] = None  # "line" | "area"
    showPolling: Optional[bool] = None
    showSources: Optional[list[str]] = None  # ["polymarket", "kalshi", "predictit", "polling"]
    defaultDays: Optional[int] = None


# ---------------------------------------------------------------------------
# Shared application state (populated during lifespan)
# ---------------------------------------------------------------------------
class AppState:
    db: Database
    http_session: aiohttp.ClientSession
    polymarket: PolymarketAggregator
    kalshi: KalshiAggregator
    predictit: PredictItAggregator
    polling: PollingAggregator
    background_tasks: list[asyncio.Task] = []
    # In-memory rate-limit store: {ip: [timestamps]}
    rate_limit_store: dict[str, list[float]] = defaultdict(list)


state = AppState()

# ---------------------------------------------------------------------------
# Password hashing (bcrypt-style via hashlib for zero extra deps)
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return h.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = _hash_password(password, salt)
    return secrets.compare_digest(h, stored_hash)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

async def require_auth(request: Request) -> dict:
    """Return the current user dict or raise 401.

    When this dashboard runs behind the Habbig gateway, the upstream sets
    ``X-Gateway-User-Id`` and ``X-Gateway-User-Email`` headers after verifying
    the user's session + subscription. Trust is proved by a shared-secret
    header (``X-Gateway-Secret``) because uvicorn's default proxy_headers
    rewrites request.client.host from X-Forwarded-For, so peer-IP checks are
    unreliable. If a matching local user row doesn't exist, we synthesize a
    user dict on the fly so the rest of the handler works.
    """
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret and request.headers.get("x-gateway-secret") == _sso_secret:
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        if gw_id and gw_email:
            try:
                gw_user_id = int(gw_id)
            except ValueError:
                gw_user_id = None
            if gw_user_id is not None:
                existing = await state.db.get_user(gw_user_id)
                if existing:
                    return existing
                # No local row for this gateway user — return a synthetic dict
                # that downstream handlers can read without crashing.
                return {
                    "id": gw_user_id,
                    "email": gw_email,
                    "tier": "pro",
                    "display_name": gw_email.split("@")[0],
                }

    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = await state.db.validate_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    user = await state.db.get_user(session["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def require_tier(request: Request, tier: str) -> dict:
    """Return the current user if their tier >= *tier*, else raise 403."""
    user = await require_auth(request)
    user_rank = TIER_RANK.get(user.get("tier", "free"), 0)
    required_rank = TIER_RANK.get(tier, 99)
    if user_rank < required_rank:
        raise HTTPException(status_code=403, detail=f"Requires {tier} tier or above")
    return user


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def _check_rate_limit(ip: str, tier: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    limit = RATE_LIMITS.get(tier, 60)
    if limit == 0:
        return True  # unlimited

    now = time.time()
    window = 60.0
    timestamps = state.rate_limit_store[ip]

    # Prune entries older than the window
    cutoff = now - window
    state.rate_limit_store[ip] = [t for t in timestamps if t > cutoff]
    timestamps = state.rate_limit_store[ip]

    if len(timestamps) >= limit:
        return False

    state.rate_limit_store[ip].append(now)
    return True


# ---------------------------------------------------------------------------
# Audit logging helper
# ---------------------------------------------------------------------------

async def _audit_log(action: str, user_id: Optional[int], ip: str, detail: str = ""):
    try:
        await state.db.log_action(user_id=user_id, action=action, details=detail, ip=ip)
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def data_refresh_loop():
    """Fetch market data from all aggregators every 5 minutes and store in DB."""
    while True:
        try:
            logger.info("Starting data refresh cycle")
            async def with_timeout(coro, name, seconds=60):
                try:
                    return await asyncio.wait_for(coro, timeout=seconds)
                except asyncio.TimeoutError:
                    logger.warning(f"{name} fetch timed out after {seconds}s")
                    return TimeoutError(f"{name} timed out")

            # Fetch sources in parallel (except Kalshi world which reuses Kalshi's cache)
            results = await asyncio.gather(
                with_timeout(state.polymarket.fetch_election_markets(), "Polymarket", seconds=120),
                with_timeout(state.kalshi.fetch_election_markets(), "Kalshi", seconds=180),
                with_timeout(state.predictit.fetch_election_markets(), "PredictIt"),
                with_timeout(state.polling.fetch_all_polls(), "Polling", seconds=30),
                with_timeout(state.polymarket.fetch_world_election_markets(), "Polymarket-World", seconds=120),
                return_exceptions=True,
            )
            poly_data, kalshi_data, pi_data, poll_data, poly_world = results

            # Kalshi world uses cached data from the midterm fetch above
            kalshi_world = await with_timeout(
                state.kalshi.fetch_world_election_markets(), "Kalshi-World", seconds=30
            )

            # Store midterm markets
            for label, data in [("Polymarket", poly_data), ("Kalshi", kalshi_data), ("PredictIt", pi_data)]:
                if isinstance(data, list):
                    await state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                else:
                    logger.error(f"{label} fetch error: {data}")

            # Store polls
            if isinstance(poll_data, dict):
                all_polls = []
                for poll_type, polls in poll_data.items():
                    all_polls.extend(polls)
                if all_polls:
                    await state.db.store_polls_batch(all_polls)
                logger.info(f"Stored {len(all_polls)} polls")
            else:
                logger.error(f"Polling fetch error: {poll_data}")

            # Store world election markets
            for label, data in [("Polymarket world", poly_world), ("Kalshi world", kalshi_world)]:
                if isinstance(data, list):
                    await state.db.upsert_markets_batch(data)
                    logger.info(f"Stored {len(data)} {label} markets")
                else:
                    logger.error(f"{label} fetch error: {data}")

        except Exception as e:
            logger.error(f"Data refresh error: {e}", exc_info=True)

        await asyncio.sleep(DATA_REFRESH_INTERVAL)


async def divergence_calculator():
    """Compute divergence across sources for matched races every 5 minutes."""
    while True:
        try:
            logger.info("Computing divergence snapshots")
            all_markets = await state.db.get_all_markets(active_only=True)

            # Group markets by race_key (race_type + state)
            by_race: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for m in all_markets:
                race_key = f"{m.get('race_type', 'other')}_{m.get('state', 'US')}"
                source = m.get("source", "unknown")
                by_race[race_key][source].append(m)

            count = 0
            for race_key, sources in by_race.items():
                if len(sources) < 2:
                    continue

                source_probs: dict[str, float] = {}
                for source, markets in sources.items():
                    # Use the first market's top outcome probability
                    for market in markets:
                        outcomes = market.get("outcomes", [])
                        if outcomes and outcomes[0].get("probability") is not None:
                            source_probs[source] = outcomes[0]["probability"]
                            break

                if len(source_probs) < 2:
                    continue

                values = list(source_probs.values())
                max_div = max(values) - min(values)

                parts = race_key.split("_", 1)
                race_type = parts[0] if parts else "other"
                state_abbr = parts[1] if len(parts) > 1 else None

                await state.db.record_divergence(
                    race_key=race_key,
                    state=state_abbr,
                    race_type=race_type,
                    data={
                        "polymarket": source_probs.get("polymarket"),
                        "kalshi": source_probs.get("kalshi"),
                        "predictit": source_probs.get("predictit"),
                        "polling": source_probs.get("polling"),
                        "max_divergence": round(max_div, 4),
                        "details": source_probs,
                    }
                )
                count += 1

            logger.info(f"Divergence calculated for {count} races")
        except Exception as e:
            logger.error(f"Divergence calculator error: {e}", exc_info=True)

        await asyncio.sleep(DIVERGENCE_INTERVAL)


async def session_cleanup():
    """Remove expired sessions every hour."""
    while True:
        try:
            await state.db.cleanup_expired_sessions()
            logger.info("Session cleanup completed")
        except Exception as e:
            logger.error(f"Session cleanup error: {e}", exc_info=True)
        await asyncio.sleep(SESSION_CLEANUP_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing application")
    state.db = Database()
    await state.db.connect()

    state.http_session = aiohttp.ClientSession()

    state.polymarket = PolymarketAggregator(session=state.http_session)
    state.kalshi = KalshiAggregator(session=state.http_session)
    state.predictit = PredictItAggregator(session=state.http_session)
    state.polling = PollingAggregator(session=state.http_session)

    # Start background tasks
    state.background_tasks = [
        asyncio.create_task(data_refresh_loop(), name="data_refresh"),
        asyncio.create_task(divergence_calculator(), name="divergence"),
        asyncio.create_task(session_cleanup(), name="session_cleanup"),
    ]

    logger.info("Background tasks started")
    yield

    # Shutdown
    logger.info("Shutting down")
    for task in state.background_tasks:
        task.cancel()
    await asyncio.gather(*state.background_tasks, return_exceptions=True)

    await state.http_session.close()
    await state.db.close()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Midterm Elections Dashboard",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Determine tier from session (best-effort, default to free)
    tier = "free"
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        try:
            session = await state.db.validate_session(session_id)
            if session:
                tier = session.get("tier", "free")
        except Exception:
            pass

    ip = request.client.host if request.client else "unknown"

    if not _check_rate_limit(ip, tier):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please slow down."},
        )

    return await call_next(request)


@app.middleware("http")
async def audit_sensitive_actions(request: Request, call_next):
    """Log sensitive actions (auth, admin, premium) to the audit log."""
    response = await call_next(request)

    path = request.url.path
    if path.startswith(("/auth/", "/admin/", "/premium/")):
        ip = request.client.host if request.client else "unknown"
        session_id = request.cookies.get(SESSION_COOKIE)
        user_id = None
        if session_id:
            try:
                session = await state.db.validate_session(session_id)
                if session:
                    user_id = session.get("user_id")
            except Exception:
                pass
        await _audit_log(
            action=f"{request.method} {path}",
            user_id=user_id,
            ip=ip,
            detail=f"status={response.status_code}",
        )

    return response


# ===================================================================
# AUTH ENDPOINTS
# ===================================================================

@app.post("/auth/register")
async def auth_register(body: RegisterBody, request: Request, response: Response):
    existing = await state.db.get_user_by_email(body.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = await state.db.create_user(body.email, body.password, body.display_name)
    if not user_id:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Auto-login after registration
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")
    session_token = await state.db.create_session(user_id, ip=ip, user_agent=ua)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        domain=COOKIE_DOMAIN,
    )

    await _audit_log("register", user_id, ip, f"email={body.email}")

    return {"ok": True, "user": {"id": user_id, "email": body.email, "display_name": body.display_name, "tier": "free"}}


@app.post("/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response):
    user = await state.db.verify_login(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")
    session_id = await state.db.create_session(user["id"], ip=ip, user_agent=ua)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        domain=COOKIE_DOMAIN,
    )

    ip = request.client.host if request.client else "unknown"
    await _audit_log("login", user["id"], ip, f"email={body.email}")

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user.get("display_name", ""),
            "tier": user.get("tier", "free"),
        },
    }


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        await state.db.delete_session(session_id)
    response.delete_cookie(SESSION_COOKIE, domain=COOKIE_DOMAIN)
    return {"ok": True}


@app.get("/auth/me")
async def auth_me(request: Request):
    user = await require_auth(request)
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name", ""),
        "tier": user.get("tier", "free"),
        "created_at": user.get("created_at"),
        "settings": json.loads(user.get("settings") or "{}"),
    }


@app.get("/auth/settings")
async def auth_get_settings(request: Request):
    user = await require_auth(request)
    return {"settings": json.loads(user.get("settings") or "{}")}


@app.put("/auth/settings")
async def auth_update_settings(body: SettingsBody, request: Request):
    user = await require_auth(request)
    # Only include keys that were explicitly provided (non-None)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = await state.db.update_user_settings(user["id"], updates)
    return {"ok": True, "settings": merged}


# ===================================================================
# PUBLIC DATA ENDPOINTS
# ===================================================================

@app.get("/data/overview")
async def data_overview():
    """Senate/House control probabilities from all sources."""
    all_markets = await state.db.get_all_markets(active_only=True)

    # Build control overview from "control" type markets
    senate_sources = {}
    house_sources = {}
    source_summary = defaultdict(lambda: {"market_count": 0, "status": "ok"})

    for m in all_markets:
        source = m.get("source", "unknown")
        source_summary[source]["market_count"] += 1

        if m.get("race_type") != "control":
            continue
        title = (m.get("title") or "").lower()
        outcomes = m.get("outcomes", [])

        target = None
        if "senate" in title:
            target = senate_sources
        elif "house" in title:
            target = house_sources

        if target is not None and outcomes:
            dem_prob = 0
            rep_prob = 0
            for o in outcomes:
                name = (o.get("name") or "").lower()
                prob = o.get("probability") or 0
                if "democrat" in name or "dem" in name or "yes" in name:
                    dem_prob = prob
                elif "republican" in name or "rep" in name or "no" in name:
                    rep_prob = prob
            if not rep_prob and dem_prob:
                rep_prob = 1 - dem_prob
            target[source] = {"democrat": dem_prob, "republican": rep_prob}

    return {
        "senate_control": {"sources": senate_sources},
        "house_control": {"sources": house_sources},
        "source_summary": dict(source_summary),
    }


@app.get("/data/races")
async def data_races(
    race_type: Optional[str] = None,
    state_abbr: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_volume: Optional[float] = None,
):
    """List all tracked races with latest odds."""
    markets = await state.db.get_markets(
        race_type=race_type, state=state_abbr, source=source,
        search=search, min_volume=min_volume,
    )
    # Exclude world markets from races list unless explicitly requested
    if race_type != "world":
        markets = [m for m in markets if m.get("race_type") != "world"]
    for m in markets:
        # Use race_type + state as the grouping key so all sources share the same key
        m["race_key"] = f"{m.get('race_type', 'other')}_{m.get('state') or 'US'}"
        # Also keep a unique key for direct lookup
        m["market_id"] = f"{m.get('source', 'unknown')}_{m.get('source_id', '')}"
    return {"races": markets}


@app.get("/data/race/{race_key}")
async def data_race_detail(race_key: str):
    """Single race detail with all source data.

    race_key can be:
    - "race_type_STATE" e.g. "senate_GA" — matches all sources for that race
    - "source_sourceId" e.g. "predictit_8156" — direct market lookup, then find siblings
    - legacy "race_type_STATE_sourceId" format
    """
    all_markets = await state.db.get_all_markets(active_only=True)

    # Step 1: find the target market(s)
    matched = {}
    target_race_type = None
    target_state = None

    for m in all_markets:
        rt = m.get("race_type", "other")
        st = m.get("state") or "US"
        sid = m.get("source_id", "")
        source = m.get("source", "unknown")
        group_key = f"{rt}_{st}"

        if (group_key == race_key
            or f"{source}_{sid}" == race_key
            or f"{rt}_{st}_{sid}" == race_key
            or sid == race_key):
            matched[source] = m
            target_race_type = rt
            target_state = st

    # Step 2: if we found a target, also grab all other sources with same race_type + state
    if target_race_type and target_state:
        for m in all_markets:
            rt = m.get("race_type", "other")
            st = m.get("state") or "US"
            source = m.get("source", "unknown")
            if rt == target_race_type and st == target_state and source not in matched:
                matched[source] = m

    if not matched:
        raise HTTPException(status_code=404, detail="Race not found")

    first = list(matched.values())[0]
    return {
        "race_key": f"{target_race_type}_{target_state}",
        "title": first.get("title"),
        "event_title": first.get("event_title"),
        "race_type": first.get("race_type"),
        "state": first.get("state"),
        "by_source": {
            s: {
                "outcomes": m.get("outcomes", []),
                "title": m.get("title"),
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
            }
            for s, m in matched.items()
        },
    }


@app.get("/data/history/{race_key}")
async def data_history(race_key: str, days: int = 30):
    """Historical price/divergence data for a race."""
    history = await state.db.get_divergence_history(race_key=race_key, days=days)
    # Reformat for chart consumption
    chart_data = []
    for h in history:
        point = {"date": h.get("snapshot_time", "")[:10]}
        if h.get("polymarket_prob") is not None:
            point["polymarket"] = h["polymarket_prob"]
        if h.get("kalshi_prob") is not None:
            point["kalshi"] = h["kalshi_prob"]
        if h.get("predictit_prob") is not None:
            point["predictit"] = h["predictit_prob"]
        if h.get("polling_avg") is not None:
            point["polling"] = h["polling_avg"]
        chart_data.append(point)
    return {"race_key": race_key, "days": days, "history": chart_data}


@app.get("/data/divergence")
async def data_divergence():
    """Current divergence data across all sources."""
    divergences = await state.db.get_divergence_history(days=1)
    # Deduplicate by race_key, keeping latest
    latest = {}
    for d in divergences:
        rk = d.get("race_key")
        if rk and (rk not in latest or d.get("snapshot_time", "") > latest[rk].get("snapshot_time", "")):
            latest[rk] = d
    return {"divergences": list(latest.values())}


@app.get("/data/divergence/history/{race_key}")
async def data_divergence_history(race_key: str, days: int = 30):
    """Divergence over time for a specific race."""
    history = await state.db.get_divergence_history(race_key=race_key, days=days)
    return {"race_key": race_key, "days": days, "history": history}


@app.get("/data/sources")
async def data_sources():
    """Data sources and their status."""
    all_markets = await state.db.get_all_markets(active_only=True)
    sources = defaultdict(lambda: {"market_count": 0, "status": "ok", "last_updated": None})
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu
    return {"sources": dict(sources)}


@app.get("/data/polling/recent")
async def data_polling_recent(limit: int = 50):
    """Most recent polls across all races."""
    async with state.db._db.execute(
        "SELECT * FROM polling_data ORDER BY end_date DESC, id DESC LIMIT ?",
        (min(limit, 200),)
    ) as cursor:
        polls = [dict(r) for r in await cursor.fetchall()]
    return {"polls": [
        {
            "pollster": p.get("pollster"),
            "candidate": p.get("candidate"),
            "party": p.get("party"),
            "percentage": p.get("percentage"),
            "sample_size": p.get("sample_size"),
            "state": p.get("state"),
            "poll_type": p.get("poll_type"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
        }
        for p in polls
    ]}


@app.get("/data/polling/{race_key}")
async def data_polling(race_key: str):
    """Raw polling data for a specific race.

    The race_key format is ``{poll_type}_{state}`` (e.g. ``senate_PA``).
    If the key contains only one segment it is treated as a national-level
    poll_type with no state filter.
    """
    parts = race_key.split("_", 1)
    poll_type = parts[0] if parts else race_key
    state_abbr = parts[1] if len(parts) > 1 else None

    polls = await state.db.get_polls(state=state_abbr, poll_type=poll_type)

    results = []
    for p in polls:
        results.append({
            "pollster": p.get("pollster"),
            "candidate": p.get("candidate"),
            "party": p.get("party"),
            "percentage": p.get("percentage"),
            "sample_size": p.get("sample_size"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
        })

    return {"race_key": race_key, "poll_type": poll_type, "state": state_abbr, "polls": results}


@app.get("/data/historical")
async def data_historical(
    year: Optional[int] = None,
    race_type: Optional[str] = None,
    state: Optional[str] = None,
):
    """Historical US election results (presidential, senate, governor).

    Returns past winners with vote totals, percentages, and margins so users
    can compare current prediction markets against prior outcomes.
    Filter by year, race_type (president/senate/governor), and/or state.
    """
    from historical_results import get_results, HISTORICAL_RESULTS
    results = get_results(year=year, race_type=race_type, state=state)
    # Available filter options
    all_years = sorted({r["year"] for r in HISTORICAL_RESULTS}, reverse=True)
    all_types = sorted({r["race_type"] for r in HISTORICAL_RESULTS})
    all_states = sorted({r["state"] for r in HISTORICAL_RESULTS})
    return {
        "results": results,
        "filters": {
            "years": all_years,
            "race_types": all_types,
            "states": all_states,
        },
    }


@app.get("/data/world-elections")
async def data_world_elections(
    country: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_volume: Optional[float] = None,
):
    """World leader election markets from prediction platforms."""
    markets = await state.db.get_markets(
        race_type="world", state=country, source=source,
        search=search, min_volume=min_volume,
    )
    for m in markets:
        m["race_key"] = f"world_{m.get('state') or 'INTL'}"
        m["market_id"] = f"{m.get('source', 'unknown')}_{m.get('source_id', '')}"
    return {"markets": markets}


# ===================================================================
# PREMIUM ENDPOINTS
# ===================================================================

@app.get("/premium/alerts")
async def premium_alerts(request: Request):
    user = await require_tier(request, "premium")
    # Alert settings are stored in alert_settings table
    async with state.db._db.execute(
        "SELECT * FROM alert_settings WHERE user_id = ? AND enabled = 1", (user["id"],)
    ) as cursor:
        alerts = [dict(r) for r in await cursor.fetchall()]
    return {"alerts": alerts}


@app.post("/premium/alerts")
async def premium_create_alert(body: AlertBody, request: Request):
    user = await require_tier(request, "premium")
    await state.db._db.execute(
        """INSERT OR REPLACE INTO alert_settings (user_id, race_key, threshold, enabled)
           VALUES (?, ?, ?, 1)""",
        (user["id"], body.race_key, body.threshold or 5.0)
    )
    await state.db._db.commit()
    return {"ok": True, "race_key": body.race_key}


@app.get("/premium/watchlist")
async def premium_watchlist(request: Request):
    user = await require_tier(request, "premium")
    async with state.db._db.execute(
        "SELECT race_key, created_at FROM user_watchlists WHERE user_id = ?", (user["id"],)
    ) as cursor:
        watchlist = [dict(r) for r in await cursor.fetchall()]
    return {"watchlist": watchlist}


@app.post("/premium/watchlist/{race_key}")
async def premium_watchlist_add(race_key: str, request: Request):
    user = await require_tier(request, "premium")
    await state.db._db.execute(
        "INSERT OR IGNORE INTO user_watchlists (user_id, race_key) VALUES (?, ?)",
        (user["id"], race_key)
    )
    await state.db._db.commit()
    return {"ok": True, "race_key": race_key}


@app.delete("/premium/watchlist/{race_key}")
async def premium_watchlist_remove(race_key: str, request: Request):
    user = await require_tier(request, "premium")
    await state.db._db.execute(
        "DELETE FROM user_watchlists WHERE user_id = ? AND race_key = ?",
        (user["id"], race_key)
    )
    await state.db._db.commit()
    return {"ok": True, "race_key": race_key}


@app.get("/premium/detailed-comparison/{race_key}")
async def premium_detailed_comparison(race_key: str, request: Request):
    """Deep comparison with orderbook data."""
    user = await require_tier(request, "premium")
    all_markets = await state.db.get_all_markets(active_only=True)
    matched = {}
    for m in all_markets:
        partial = f"{m.get('race_type', 'other')}_{m.get('state', 'US')}"
        if partial == race_key or m.get("source_id") == race_key:
            matched[m.get("source", "unknown")] = m

    if not matched:
        raise HTTPException(status_code=404, detail="Race not found")

    orderbooks = {}
    for source, market in matched.items():
        outcomes = market.get("outcomes", [])
        if source == "polymarket" and outcomes:
            token_id = outcomes[0].get("token_id")
            if token_id:
                try:
                    orderbooks["polymarket"] = await state.polymarket.fetch_orderbook(token_id)
                except Exception as e:
                    logger.warning(f"Polymarket orderbook error: {e}")
        elif source == "kalshi":
            ticker = market.get("source_id")
            if ticker:
                try:
                    orderbooks["kalshi"] = await state.kalshi.fetch_orderbook(ticker)
                except Exception as e:
                    logger.warning(f"Kalshi orderbook error: {e}")

    first = list(matched.values())[0]
    return {
        "race_key": race_key,
        "race": {
            "title": first.get("title"),
            "state": first.get("state"),
            "race_type": first.get("race_type"),
            "by_source": {s: {"outcomes": m.get("outcomes", [])} for s, m in matched.items()},
        },
        "orderbooks": orderbooks,
    }


@app.get("/premium/campaign-finance/{state_abbr}")
async def premium_campaign_finance(state_abbr: str, request: Request):
    """FEC fundraising data placeholder."""
    user = await require_tier(request, "premium")
    return {"state": state_abbr, "finance": [], "note": "FEC integration coming soon"}


# ===================================================================
# ADMIN ENDPOINTS
# ===================================================================

@app.get("/admin/stats")
async def admin_stats(request: Request):
    await require_tier(request, "admin")
    stats = await state.db.get_admin_stats()
    return stats


@app.get("/admin/users")
async def admin_users(request: Request, limit: int = 100, offset: int = 0):
    await require_tier(request, "admin")
    users = await state.db.get_all_users(limit=limit, offset=offset)
    return {"users": users}


@app.get("/admin/user/{user_id}")
async def admin_user_detail(user_id: int, request: Request):
    await require_tier(request, "admin")
    user = await state.db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.pop("password_hash", None)
    user.pop("salt", None)
    return user


@app.put("/admin/user/{user_id}/tier")
async def admin_update_tier(user_id: int, body: TierUpdateBody, request: Request):
    admin_user = await require_tier(request, "admin")
    if body.tier not in TIER_RANK:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {body.tier}")
    await state.db.update_user_tier(user_id, body.tier)
    ip = request.client.host if request.client else "unknown"
    await _audit_log("tier_change", admin_user["id"], ip, f"target_user={user_id} new_tier={body.tier}")
    return {"ok": True, "user_id": user_id, "tier": body.tier}


@app.get("/admin/growth")
async def admin_growth(request: Request, days: int = 90):
    await require_tier(request, "admin")
    data = await state.db.get_user_growth(days=days)
    return {"days": days, "growth": data}


@app.get("/admin/churn")
async def admin_churn(request: Request):
    await require_tier(request, "admin")
    data = await state.db.get_churn_data()
    return data


@app.get("/admin/audit-log")
async def admin_audit_log(request: Request, limit: int = 100):
    await require_tier(request, "admin")
    entries = await state.db.get_audit_log(limit=limit)
    return {"logs": entries}


@app.get("/admin/data-status")
async def admin_data_status(request: Request):
    await require_tier(request, "admin")
    all_markets = await state.db.get_all_markets(active_only=True)
    sources = defaultdict(lambda: {"market_count": 0, "status": "ok", "last_updated": None})
    for m in all_markets:
        s = m.get("source", "unknown")
        sources[s]["market_count"] += 1
        lu = m.get("last_updated")
        if lu and (sources[s]["last_updated"] is None or lu > sources[s]["last_updated"]):
            sources[s]["last_updated"] = lu
    return {"sources": dict(sources)}


# ===================================================================
# Static file serving for React SPA (production)
# ===================================================================

_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _frontend_dist.is_dir():
    # Serve static assets (js, css, images) under /assets
    app.mount(
        "/assets",
        StaticFiles(directory=str(_frontend_dist / "assets")),
        name="static-assets",
    )

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the React SPA index.html for all non-API routes."""
        # If the request matches a real file, serve it
        file_path = _frontend_dist / full_path
        if full_path and file_path.is_file():
            return FileResponse(str(file_path))
        # Otherwise serve index.html for client-side routing
        index = _frontend_dist / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=bool(os.getenv("DEV")),
        log_level="info",
    )
