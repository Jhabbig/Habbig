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
        user_id = db.create_user(DEV_USER_EMAIL, DEV_USER_PASSWORD, is_admin=True)
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
            return {
                "user_id": session["user_id"],
                "email": session["email"],
                "is_admin": session["is_admin"],
            }
    # Dev bypass: if this is a localhost request, return a synthetic "logged in"
    # dict for the dev user so the UI is usable without a real signup flow.
    if is_local_host(request):
        user_id = ensure_dev_user()
        row = db.get_user_by_id(user_id)
        if not row:
            # Extremely rare race (user deleted mid-request). Fail closed.
            return None
        return {
            "user_id": user_id,
            "email": row["email"],
            "is_admin": row["is_admin"],
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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")
    return render_page("login", error="")


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/login")
    email = (email or "").lower().strip()
    user = db.get_user_by_email(email) if email else None
    if not user or not db.verify_password(password, user["password_hash"], user["password_salt"]):
        return render_page("login", error="Invalid email or password.")
    token = db.create_session(user["id"])
    response = RedirectResponse("/dashboards", status_code=302)
    set_session_cookie(response, token, request)
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    return render_page("signup", error="")


@app.post("/signup")
async def signup_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/signup")
    email = (email or "").lower().strip()
    if not is_valid_email(email):
        return render_page("signup", error="Enter a valid email address.")
    if len(password) < 8:
        return render_page("signup", error="Password must be at least 8 characters.")
    if len(password) > 256:
        return render_page("signup", error="Password is too long.")
    if db.get_user_by_email(email):
        return render_page("signup", error="An account with that email already exists.")
    user_id = db.create_user(email, password)
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
    response = RedirectResponse("/login", status_code=302)
    clear_session_cookie(response, request)
    return response


@app.get("/dashboards", response_class=HTMLResponse)
async def my_dashboards(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/dashboards")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    local_mode = is_local_host(request)
    cards_html = []
    for key, cfg in DASHBOARDS.items():
        has_sub = key in subs and subs[key]["status"] == "active"
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

    return render_page(
        "dashboards",
        email=user["email"],
        dashboard_cards="".join(cards_html),
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
        return RedirectResponse("/login", status_code=302)

    if dashboard and dashboard not in DASHBOARDS:
        dashboard = None

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    rows_html = []
    for key, cfg in DASHBOARDS.items():
        s = subs.get(key)
        is_active = s is not None and s["status"] == "active"
        status_label = "Active" if is_active else "—"
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

    return render_page(
        "billing",
        email=user["email"],
        billing_rows="".join(rows_html),
    )


@app.post("/billing")
async def billing_action(request: Request, action: str = Form(...)):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/billing")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

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


# ── Settings ──────────────────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: Optional[str] = None):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings")
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

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

    return render_page(
        "settings",
        email=user["email"],
        raw_options="".join(option_html),
        raw_saved_banner=saved_banner,
    )


@app.post("/settings")
async def settings_save(request: Request, default_dashboard: str = Form("")):
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
    return RedirectResponse("/settings?saved=1", status_code=302)


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
        return RedirectResponse(f"https://{DOMAIN}/login", status_code=302)

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
    return Response(
        content=upstream.content,
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
