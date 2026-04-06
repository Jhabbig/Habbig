#!/usr/bin/env python3
"""
Polymarket Dashboard Gateway
============================
Single entry point for all dashboards. Routes by subdomain:

    habbig.com              → apex (login, signup, "my dashboards", billing)
    <subdomain>.habbig.com  → reverse-proxied to the matching local dashboard

Session cookie is scoped to `.habbig.com` so one login covers every subdomain.
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
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, Response, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DOMAIN: str = CONFIG["domain"]
GATEWAY_PORT: int = CONFIG["gateway_port"]
DASHBOARDS: dict = CONFIG["dashboards"]

# Build reverse lookup: subdomain → dashboard_key
SUBDOMAIN_TO_KEY = {cfg["subdomain"]: key for key, cfg in DASHBOARDS.items()}

# Production flag: set PRODUCTION=1 on the deployed server. Disables the
# localhost dev bypass and flips the session cookie to secure=True.
IS_PRODUCTION: bool = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")

COOKIE_NAME = "pm_gateway_session"
# Leading dot makes the cookie apply to every subdomain.
# Computed per-request below to support both production (.habbig.com) and
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
      * Otherwise → None (safest fallback).
    """
    host = request.headers.get("host", "").split(":")[0].lower()
    if not host:
        return None
    if host == DOMAIN or host.endswith("." + DOMAIN):
        return PROD_COOKIE_DOMAIN
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


@app.on_event("startup")
async def _startup():
    global HTTP_CLIENT
    HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
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
        log.info("  FIRST ADMIN INVITE TOKEN: %s", first_token)
        log.info("=" * 50)


@app.on_event("shutdown")
async def _shutdown():
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()


# Static files for apex pages (CSS, JS, images).
if STATIC_DIR.exists():
    app.mount("/_gateway_static", StaticFiles(directory=str(STATIC_DIR)), name="gateway_static")


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


def current_user(request: Request) -> Optional[dict]:
    """Return a dict describing the current session user, or None.

    Always returns a plain dict (never a sqlite3.Row) so callers can use
    ``.get()`` and ``["key"]`` uniformly. Keys:
        user_id, email, is_admin, _dev_bypass (optional)
    """
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


def render_page(name: str, **context) -> HTMLResponse:
    """Tiny templating: load static/<name>.html and do {{ key }} substitution.

    Keys prefixed with ``raw_`` are inserted verbatim (used for pre-escaped
    server-side HTML). All other values are HTML-escaped before insertion.
    For convenience, the well-known keys ``dashboard_cards`` and
    ``billing_rows`` are also treated as raw.
    """
    path = STATIC_DIR / f"{name}.html"
    page = path.read_text()
    # Auto-fill empty raw_admin_link if not provided (prevents {{ raw_admin_link }} showing)
    if "raw_admin_link" not in context:
        context["raw_admin_link"] = ""
    raw_keys = {"dashboard_cards", "billing_rows"}
    for key, value in context.items():
        placeholder = "{{ " + key + " }}"
        if key in raw_keys or key.startswith("raw_"):
            page = page.replace(placeholder, str(value))
        else:
            page = page.replace(placeholder, html.escape(str(value)))
    return HTMLResponse(page)


# ── Apex routes (login / signup / my dashboards / billing) ────────────────────


@app.get("/", response_class=HTMLResponse)
async def apex_root(request: Request):
    sub = get_subdomain(request)
    if sub:
        # Subdomain request — delegate to the proxy handler below.
        return await proxy_request(request, "/")

    user = current_user(request)
    if not user:
        # Logged-out visitors see the marketing / onboarding landing page so
        # they understand what the product is before we ask for an email.
        return _render_landing()

    # Logged-in: honor the user's configured default dashboard if they have
    # an active subscription for it. Otherwise fall through to the hub.
    pref = db.get_default_dashboard(user["user_id"])
    if pref and pref in DASHBOARDS and db.has_active_subscription(user["user_id"], pref):
        return RedirectResponse(
            f"https://{DOMAIN}/" if False else f"https://{DASHBOARDS[pref]['subdomain']}.{DOMAIN}/",
            status_code=302,
        )
    return RedirectResponse("/dashboards", status_code=302)


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


