from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import html as html_mod
import json as _json
import logging
import os
import re as _re
import secrets
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select

from app.config import settings, yaml_config
from app.db import AsyncSession, engine, get_session, init_db
from app.models import (
    APIKey, CredibilitySnapshot, MarketSnapshot, MonthlyQuota, PaperTrade, Prediction, RawPost, Source, SourcePredictionRecord, User, UserPrediction, UserSession,
)
from app.security import decrypt_field as _decrypt_field, encrypt_field as _encrypt_field

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, settings.get("LOG_LEVEL", "INFO")), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ONE_YEAR_FROM_NOW = lambda: datetime.now(timezone.utc) + timedelta(days=365)
_last_run_stats: dict = {}


def _persistent_secret(env_var: str, filename: str, nbytes: int) -> str:
    """Load a long-lived secret from env, file, or generate-and-persist.

    Both the session-token signer and the CSRF token derivation must survive
    process restarts. Generating fresh secrets per boot invalidates every
    live cookie — users get kicked off and CSRF validation rejects every
    pending form. We mirror ``app.security._get_or_create_encryption_key``.
    """
    val = os.environ.get(env_var)
    if val:
        return val
    path = Path(__file__).parent.parent / filename
    if path.exists():
        return path.read_text().strip()
    val = secrets.token_hex(nbytes)
    try:
        path.write_text(val)
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not persist %s to %s — secret will reset on next process restart", env_var, filename)
    return val


_SESSION_SECRET = _persistent_secret("SESSION_SECRET", ".session_secret", 32)
_CSRF_SECRET = _persistent_secret("CSRF_SECRET", ".csrf_secret", 16)

# Rate limiting: track login attempts per IP
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _rehydrate_sessions()
    # Create default admin user if none exist
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(func.count()).select_from(User))
        if (result.first() or 0) == 0:
            admin_user = os.getenv("DASHBOARD_USER", "").strip() or settings.get("DASHBOARD_USER", "admin")
            admin_pass = os.getenv("DASHBOARD_PASSWORD", "").strip()
            if not admin_pass:
                admin_pass = settings.get("DASHBOARD_PASSWORD", "").strip() if settings.get("DASHBOARD_PASSWORD", "").strip() else ""
            if not admin_pass:
                # Generate a random password and log it ONCE so the operator can grab it
                # from startup logs. This avoids the well-known 'changeme' default which
                # was a hardcoded credential bypass for anyone who knew about it.
                admin_pass = secrets.token_urlsafe(24)
                logger.warning(
                    "DASHBOARD_PASSWORD not set -- generated a random one-time admin password "
                    "for user %r. Save it now (it will not be shown again): %s",
                    admin_user, admin_pass,
                )
            session.add(User(
                username=admin_user,
                email="",
                password_hash=_hash_password(admin_pass),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))
            await session.commit()
            logger.info("Created default admin user: %s", admin_user)
    from app.scheduler import start_scheduler
    start_scheduler()
    asyncio.create_task(_initial_run())
    yield
    from app.scheduler import shutdown_scheduler
    shutdown_scheduler()


async def _initial_run():
    global _last_run_stats
    try:
        from app.scheduler import run_pipeline
        _last_run_stats = await run_pipeline()
    except Exception as exc:
        logger.error("Initial pipeline run failed: %s", exc)


app = FastAPI(title="Polymarket Prediction Intelligence Dashboard", lifespan=lifespan)
_app_dir = Path(__file__).parent
templates = Jinja2Templates(directory=str(_app_dir / "templates"))
_static_dir = _app_dir / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2 with random salt)
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: str | None = None) -> str:
    """Hash password with PBKDF2-SHA256 + random salt. Returns 'salt$hash'."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify password against stored 'salt$hash' string."""
    if not stored or not isinstance(stored, str):
        return False
    if "$" not in stored:
        # Legacy SHA256 hash — verify and return True to allow migration
        return hashlib.sha256(password.encode()).hexdigest() == stored
    parts = stored.split("$", 1)
    if len(parts) != 2 or not parts[0]:
        return False
    salt = parts[0]
    return hmac.compare_digest(_hash_password(password, salt), stored)


def _make_session_token() -> str:
    """Generate a random session token (non-deterministic)."""
    return secrets.token_urlsafe(48)


def _make_csrf_token(session_token: str) -> str:
    """Generate CSRF token tied to the session."""
    return hashlib.sha256(f"{session_token}:{_CSRF_SECRET}".encode()).hexdigest()[:32]


def _get_csrf_token(request: Request, response=None) -> str:
    """Get or create a CSRF token for the current request/session.

    For authenticated users the token is derived from their session cookie.
    For unauthenticated visitors (login/register pages) a temporary token is
    stored in a separate cookie so that the CSRF check survives the stateless
    round-trip.

    If *response* is provided and a new seed must be generated, the seed is
    persisted as a ``_csrf_seed`` cookie on that response so it can be
    validated on the next submission.
    """
    session_token = request.cookies.get("session")
    if session_token and session_token in _active_sessions:
        return _make_csrf_token(session_token)
    # Unauthenticated: use a dedicated csrf cookie
    csrf_cookie = request.cookies.get("_csrf_seed")
    if csrf_cookie:
        return _make_csrf_token(csrf_cookie)
    # Generate a new seed and persist it so validation works on the next request
    seed = secrets.token_urlsafe(32)
    if response is not None:
        is_secure = request.url.scheme == "https"
        response.set_cookie("_csrf_seed", seed, httponly=True, samesite="strict", max_age=3600, secure=is_secure)
    return _make_csrf_token(seed)


def _set_csrf_cookie(request: Request, response) -> str:
    """Ensure a _csrf_seed cookie exists for unauthenticated pages.

    Returns the csrf_token to embed in the form.
    """
    session_token = request.cookies.get("session")
    if session_token and session_token in _active_sessions:
        return _make_csrf_token(session_token)
    csrf_cookie = request.cookies.get("_csrf_seed")
    if csrf_cookie:
        return _make_csrf_token(csrf_cookie)
    seed = secrets.token_urlsafe(32)
    is_secure = request.url.scheme == "https"
    response.set_cookie("_csrf_seed", seed, httponly=True, samesite="strict", max_age=3600, secure=is_secure)
    return _make_csrf_token(seed)


def _validate_csrf(request: Request, form_token: str) -> bool:
    """Validate a CSRF token submitted in a form against the expected value."""
    session_token = request.cookies.get("session")
    if session_token and session_token in _active_sessions:
        expected = _make_csrf_token(session_token)
        return hmac.compare_digest(expected, form_token)
    csrf_cookie = request.cookies.get("_csrf_seed")
    if csrf_cookie:
        expected = _make_csrf_token(csrf_cookie)
        return hmac.compare_digest(expected, form_token)
    return False


def _client_identity(request: Request) -> str:
    """Identify the caller for login throttling.

    When this dashboard runs behind the gateway every request appears to
    originate from the gateway's single IP, which silently merges every
    user's failed-login budget into one global counter. We instead trust
    X-Forwarded-For only when the gateway HMAC header validates, then
    fall back to the raw socket peer.
    """
    secret = os.environ.get("GATEWAY_SSO_SECRET", "")
    if secret:
        provided = request.headers.get("x-gateway-secret", "")
        if provided and hmac.compare_digest(provided, secret):
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                first = xff.split(",")[0].strip()
                if first:
                    return first
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limit exceeded."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Clean old attempts
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    # Remove empty key to prevent memory leak
    if not _login_attempts[ip]:
        del _login_attempts[ip]
        return False
    return len(_login_attempts[ip]) >= _MAX_LOGIN_ATTEMPTS


# Active sessions: token -> (username, created_timestamp)
# Persisted to UserSession table; this dict is a hot-path cache that is
# rehydrated from DB on startup so a process restart no longer logs everyone out.
_active_sessions: dict[str, tuple[str, float]] = {}
_SESSION_MAX_AGE = 86400 * 7  # 7 days, matches cookie max_age
_SESSION_MAX_SIZE = 5000  # cap to prevent unbounded memory growth


