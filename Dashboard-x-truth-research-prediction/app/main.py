from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import html as html_mod
import logging
import os
import re as _re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select

from app.config import settings, yaml_config
from app.db import AsyncSession, engine, get_session, init_db
from app.models import (
    CredibilitySnapshot, MarketSnapshot, MonthlyQuota, Prediction, RawPost, Source, SourcePredictionRecord, User,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, settings.get("LOG_LEVEL", "INFO")), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ONE_YEAR_FROM_NOW = lambda: datetime.now(timezone.utc) + timedelta(days=365)
_last_run_stats: dict = {}
_SESSION_SECRET = secrets.token_hex(32)
_CSRF_SECRET = secrets.token_hex(16)

# ---------------------------------------------------------------------------
# Fernet symmetric encryption for sensitive fields (e.g. truthsocial_password)
# ---------------------------------------------------------------------------
def _get_or_create_encryption_key() -> str:
    """Get encryption key from env, or generate and persist to a local file."""
    key = os.environ.get("ENCRYPTION_KEY")
    if key:
        return key
    key_file = Path(__file__).parent.parent / ".encryption_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = Fernet.generate_key().decode()
    try:
        key_file.write_text(key)
        key_file.chmod(0o600)
    except OSError:
        pass  # non-fatal -- key still usable for this process lifetime
    return key


_ENCRYPTION_KEY = _get_or_create_encryption_key()
_fernet = Fernet(_ENCRYPTION_KEY if isinstance(_ENCRYPTION_KEY, bytes) else _ENCRYPTION_KEY.encode())