@app.get("/gate", response_class=HTMLResponse)
async def gate_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    return render_page("gate", error="")


@app.post("/gate")
async def gate_submit(request: Request, token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/gate")
    token = token.strip()
    if not token:
        return render_page("gate", error="Please enter an invite token.")
    invite = db.get_invite_token(token)
    if not invite or invite["status"] == "revoked":
        return render_page("gate", error="Invalid or revoked token.")
    if invite["status"] == "claimed":
        email_hint = db.mask_email(invite["claimed_by_email"] or "")
        return render_page("login", error="", invite_token=invite["token"], email_hint=email_hint)
    # Unclaimed — go to signup
    return render_page("signup", error="", invite_token=invite["token"])


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")
    # Must come through gate with a token
    return RedirectResponse("/gate", status_code=302)


@app.post("/login")
async def login_submit(request: Request, identifier: str = Form(""), password: str = Form(...), invite_token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")

    # Validate invite token
    invite_token = invite_token.strip()
    invite = db.get_invite_token(invite_token) if invite_token else None
    if not invite or invite["status"] != "claimed":
        return render_page("gate", error="Invalid or expired token. Please enter your invite token again.")

    email_hint = db.mask_email(invite["claimed_by_email"] or "")

    # Look up user by email or username
    identifier = identifier.strip()
    if not identifier:
        return render_page("login", error="Please enter your username or email.", invite_token=invite_token, email_hint=email_hint)
    user = db.get_user_by_email_or_username(identifier)

    if not user:
        return render_page("login", error="Account not found.", invite_token=invite_token, email_hint=email_hint)
    # Token A can only log into User A — enforce token-to-user binding
    if invite["claimed_by_user_id"] != user["id"]:
        return render_page("login", error="This token does not belong to that account.", invite_token=invite_token, email_hint=email_hint)
    if user["suspended"]:
        return render_page("login", error="Error 226: This account has been suspended. No further action available.", invite_token=invite_token, email_hint=email_hint)
    if not db.verify_password(password, user["password_hash"], user["password_salt"]):
        return render_page("login", error="Invalid password.", invite_token=invite_token, email_hint=email_hint)
    token = db.create_session(user["id"])
    response = RedirectResponse("/dashboards", status_code=302)
    set_session_cookie(response, token, request)
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    # Direct access without a token — redirect to gate
    return RedirectResponse("/gate", status_code=302)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.post("/signup")
async def signup_submit(request: Request, username: str = Form(""), email: str = Form(...), password: str = Form(...), invite_token: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")

    invite_token = invite_token.strip()
    invite = db.get_invite_token(invite_token) if invite_token else None
    if not invite or invite["status"] != "unclaimed":
        return render_page("gate", error="Invalid or already used invite token. Please enter a valid token.")

    username = username.strip()
    if not username or not USERNAME_RE.match(username):
        return render_page("signup", error="Username must be 3\u201320 characters: letters, numbers, underscores only.", invite_token=invite_token)
    if db.get_user_by_username(username):
        return render_page("signup", error="That username is already taken.", invite_token=invite_token)

    email = (email or "").lower().strip()
    if not is_valid_email(email):
        return render_page("signup", error="Enter a valid email address.", invite_token=invite_token)
    if len(password) < 12:
        return render_page("signup", error="Password must be at least 12 characters.", invite_token=invite_token)
    if len(password) > 256:
        return render_page("signup", error="Password is too long.", invite_token=invite_token)
    if not re.search(r"[A-Z]", password):
        return render_page("signup", error="Password must contain at least one uppercase letter.", invite_token=invite_token)
    if not re.search(r"[a-z]", password):
        return render_page("signup", error="Password must contain at least one lowercase letter.", invite_token=invite_token)
    if not re.search(r"[0-9]", password):
        return render_page("signup", error="Password must contain at least one number.", invite_token=invite_token)
    if not re.search(r"[^A-Za-z0-9]", password):
        return render_page("signup", error="Password must contain at least one special character.", invite_token=invite_token)
    if db.get_user_by_email(email):
        return render_page("signup", error="An account with that email already exists.", invite_token=invite_token)
    user_id = db.create_user(email, password, username=username)
    db.claim_invite_token(invite_token, user_id, email)
    token = db.create_session(user_id)
    response = RedirectResponse("/dashboards", status_code=302)
    set_session_cookie(response, token, request)
    return response


@app.get("/logout")
async def logout(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/logout")
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.delete_session(token)
    response = RedirectResponse("/gate", status_code=302)
    clear_session_cookie(response, request)
    return response


@app.get("/dashboards", response_class=HTMLResponse)
async def my_dashboards(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/dashboards")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    is_admin_user = bool(user.get("is_admin"))
    local_mode = is_local_host(request)
    cards_html = []
    for key, cfg in DASHBOARDS.items():
        has_sub = is_admin_user or (key in subs and subs[key]["status"] == "active")
        active_badge = (
            '<span class="badge badge-active">Active</span>' if has_sub
            else '<span class="badge badge-locked">Locked</span>'
        )
        if has_sub:
            # Local dev: link directly to the dashboard's own port so click-through
            # works without DNS/Cloudflare. Production: use the configured subdomain.
            if local_mode:
                open_url = f"http://localhost:{cfg['target']}"
            else:
                open_url = f"https://{cfg['subdomain']}.{DOMAIN}"
            cta = f'<a class="card-cta cta-open" href="{open_url}" target="_blank">Open →</a>'
        else:
            cta = f'<a class="card-cta cta-sub" href="/billing?dashboard={key}">Subscribe</a>'

        cards_html.append(f"""
        <div class="dash-card" style="--accent: {cfg['accent']}">
          <div class="dash-card-head">
            <div class="dash-accent-dot"></div>
            {active_badge}
          </div>
          <div class="dash-card-title">{cfg['display_name']}</div>
          <div class="dash-card-desc">{cfg['description']}</div>
          <div class="dash-card-price">${cfg['monthly_cents']/100:.2f}/mo · ${cfg['annual_cents']/100:.2f}/yr</div>
          <div class="dash-card-foot">{cta}</div>
        </div>
        """)

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    return render_page(
        "dashboards",
        email=user["email"], username=user.get("username", user["email"]),
        dashboard_cards="".join(cards_html),
        raw_admin_link=admin_link,
    )


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, dashboard: Optional[str] = None):
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
    rows_html = []
    for key, cfg in DASHBOARDS.items():
        s = subs.get(key)
        is_active = is_admin_user or (s is not None and s["status"] == "active")
        status_label = "Active (admin)" if (is_admin_user and not s) else "Active" if is_active else "—"
        monthly_btn = (
            f'<button type="submit" name="action" value="sub:{key}:monthly" class="btn btn-primary" style="--accent:{cfg["accent"]}">Monthly ${cfg["monthly_cents"]/100:.2f}</button>'
        )
        annual_btn = (
            f'<button type="submit" name="action" value="sub:{key}:annual" class="btn btn-primary-outline" style="--accent:{cfg["accent"]}">Annual ${cfg["annual_cents"]/100:.2f}</button>'
        )
        cancel_btn = (
            f'<button type="submit" name="action" value="cancel:{key}" class="btn btn-danger">Cancel</button>'
            if is_active else ""
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
              {monthly_btn}
              {annual_btn}
              {cancel_btn}
            </form>
          </div>
        </div>
        """)

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    return render_page(
        "billing",
        email=user["email"], username=user.get("username", user["email"]),
        billing_rows="".join(rows_html),
        raw_admin_link=admin_link,
    )


@app.post("/billing")
async def billing_action(request: Request, action: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/billing")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    # Placeholder checkout: no real payment. Stripe hook lives here later.
    parts = action.split(":")
    if parts[0] == "sub" and len(parts) == 3:
        _, key, plan = parts
        if key in DASHBOARDS and plan in ("monthly", "annual"):
            duration = 30 if plan == "monthly" else 365
            db.upsert_subscription(
                user_id=user["user_id"],
                dashboard_key=key,
                plan=plan,
                duration_days=duration,
                source="placeholder",
            )
    elif parts[0] == "cancel" and len(parts) == 2:
        _, key = parts
        if key in DASHBOARDS:
            db.cancel_subscription(user["user_id"], key)

    return RedirectResponse("/billing", status_code=302)


# ── Profile page ────────────────────────────────────────────────────────────


def _profile_context(user: dict, banner: str = "") -> dict:
    import datetime as _dt
    db_user = db.get_user_by_id(user["user_id"])
    joined = _dt.datetime.utcfromtimestamp(db_user["created_at"]).strftime("%b %d, %Y UTC") if db_user else "—"
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
    }


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)
    return render_page("profile", **_profile_context(user))


@app.post("/profile/password")
async def profile_change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), confirm_password: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/profile/password")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    db_user = db.get_user_by_id(user["user_id"])
    if not db_user:
        return RedirectResponse("/gate", status_code=302)

    err_banner = lambda msg: f'<div class="notice notice-error" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--red)">{html.escape(msg)}</div>'
    ok_banner = lambda msg: f'<div class="notice notice-success" style="padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;border:1px solid var(--green)">{html.escape(msg)}</div>'

    if not db.verify_password(current_password, db_user["password_hash"], db_user["password_salt"]):
        return render_page("profile", **_profile_context(user, err_banner("Current password is incorrect.")))
    if new_password != confirm_password:
        return render_page("profile", **_profile_context(user, err_banner("New passwords don't match.")))
    if len(new_password) < 12:
        return render_page("profile", **_profile_context(user, err_banner("Password must be at least 12 characters.")))
    if not re.search(r"[A-Z]", new_password) or not re.search(r"[a-z]", new_password) or not re.search(r"[0-9]", new_password) or not re.search(r"[^A-Za-z0-9]", new_password):
        return render_page("profile", **_profile_context(user, err_banner("Password must include uppercase, lowercase, number, and special character.")))

    pwd_hash, salt = db._hash_password(new_password)
    with db.conn() as c:
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwd_hash, salt, user["user_id"]))

    log.info("User %s changed their password", user.get("username", user["email"]))
    return render_page("profile", **_profile_context(user, ok_banner("Password changed successfully.")))


# ── Enquiry page + API ───────────────────────────────────────────────────────


@app.get("/enquire", response_class=HTMLResponse)
async def enquire_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/enquire")
    return render_page("enquire")


@app.post("/api/enquire")
async def api_enquire(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/api/enquire")
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
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASS", "")

            body_text = (
                f"New enquiry from the Habbig landing page.\n\n"
                f"Email: {email}\n"
                f"Role: {job_title}\n\n"
                f"Message:\n{message}\n"
            )
            msg = MIMEText(body_text)
            msg["Subject"] = "New Enquiry \u2014 Habbig"
            msg["From"] = smtp_user or enquiry_email
            msg["To"] = enquiry_email

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_user and smtp_pass:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                server.sendmail(msg["From"], [enquiry_email], msg.as_string())
            log.info("Enquiry notification email sent to %s", enquiry_email)
        except Exception as exc:
            log.error("Failed to send enquiry email: %s", exc)

    return JSONResponse({"success": True})


# ── Admin panel ──────────────────────────────────────────────────────────────


def _require_admin_user(request: Request) -> dict:
    """Return the current user dict if admin, otherwise raise 403."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


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
        meta_parts.append(_dt.datetime.fromtimestamp(t["created_at"]).strftime("%Y-%m-%d %H:%M"))
        if t["claimed_at"]:
            meta_parts.append(f'Claimed {_dt.datetime.fromtimestamp(t["claimed_at"]).strftime("%Y-%m-%d")}')
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
        joined = _dt.datetime.utcfromtimestamp(u["created_at"]).strftime("%Y-%m-%d %H:%M UTC")
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

            # Grant subscription (super admin only)
            if is_super:
                detail_extra += (
                    f'<form method="post" action="/admin/users/{u["id"]}/grant" onclick="event.stopPropagation()" '
                    f'style="display:flex;gap:6px;align-items:center;margin-top:8px">'
                    f'<select name="dashboard_key" {sel_style}>{dash_opts}</select>'
                    f'<select name="plan" {sel_style}><option value="monthly">Monthly</option><option value="annual">Annual</option></select>'
                    f'<button class="btn btn-primary-outline" style="font-size:11px;color:var(--green);border-color:var(--green)">Grant Free</button></form>'
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

    return {
        "raw_token_rows": "".join(token_rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No tokens yet.</div></div></div>',
        "raw_user_rows": "".join(user_rows),
        "raw_stat_cards": stat_cards,
        "raw_new_token_banner": new_token_banner,
        "raw_enquiry_rows": _build_enquiry_rows(),
        "raw_revenue_content": _build_revenue_content(),
    }


def _build_enquiry_rows() -> str:
    enquiries = db.list_enquiries()
    if not enquiries:
        return '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No enquiries yet.</div></div></div>'
    import datetime as _dt
    rows = []
    for e in enquiries:
        read_badge = "" if e["read"] else '<span class="badge" style="background:var(--accent-light);color:var(--accent)">NEW</span> '
        ts = _dt.datetime.fromtimestamp(e["created_at"]).strftime("%Y-%m-%d %H:%M")
        mark_btn = ""
        if not e["read"]:
            mark_btn = (
                f'<form method="post" action="/admin/enquiries/{e["id"]}/read">'
                f'<button class="btn btn-primary-outline" style="font-size:11px">Mark Read</button></form>'
            )
        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">{read_badge}<span style="font-weight:600">{html.escape(e["email"])}</span>'
            f' <span class="badge" style="background:var(--surface-hover);color:var(--text-secondary)">{html.escape(e["job_title"])}</span></div>'
            f'<div style="font-size:13px;color:var(--text-secondary);margin:8px 0;line-height:1.5">{html.escape(e["message"][:300])}</div>'
            f'<div class="admin-row-meta">{ts}</div>'
            f'</div>'
            f'<div class="admin-row-actions">{mark_btn}</div></div>'
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
            ts = _dt.datetime.fromtimestamp(s["started_at"]).strftime("%Y-%m-%d %H:%M")
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
    ctx = _build_admin_context(caller_level=user.get("admin_level", 1))
    return render_page("admin", email=user["email"], username=user.get("username", user["email"]), **ctx)


@app.post("/admin/tokens/generate")
async def admin_generate_token(request: Request, note: str = Form("")):
    user = _require_admin_user(request)
    new_token = db.create_invite_token(note.strip())
    log.info("Admin %s generated invite token: %s", user["email"], new_token)
    ctx = _build_admin_context(new_token_str=new_token, caller_level=user.get("admin_level", 1))
    return render_page("admin", email=user["email"], username=user.get("username", user["email"]), **ctx)


@app.post("/admin/tokens/revoke")
async def admin_revoke_token(request: Request, token_id: int = Form(0)):
    user = _require_admin_user(request)
    db.revoke_invite_token(token_id)
    log.info("Admin %s revoked token id=%d", user["email"], token_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/promote")
async def admin_promote(request: Request, user_id: int):
    _require_admin_user(request)
    db.set_user_admin(user_id, True)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/demote")
async def admin_demote(request: Request, user_id: int):
    _require_admin_user(request)
    db.set_user_admin(user_id, False)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend(request: Request, user_id: int):
    _require_admin_user(request)
    db.set_user_suspended(user_id, True)
    log.info("Admin suspended user id=%d", user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend(request: Request, user_id: int):
    _require_admin_user(request)
    db.set_user_suspended(user_id, False)
    log.info("Admin unsuspended user id=%d", user_id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/enquiries/{enquiry_id}/read")
async def admin_mark_enquiry_read(request: Request, enquiry_id: int):
    _require_admin_user(request)
    db.mark_enquiry_read(enquiry_id)
    return RedirectResponse("/admin", status_code=302)


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
    db.set_user_role(user_id, level)
    log.info("Super admin %s set user %d role to %d", admin["email"], user_id, level)
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
    with db.conn() as c:
        c.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, user_id))
    log.info("Super admin %s changed email for user %d to %s", admin["email"], user_id, new_email)
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
    log.info("Super admin %s generated new token %s for user %d", admin["email"], new_token, user_id)
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
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/bulk")
async def admin_bulk_users(request: Request):
    admin = _require_admin_user(request)
    form = await request.form()
    action = form.get("bulk_action", "")
    user_ids = [int(uid) for uid in form.getlist("user_ids") if uid.isdigit() and int(uid) != 1]
    if not user_ids or not action:
        return RedirectResponse("/admin", status_code=302)
    for uid in user_ids:
        if action == "promote":
            db.set_user_admin(uid, True)
        elif action == "demote":
            db.set_user_admin(uid, False)
        elif action == "suspend":
            db.set_user_suspended(uid, True)
        elif action == "unsuspend":
            db.set_user_suspended(uid, False)
    log.info("Admin %s bulk %s %d users: %s", admin["email"], action, len(user_ids), user_ids)
    return RedirectResponse("/admin", status_code=302)


# ── Settings ──────────────────────────────────────────────────────────────────


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
        has_access = is_admin or (
            key in subs and subs[key]["status"] == "active"
        )
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
    return render_page(
        "settings",
        email=user["email"], username=user.get("username", user["email"]),
        raw_options="".join(option_html),
        raw_saved_banner=saved_banner,
        raw_admin_link=admin_link,
    )


@app.post("/settings")
async def settings_save(request: Request, default_dashboard: str = Form("")):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    user = current_user(request)
    if not user:
        return RedirectResponse("/gate", status_code=302)

    # Blank → clear preference. Otherwise must be a real dashboard key the
    # user has access to (admin bypasses the subscription check).
    key: Optional[str] = default_dashboard.strip() or None
    if key is not None:
        if key not in DASHBOARDS:
            return RedirectResponse("/settings", status_code=302)
        if not user.get("is_admin") and not db.has_active_subscription(user["user_id"], key):
            return RedirectResponse("/settings", status_code=302)

    db.set_default_dashboard(user["user_id"], key)
    return RedirectResponse("/settings?saved=1", status_code=302)


# ── Switcher injection ────────────────────────────────────────────────────────


def _switcher_snippet(dashboard_key: str, user_id: int) -> str:
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
    cfg_json = json.dumps({"dashboards": items, "current": dashboard_key, "domain": DOMAIN})
    return (
        f'<script>window.__hbSwitcher={cfg_json};</script>'
        f'<script src="/_gateway_static/switcher.js"></script>'
    )


def _inject_switcher(content: bytes, content_type: str, key: str, user_id: int) -> bytes:
    """Inject the switcher into HTML responses (before </body>)."""
    if "text/html" not in (content_type or ""):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    snippet = _switcher_snippet(key, user_id)
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
    sub = get_subdomain(request)
    key = SUBDOMAIN_TO_KEY.get(sub)
    if not key:
        # Unknown subdomain — redirect to apex.
        return RedirectResponse(f"https://{DOMAIN}/", status_code=302)

    dash_cfg = DASHBOARDS[key]

    # 1. Require login.
    user = current_user(request)
    if not user:
        return RedirectResponse(f"https://{DOMAIN}/gate", status_code=302)

    # 2. Require active subscription.
    if not db.has_active_subscription(user["user_id"], key):
        return RedirectResponse(
            f"https://{DOMAIN}/billing?dashboard={key}",
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

    # Inject dashboard switcher into HTML responses.
    body = _inject_switcher(
        upstream.content,
        upstream.headers.get("content-type", ""),
        key,
        user["user_id"],
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
    # Extract subdomain from headers (WebSocket Request doesn't expose it the same way).
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

    # Auth check via cookie.
    token = ws.cookies.get(COOKIE_NAME)
    session = db.get_session(token) if token else None
    if not session:
        await ws.close(code=1008, reason="Not authenticated")
        return
    if not db.has_active_subscription(session["user_id"], key):
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