async def _persist_session(token: str, username: str) -> None:
    """Write a new session row to the DB (and add to the in-memory cache)."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_SESSION_MAX_AGE)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(UserSession(token=token, username=username, created_at=now, last_seen_at=now, expires_at=expires))
        await session.commit()
    _active_sessions[token] = (username, now.timestamp())


async def _drop_session(token: str) -> None:
    _active_sessions.pop(token, None)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        existing = await session.exec(select(UserSession).where(UserSession.token == token))
        row = existing.first()
        if row:
            await session.delete(row)
            await session.commit()


async def _rehydrate_sessions() -> None:
    """Load non-expired sessions from DB into the cache (called at startup)."""
    now = datetime.now(timezone.utc)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(UserSession).where(UserSession.expires_at >= now))
        for row in result.all():
            _active_sessions[row.token] = (row.username, row.created_at.timestamp())
        # Cull stale rows.
        expired = await session.exec(select(UserSession).where(UserSession.expires_at < now))
        for row in expired.all():
            await session.delete(row)
        await session.commit()
    logger.info("Rehydrated %d active sessions from DB", len(_active_sessions))


async def _rename_session_username(old: str, new: str, keep_token: str | None) -> None:
    """Rename sessions from `old` to `new`, dropping all except `keep_token`.

    Used after a successful username change so the active session keeps working
    while every other session for the old username is invalidated.
    """
    # Update memory cache.
    for tok in list(_active_sessions):
        uname, ts = _active_sessions[tok]
        if uname != old:
            continue
        if tok == keep_token:
            _active_sessions[tok] = (new, ts)
        else:
            del _active_sessions[tok]
    # Mirror to DB.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(UserSession).where(UserSession.username == old))
        for row in result.all():
            if row.token == keep_token:
                row.username = new
                session.add(row)
            else:
                await session.delete(row)
        await session.commit()


def _prune_expired_sessions() -> None:
    """Remove sessions older than _SESSION_MAX_AGE from the in-memory cache.

    This is called per request and must stay synchronous + cheap. DB-side stale
    rows are culled at startup by `_rehydrate_sessions`.
    """
    now = time.time()
    expired = [t for t, (_, ts) in _active_sessions.items() if now - ts > _SESSION_MAX_AGE]
    for t in expired:
        del _active_sessions[t]
    if len(_active_sessions) > _SESSION_MAX_SIZE:
        sorted_tokens = sorted(_active_sessions, key=lambda t: _active_sessions[t][1])
        for t in sorted_tokens[:len(_active_sessions) - _SESSION_MAX_SIZE]:
            del _active_sessions[t]
    stale = [ip for ip, attempts in _login_attempts.items()
             if not attempts or (now - attempts[-1]) > 600]
    for ip in stale:
        del _login_attempts[ip]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
async def _get_current_user_from_session(session: AsyncSession, request: Request) -> User | None:
    _prune_expired_sessions()
    token = request.cookies.get("session")
    if not token or token not in _active_sessions:
        return None
    username, _ts = _active_sessions[token]
    result = await session.exec(select(User).where(User.username == username))
    return result.first()


async def _get_current_user(request: Request) -> User | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await _get_current_user_from_session(session, request)


def require_auth(request: Request) -> str:
    token = request.cookies.get("session")
    if token and token in _active_sessions:
        username, _ts = _active_sessions[token]
        return username
    raise HTTPException(status_code=401, detail="Not authenticated")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    public_paths = {"/login", "/register", "/forgot-password", "/health", "/favicon.ico"}
    if request.url.path in public_paths:
        return await call_next(request)
    # The /api/v1/* surface authenticates via X-API-Key header, not session cookie.
    if request.url.path.startswith("/api/v1/"):
        return await call_next(request)
    # /share/<handle>.svg are public source-card images — meant to be embedded
    # in tweets, posts, blog comments. No auth required by design.
    if request.url.path.startswith("/share/"):
        return await call_next(request)
    token = request.cookies.get("session")
    if token and token in _active_sessions:
        return await call_next(request)
    return RedirectResponse("/login", status_code=302)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-XSS-Protection"] = "0"
    return response


# ---------------------------------------------------------------------------
# Login / Register / Logout
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", msg: str = ""):
    resp = templates.TemplateResponse("login.html", {"request": request, "error": error, "msg": msg, "csrf_token": ""})
    csrf_token = _set_csrf_cookie(request, resp)
    # Re-render with the actual token now that the cookie is set
    resp = templates.TemplateResponse("login.html", {"request": request, "error": error, "msg": msg, "csrf_token": csrf_token})
    _set_csrf_cookie(request, resp)
    return resp


@app.post("/login")
async def login_submit(request: Request, session: AsyncSession = Depends(get_session), username: str = Form(""), password: str = Form(""), start_platform: str = Form("polymarket"), csrf_token_field: str = Form("", alias="_csrf_token")):
    # CSRF validation
    if not _validate_csrf(request, csrf_token_field):
        csrf_token = _get_csrf_token(request)
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid form submission. Please try again.", "msg": "", "csrf_token": csrf_token})

    client_ip = _client_identity(request)
    if _check_rate_limit(client_ip):
        csrf_token = _get_csrf_token(request)
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many login attempts. Try again in 5 minutes.", "msg": "", "csrf_token": csrf_token})
    # Record attempt AFTER the check so the limit is exact
    _login_attempts[client_ip].append(time.time())

    result = await session.exec(select(User).where(User.username == username))
    user = result.first()
    if user and _verify_password(password, user.password_hash):
        # Migrate legacy hash to PBKDF2 on successful login
        if "$" not in user.password_hash:
            user.password_hash = _hash_password(password)
        if start_platform in ("polymarket", "kalshi") and user.preferred_platform != start_platform:
            user.preferred_platform = start_platform
        session.add(user)
        await session.commit()
        token = _make_session_token()
        await _persist_session(token, username)
        resp = RedirectResponse("/", status_code=302)
        is_secure = request.url.scheme == "https"
        resp.set_cookie("session", token, httponly=True, samesite="strict", max_age=86400 * 7, secure=is_secure)
        return resp
    csrf_token = _get_csrf_token(request)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password", "msg": "", "csrf_token": csrf_token})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = ""):
    resp = templates.TemplateResponse("register.html", {"request": request, "error": error, "csrf_token": ""})
    csrf_token = _set_csrf_cookie(request, resp)
    resp = templates.TemplateResponse("register.html", {"request": request, "error": error, "csrf_token": csrf_token})
    _set_csrf_cookie(request, resp)
    return resp


@app.post("/register")
async def register_submit(request: Request, session: AsyncSession = Depends(get_session), username: str = Form(""), email: str = Form(""), password: str = Form(""), password2: str = Form(""), start_platform: str = Form("polymarket"), csrf_token_field: str = Form("", alias="_csrf_token")):
    # CSRF validation
    if not _validate_csrf(request, csrf_token_field):
        csrf_token = _get_csrf_token(request)
        return templates.TemplateResponse("register.html", {"request": request, "error": "Invalid form submission. Please try again.", "csrf_token": csrf_token})

    csrf_token = _get_csrf_token(request)
    if len(username) < 3 or len(username) > 15:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username must be 3\u201315 characters", "csrf_token": csrf_token})
    import re as _re
    if len(password) < 12 or not _re.search(r"[A-Z]", password) or not _re.search(r"[a-z]", password) or not _re.search(r"[0-9]", password):
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must be at least 12 characters with an uppercase letter, lowercase letter, and number", "csrf_token": csrf_token})
    if password != password2:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords don't match", "csrf_token": csrf_token})
    existing = await session.exec(select(User).where(User.username == username))
    if existing.first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username already taken", "csrf_token": csrf_token})
    if email:
        existing_email = await session.exec(select(User).where(User.email == email))
        if existing_email.first():
            return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered", "csrf_token": csrf_token})
    session.add(User(username=username, email=email, password_hash=_hash_password(password), preferred_platform=start_platform if start_platform in ("polymarket", "kalshi") else "polymarket", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
    await session.commit()
    return RedirectResponse("/login?msg=Account+created.+Sign+in+below.", status_code=302)


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    csrf_token = form.get("_csrf_token", "")
    if not _validate_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    token = request.cookies.get("session")
    if token:
        await _drop_session(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth), new_key: str = ""):
    user = await _get_current_user_from_session(session, request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    preferred_platform = getattr(user, "preferred_platform", None) or "polymarket"
    preferred_theme = getattr(user, "preferred_theme", None) or "dark"
    csrf_token = _get_csrf_token(request)
    keys_result = await session.exec(
        select(APIKey).where(APIKey.user_id == user.id, APIKey.revoked == False).order_by(APIKey.created_at.desc())  # noqa: E712
    )
    api_keys = keys_result.all()
    return templates.TemplateResponse("profile.html", {
        "request": request, "user": user, "msg": "", "error": "",
        "preferred_platform": preferred_platform, "preferred_theme": preferred_theme,
        "ts_password_decrypted": "••••••••" if user.truthsocial_password else "",
        "csrf_token": csrf_token, "api_keys": api_keys, "new_key": new_key,
    })


@app.post("/profile/update", response_class=HTMLResponse)
async def profile_update(request: Request, confirm_password: str = Form(""), new_username: str = Form(""), email: str = Form(""), twitter_bearer_token: str = Form(""), truthsocial_username: str = Form(""), truthsocial_password: str = Form(""), truthsocial_access_token: str = Form(""), telegram_bot_token: str = Form(""), telegram_chat_id: str = Form(""), telegram_alerts_enabled: str = Form(""), preferred_platform: str = Form("polymarket"), preferred_theme: str = Form("dark"), csrf_token_field: str = Form("", alias="_csrf_token")):
    csrf_token = _get_csrf_token(request)
    user = await _get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # CSRF validation
    if not _validate_csrf(request, csrf_token_field):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(select(User).where(User.id == user.id))
            db_user = result.first() or user
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Invalid form submission. Please try again.", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})

    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            return RedirectResponse("/login", status_code=302)

        # Require password to save profile changes
        if not _verify_password(confirm_password, db_user.password_hash):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Enter your current password to save changes", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})

        if new_username and new_username != db_user.username:
            if len(new_username) < 3 or len(new_username) > 15:
                return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Username must be 3\u201315 characters", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
            existing = await session.exec(select(User).where(User.username == new_username))
            if existing.first():
                return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Username already taken", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
            db_user.username = new_username

        db_user.email = email
        if twitter_bearer_token:
            db_user.twitter_bearer_token = _encrypt_field(twitter_bearer_token) if twitter_bearer_token else None
        db_user.truthsocial_username = truthsocial_username
        if truthsocial_password and truthsocial_password != "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022":
            db_user.truthsocial_password = _encrypt_field(truthsocial_password)
        if truthsocial_access_token:
            db_user.truthsocial_access_token = _encrypt_field(truthsocial_access_token) if truthsocial_access_token else None
        if telegram_bot_token:
            db_user.telegram_bot_token = _encrypt_field(telegram_bot_token)
        db_user.telegram_chat_id = telegram_chat_id.strip()
        db_user.telegram_alerts_enabled = telegram_alerts_enabled.lower() in ("1", "true", "on", "yes")
        db_user.preferred_platform = preferred_platform
        db_user.preferred_theme = preferred_theme
        db_user.updated_at = datetime.now(timezone.utc)
        session.add(db_user)
        await session.commit()

        resp = templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "Profile updated", "error": "", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
        if new_username and new_username != user.username:
            current_token = request.cookies.get("session")
            await _rename_session_username(user.username, new_username, current_token)
        return resp


@app.post("/profile/password", response_class=HTMLResponse)
async def profile_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), new_password2: str = Form(""), csrf_token_field: str = Form("", alias="_csrf_token")):
    csrf_token = _get_csrf_token(request)
    user = await _get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # CSRF validation
    if not _validate_csrf(request, csrf_token_field):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(select(User).where(User.id == user.id))
            db_user = result.first() or user
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Invalid form submission. Please try again.", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})

    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            return RedirectResponse("/login", status_code=302)
        if not _verify_password(current_password, db_user.password_hash):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Current password is incorrect", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
        if len(new_password) < 12 or not _re.search(r"[A-Z]", new_password) or not _re.search(r"[a-z]", new_password) or not _re.search(r"[0-9]", new_password):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Password must be 12+ chars with uppercase, lowercase, and a number", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
        if new_password != new_password2:
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "New passwords don't match", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})
        db_user.password_hash = _hash_password(new_password)
        db_user.updated_at = datetime.now(timezone.utc)
        session.add(db_user)
        await session.commit()
        return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "Password changed", "error": "", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": "••••••••" if db_user.truthsocial_password else "", "csrf_token": csrf_token})


# ---------------------------------------------------------------------------
# Preferences (HTMX toggle endpoint)
# ---------------------------------------------------------------------------
@app.post("/preferences")
async def update_preferences(request: Request):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    # Validate CSRF token from header (AJAX requests send it as X-CSRF-Token)
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_token or not _validate_csrf(request, csrf_token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=403)
    user = await _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        body = await request.json()
    except (ValueError, _json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            raise HTTPException(status_code=404, detail="User not found")
        if "platform" in body:
            db_user.preferred_platform = body["platform"]
        if "theme" in body:
            db_user.preferred_theme = body["theme"]
        db_user.updated_at = datetime.now(timezone.utc)
        session.add(db_user)
        await session.commit()
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cred_color(score: float) -> str:
    if score >= 0.7: return "bg-green-500/20 text-green-400 border border-green-500/30"
    elif score >= 0.4: return "bg-[#2D64F3]/20 text-[#5B8DF5] border border-[#2D64F3]/30"
    elif score > 0: return "bg-red-500/20 text-red-400 border border-red-500/30"
    return "bg-gray-500/20 text-gray-500 border border-gray-500/30"

def _cred_bar_color(score: float) -> str:
    if score >= 0.7: return "bg-green-500"
    elif score >= 0.4: return "bg-[#2D64F3]"
    elif score > 0: return "bg-red-500"
    return "bg-gray-600"

def _esc(text: str) -> str:
    return html_mod.escape(str(text)) if text else ""

def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))

def _time_ago(dt: datetime | None) -> str:
    if not dt: return "\u2014"
    now = datetime.now(timezone.utc)
    dt_utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    diff = now - dt_utc
    mins = int(diff.total_seconds() / 60)
    if mins < 1: return "just now"
    if mins < 60: return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24: return f"{hrs}h ago"
    return f"{hrs // 24}d ago"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    user = await _get_current_user_from_session(session, request)
    missing = []
    has_twitter = bool(settings.get("TWITTER_BEARER_TOKEN") or (user and user.twitter_bearer_token))
    has_truth = bool(settings.get("TRUTHSOCIAL_USERNAME") or settings.get("TRUTHSOCIAL_ACCESS_TOKEN") or (user and (user.truthsocial_username or user.truthsocial_access_token)))
    if not has_twitter:
        missing.append("X (Twitter) Bearer Token")
    if not has_truth:
        missing.append("TruthSocial credentials")
    preferred_platform = (getattr(user, "preferred_platform", None) or "polymarket") if user else "polymarket"
    preferred_theme = (getattr(user, "preferred_theme", None) or "dark") if user else "dark"
    return templates.TemplateResponse("dashboard.html", {"request": request, "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "missing_keys": missing, "user": user.username if user else _user or "anonymous", "preferred_platform": preferred_platform, "preferred_theme": preferred_theme, "csrf_token": _get_csrf_token(request)})


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
@app.get("/feed", response_class=HTMLResponse)
async def feed(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth), category: str = "", platform: str = "", hide_risk: bool = False, hide_unrated: bool = False, market: str = "", sort: str = "recent", page: int = 1, per_page: int = 50):
    cutoff = ONE_YEAR_FROM_NOW()
    stmt = select(Prediction, RawPost).join(RawPost, Prediction.raw_post_id == RawPost.id)
    stmt = stmt.where((Prediction.market_close_time.is_(None)) | (Prediction.market_close_time <= cutoff))
    if category: stmt = stmt.where(Prediction.category == category)
    if platform: stmt = stmt.where(RawPost.platform == platform)
    if hide_risk: stmt = stmt.where(Prediction.risk_flag == False)  # noqa: E712
    if hide_unrated: stmt = stmt.where(Prediction.global_credibility_at_time > 0)
    if market: stmt = stmt.where(Prediction.market_question.contains(market))
    if sort == "ev": stmt = stmt.order_by(Prediction.ev_score.desc().nullslast())
    elif sort == "credibility": stmt = stmt.order_by(Prediction.global_credibility_at_time.desc())
    else: stmt = stmt.order_by(Prediction.extracted_at.desc())
    page, per_page = _clamp(page, 1, 1000), _clamp(per_page, 1, 100)
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.exec(stmt)
    rows = result.all()
    html_rows = []
    for pred, post in rows:
        cc = _cred_color(pred.global_credibility_at_time)
        ev = f"{pred.ev_score:+.2f}" if pred.ev_score is not None else "\u2014"
        ec = "text-green-400" if (pred.ev_score or 0) > 0 else "text-red-400" if (pred.ev_score or 0) < 0 else "text-gray-500"
        rh = ""
        if pred.risk_flag:
            _rr = pred.risk_reasons if isinstance(pred.risk_reasons, list) else []
            rh = f'<span class="text-amber-400 cursor-help" title="{_esc(", ".join(_esc(r) for r in _rr))}"><svg class="w-3.5 h-3.5 inline" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/></svg></span>'
        pl = '<span class="text-blue-400 text-xs">X</span>' if post.platform == "twitter" else '<span class="text-purple-400 text-xs">TS</span>'
        mk = f'<a href="https://polymarket.com/event/{pred.market_slug}" target="_blank" class="text-blue-400 hover:text-blue-300">{_esc((pred.market_question or "")[:45])}</a>' if pred.market_slug else '<span class="text-gray-600">\u2014</span>'
        html_rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02]"><td class="px-4 py-3 max-w-[280px]"><div class="truncate text-gray-300">{_esc(pred.predicted_outcome)}: {_esc(post.content[:70])}</div></td><td class="px-4 py-3 text-sm text-gray-400">@{_esc(post.author_handle)}</td><td class="px-4 py-3 text-center">{pl}</td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{_esc(pred.category)}</span></td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full {cc}">{pred.global_credibility_at_time:.2f}</span></td><td class="px-4 py-3 font-mono text-sm {ec}">{ev}</td><td class="px-4 py-3 text-center">{rh}</td><td class="px-4 py-3 text-xs">{mk}</td><td class="px-4 py-3 text-xs text-gray-500">{_time_ago(pred.extracted_at)}</td></tr>')
    if not html_rows:
        return HTMLResponse('<tr><td colspan="9" class="text-center py-16 text-gray-600">No predictions yet. Pipeline runs every 5 minutes.</td></tr>')
    return HTMLResponse("\n".join(html_rows))


# ---------------------------------------------------------------------------
# Best Bets
# ---------------------------------------------------------------------------
@app.get("/best-bets", response_class=HTMLResponse)
async def best_bets(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    cutoff = ONE_YEAR_FROM_NOW()
    stmt = select(Prediction, RawPost).join(RawPost, Prediction.raw_post_id == RawPost.id).where(Prediction.risk_flag == False, Prediction.ev_score.isnot(None), (Prediction.market_close_time.is_(None)) | (Prediction.market_close_time <= cutoff)).order_by(Prediction.ev_score.desc()).limit(10)  # noqa: E712
    result = await session.exec(stmt)
    rows = result.all()
    if not rows:
        return HTMLResponse('<div class="flex flex-col items-center justify-center py-20 text-gray-600"><p class="text-lg">No qualifying bets found yet</p></div>')

    # Batch-load event metadata for every matched market in one query so we
    # don't N+1 the snapshot table.
    slugs = [p.market_slug for p, _ in rows if p.market_slug]
    event_meta: dict[str, tuple[str | None, str | None]] = {}
    if slugs:
        ms_result = await session.exec(select(MarketSnapshot).where(MarketSnapshot.market_slug.in_(slugs)))
        for ms in ms_result.all():
            # Latest write wins; per-slug we only care about the most recent.
            event_meta[ms.market_slug] = (ms.event_title, ms.outcome_name)

    cards = []
    for i, (pred, post) in enumerate(rows):
        src_result = await session.exec(select(Source).where(Source.handle == post.author_handle))
        source = src_result.first()
        evc = "text-green-400" if (pred.ev_score or 0) > 0 else "text-red-400"
        gc = source.global_credibility if source else 0.0
        catc = pred.category_credibility_at_time or 0.0
        mp = f"{pred.market_implied_probability:.0%}" if pred.market_implied_probability else "\u2014"
        pp = f"{pred.predicted_probability:.0%}" if pred.predicted_probability else "\u2014"
        acc = f"{source.correct_qualifying}/{source.qualifying_predictions}" if source else "\u2014"
        pl = "X" if post.platform == "twitter" else "TS"
        poly = f'<a href="https://polymarket.com/event/{pred.market_slug}" target="_blank" class="text-xs text-blue-400 hover:text-blue-300">Polymarket &nearr;</a>' if pred.market_slug else ""
        rk = f'<span class="absolute -top-2 -left-2 w-7 h-7 rounded-full bg-[#2D64F3] flex items-center justify-center text-xs font-bold text-white shadow-lg">#{i+1}</span>'
        side = (pred.bet_side or "YES").upper()
        side_color = "bg-green-500/20 text-green-400 border-green-500/30" if side == "YES" else "bg-red-500/20 text-red-400 border-red-500/30"
        side_badge = f'<span class="text-[10px] px-2 py-0.5 rounded-full border {side_color}">BUY {side}</span>'
        # Event grouping \u2014 when this market is one option inside a multi-outcome
        # event ("Trump" inside "2028 Presidential Election Winner"), show the
        # parent event title above the question + the outcome chip in the meta row.
        event_title, outcome_name = event_meta.get(pred.market_slug or "", (None, None))
        event_caption = (
            f'<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1">in: {_esc(event_title[:60])}</div>'
            if event_title else ""
        )
        outcome_chip = (
            f'<span class="text-[10px] px-2 py-0.5 rounded-full bg-[#2D64F3]/20 text-[#5B8DF5] border border-[#2D64F3]/30 ml-1">{_esc(outcome_name[:30])}</span>'
            if outcome_name else ""
        )
        cards.append(f'<div class="relative bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-white/5 hover:border-white/10 transition-all">{rk}{event_caption}<h3 class="text-sm font-semibold text-gray-200 mb-3 pr-4">{_esc((pred.market_question or "Unmatched")[:80])}</h3><div class="flex items-center gap-2 mb-4 text-xs text-gray-500">{side_badge}{outcome_chip}<span>&middot;</span><span class="font-medium text-gray-300">{_esc(pred.predicted_outcome)}</span><span>&middot;</span><span>@{_esc(post.author_handle)}</span><span class="px-1.5 py-0.5 rounded bg-white/5">{pl}</span></div><div class="{evc} text-3xl font-bold tracking-tight mb-4">{pred.ev_score:+.2f}<span class="text-sm font-normal text-gray-500 ml-1">EV</span></div><div class="grid grid-cols-2 gap-4 mb-4"><div><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Global</div><div class="w-full bg-gray-700/50 rounded-full h-1.5"><div class="{_cred_bar_color(gc)} h-1.5 rounded-full" style="width:{int(gc*100)}%"></div></div><div class="text-xs mt-1 text-gray-400">{gc:.2f}</div></div><div><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Category</div><div class="w-full bg-gray-700/50 rounded-full h-1.5"><div class="{_cred_bar_color(catc)} h-1.5 rounded-full" style="width:{int(catc*100)}%"></div></div><div class="text-xs mt-1 text-gray-400">{catc:.2f}</div></div></div><div class="flex justify-between text-xs text-gray-400 mb-3 py-2 border-t border-white/5"><span>Market: <span class="text-gray-300">{mp}</span></span><span>Predicted: <span class="text-gray-300">{pp}</span></span><span>Record: <span class="text-gray-300">{acc}</span></span></div><div class="flex justify-end">{poly}</div></div>')
    return HTMLResponse(f'<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">{chr(10).join(cards)}</div>')


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------
@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    stmt = select(Source).order_by(Source.global_credibility.desc()).limit(50)
    result = await session.exec(stmt)
    all_sources = result.all()
    if not all_sources:
        return HTMLResponse('<tr><td colspan="9" class="text-center py-16 text-gray-600">No sources tracked yet.</td></tr>')
    rows = []
    for rank, s in enumerate(all_sources, 1):
        cb = _cred_bar_color(s.global_credibility)
        pl = '<span class="text-blue-400">X</span>' if s.platform == "twitter" else '<span class="text-purple-400">TS</span>'
        acc = f"{s.accuracy_global:.0%}" if s.accuracy_global is not None else "\u2014"
        rec = f"{s.correct_qualifying}/{s.qualifying_predictions}" if s.qualifying_predictions > 0 else "0/0"
        # Brier — lower is better; <0.18 green, <0.25 amber, else red.
        if s.brier_score is None:
            brier_html = '<span class="text-xs text-gray-600">—</span>'
        else:
            _bcol = ("text-green-400" if s.brier_score < 0.18
                     else "text-amber-400" if s.brier_score < 0.25
                     else "text-red-400")
            brier_html = (
                f'<span class="text-xs font-mono {_bcol}" title="Brier score (lower=better); n={s.brier_n}">'
                f'{s.brier_score:.3f}<span class="text-gray-500 ml-1">({s.brier_n})</span></span>'
            )
        cp = "".join(f'<span class="text-[10px] px-1.5 py-0.5 rounded-full {_cred_color(v)}">{c[:4]}</span> ' for c in (s.categories_predicted_in or [])[:5] if (v := (s.category_credibility or {}).get(c)) is not None)
        th = '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/20 text-green-400">Trusted</span>' if s.trusted is True else '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-red-500/20 text-red-400">Untrusted</span>' if s.trusted is False else ""
        st = '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/10 text-green-400 border border-green-500/20">Rated</span>' if s.accuracy_unlocked else '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-500/10 text-gray-500 border border-gray-500/20">Unrated</span>'
        rk = ['', '<span class="text-lg">&#129351;</span>', '<span class="text-lg">&#129352;</span>', '<span class="text-lg">&#129353;</span>']
        rk_html = rk[rank] if rank <= 3 else f'<span class="text-sm text-gray-500 font-mono">{rank}</span>'
        tb = f'<div class="flex gap-1"><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": true}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs {"bg-green-600 text-white" if s.trusted is True else "bg-white/5 text-gray-500 hover:bg-white/10"}">+</button><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": false}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs {"bg-red-600 text-white" if s.trusted is False else "bg-white/5 text-gray-500 hover:bg-white/10"}">-</button><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": null}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs bg-white/5 text-gray-500 hover:bg-white/10">&#8635;</button></div>'
        rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02] group"><td class="px-4 py-3 text-center w-12">{rk_html}</td><td class="px-4 py-3"><div class="flex items-center gap-2"><span class="font-medium text-gray-200">@{_esc(s.handle)}</span>{pl}{th}{st}</div></td><td class="px-4 py-3 w-40"><div class="flex items-center gap-2"><div class="flex-1 bg-gray-700/30 rounded-full h-2"><div class="{cb} h-2 rounded-full" style="width:{int(s.global_credibility*100)}%"></div></div><span class="text-sm font-mono text-gray-300 w-10 text-right">{s.global_credibility:.2f}</span></div></td><td class="px-4 py-3 text-sm text-gray-400">{acc}</td><td class="px-4 py-3 text-sm text-gray-400 font-mono">{rec}</td><td class="px-4 py-3">{brier_html}</td><td class="px-4 py-3"><div class="flex gap-1 flex-wrap">{cp}</div></td><td class="px-4 py-3 text-xs text-gray-500">{s.follower_count:,}</td><td class="px-4 py-3 opacity-0 group-hover:opacity-100 transition-opacity">{tb}</td></tr>')
    return HTMLResponse("\n".join(rows))


@app.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    return await leaderboard(request, session, _user)


@app.post("/sources/{handle}/trust", response_class=HTMLResponse)
async def update_trust(handle: str, request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        body = await request.json()
    except (ValueError, _json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    tv = body.get("trusted")
    result = await session.exec(select(Source).where(Source.handle == handle))
    source = result.first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    source.trusted = True if (tv is True or tv == "true") else False if (tv is False or tv == "false") else None
    session.add(source)
    await session.commit()
    try:
        from app.credibility.engine import CredibilityEngine
        await CredibilityEngine().recompute(session, handle)
    except Exception as exc:
        logger.error("Recompute failed: %s", exc)
    return await leaderboard(request, session, _user)


@app.get("/sources/{handle}/history", response_class=HTMLResponse)
async def source_history(handle: str, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    result = await session.exec(select(CredibilitySnapshot).where(CredibilitySnapshot.handle == handle).order_by(CredibilitySnapshot.snapshotted_at.desc()).limit(30))
    snapshots = list(reversed(result.all()))
    if not snapshots:
        return HTMLResponse('<div class="text-gray-600 text-sm">No history.</div>')
    labels = _json.dumps([s.snapshotted_at.strftime("%m/%d") for s in snapshots])
    values = _json.dumps([s.global_credibility for s in snapshots])
    cid = "spark-" + _re.sub(r'[^a-zA-Z0-9-]', '', handle.replace('.', '-').replace('@', ''))
    return HTMLResponse(f'<canvas id="{cid}" width="200" height="50"></canvas><script>new Chart(document.getElementById("{cid}"),{{type:"line",data:{{labels:{labels},datasets:[{{data:{values},borderColor:"#2D64F3",borderWidth:1.5,fill:false,pointRadius:0,tension:0.3}}]}},options:{{responsive:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{display:false}},y:{{display:false,min:0,max:1}}}}}}}});</script>')


# ---------------------------------------------------------------------------
# Markets (with filters + pagination)
# ---------------------------------------------------------------------------
@app.get("/markets", response_class=HTMLResponse)
async def markets(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth), category: str = "", search: str = "", sort: str = "volume", page: int = 1, per_page: int = 20, platform: str = "polymarket"):
    cutoff = ONE_YEAR_FROM_NOW()
    stmt = select(MarketSnapshot).where((MarketSnapshot.close_time.is_(None)) | (MarketSnapshot.close_time <= cutoff))
    stmt = stmt.where(MarketSnapshot.platform == platform)
    if category:
        stmt = stmt.where(MarketSnapshot.category == category)
    if search:
        stmt = stmt.where(MarketSnapshot.market_question.contains(search))
    if sort == "price_high":
        stmt = stmt.order_by(MarketSnapshot.yes_price.desc())
    elif sort == "price_low":
        stmt = stmt.order_by(MarketSnapshot.yes_price.asc())
    elif sort == "closing_soon":
        stmt = stmt.where(MarketSnapshot.close_time.isnot(None)).order_by(MarketSnapshot.close_time.asc())
    else:
        stmt = stmt.order_by(MarketSnapshot.volume_usd.desc())

    # Count total for pagination
    count_stmt = select(func.count()).select_from(MarketSnapshot).where((MarketSnapshot.close_time.is_(None)) | (MarketSnapshot.close_time <= cutoff))
    count_stmt = count_stmt.where(MarketSnapshot.platform == platform)
    if category:
        count_stmt = count_stmt.where(MarketSnapshot.category == category)
    if search:
        count_stmt = count_stmt.where(MarketSnapshot.market_question.contains(search))
    total_result = await session.exec(count_stmt)
    total = total_result.first() or 0
    page, per_page = _clamp(page, 1, 1000), _clamp(per_page, 1, 100)
    total_pages = max(1, (total + per_page - 1) // per_page)

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await session.exec(stmt)
    all_markets = result.all()

    if not all_markets:
        return HTMLResponse(f'<tr><td colspan="6" class="text-center py-16 text-gray-600">No markets found.</td></tr><tr><td colspan="6" class="px-4 py-2 text-xs text-gray-600 text-center">0 results</td></tr>')

    rows = []
    for m in all_markets:
        close_str = m.close_time.strftime("%b %d, %Y") if m.close_time else "\u2014"
        pc = "text-green-400" if m.yes_price >= 0.6 else "text-red-400" if m.yes_price <= 0.4 else "text-gray-300"
        rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02] cursor-pointer" hx-get="/feed?market={_esc(m.market_question[:50])}" hx-target="#feed-body" hx-swap="innerHTML" onclick="switchTab(\'feed\')"><td class="px-4 py-3 max-w-[300px]"><div class="truncate text-gray-300">{_esc(m.market_question[:65])}</div></td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{m.category}</span></td><td class="px-4 py-3"><div class="flex items-center gap-2"><div class="w-16 bg-gray-700/30 rounded-full h-1.5"><div class="bg-[#2D64F3] h-1.5 rounded-full" style="width:{int(m.yes_price*100)}%"></div></div><span class="font-mono text-sm {pc}">{m.yes_price:.0%}</span></div></td><td class="px-4 py-3 text-sm text-gray-400"><span class="narve-money" data-usd="{m.volume_usd or 0}">${m.volume_usd:,.0f}</span></td><td class="px-4 py-3 text-xs text-gray-500">{close_str}</td><td class="px-4 py-3"><button hx-get="/markets/{m.market_slug}/chart" hx-target="#market-chart" hx-swap="innerHTML" class="text-xs text-blue-400 hover:text-blue-300">Chart</button></td></tr>')

    # Pagination row
    safe_cat = _esc(category)
    safe_search = _esc(search)
    safe_sort = _esc(sort)
    prev_btn = f'<button hx-get="/markets?page={page-1}&per_page={per_page}&category={safe_cat}&search={safe_search}&sort={safe_sort}" hx-target="#markets-body" hx-swap="innerHTML" hx-include=".mkt-filter" class="px-2 py-1 rounded bg-white/5 text-gray-400 hover:bg-white/10 text-xs">&laquo; Prev</button>' if page > 1 else '<span class="px-2 py-1 text-xs text-gray-700">&laquo; Prev</span>'
    next_btn = f'<button hx-get="/markets?page={page+1}&per_page={per_page}&category={safe_cat}&search={safe_search}&sort={safe_sort}" hx-target="#markets-body" hx-swap="innerHTML" hx-include=".mkt-filter" class="px-2 py-1 rounded bg-white/5 text-gray-400 hover:bg-white/10 text-xs">Next &raquo;</button>' if page < total_pages else '<span class="px-2 py-1 text-xs text-gray-700">Next &raquo;</span>'
    rows.append(f'<tr><td colspan="6" class="px-4 py-3"><div class="flex items-center justify-between"><span class="text-xs text-gray-500">{total} markets &middot; Page {page} of {total_pages}</span><div class="flex gap-2">{prev_btn}{next_btn}</div></div></td></tr>')

    return HTMLResponse("\n".join(rows))


@app.get("/markets/{slug:path}/chart", response_class=HTMLResponse)
async def market_chart(slug: str, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    result = await session.exec(select(MarketSnapshot).where(MarketSnapshot.market_slug == slug).order_by(MarketSnapshot.snapshotted_at.asc()).limit(100))
    snapshots = list(result.all() or [])
    if not snapshots:
        return HTMLResponse('<div class="text-gray-600 text-sm p-4">No history.</div>')
    labels = _json.dumps([s.snapshotted_at.strftime("%m/%d %H:%M") for s in snapshots])
    prices = _json.dumps([s.yes_price for s in snapshots])
    title = _esc((snapshots[0].market_question or "")[:60])
    return HTMLResponse(f'<div class="mt-4 bg-gray-800/50 rounded-xl p-5 border border-white/5"><h4 class="text-sm font-semibold text-gray-300 mb-3">{title}</h4><canvas id="market-odds-chart" height="120"></canvas><script>if(window._mktChart)window._mktChart.destroy();window._mktChart=new Chart(document.getElementById("market-odds-chart"),{{type:"line",data:{{labels:{labels},datasets:[{{label:"Yes",data:{prices},borderColor:"#2D64F3",borderWidth:2,fill:true,backgroundColor:"rgba(45,100,243,0.08)",tension:0.3,pointRadius:1}}]}},options:{{responsive:true,plugins:{{legend:{{display:false}}}},scales:{{y:{{min:0,max:1,grid:{{color:"rgba(255,255,255,0.03)"}},ticks:{{color:"#6b7280"}}}},x:{{grid:{{display:false}},ticks:{{color:"#6b7280",maxRotation:45}}}}}}}}}});</script></div>')


# ---------------------------------------------------------------------------
# FX rates (frankfurter.dev) — for client-side currency picker
# ---------------------------------------------------------------------------
_FX_CACHE: dict = {"rates": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
    "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
    "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
    "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
    "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
    "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
}


def _fetch_fx_blocking() -> dict:
    try:
        url = "https://api.frankfurter.dev/v1/latest?" + urllib.parse.urlencode({"base": "USD"})
        req = urllib.request.Request(url, headers={"User-Agent": "narve-polymarket/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        rates = dict(data.get("rates") or {})
        rates["USD"] = 1.0
        return rates
    except Exception as exc:
        logger.warning("FX fetch failed, using fallback: %s", exc)
        return dict(_FX_FALLBACK)


@app.get("/api/fx-rates")
async def api_fx_rates(_user: str = Depends(require_auth)):
    """USD-base FX rates with 1h server cache."""
    now = time.time()
    cached = _FX_CACHE.get("rates")
    fetched = _FX_CACHE.get("fetched_at", 0.0)
    if cached and (now - fetched) < _FX_TTL:
        return JSONResponse({"base": "USD", "rates": cached, "fetched_at": fetched})
    rates = await asyncio.to_thread(_fetch_fx_blocking)
    _FX_CACHE["rates"] = rates
    _FX_CACHE["fetched_at"] = now
    return JSONResponse({"base": "USD", "rates": rates, "fetched_at": now})


# ---------------------------------------------------------------------------
# Cross-venue arbitrage — match Polymarket vs Kalshi, surface YES-price spreads.
# ---------------------------------------------------------------------------
@app.get("/arbitrage", response_class=HTMLResponse)
async def arbitrage(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth), min_edge: float = 3.0):
    """Render the cross-venue arbitrage table.

    A match is a Polymarket market and a Kalshi market that are the same
    underlying event (Jaccard ≥0.6 on de-templated question tokens, same
    category, close times within 14 days). The "edge" is the absolute
    percentage-point difference in YES prices — that's roughly the gross
    profit per share you'd capture by buying YES on the cheaper venue and
    NO (or equivalent) on the expensive one.
    """
    from app.processing.arbitrage import find_arbs
    arbs = await find_arbs(session, min_edge_pp=max(0.0, min(50.0, min_edge)))
    if not arbs:
        return HTMLResponse(
            f'<div class="text-center py-16 text-gray-600 text-sm">No arbitrage opportunities found ≥{min_edge:.1f}pp.<br>'
            'Re-sync markets to refresh, or lower the threshold.</div>'
        )
    rows = []
    for a in arbs[:50]:
        # Buy cheap → Sell expensive direction.
        buy_link = (
            f'https://polymarket.com/event/{a.polymarket_slug}'
            if a.cheaper_venue == "polymarket"
            else f'https://kalshi.com/markets/{a.kalshi_ticker}'
        )
        sell_link = (
            f'https://kalshi.com/markets/{a.kalshi_ticker}'
            if a.cheaper_venue == "polymarket"
            else f'https://polymarket.com/event/{a.polymarket_slug}'
        )
        buy_name = "Poly" if a.cheaper_venue == "polymarket" else "Kalshi"
        sell_name = "Kalshi" if a.cheaper_venue == "polymarket" else "Poly"
        rows.append(
            f'<tr class="border-b border-white/5 hover:bg-white/[0.02]">'
            f'<td class="px-4 py-3 max-w-[320px]"><div class="truncate text-gray-300">{_esc(a.question[:80])}</div>'
            f'<div class="text-[10px] text-gray-600 truncate">vs Kalshi: {_esc(a.kalshi_title[:80])}</div></td>'
            f'<td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{a.category}</span></td>'
            f'<td class="px-4 py-3 font-mono text-sm text-gray-300">{a.poly_yes:.2f}</td>'
            f'<td class="px-4 py-3 font-mono text-sm text-gray-300">{a.kalshi_yes:.2f}</td>'
            f'<td class="px-4 py-3 font-mono text-sm text-green-400">+{a.edge_pp:.1f}pp</td>'
            f'<td class="px-4 py-3 text-xs">'
            f'<a href="{buy_link}" target="_blank" class="text-green-400 hover:text-green-300">Buy {buy_name} &nearr;</a>'
            f' / <a href="{sell_link}" target="_blank" class="text-red-400 hover:text-red-300">Sell {sell_name} &nearr;</a>'
            f'</td>'
            f'<td class="px-4 py-3 text-xs text-gray-500" title="match score">{a.match_score:.2f}</td>'
            f'</tr>'
        )
    return HTMLResponse(
        '<div class="text-xs text-gray-500 mb-3">'
        f'{len(arbs)} opportunities ≥{min_edge:.1f}pp — buy YES on the cheaper venue, '
        'NO (or "sell YES") on the expensive one.</div>'
        '<div class="overflow-x-auto rounded-xl border themed-border"><table class="w-full text-sm">'
        '<thead><tr class="themed-card text-[10px] uppercase tracking-wider themed-muted">'
        '<th class="px-4 py-2.5 text-left font-medium">Event</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Category</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Poly YES</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Kalshi YES</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Edge</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Trade</th>'
        '<th class="px-4 py-2.5 text-left font-medium">Match</th>'
        '</tr></thead><tbody>' + "\n".join(rows) + '</tbody></table></div>'
    )


# ---------------------------------------------------------------------------
# Liquidity-aware EV — size-adjusted execution prices from the Polymarket CLOB.
# ---------------------------------------------------------------------------
@app.get("/markets/{slug:path}/liquidity")
async def market_liquidity(slug: str, side: str = "YES", _user: str = Depends(require_auth)):
    """Return execution prices at standard stake sizes for a Polymarket market.

    Walks the order book to compute the volume-weighted average price you'd
    actually pay to fill a $stake order, plus slippage in basis points vs the
    book mid. Surfaces the "this edge dies past $X stake" reality that the
    midpoint EV hides.
    """
    from app.markets.polymarket_clob import (
        PolymarketCLOBClient,
        avg_fill_price,
        slippage_bps,
    )
    client = PolymarketCLOBClient()
    book = await client.book_for_side(slug, side)
    if book is None:
        return JSONResponse({"error": "Order book unavailable"}, status_code=404)
    mid = book.mid
    sizes = [100, 1000, 10000]
    breakdown = []
    for size in sizes:
        fill = avg_fill_price(book.asks, size)
        breakdown.append({
            "stake_usd": size,
            "avg_fill_price": round(fill, 4) if fill is not None else None,
            "slippage_bps": slippage_bps(mid, fill),
            "fillable": fill is not None,
        })
    return JSONResponse({
        "market_slug": slug,
        "side": side.upper(),
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "mid": mid,
        "stake_breakdown": breakdown,
    })


# ---------------------------------------------------------------------------
# Performance (paper-trade ledger) + Backtest
# ---------------------------------------------------------------------------
@app.get("/performance", response_class=HTMLResponse)
async def performance(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    """Render the paper-trade summary card + recent ledger rows.

    Shows the running P&L of "$1 every time the system fires a tradeable
    signal" — answers the question "would following these picks have made
    money?". The ledger is opened by the scheduler and settled by the resolver.
    """
    from app.processing.paper_trade import summary
    s = await summary(session)
    # Ledger — most recent 50 rows.
    rows_result = await session.exec(select(PaperTrade).order_by(PaperTrade.opened_at.desc()).limit(50))
    rows = rows_result.all()

    pnl_color = "text-green-400" if (s["total_pnl_usd"] or 0) >= 0 else "text-red-400"
    hr = f"{s['hit_rate']:.0%}" if s["hit_rate"] is not None else "—"
    roi = f"{s['roi']:+.1%}" if s["roi"] is not None else "—"
    open_count = s["open"]
    settled = s["settled"]

    summary_html = (
        f'<div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">'
        f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Total P&amp;L (paper)</div><div class="{pnl_color} text-2xl font-bold">{s["total_pnl_usd"]:+.2f}</div><div class="text-xs text-gray-500 mt-1">on ${s["total_staked_usd"]:.0f} staked</div></div>'
        f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">ROI</div><div class="text-2xl font-bold text-gray-200">{roi}</div></div>'
        f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Hit rate</div><div class="text-2xl font-bold text-gray-200">{hr}</div><div class="text-xs text-gray-500 mt-1">{s["wins"]}W / {s["losses"]}L</div></div>'
        f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Open / Settled</div><div class="text-2xl font-bold text-gray-200">{open_count} / {settled}</div></div>'
        f'</div>'
    )

    if not rows:
        return HTMLResponse(summary_html + '<div class="text-center py-12 text-gray-600 text-sm">No paper trades yet — the scheduler opens one whenever it sees a high-EV signal from a credible source.</div>')

    ledger_rows = []
    for t in rows:
        pnl = t.pnl_usd
        pcol = "text-green-400" if (pnl or 0) > 0 else "text-red-400" if (pnl or 0) < 0 else "text-gray-500"
        pnl_str = f"{pnl:+.2f}" if pnl is not None else ("OPEN" if not t.resolved else "—")
        side_color = "bg-green-500/20 text-green-400" if t.bet_side == "YES" else "bg-red-500/20 text-red-400"
        ledger_rows.append(
            f'<tr class="border-b border-white/5 hover:bg-white/[0.02]">'
            f'<td class="px-4 py-2.5 text-xs text-gray-400 max-w-[300px]"><div class="truncate">{_esc(t.market_slug)}</div></td>'
            f'<td class="px-4 py-2.5 text-xs"><span class="text-[10px] px-2 py-0.5 rounded-full {side_color}">BUY {t.bet_side}</span></td>'
            f'<td class="px-4 py-2.5 text-xs text-gray-400 font-mono">{t.entry_price:.2f}</td>'
            f'<td class="px-4 py-2.5 text-xs text-gray-400 font-mono">{t.entry_ev_score:+.2f}</td>'
            f'<td class="px-4 py-2.5 text-xs text-gray-400 font-mono">{t.entry_credibility:.2f}</td>'
            f'<td class="px-4 py-2.5 text-xs">@{_esc(t.handle)}</td>'
            f'<td class="px-4 py-2.5 font-mono text-xs {pcol}">{pnl_str}</td>'
            f'<td class="px-4 py-2.5 text-xs text-gray-500">{_time_ago(t.opened_at)}</td>'
            f'</tr>'
        )

    table_html = (
        '<div class="rounded-xl border border-white/5 overflow-hidden">'
        '<table class="w-full"><thead class="bg-white/[0.02] border-b border-white/5"><tr>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Market</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Side</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Entry</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">EV</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Cred</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Source</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">P&amp;L</th>'
        '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Opened</th>'
        '</tr></thead><tbody>' + "\n".join(ledger_rows) + '</tbody></table></div>'
    )

    return HTMLResponse(summary_html + table_html)


@app.get("/backtest")
async def backtest_endpoint(
    session: AsyncSession = Depends(get_session),
    _user: str = Depends(require_auth),
    min_ev: float = 0.10,
    min_credibility: float = 0.55,
    stake_usd: float = 1.0,
):
    """Replay every resolved prediction under the given thresholds and return P&L.

    Used by the Performance tab to let users tune the (EV, credibility) knobs
    against historical data before flipping them on for live paper-trading.
    """
    from app.processing.paper_trade import TradeFilter, backtest as run_backtest
    filt = TradeFilter(
        min_ev=max(0.0, min(1.0, min_ev)),
        min_credibility=max(0.0, min(1.0, min_credibility)),
        stake_usd=max(0.01, min(1000.0, stake_usd)),
    )
    return JSONResponse(await run_backtest(session, filt))


# ---------------------------------------------------------------------------
# Refresh / Health
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Public API — key-authenticated programmatic access to /api/v1/*.
# Quants build bots on top of our signals; user-facing UI doesn't need this.
# ---------------------------------------------------------------------------
_API_KEY_PREFIX = "narve_"


def _hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_api_key() -> str:
    """Generate a fresh user-facing API key. Shown once at creation, never persisted in plaintext."""
    return _API_KEY_PREFIX + secrets.token_urlsafe(32)


async def _require_api_user(request: Request) -> User:
    """API-key authentication. Reads `X-API-Key`, looks up the owning user.

    Distinct from the cookie-session middleware (which gates the HTML routes).
    """
    key = request.headers.get("X-API-Key", "").strip()
    if not key or not key.startswith(_API_KEY_PREFIX):
        raise HTTPException(status_code=401, detail="API key required")
    key_hash = _hash_api_key(key)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        ak_result = await session.exec(select(APIKey).where(APIKey.key_hash == key_hash, APIKey.revoked == False))  # noqa: E712
        ak = ak_result.first()
        if ak is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        u_result = await session.exec(select(User).where(User.id == ak.user_id))
        user = u_result.first()
        if user is None:
            raise HTTPException(status_code=401, detail="Owning user no longer exists")
        ak.last_used_at = datetime.now(timezone.utc)
        session.add(ak)
        await session.commit()
        return user


@app.get("/api/v1/signals")
async def api_signals(
    request: Request,
    limit: int = 50,
    min_ev: float = 0.0,
    min_credibility: float = 0.0,
    category: str = "",
    _user: User = Depends(_require_api_user),
):
    """Recent qualifying signals as a JSON feed. Stable schema for bot consumers."""
    limit = _clamp(limit, 1, 500)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            select(Prediction, RawPost)
            .join(RawPost, Prediction.raw_post_id == RawPost.id)
            .where(
                Prediction.ev_score.isnot(None),
                Prediction.ev_score >= min_ev,
                Prediction.global_credibility_at_time >= min_credibility,
                Prediction.risk_flag == False,  # noqa: E712
            )
            .order_by(Prediction.extracted_at.desc())
            .limit(limit)
        )
        if category:
            stmt = stmt.where(Prediction.category == category)
        rows = (await session.exec(stmt)).all()
    return JSONResponse({
        "count": len(rows),
        "filter": {"limit": limit, "min_ev": min_ev, "min_credibility": min_credibility, "category": category or None},
        "signals": [
            {
                "prediction_id": pred.id,
                "source": post.author_handle,
                "platform": post.platform,
                "category": pred.category,
                "predicted_outcome": pred.predicted_outcome,
                "bet_side": pred.bet_side,
                "predicted_probability": pred.predicted_probability,
                "market_implied_probability": pred.market_implied_probability,
                "ev_score": pred.ev_score,
                "source_credibility_at_time": pred.global_credibility_at_time,
                "category_credibility_at_time": pred.category_credibility_at_time,
                "market_slug": pred.market_slug,
                "market_question": pred.market_question,
                "market_close_time": pred.market_close_time.isoformat() if pred.market_close_time else None,
                "extracted_at": pred.extracted_at.isoformat() if pred.extracted_at else None,
            }
            for pred, post in rows
        ],
    })


@app.get("/api/v1/sources")
async def api_sources(
    limit: int = 100,
    min_credibility: float = 0.0,
    only_rated: bool = False,
    _user: User = Depends(_require_api_user),
):
    """Source leaderboard as JSON, sorted by credibility desc."""
    limit = _clamp(limit, 1, 1000)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = select(Source).where(Source.global_credibility >= min_credibility).order_by(Source.global_credibility.desc()).limit(limit)
        if only_rated:
            stmt = stmt.where(Source.accuracy_unlocked == True)  # noqa: E712
        rows = (await session.exec(stmt)).all()
    return JSONResponse({
        "count": len(rows),
        "sources": [
            {
                "handle": s.handle,
                "platform": s.platform,
                "global_credibility": s.global_credibility,
                "category_credibility": s.category_credibility,
                "accuracy": s.accuracy_global,
                "decay_weighted_accuracy": s.decay_weighted_accuracy,
                "brier_score": s.brier_score,
                "brier_n": s.brier_n,
                "qualifying_predictions": s.qualifying_predictions,
                "correct_qualifying": s.correct_qualifying,
                "accuracy_unlocked": s.accuracy_unlocked,
                "categories": s.categories_predicted_in,
                "trusted": s.trusted,
                "verified": s.verified,
                "follower_count": s.follower_count,
            }
            for s in rows
        ],
    })


@app.get("/api/v1/sources/{handle}")
async def api_source_detail(handle: str, _user: User = Depends(_require_api_user)):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        s = (await session.exec(select(Source).where(Source.handle == handle))).first()
        if s is None:
            raise HTTPException(status_code=404, detail="Source not found")
        # Last 30 credibility snapshots so callers can plot trend.
        snaps = (await session.exec(
            select(CredibilitySnapshot).where(CredibilitySnapshot.handle == handle)
            .order_by(CredibilitySnapshot.snapshotted_at.desc()).limit(30)
        )).all()
    return JSONResponse({
        "handle": s.handle,
        "platform": s.platform,
        "global_credibility": s.global_credibility,
        "category_credibility": s.category_credibility,
        "accuracy": s.accuracy_global,
        "brier_score": s.brier_score,
        "brier_n": s.brier_n,
        "qualifying_predictions": s.qualifying_predictions,
        "correct_qualifying": s.correct_qualifying,
        "accuracy_unlocked": s.accuracy_unlocked,
        "categories": s.categories_predicted_in,
        "trusted": s.trusted,
        "history": [
            {"at": sn.snapshotted_at.isoformat(), "credibility": sn.global_credibility}
            for sn in reversed(snaps)
        ],
    })


@app.get("/api/v1/backtest")
async def api_backtest(
    min_ev: float = 0.10,
    min_credibility: float = 0.55,
    stake_usd: float = 1.0,
    _user: User = Depends(_require_api_user),
):
    """Programmatic backtest. Same math as the UI button."""
    from app.processing.paper_trade import TradeFilter, backtest as run_backtest
    filt = TradeFilter(
        min_ev=max(0.0, min(1.0, min_ev)),
        min_credibility=max(0.0, min(1.0, min_credibility)),
        stake_usd=max(0.01, min(1000.0, stake_usd)),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return JSONResponse(await run_backtest(session, filt))


@app.get("/api/v1/arbitrage")
async def api_arbitrage(min_edge: float = 3.0, _user: User = Depends(_require_api_user)):
    from app.processing.arbitrage import find_arbs
    async with AsyncSession(engine, expire_on_commit=False) as session:
        arbs = await find_arbs(session, min_edge_pp=max(0.0, min(50.0, min_edge)))
    return JSONResponse({"count": len(arbs), "opportunities": [
        {
            "polymarket_slug": a.polymarket_slug,
            "kalshi_ticker": a.kalshi_ticker,
            "category": a.category,
            "poly_yes": a.poly_yes,
            "kalshi_yes": a.kalshi_yes,
            "edge_pp": a.edge_pp,
            "cheaper_venue": a.cheaper_venue,
            "match_score": a.match_score,
        }
        for a in arbs
    ]})


# ---------------------------------------------------------------------------
# Shareable source cards — public SVG images for embedding in social media.
# No auth: that's the whole point (the card is the marketing surface).
# ---------------------------------------------------------------------------
@app.get("/share/{handle}.svg")
async def share_source_card(handle: str):
    """Generate an SVG card with a source's credibility stats.

    Returned with a short cache-control so social media unfurlers see a fresh
    image but don't hammer the DB on every preview. Always returns a valid SVG
    (even for unknown handles) so embedding code doesn't break on 404s.
    """
    # Defensive cap on handle length — we render it verbatim into the SVG.
    handle = (handle or "").strip()[:64]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        s = (await session.exec(select(Source).where(Source.handle == handle))).first()

    if s is None:
        svg = _svg_unknown_card(handle)
    else:
        svg = _svg_source_card(s)

    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


def _svg_text_escape(value: str) -> str:
    """Escape `&<>"'` for safe inclusion in an SVG <text> body or attribute."""
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _svg_unknown_card(handle: str) -> str:
    safe = _svg_text_escape(handle)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200" viewBox="0 0 640 200">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0d1117"/><stop offset="1" stop-color="#1a1f29"/></linearGradient></defs>
  <rect width="640" height="200" fill="url(#g)" rx="16"/>
  <text x="320" y="100" text-anchor="middle" font-family="Inter, system-ui, sans-serif" font-size="18" fill="#8b949e">@{safe} is not tracked yet</text>
  <text x="320" y="130" text-anchor="middle" font-family="Inter, system-ui, sans-serif" font-size="11" fill="#484f58">narve.ai · truth research</text>
</svg>"""