def _encrypt_field(value: str) -> str:
    """Encrypt a string value. Returns empty string for empty input."""
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def _decrypt_field(value: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns original if decryption fails (legacy plaintext)."""
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except Exception:
        # Legacy plaintext value — return as-is
        return value

# Rate limiting: track login attempts per IP
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Create default admin user if none exist
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(func.count()).select_from(User))
        if (result.first() or 0) == 0:
            admin_user = settings.get("DASHBOARD_USER", "admin")
            admin_pass = settings.get("DASHBOARD_PASSWORD", "changeme")
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
templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent / "templates"))


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
    if "$" not in stored:
        # Legacy SHA256 hash — verify and return True to allow migration
        return hashlib.sha256(password.encode()).hexdigest() == stored
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(_hash_password(password, salt), stored)


def _make_session_token() -> str:
    """Generate a random session token (non-deterministic)."""
    return secrets.token_urlsafe(48)


def _make_csrf_token(session_token: str) -> str:
    """Generate CSRF token tied to the session."""
    return hashlib.sha256(f"{session_token}:{_CSRF_SECRET}".encode()).hexdigest()[:32]


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
_active_sessions: dict[str, tuple[str, float]] = {}
_SESSION_MAX_AGE = 86400 * 7  # 7 days, matches cookie max_age


def _prune_expired_sessions() -> None:
    """Remove sessions older than _SESSION_MAX_AGE."""
    now = time.time()
    expired = [t for t, (_, ts) in _active_sessions.items() if now - ts > _SESSION_MAX_AGE]
    for t in expired:
        del _active_sessions[t]


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
    return ""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    public_paths = {"/login", "/register", "/forgot-password", "/health", "/favicon.ico"}
    if request.url.path in public_paths:
        return await call_next(request)
    if request.url.path in ("/login", "/register") and request.method == "POST":
        return await call_next(request)
    token = request.cookies.get("session")
    if token and token in _active_sessions:
        return await call_next(request)
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Login / Register / Logout
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", msg: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "msg": msg})


@app.post("/login")
async def login_submit(request: Request, session: AsyncSession = Depends(get_session), username: str = Form(""), password: str = Form(""), start_platform: str = Form("polymarket")):
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many login attempts. Try again in 5 minutes."})

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
        _active_sessions[token] = (username, time.time())
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session", token, httponly=True, samesite="strict", max_age=86400 * 7, secure=False)
        return resp
    # Record failed attempt
    _login_attempts[client_ip].append(time.time())
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = ""):
    return templates.TemplateResponse("register.html", {"request": request, "error": error})


@app.post("/register")
async def register_submit(request: Request, session: AsyncSession = Depends(get_session), username: str = Form(""), email: str = Form(""), password: str = Form(""), password2: str = Form(""), start_platform: str = Form("polymarket")):
    if len(username) < 3 or len(username) > 15:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username must be 3–15 characters"})
    import re as _re
    if len(password) < 12 or not _re.search(r"[A-Z]", password) or not _re.search(r"[a-z]", password) or not _re.search(r"[0-9]", password):
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must be at least 12 characters with an uppercase letter, lowercase letter, and number"})
    if password != password2:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords don't match"})
    existing = await session.exec(select(User).where(User.username == username))
    if existing.first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username already taken"})
    if email:
        existing_email = await session.exec(select(User).where(User.email == email))
        if existing_email.first():
            return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"})
    session.add(User(username=username, email=email, password_hash=_hash_password(password), preferred_platform=start_platform if start_platform in ("polymarket", "kalshi") else "polymarket", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
    await session.commit()
    return RedirectResponse("/login?msg=Account+created.+Sign+in+below.", status_code=302)


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token and token in _active_sessions:
        del _active_sessions[token]
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    user = await _get_current_user_from_session(session, request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    preferred_platform = getattr(user, "preferred_platform", None) or "polymarket"
    preferred_theme = getattr(user, "preferred_theme", None) or "dark"
    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "msg": "", "error": "", "preferred_platform": preferred_platform, "preferred_theme": preferred_theme, "ts_password_decrypted": _decrypt_field(user.truthsocial_password)})


@app.post("/profile/update", response_class=HTMLResponse)
async def profile_update(request: Request, confirm_password: str = Form(""), new_username: str = Form(""), email: str = Form(""), twitter_bearer_token: str = Form(""), truthsocial_username: str = Form(""), truthsocial_password: str = Form(""), truthsocial_access_token: str = Form(""), preferred_platform: str = Form("polymarket"), preferred_theme: str = Form("dark")):
    user = await _get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            return RedirectResponse("/login", status_code=302)

        # Require password to save profile changes
        if not _verify_password(confirm_password, db_user.password_hash):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Enter your current password to save changes", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})

        if new_username and new_username != db_user.username:
            if len(new_username) < 3 or len(new_username) > 15:
                return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Username must be 3\u201315 characters", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
            existing = await session.exec(select(User).where(User.username == new_username))
            if existing.first():
                return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Username already taken", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
            old_username = db_user.username
            db_user.username = new_username

        db_user.email = email
        db_user.twitter_bearer_token = twitter_bearer_token
        db_user.truthsocial_username = truthsocial_username
        db_user.truthsocial_password = _encrypt_field(truthsocial_password)
        db_user.truthsocial_access_token = truthsocial_access_token
        db_user.preferred_platform = preferred_platform
        db_user.preferred_theme = preferred_theme
        db_user.updated_at = datetime.now(timezone.utc)
        session.add(db_user)
        await session.commit()

        resp = templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "Profile updated", "error": "", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
        if new_username and new_username != user.username:
            # Update session for new username
            token = request.cookies.get("session")
            if token and token in _active_sessions:
                _, ts = _active_sessions[token]
                _active_sessions[token] = (new_username, ts)
        return resp


@app.post("/profile/password", response_class=HTMLResponse)
async def profile_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), new_password2: str = Form("")):
    user = await _get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(select(User).where(User.id == user.id))
        db_user = result.first()
        if not db_user:
            return RedirectResponse("/login", status_code=302)
        if not _verify_password(current_password, db_user.password_hash):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Current password is incorrect", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
        if len(new_password) < 12 or not _re.search(r"[A-Z]", new_password) or not _re.search(r"[a-z]", new_password) or not _re.search(r"[0-9]", new_password):
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "Password must be 12+ chars with uppercase, lowercase, and a number", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
        if new_password != new_password2:
            return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "", "error": "New passwords don't match", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})
        db_user.password_hash = _hash_password(new_password)
        db_user.updated_at = datetime.now(timezone.utc)
        session.add(db_user)
        await session.commit()
        return templates.TemplateResponse("profile.html", {"request": request, "user": db_user, "msg": "Password changed", "error": "", "preferred_platform": getattr(db_user, "preferred_platform", "polymarket"), "preferred_theme": getattr(db_user, "preferred_theme", "dark"), "ts_password_decrypted": _decrypt_field(db_user.truthsocial_password)})


# ---------------------------------------------------------------------------
# Preferences (HTMX toggle endpoint)
# ---------------------------------------------------------------------------
@app.post("/preferences")
async def update_preferences(request: Request):
    user = await _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()
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
    return templates.TemplateResponse("dashboard.html", {"request": request, "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "missing_keys": missing, "user": user.username if user else _user or "anonymous", "preferred_platform": preferred_platform, "preferred_theme": preferred_theme})


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
            rh = f'<span class="text-amber-400 cursor-help" title="{_esc(", ".join(pred.risk_reasons))}"><svg class="w-3.5 h-3.5 inline" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.168 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/></svg></span>'
        pl = '<span class="text-blue-400 text-xs">X</span>' if post.platform == "twitter" else '<span class="text-purple-400 text-xs">TS</span>'
        mk = f'<a href="https://polymarket.com/event/{pred.market_slug}" target="_blank" class="text-blue-400 hover:text-blue-300">{_esc((pred.market_question or "")[:45])}</a>' if pred.market_slug else '<span class="text-gray-600">\u2014</span>'
        html_rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02]"><td class="px-4 py-3 max-w-[280px]"><div class="truncate text-gray-300">{_esc(pred.predicted_outcome)}: {_esc(post.content[:70])}</div></td><td class="px-4 py-3 text-sm text-gray-400">@{_esc(post.author_handle)}</td><td class="px-4 py-3 text-center">{pl}</td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{pred.category}</span></td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full {cc}">{pred.global_credibility_at_time:.2f}</span></td><td class="px-4 py-3 font-mono text-sm {ec}">{ev}</td><td class="px-4 py-3 text-center">{rh}</td><td class="px-4 py-3 text-xs">{mk}</td><td class="px-4 py-3 text-xs text-gray-500">{_time_ago(pred.extracted_at)}</td></tr>')
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
        cards.append(f'<div class="relative bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-white/5 hover:border-white/10 transition-all">{rk}<h3 class="text-sm font-semibold text-gray-200 mb-3 pr-4">{_esc((pred.market_question or "Unmatched")[:80])}</h3><div class="flex items-center gap-2 mb-4 text-xs text-gray-500"><span class="font-medium text-gray-300">{_esc(pred.predicted_outcome)}</span><span>&middot;</span><span>@{_esc(post.author_handle)}</span><span class="px-1.5 py-0.5 rounded bg-white/5">{pl}</span></div><div class="{evc} text-3xl font-bold tracking-tight mb-4">{pred.ev_score:+.2f}<span class="text-sm font-normal text-gray-500 ml-1">EV</span></div><div class="grid grid-cols-2 gap-4 mb-4"><div><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Global</div><div class="w-full bg-gray-700/50 rounded-full h-1.5"><div class="{_cred_bar_color(gc)} h-1.5 rounded-full" style="width:{int(gc*100)}%"></div></div><div class="text-xs mt-1 text-gray-400">{gc:.2f}</div></div><div><div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">Category</div><div class="w-full bg-gray-700/50 rounded-full h-1.5"><div class="{_cred_bar_color(catc)} h-1.5 rounded-full" style="width:{int(catc*100)}%"></div></div><div class="text-xs mt-1 text-gray-400">{catc:.2f}</div></div></div><div class="flex justify-between text-xs text-gray-400 mb-3 py-2 border-t border-white/5"><span>Market: <span class="text-gray-300">{mp}</span></span><span>Predicted: <span class="text-gray-300">{pp}</span></span><span>Record: <span class="text-gray-300">{acc}</span></span></div><div class="flex justify-end">{poly}</div></div>')
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
        return HTMLResponse('<tr><td colspan="8" class="text-center py-16 text-gray-600">No sources tracked yet.</td></tr>')
    rows = []
    for rank, s in enumerate(all_sources, 1):
        cb = _cred_bar_color(s.global_credibility)
        pl = '<span class="text-blue-400">X</span>' if s.platform == "twitter" else '<span class="text-purple-400">TS</span>'
        acc = f"{s.accuracy_global:.0%}" if s.accuracy_global is not None else "\u2014"
        rec = f"{s.correct_qualifying}/{s.qualifying_predictions}" if s.qualifying_predictions > 0 else "0/0"
        cp = "".join(f'<span class="text-[10px] px-1.5 py-0.5 rounded-full {_cred_color(v)}">{c[:4]}</span> ' for c in s.categories_predicted_in[:5] if (v := s.category_credibility.get(c)) is not None)
        th = '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/20 text-green-400">Trusted</span>' if s.trusted is True else '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-red-500/20 text-red-400">Untrusted</span>' if s.trusted is False else ""
        st = '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/10 text-green-400 border border-green-500/20">Rated</span>' if s.accuracy_unlocked else '<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-500/10 text-gray-500 border border-gray-500/20">Unrated</span>'
        rk = ['', '<span class="text-lg">&#129351;</span>', '<span class="text-lg">&#129352;</span>', '<span class="text-lg">&#129353;</span>']
        rk_html = rk[rank] if rank <= 3 else f'<span class="text-sm text-gray-500 font-mono">{rank}</span>'
        tb = f'<div class="flex gap-1"><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": true}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs {"bg-green-600 text-white" if s.trusted is True else "bg-white/5 text-gray-500 hover:bg-white/10"}">+</button><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": false}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs {"bg-red-600 text-white" if s.trusted is False else "bg-white/5 text-gray-500 hover:bg-white/10"}">-</button><button hx-post="/sources/{s.handle}/trust" hx-vals=\'{{"trusted": null}}\' hx-target="#leaderboard-content" hx-swap="innerHTML" class="w-6 h-6 rounded flex items-center justify-center text-xs bg-white/5 text-gray-500 hover:bg-white/10">&#8635;</button></div>'
        rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02] group"><td class="px-4 py-3 text-center w-12">{rk_html}</td><td class="px-4 py-3"><div class="flex items-center gap-2"><span class="font-medium text-gray-200">@{_esc(s.handle)}</span>{pl}{th}{st}</div></td><td class="px-4 py-3 w-40"><div class="flex items-center gap-2"><div class="flex-1 bg-gray-700/30 rounded-full h-2"><div class="{cb} h-2 rounded-full" style="width:{int(s.global_credibility*100)}%"></div></div><span class="text-sm font-mono text-gray-300 w-10 text-right">{s.global_credibility:.2f}</span></div></td><td class="px-4 py-3 text-sm text-gray-400">{acc}</td><td class="px-4 py-3 text-sm text-gray-400 font-mono">{rec}</td><td class="px-4 py-3"><div class="flex gap-1 flex-wrap">{cp}</div></td><td class="px-4 py-3 text-xs text-gray-500">{s.follower_count:,}</td><td class="px-4 py-3 opacity-0 group-hover:opacity-100 transition-opacity">{tb}</td></tr>')
    return HTMLResponse("\n".join(rows))


@app.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    return await leaderboard(request, session, _user)


@app.post("/sources/{handle}/trust", response_class=HTMLResponse)
async def update_trust(handle: str, request: Request, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    body = await request.json()
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
    labels = [s.snapshotted_at.strftime("%m/%d") for s in snapshots]
    values = [s.global_credibility for s in snapshots]
    cid = f"spark-{handle.replace('.', '-').replace('@', '')}"
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
        rows.append(f'<tr class="border-b border-white/5 hover:bg-white/[0.02] cursor-pointer" hx-get="/feed?market={_esc(m.market_question[:50])}" hx-target="#feed-body" hx-swap="innerHTML" onclick="switchTab(\'feed\')"><td class="px-4 py-3 max-w-[300px]"><div class="truncate text-gray-300">{_esc(m.market_question[:65])}</div></td><td class="px-4 py-3"><span class="text-[11px] px-2 py-0.5 rounded-full bg-white/5 text-gray-400">{m.category}</span></td><td class="px-4 py-3"><div class="flex items-center gap-2"><div class="w-16 bg-gray-700/30 rounded-full h-1.5"><div class="bg-[#2D64F3] h-1.5 rounded-full" style="width:{int(m.yes_price*100)}%"></div></div><span class="font-mono text-sm {pc}">{m.yes_price:.0%}</span></div></td><td class="px-4 py-3 text-sm text-gray-400">${m.volume_usd:,.0f}</td><td class="px-4 py-3 text-xs text-gray-500">{close_str}</td><td class="px-4 py-3"><button hx-get="/markets/{m.market_slug}/chart" hx-target="#market-chart" hx-swap="innerHTML" class="text-xs text-blue-400 hover:text-blue-300">Chart</button></td></tr>')

    # Pagination row
    prev_btn = f'<button hx-get="/markets?page={page-1}&per_page={per_page}&category={category}&search={search}&sort={sort}" hx-target="#markets-body" hx-swap="innerHTML" hx-include=".mkt-filter" class="px-2 py-1 rounded bg-white/5 text-gray-400 hover:bg-white/10 text-xs">&laquo; Prev</button>' if page > 1 else '<span class="px-2 py-1 text-xs text-gray-700">&laquo; Prev</span>'
    next_btn = f'<button hx-get="/markets?page={page+1}&per_page={per_page}&category={category}&search={search}&sort={sort}" hx-target="#markets-body" hx-swap="innerHTML" hx-include=".mkt-filter" class="px-2 py-1 rounded bg-white/5 text-gray-400 hover:bg-white/10 text-xs">Next &raquo;</button>' if page < total_pages else '<span class="px-2 py-1 text-xs text-gray-700">Next &raquo;</span>'
    rows.append(f'<tr><td colspan="6" class="px-4 py-3"><div class="flex items-center justify-between"><span class="text-xs text-gray-500">{total} markets &middot; Page {page} of {total_pages}</span><div class="flex gap-2">{prev_btn}{next_btn}</div></div></td></tr>')

    return HTMLResponse("\n".join(rows))


@app.get("/markets/{slug:path}/chart", response_class=HTMLResponse)
async def market_chart(slug: str, session: AsyncSession = Depends(get_session), _user: str = Depends(require_auth)):
    result = await session.exec(select(MarketSnapshot).where(MarketSnapshot.market_slug == slug).order_by(MarketSnapshot.snapshotted_at.asc()).limit(100))
    snapshots = result.all()
    if not snapshots:
        return HTMLResponse('<div class="text-gray-600 text-sm p-4">No history.</div>')
    labels = [s.snapshotted_at.strftime("%m/%d %H:%M") for s in snapshots]
    prices = [s.yes_price for s in snapshots]
    title = _esc(snapshots[0].market_question[:60])
    return HTMLResponse(f'<div class="mt-4 bg-gray-800/50 rounded-xl p-5 border border-white/5"><h4 class="text-sm font-semibold text-gray-300 mb-3">{title}</h4><canvas id="market-odds-chart" height="120"></canvas><script>if(window._mktChart)window._mktChart.destroy();window._mktChart=new Chart(document.getElementById("market-odds-chart"),{{type:"line",data:{{labels:{labels},datasets:[{{label:"Yes",data:{prices},borderColor:"#2D64F3",borderWidth:2,fill:true,backgroundColor:"rgba(45,100,243,0.08)",tension:0.3,pointRadius:1}}]}},options:{{responsive:true,plugins:{{legend:{{display:false}}}},scales:{{y:{{min:0,max:1,grid:{{color:"rgba(255,255,255,0.03)"}},ticks:{{color:"#6b7280"}}}},x:{{grid:{{display:false}},ticks:{{color:"#6b7280",maxRotation:45}}}}}}}}}});</script></div>')


# ---------------------------------------------------------------------------
# Refresh / Health
# ---------------------------------------------------------------------------
@app.get("/refresh", response_class=HTMLResponse)
async def refresh(_user: str = Depends(require_auth)):
    global _last_run_stats
    try:
        from app.scheduler import run_pipeline
        _last_run_stats = await run_pipeline()
        s = _last_run_stats
        ec = len(s.get("errors", []))
        return HTMLResponse(f'<div class="flex items-center gap-3 text-xs"><span class="text-green-400">&#10003; Synced</span><span class="text-gray-500">{s.get("posts_fetched",0)} posts</span><span class="text-gray-500">{s.get("predictions_extracted",0)} preds</span><span class="text-gray-500">{s.get("markets_synced",0)} mkts</span>{"<span class=text-red-400>" + str(ec) + " err</span>" if ec else ""}</div>')
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
    return JSONResponse({"status": "ok", "last_run": _last_run_stats.get("run_at"), "predictions_total": pt, "sources_total": st, "accuracy_unlocked_count": uc, "twitter_quota_remaining": settings["TWITTER_MONTHLY_QUOTA"] - tu})