def _svg_source_card(s: Source) -> str:
    """The headline credibility card. Designed to fit social-media link previews
    at 1.2:1 aspect (640×200) and read well at thumbnail size."""
    handle = _svg_text_escape(s.handle)[:24]
    cred_pct = max(0, min(100, int((s.global_credibility or 0) * 100)))
    cred_color = "#22c55e" if (s.global_credibility or 0) >= 0.7 else "#f59e0b" if (s.global_credibility or 0) >= 0.4 else "#ef4444"
    accuracy_str = f"{s.accuracy_global:.0%}" if s.accuracy_global is not None else "—"
    record_str = f"{s.correct_qualifying}/{s.qualifying_predictions}" if s.qualifying_predictions > 0 else "0/0"
    brier_str = f"{s.brier_score:.3f}" if s.brier_score is not None else "—"
    rated_badge = (
        '<rect x="476" y="20" width="68" height="22" rx="11" fill="#22c55e" fill-opacity="0.15" stroke="#22c55e" stroke-opacity="0.4"/>'
        '<text x="510" y="35" text-anchor="middle" font-family="Inter, system-ui, sans-serif" font-size="11" font-weight="600" fill="#22c55e">RATED</text>'
        if s.accuracy_unlocked else
        '<rect x="476" y="20" width="68" height="22" rx="11" fill="#484f58" fill-opacity="0.15" stroke="#484f58" stroke-opacity="0.4"/>'
        '<text x="510" y="35" text-anchor="middle" font-family="Inter, system-ui, sans-serif" font-size="11" font-weight="600" fill="#8b949e">UNRATED</text>'
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200" viewBox="0 0 640 200">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0d1117"/><stop offset="1" stop-color="#1a1f29"/></linearGradient>
    <linearGradient id="credbar" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{cred_color}" stop-opacity="0.3"/><stop offset="1" stop-color="{cred_color}"/></linearGradient>
  </defs>
  <rect width="640" height="200" fill="url(#bg)" rx="16"/>
  <text x="32" y="42" font-family="Inter, system-ui, sans-serif" font-size="22" font-weight="700" fill="#e6edf3">@{handle}</text>
  <text x="32" y="62" font-family="Inter, system-ui, sans-serif" font-size="11" fill="#8b949e">via narve.ai · truth research</text>
  {rated_badge}

  <text x="32" y="98" font-family="Inter, system-ui, sans-serif" font-size="10" letter-spacing="1.5" fill="#484f58">CREDIBILITY</text>
  <rect x="32" y="106" width="280" height="6" rx="3" fill="#1e2a3a"/>
  <rect x="32" y="106" width="{int(280 * cred_pct / 100)}" height="6" rx="3" fill="url(#credbar)"/>
  <text x="320" y="113" text-anchor="end" font-family="JetBrains Mono, monospace" font-size="13" fill="{cred_color}">{s.global_credibility:.2f}</text>

  <line x1="340" y1="90" x2="340" y2="160" stroke="#1e2a3a" stroke-width="1"/>

  <text x="360" y="98" font-family="Inter, system-ui, sans-serif" font-size="10" letter-spacing="1.5" fill="#484f58">ACCURACY</text>
  <text x="360" y="124" font-family="JetBrains Mono, monospace" font-size="20" font-weight="700" fill="#e6edf3">{accuracy_str}</text>
  <text x="360" y="142" font-family="Inter, system-ui, sans-serif" font-size="10" fill="#8b949e">{record_str} record</text>

  <text x="492" y="98" font-family="Inter, system-ui, sans-serif" font-size="10" letter-spacing="1.5" fill="#484f58">BRIER</text>
  <text x="492" y="124" font-family="JetBrains Mono, monospace" font-size="20" font-weight="700" fill="#e6edf3">{brier_str}</text>
  <text x="492" y="142" font-family="Inter, system-ui, sans-serif" font-size="10" fill="#8b949e">lower=better</text>

  <text x="32" y="180" font-family="Inter, system-ui, sans-serif" font-size="10" fill="#484f58">{len(s.categories_predicted_in or [])} categories · follow @narve_ai</text>
</svg>"""


# ---------------------------------------------------------------------------
# API key management (logged-in users mint and revoke keys from /profile).
# ---------------------------------------------------------------------------
@app.post("/api-keys/create")
async def api_key_create(request: Request, label: str = Form(""), csrf_token_field: str = Form("", alias="_csrf_token")):
    """Mint a new API key. Plaintext returned once and never stored."""
    if not _validate_csrf(request, csrf_token_field):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    user = await _get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    plaintext = _generate_api_key()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(APIKey(
            user_id=user.id,
            key_hash=_hash_api_key(plaintext),
            key_prefix=plaintext[:14],
            label=(label or "")[:64],
            created_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    # Show the plaintext exactly once. The user is responsible for capturing it.
    return RedirectResponse(f"/profile?new_key={urllib.parse.quote(plaintext)}", status_code=302)


@app.post("/api-keys/{key_id}/revoke")
async def api_key_revoke(key_id: int, request: Request, csrf_token_field: str = Form("", alias="_csrf_token")):
    if not _validate_csrf(request, csrf_token_field):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    user = await _get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id))
        ak = result.first()
        if ak is None:
            raise HTTPException(status_code=404, detail="Key not found")
        ak.revoked = True
        session.add(ak)
        await session.commit()
    return RedirectResponse("/profile", status_code=302)


# ---------------------------------------------------------------------------
# User calibration mode — let users record their own predictions and get
# their own Brier score. Network effect: every recorded prediction is also
# free training data for our extractor down the road.
# ---------------------------------------------------------------------------
@app.post("/me/predictions")
async def me_predict(
    request: Request,
    market_slug: str = Form(""),
    market_question: str = Form(""),
    category: str = Form("other"),
    predicted_probability: float = Form(0.5),
    note: str = Form(""),
    csrf_token_field: str = Form("", alias="_csrf_token"),
):
    if not _validate_csrf(request, csrf_token_field):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    user = await _get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    prob = max(0.0, min(1.0, predicted_probability))
    side = "YES" if prob >= 0.5 else "NO"

    # Snapshot the market's current implied price so we can score correctly.
    market_implied = None
    if market_slug:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await session.exec(
                select(MarketSnapshot).where(MarketSnapshot.market_slug == market_slug)
                .order_by(MarketSnapshot.snapshotted_at.desc()).limit(1)
            )
            ms = r.first()
            if ms is not None:
                market_implied = ms.yes_price
                if not market_question:
                    market_question = ms.market_question
                if category == "other" and ms.category:
                    category = ms.category

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(UserPrediction(
            user_id=user.id,
            market_slug=market_slug[:200],
            market_question=market_question[:500],
            category=category[:32],
            predicted_probability=prob,
            bet_side=side,
            market_implied_probability=market_implied,
            note=(note or "")[:1000],
            recorded_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    return RedirectResponse("/me/calibration", status_code=302)


@app.get("/me/calibration", response_class=HTMLResponse)
async def me_calibration(request: Request, _user: str = Depends(require_auth)):
    """Render the user's own Brier score + reliability curve + recent predictions."""
    user = await _get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        ups = (await session.exec(
            select(UserPrediction).where(UserPrediction.user_id == user.id)
            .order_by(UserPrediction.recorded_at.desc()).limit(200)
        )).all()

    # Compute Brier over the user's resolved predictions.
    from app.credibility.calibration import compute_calibration

    class _Proxy:
        def __init__(self, up):
            self.predicted_probability = up.predicted_probability
            self.predicted_outcome = up.bet_side  # "YES" / "NO" -> compute_calibration handles both
            self.resolved_correct = up.resolved_correct

    resolved = [_Proxy(u) for u in ups if u.resolved and u.resolved_correct is not None]
    calib = compute_calibration(resolved)
    csrf_token = _get_csrf_token(request)

    if not ups:
        body = (
            '<div class="text-center py-12 text-gray-600 text-sm">No predictions recorded yet. '
            'Make your first one above — over time you build a Brier-scored track record.</div>'
        )
    else:
        rows_html = []
        for up in ups[:50]:
            status = "✓" if up.resolved_correct else ("✗" if up.resolved else "—")
            sc = ("text-green-400" if up.resolved_correct else "text-red-400" if up.resolved else "text-gray-500")
            # Build the market-implied td separately — putting the conditional
            # inline in implicit string concatenation broke the <tr>/<td>
            # structure for rows where the field is None (Python parsed the
            # whole block as one expression and dropped the wrong half).
            if up.market_implied_probability is not None:
                mip_td = f'<td class="px-4 py-2.5 font-mono text-xs text-gray-400">{up.market_implied_probability:.0%}</td>'
            else:
                mip_td = '<td class="px-4 py-2.5 text-xs text-gray-600">—</td>'
            safe_cat = _esc(up.category)
            rows_html.append(
                f'<tr class="border-b border-white/5 hover:bg-white/[0.02]">'
                f'<td class="px-4 py-2.5 text-xs text-gray-300 max-w-[300px]"><div class="truncate">{_esc(up.market_question or up.market_slug)}</div></td>'
                f'<td class="px-4 py-2.5 text-xs"><span class="text-[10px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{safe_cat}</span></td>'
                f'<td class="px-4 py-2.5 font-mono text-xs text-gray-200">{up.predicted_probability:.0%}</td>'
                f'{mip_td}'
                f'<td class="px-4 py-2.5 text-xs font-bold {sc}">{status}</td>'
                f'<td class="px-4 py-2.5 text-xs text-gray-500">{_time_ago(up.recorded_at)}</td>'
                f'</tr>'
            )
        brier_str = f"{calib.brier_score:.3f}" if calib.brier_score is not None else "—"
        brier_color = ("text-green-400" if calib.brier_score is not None and calib.brier_score < 0.18
                       else "text-amber-400" if calib.brier_score is not None and calib.brier_score < 0.25
                       else "text-red-400" if calib.brier_score is not None else "text-gray-500")
        n_resolved = len(resolved)
        body = (
            f'<div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">'
            f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Your Brier</div><div class="{brier_color} text-2xl font-bold">{brier_str}</div><div class="text-xs text-gray-500 mt-1">n={n_resolved}</div></div>'
            f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Total predictions</div><div class="text-2xl font-bold text-gray-200">{len(ups)}</div></div>'
            f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Resolved</div><div class="text-2xl font-bold text-gray-200">{n_resolved}</div></div>'
            f'<div class="rounded-xl p-5 border border-white/5 themed-card"><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Open</div><div class="text-2xl font-bold text-gray-200">{len(ups) - n_resolved}</div></div>'
            f'</div>'
            '<div class="rounded-xl border border-white/5 overflow-hidden">'
            '<table class="w-full"><thead class="bg-white/[0.02] border-b border-white/5"><tr>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Market</th>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Cat</th>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Your P(YES)</th>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Mkt</th>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">Result</th>'
            '<th class="px-4 py-2.5 text-left text-[10px] uppercase tracking-wider text-gray-500">When</th>'
            '</tr></thead><tbody>' + "\n".join(rows_html) + '</tbody></table></div>'
        )

    form_html = (
        f'<form method="POST" action="/me/predictions" class="rounded-xl border border-white/5 themed-card p-5 mb-6 grid grid-cols-1 md:grid-cols-5 gap-3 items-end">'
        f'<input type="hidden" name="_csrf_token" value="{csrf_token}">'
        '<div class="md:col-span-2"><label class="text-[10px] uppercase tracking-wider text-gray-500 mb-1 block">Market slug or URL</label>'
        '<input name="market_slug" placeholder="trump-2028-election" class="w-full themed-card border themed-border rounded-lg px-2.5 py-1.5 text-xs" style="color:var(--text-primary)" required></div>'
        '<div><label class="text-[10px] uppercase tracking-wider text-gray-500 mb-1 block">Category</label>'
        '<select name="category" class="w-full themed-card border themed-border rounded-lg px-2.5 py-1.5 text-xs" style="color:var(--text-primary)">'
        '<option value="other">other</option><option value="politics">politics</option><option value="sports">sports</option>'
        '<option value="crypto">crypto</option><option value="geopolitics">geopolitics</option></select></div>'
        '<div><label class="text-[10px] uppercase tracking-wider text-gray-500 mb-1 block">P(YES)</label>'
        '<input type="number" step="0.01" min="0" max="1" name="predicted_probability" value="0.55" class="w-full themed-card border themed-border rounded-lg px-2.5 py-1.5 text-xs" style="color:var(--text-primary)"></div>'
        '<button type="submit" class="accent-bg text-white text-xs font-medium px-4 py-2 rounded-lg">Record</button></form>'
    )

    page = (
        '<!DOCTYPE html><html><head><title>My Calibration · narve.ai</title>'
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<style>:root{--bg-primary:#0d1117;--text-primary:#e6edf3;--text-secondary:#8b949e;--text-muted:#484f58;--border:#1e2a3a;--bg-secondary:#161b22;--bg-card:rgba(255,255,255,0.02);--accent:#2D64F3;}'
        '.themed-card{background:var(--bg-card);border-color:var(--border);} .themed-border{border-color:var(--border);} .accent-bg{background:var(--accent);}'
        'body{background:var(--bg-primary);color:var(--text-primary);font-family:Inter,system-ui,sans-serif;}</style></head>'
        '<body class="min-h-screen p-6"><div class="max-w-6xl mx-auto">'
        '<div class="flex items-center justify-between mb-6">'
        '<h1 class="text-xl font-semibold">My Calibration</h1>'
        '<a href="/" class="text-xs text-gray-500 hover:text-gray-300">← Back to dashboard</a>'
        '</div>'
        f'{form_html}{body}</div></body></html>'
    )
    return HTMLResponse(page)


# Per-user cooldown on the manual refresh button so a user can't repeatedly
# trigger pipeline runs (each run hits Polymarket/Kalshi/Twitter externally).
_REFRESH_COOLDOWN_SECONDS = 60
_last_refresh_at: dict[str, float] = {}


@app.get("/refresh", response_class=HTMLResponse)
async def refresh(request: Request, _user: str = Depends(require_auth)):
    global _last_run_stats
    user_key = _user or "anon"
    now = time.time()
    last = _last_refresh_at.get(user_key, 0.0)
    wait = _REFRESH_COOLDOWN_SECONDS - (now - last)
    if wait > 0:
        return HTMLResponse(f'<div class="text-amber-400 text-xs">Cooldown: try again in {int(wait)}s</div>')
    _last_refresh_at[user_key] = now
    try:
        from app.scheduler import run_pipeline
        _last_run_stats = await run_pipeline()
        s = _last_run_stats
        ec = len(s.get("errors", []))
        opens = s.get("paper_trades_opened", 0)
        opens_html = f'<span class="text-gray-500">{opens} trades</span>' if opens else ""
        return HTMLResponse(f'<div class="flex items-center gap-3 text-xs"><span class="text-green-400">&#10003; Synced</span><span class="text-gray-500">{s.get("posts_fetched",0)} posts</span><span class="text-gray-500">{s.get("predictions_extracted",0)} preds</span><span class="text-gray-500">{s.get("markets_synced",0)} mkts</span>{opens_html}{"<span class=text-red-400>" + str(ec) + " err</span>" if ec else ""}</div>')
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_esc(str(exc))}</div>')


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    pt = (await session.exec(select(func.count()).select_from(Prediction))).first() or 0
    st = (await session.exec(select(func.count()).select_from(Source))).first() or 0
    uc = (await session.exec(select(func.count()).select_from(Source).where(Source.accuracy_unlocked == True))).first() or 0  # noqa: E712
    mk = datetime.now(timezone.utc).strftime("%Y-%m")
    qr = (await session.exec(select(MonthlyQuota).where(MonthlyQuota.platform == "twitter", MonthlyQuota.year_month == mk))).first()
    tu = qr.tweets_read if qr else 0
    return JSONResponse({"status": "ok", "last_run": _last_run_stats.get("run_at"), "predictions_total": pt, "sources_total": st, "accuracy_unlocked_count": uc, "twitter_quota_remaining": settings.get("TWITTER_MONTHLY_QUOTA", 500) - tu})
