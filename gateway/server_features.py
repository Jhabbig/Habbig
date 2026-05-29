"""Feature routes for the 10 new features.

Imported at the end of server.py via `import server_features` (after `app`
and the helpers exist). Kept in a separate file so the edit surface on
server.py stays small and the 4000-line main file isn't touched more than
necessary.

All routes here rely on symbols imported from server.* at module load.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac as _hmac
import html as _html
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import db
import server
from server import (
    app,
    render_page,
    current_user,
    _get_client_ip,
    clear_session_cookie,
    _validate_password,
    COOKIE_NAME,
    log,
)
from email_system.unsubscribe import UnsubscribeManager
from jobs.email_jobs import enqueue_email
from jobs import enqueue_job, get_worker_status, list_recent_jobs, retry_job
from sidebar import render_sidebar

# Canonical rate-limit decorator. Fall back to no-op so the module still
# imports if the security subpackage is missing (matches server.py).
try:
    from security.rate_limiter import rate_limit
except ImportError:  # pragma: no cover — defensive
    def rate_limit(*args, **kwargs):
        def deco(fn):
            return fn
        return deco

# Dedicated security-channel logger (routes to logs/security.log).
import logging as _logging
security_log = _logging.getLogger("security.auth")


_APP_URL = os.environ.get("APP_URL", "https://narve.ai")
_EMAIL_SECRET = (os.environ.get("GATEWAY_COOKIE_SECRET") or "narve-email").encode()


# ── /terms + /privacy (serve existing static pages) ──────────────────────


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    today = _dt.date.today().strftime("%B %d, %Y")
    return render_page(
        "terms",
        request=request,
        last_updated=today,
        effective_date=today,
        legal_email=os.environ.get("LEGAL_EMAIL", "legal@narve.ai"),
        support_email=os.environ.get("SUPPORT_EMAIL", "support@narve.ai"),
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    today = _dt.date.today().strftime("%B %d, %Y")
    return render_page(
        "privacy",
        request=request,
        last_updated=today,
        effective_date=today,
        privacy_email=os.environ.get("PRIVACY_EMAIL", "privacy@narve.ai"),
    )


@app.get("/dpa", response_class=HTMLResponse)
async def dpa_page(request: Request):
    """Data Processing Agreement — public informational page for enterprise
    customers. Lists real sub-processors (no Supabase)."""
    today = _dt.date.today().strftime("%B %d, %Y")
    return render_page(
        "dpa",
        request=request,
        last_updated=today,
        effective_date=today,
        privacy_email=os.environ.get("PRIVACY_EMAIL", "privacy@narve.ai"),
        legal_email=os.environ.get("LEGAL_EMAIL", "legal@narve.ai"),
    )


# ── FEATURE 1: Unsubscribe + email preferences ───────────────────────────


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_page(request: Request, token: str = "", type: str = "marketing"):
    """One-click unsubscribe. No login required.

    Accepts a signed token and flips the user's email preference.
    Always returns a confirmation page — even for invalid tokens, to
    avoid leaking whether an email exists.

    AUDIT 2026-05-15 — per-IP rate-limit (10/hour). An unauthenticated
    HMAC-signed endpoint with no body validation is a tempting brute
    target for somebody hunting forged tokens; 10/hour is plenty for
    a legit user (one click), tight enough to make token enumeration
    economically pointless. The cap is per-IP because the route has
    no session identity to anchor against. Failure also masks the
    token-existence oracle (the response shape is identical to a
    rejected forged token).
    """
    token = (token or "").strip()
    # Per-IP rate-limit. 10/hour is the spec; matches the per-IP cap
    # the password-reset surface uses, which sits at the same risk
    # band (unauth, signed token).
    try:
        ip = server._get_client_ip(request)
        if server._is_rate_limited(f"unsubscribe-ip:{ip}", 10, 3600):
            log.info("unsubscribe: per-IP cap hit ip=%s", ip)
            return HTMLResponse(
                "<!DOCTYPE html><html><head><title>Too many attempts</title></head>"
                "<body><h1>Too many attempts.</h1>"
                "<p>Try again in an hour.</p></body></html>",
                status_code=429,
            )
    except Exception:
        # Rate-limit infra down — fail open rather than block a real
        # user's unsubscribe click. The token still has to verify.
        log.exception("unsubscribe: rate-limit check failed, allowing through")

    row = UnsubscribeManager.unsubscribe(token) if token else None
    scope_label = {
        "marketing": "marketing emails",
        "digest": "the weekly digest",
        "all": "all marketing + digest emails",
    }.get(type, "marketing emails")
    body = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Unsubscribed — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=5'>
<style>body{background:var(--bg-base);color:var(--text-primary);display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:var(--font-ui);}
.card{max-width:440px;padding:48px 40px;background:var(--bg-surface);border:1px solid var(--border-default);border-radius:12px;text-align:center}
h1{font-family:var(--font-display);font-size:28px;margin:0 0 16px;letter-spacing:-0.02em}
p{color:var(--text-secondary);font-size:14px;line-height:1.6}
a{color:var(--text-primary)}</style></head><body><div class='card'>"""
    if row:
        body += f"<h1>Unsubscribed.</h1><p>You have been removed from {scope_label}.</p>"
        body += "<p>You will still receive account, security, and payment emails.</p>"
    else:
        body += "<h1>Link expired or invalid.</h1><p>If you keep receiving emails, contact support.</p>"
    body += f"<p style='margin-top:28px'><a href='{_APP_URL}'>Return to narve.ai</a></p></div></body></html>"
    return HTMLResponse(body)


@app.post("/api/notifications/email-preferences")
async def api_email_preferences(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    digest = bool(body.get("digest"))
    marketing = bool(body.get("marketing"))
    with db.conn() as c:
        c.execute(
            "UPDATE users SET email_digest = ?, email_marketing = ? WHERE id = ?",
            (1 if digest else 0, 1 if marketing else 0, user["user_id"]),
        )
    return JSONResponse({"saved": True, "digest": digest, "marketing": marketing})


# ── i18n: set the display language ────────────────────────────────────────


@app.post("/api/set-language")
async def api_set_language(request: Request):
    """Switch the UI language for this user / browser.

    Accepts the target locale via ``?lang=es`` query param OR a JSON body
    ``{"lang": "es"}``. Validates against ``gateway.i18n.SUPPORTED`` — any
    unsupported value is rejected with 400 so a typo in a hand-crafted
    request doesn't silently leave the user on English.

    Side effects:
      * Sets the ``lang`` cookie (180 days, HttpOnly false so the client
        JS widget can read it without a round-trip).
      * If the session is authenticated, persists
        ``users.preferred_language`` so the choice sticks across devices.

    Anonymous users still get the cookie; it overrides Accept-Language on
    subsequent renders.
    """
    from i18n import SUPPORTED as _I18N_SUPPORTED
    from i18n import LANG_COOKIE_NAME as _LANG_COOKIE
    from i18n import normalise_lang as _normalise_lang

    # Accept ?lang= OR JSON body — whichever the caller prefers.
    raw = request.query_params.get("lang", "").strip()
    if not raw:
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = str(body.get("lang") or "").strip()
        except Exception:
            raw = ""

    lang = _normalise_lang(raw)
    if not lang or lang not in _I18N_SUPPORTED:
        return JSONResponse(
            {"error": "unsupported_language", "supported": list(_I18N_SUPPORTED)},
            status_code=400,
        )

    user = current_user(request)
    if user:
        try:
            with db.conn() as c:
                c.execute(
                    "UPDATE users SET preferred_language = ? WHERE id = ?",
                    (lang, user["user_id"]),
                )
        except Exception as e:
            # Persisting the preference is best-effort — if the column is
            # missing (migration 125 hasn't run) we still want the cookie
            # switch to succeed so the user gets their chosen language
            # this session.
            log.warning("set-language: persist failed: %s", e)

    resp = JSONResponse({"ok": True, "lang": lang, "persisted": bool(user)})
    # AUDIT #4 HIGH #3 — Secure should track PRODUCTION like every other
    # cookie. The legacy GATEWAY_COOKIE_SECURE env var is kept as an
    # explicit override for staging where PRODUCTION=1 but CF terminates
    # at HTTP upstream. Either flag being true flips Secure on.
    _secure = (
        os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
        or os.environ.get("GATEWAY_COOKIE_SECURE", "0").lower() in ("1", "true")
    )
    resp.set_cookie(
        key=_LANG_COOKIE,
        value=lang,
        max_age=60 * 60 * 24 * 180,  # 180 days
        path="/",
        samesite="lax",
        httponly=False,
        secure=_secure,
    )
    return resp


# ── FEATURE 2: Password reset end-to-end ─────────────────────────────────


def _hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@app.post("/auth/forgot-password")
@rate_limit(
    limit=3,
    window_seconds=3600,
    error_message="Too many password-reset requests. Try again in an hour.",
)
async def auth_forgot_password(request: Request, email: str = Form("")):
    """Always returns 200. Never reveals whether the email exists.

    Rate-limited two ways:
      - per IP: 3 requests per hour
      - per email: 3 requests per hour
    Both stack so an attacker cannot enumerate accounts by rotating either
    axis. The function still returns 200 silently when limits trip — leaking
    rate-limit state would itself reveal whether an email exists.
    """
    email = (email or "").strip().lower()
    ip = _get_client_ip(request)

    # Per-IP cap (cheap to evaluate; bounds cost of an attacker DoS).
    if server._is_rate_limited(f"{ip}:forgot-password", limit=3, window=3600):
        return JSONResponse({"ok": True})

    if not email or "@" not in email:
        return JSONResponse({"ok": True})

    # Per-email cap (catches attackers rotating IPs via VPN). Hashed so the
    # rate-limit key never persists the raw email.
    import hashlib as _h
    email_key = _h.sha256(email.encode()).hexdigest()[:24]
    if server._is_rate_limited(f"forgot-password:{email_key}", limit=3, window=3600):
        return JSONResponse({"ok": True})

    user = db.get_user_by_email(email)
    if user:
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
        except Exception as e:
            log.warning("password reset email enqueue failed: %s", e)
    # Always return success — no email enumeration.
    return JSONResponse({"ok": True})


# NB: GET /reset-password is handled by server.py's hardened reset_password_page,
# which now supports both the legacy `token` column AND the new `token_hash`
# (Feature 2). Leaving a second handler here would be ignored by FastAPI
# (first-registered wins) and would drift out of sync.


@app.post("/auth/reset-password")
@rate_limit(
    limit=5,
    window_seconds=3600,
    error_message="Too many reset attempts. Try again in an hour.",
)
async def auth_reset_password(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    # AUDIT #4 HIGH #1 — cap reset-password submissions to 5 per IP per hour
    # so a leaked token fragment can't be brute-forced at line rate. Every
    # other /auth/* POST already has an inline `_is_rate_limited` guard;
    # this one was the outlier.
    ip = _get_client_ip(request)
    if server._is_rate_limited(f"{ip}:reset-password", limit=5, window=3600):
        return HTMLResponse(
            _reset_page_html(token=token, error="Too many attempts. Try again in an hour."),
            status_code=429,
            headers={"Retry-After": "3600"},
        )

    token = (token or "").strip()
    if new_password != confirm_password:
        return HTMLResponse(_reset_page_html(token=token, error="Passwords do not match."), status_code=400)
    err = _validate_password(new_password)
    if err:
        return HTMLResponse(_reset_page_html(token=token, error=err), status_code=400)

    token_hash = _hash_reset_token(token)
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM password_resets WHERE token_hash = ? AND used = 0 AND invalidated = 0",
            (token_hash,),
        ).fetchone()
        if not row or row["expires_at"] < now:
            return HTMLResponse(_reset_page_html(error="This reset link has expired."), status_code=400)

        user_id = row["user_id"]
        # Re-hash the password using db.py's helper to stay consistent.
        pwd_hash, salt = db._hash_password(new_password)
        c.execute(
            "UPDATE users SET password_hash = ?, password_salt = ?, jwt_invalidated_before = ? WHERE id = ?",
            (pwd_hash, salt, now, user_id),
        )
        c.execute(
            "UPDATE password_resets SET used = 1, used_from_ip = ? WHERE id = ?",
            (_get_client_ip(request), row["id"]),
        )
        # Kill every existing session so old devices are logged out.
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    return RedirectResponse("/login?reset=success", status_code=302)


def _reset_page_html(token: str = "", error: str = "") -> str:
    """Minimal in-module template — avoids needing yet another static file."""
    err_html = (
        f"<div style='background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.2);"
        f"padding:12px 16px;border-radius:6px;color:var(--text-primary);font-size:13px;margin-bottom:20px'>{_html.escape(error)}</div>"
        if error else ""
    )
    form = ""
    if token and not error.startswith("This reset"):
        form = f"""
<form method='post' action='/auth/reset-password'>
  <input type='hidden' name='token' value='{_html.escape(token)}'>
  <label style='display:block;font-size:11px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;margin:14px 0 6px'>New password</label>
  <input type='password' name='new_password' required minlength='12' style='width:100%;padding:12px;background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border-default);border-radius:6px;font-family:inherit;font-size:14px' autocomplete='new-password'>
  <label style='display:block;font-size:11px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;margin:14px 0 6px'>Confirm password</label>
  <input type='password' name='confirm_password' required minlength='12' style='width:100%;padding:12px;background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border-default);border-radius:6px;font-family:inherit;font-size:14px' autocomplete='new-password'>
  <ul id='pw-rules' style='font-size:12px;color:var(--text-tertiary);padding-left:18px;margin:14px 0'>
    <li id='r-len'>At least 12 characters</li>
    <li id='r-upper'>One uppercase letter</li>
    <li id='r-lower'>One lowercase letter</li>
    <li id='r-digit'>One number</li>
    <li id='r-special'>One special character</li>
  </ul>
  <button type='submit' style='width:100%;padding:12px;background:var(--text-primary);color:var(--interactive-text);border:none;border-radius:6px;font-family:inherit;font-size:14px;font-weight:500;cursor:pointer'>Reset password</button>
</form>
<script>
var pw = document.querySelector('input[name=new_password]');
function check(){{
  var v = pw.value;
  document.getElementById('r-len').style.color = v.length >= 12 ? 'var(--text-primary)' : 'var(--text-tertiary)';
  document.getElementById('r-upper').style.color = /[A-Z]/.test(v) ? 'var(--text-primary)' : 'var(--text-tertiary)';
  document.getElementById('r-lower').style.color = /[a-z]/.test(v) ? 'var(--text-primary)' : 'var(--text-tertiary)';
  document.getElementById('r-digit').style.color = /[0-9]/.test(v) ? 'var(--text-primary)' : 'var(--text-tertiary)';
  document.getElementById('r-special').style.color = /[^A-Za-z0-9]/.test(v) ? 'var(--text-primary)' : 'var(--text-tertiary)';
}}
if(pw) pw.addEventListener('input', check);
</script>"""
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Reset password — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=5'>
</head>
<body style='background:var(--bg-base);color:var(--text-primary);display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:var(--font-ui);margin:0'>
<div style='max-width:440px;width:100%;padding:48px 40px;background:var(--bg-surface);border:1px solid var(--border-default);border-radius:12px'>
<h1 style='font-family:var(--font-display);font-size:26px;margin:0 0 8px;letter-spacing:-0.02em'>Reset password</h1>
<p style='color:var(--text-secondary);font-size:13px;margin:0 0 20px'>Choose a new password. Every existing session will be logged out.</p>
{err_html}
{form}
<p style='margin-top:22px;font-size:12px;color:var(--text-tertiary);text-align:center'><a href='/login' style='color:var(--text-secondary)'>Back to sign in</a></p>
</div></body></html>"""


# ── FEATURE 3: Waitlist position numbers + referrals ─────────────────────
#
# REMOVED 2026-05-29 — the `/api/newsletter` (POST) and
# `/api/newsletter/position` (GET) handlers that lived here were dead
# duplicates of the hardened, anti-enumeration versions in
# public_routes.py. public_routes.register(app) runs earlier in server.py
# (~line 6670) than `import server_features` (~line 8385), so FastAPI's
# first-match routing always dispatched to the public_routes versions.
# These raw-SQL copies leaked `already: true` (an enumeration oracle) and
# would have silently re-activated if the registration order ever flipped.
# Deleted along with their dupe-only helpers `_new_referral_code` and
# `_apply_referral_bump`. The canonical waitlist logic lives in
# queries/newsletter.py + public_routes.py.


# ── FEATURE 6: Account deletion with 30-day recovery ─────────────────────


@app.post("/api/account/delete")
async def api_account_delete(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # AUDIT 2026-05-14 — mirror the 3/hour cap from /account/delete (form
    # handler in server.py). A compromised session that flipped the
    # deletion flag could otherwise loop schedule→cancel→schedule to spam
    # the deletion_confirmation transactional email queue.
    if server._is_rate_limited(f"account-delete-api:{user['user_id']}", 3, 3600):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Require the word DELETE as an anti-footgun measure.
    if (body.get("confirm") or "").strip().upper() != "DELETE":
        return JSONResponse({"error": "Type DELETE to confirm"}, status_code=400)

    now = int(time.time())
    deletion_scheduled_for = now + 30 * 86400

    with db.conn() as c:
        c.execute(
            "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ?, "
            "deletion_cancelled_at = NULL, jwt_invalidated_before = ? WHERE id = ?",
            (now, deletion_scheduled_for, now, user["user_id"]),
        )
        # Cancel subscriptions
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND status = 'active'",
            (user["user_id"],),
        )
        # Revoke every existing session
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user["user_id"],))

    row = db.get_user_by_id(user["user_id"])
    if row and row["email"]:
        try:
            await enqueue_email(
                to=row["email"],
                template="account_deletion_confirmation",
                context={
                    "display_name": row["username"] or row["email"].split("@")[0],
                    "deletion_date": _dt.datetime.fromtimestamp(deletion_scheduled_for).strftime("%B %d, %Y"),
                },
                tags=["account_deletion", "transactional"],
            )
        except Exception as e:
            log.warning("deletion confirmation enqueue failed: %s", e)

    response = JSONResponse({"scheduled": True, "deletion_date": deletion_scheduled_for})
    clear_session_cookie(response, request)
    return response


@app.post("/api/account/delete/cancel")
async def api_account_delete_cancel(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # AUDIT 2026-05-14 — share the deletion-API budget so the cancel/
    # schedule pair can't loop without tripping a single 3/hour cap.
    if server._is_rate_limited(f"account-delete-api:{user['user_id']}", 3, 3600):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE users SET deletion_cancelled_at = ?, deletion_scheduled_for = NULL WHERE id = ?",
            (now, user["user_id"]),
        )
    return JSONResponse({"cancelled": True})


# ── FEATURE 7: Public source profiles + sitemap + robots.txt ─────────────


@app.get("/sources/{handle}", response_class=HTMLResponse)
async def public_source_profile(request: Request, handle: str):
    handle = handle.strip().lstrip("@")
    # Perf audit #3: cache the three credibility queries that drive the public
    # profile page (300s — fully public, slow-changing). Anonymous data, so
    # one shared key per handle is safe; we still render HTML per request to
    # keep the meta tags / canonical URL request-aware. `not_found` sentinels
    # are cached too so unknown handles stop hitting the DB.
    try:
        from cache import cache as _async_cache

        async def _build_source_data() -> dict:
            _cred = db.get_source_credibility(handle) if hasattr(db, "get_source_credibility") else None
            if not _cred or not _cred["accuracy_unlocked"]:
                return {"not_found": True}
            _cats = db.get_all_category_credibilities(handle) if hasattr(db, "get_all_category_credibilities") else []
            _preds = db.list_recent_predictions(limit=10) if hasattr(db, "list_recent_predictions") else []
            return {
                "not_found": False,
                "cred": dict(_cred),
                "cats": [dict(c) for c in _cats],
                "preds": [dict(p) for p in _preds],
            }

        _data = await _async_cache.get_or_set(
            f"source_profile:{handle}", _build_source_data, ttl_seconds=300,
        )
    except Exception:
        # Cache layer down — fall through to direct DB reads.
        _cred = db.get_source_credibility(handle) if hasattr(db, "get_source_credibility") else None
        if not _cred or not _cred["accuracy_unlocked"]:
            _data = {"not_found": True}
        else:
            _data = {
                "not_found": False,
                "cred": dict(_cred),
                "cats": [dict(c) for c in (db.get_all_category_credibilities(handle) if hasattr(db, "get_all_category_credibilities") else [])],
                "preds": [dict(p) for p in (db.list_recent_predictions(limit=10) if hasattr(db, "list_recent_predictions") else [])],
            }

    if _data.get("not_found"):
        return HTMLResponse(
            "<!DOCTYPE html><html><head><title>Source not found — narve.ai</title></head>"
            "<body><h1>Source not rated yet</h1>"
            "<p>This source has not made enough qualifying predictions to be rated publicly.</p>"
            "<p><a href='/'>Back to narve.ai</a></p></body></html>",
            status_code=404,
        )

    cred = _data["cred"]
    cats = _data["cats"]
    preds = _data["preds"]
    total = cred["total_predictions"] or 0
    correct = cred["correct_predictions"] or 0
    accuracy_int = int(100 * correct / total) if total else 0
    score = round(cred["global_credibility"], 2)
    tracked_since = _dt.datetime.fromtimestamp(cred["last_computed_at"]).strftime("%B %Y")

    # Build per-card prediction rows (full-width cards, body in --font-body).
    pred_rows_html = "".join(
        f'<li>'
        f'<p class="src-card__body">{_html.escape((p["content"] or "")[:240])}</p>'
        f'<div class="src-card__meta">'
        f'<span>{_html.escape((p["category"] or "—"))}</span>'
        f'<span>{"resolved" if p["resolved"] else "open"}</span>'
        f'</div>'
        f'</li>'
        for p in preds[:10]
    ) or '<li class="src-card--empty">No recent predictions tracked yet.</li>'

    # Categories tab — card-list of cat-row variants.
    cat_rows_html = "".join(
        f'<li>'
        f'<div class="src-cat-row">'
        f'<div class="src-cat-row__name">{_html.escape(c["category"].title())}</div>'
        f'<div class="src-cat-row__score">{round(c["category_credibility"], 2)}</div>'
        f'<div class="src-cat-row__sub">'
        f'{c["prediction_count"]} preds · '
        f'{int(100 * c["correct_count"] / max(c["prediction_count"], 1))}%'
        f'</div>'
        f'</div>'
        f'</li>'
        for c in cats
    ) or '<li class="src-card--empty">No category breakdown yet.</li>'

    meta_desc = (
        f"@{handle} has a credibility score of {score} on narve.ai. "
        f"{accuracy_int}% accuracy across {total} tracked predictions on Polymarket markets."
    )

    handle_safe = _html.escape(handle)
    avatar_initial = (handle[:1] or "@").upper()
    meta_desc_safe = _html.escape(meta_desc)
    canonical_url = f"{_APP_URL}/sources/{handle_safe}"
    og_meta = (
        "<meta name='robots' content='index, follow'>\n"
        "<meta property='og:type' content='profile'>\n"
        "<meta property='og:site_name' content='narve.ai'>\n"
        f"<meta property='og:title' content='@{handle_safe} on narve.ai'>\n"
        f"<meta property='og:description' content='{meta_desc_safe}'>\n"
        f"<meta property='og:url' content='{canonical_url}'>\n"
        f"<meta property='og:image' content='{_APP_URL}/og/source/{handle_safe}'>\n"
        "<meta property='og:image:width' content='1200'>\n"
        "<meta property='og:image:height' content='630'>\n"
        "<meta name='twitter:card' content='summary_large_image'>\n"
        "<meta name='twitter:site' content='@narveai'>\n"
        f"<meta name='twitter:title' content='@{handle_safe} — narve.ai'>\n"
        f"<meta name='twitter:description' content='{meta_desc_safe}'>\n"
        f"<meta name='twitter:image' content='{_APP_URL}/og/source/{handle_safe}'>"
    )
    jsonld_payload = (
        '{"@context":"https://schema.org","@type":"Person",'
        f'"name":"@{handle}","description":"{meta_desc}",'
        f'"url":"{canonical_url}"'
        '}'
    ).replace("</", "<\\/")
    description_body = (
        f"Prediction market source tracked by narve.ai. "
        f"Credibility and accuracy are updated as new predictions resolve. "
        f"Rated since {tracked_since}."
    )

    return render_page(
        "source",
        request=request,
        handle=handle,
        avatar_initial=avatar_initial,
        description=description_body,
        total_predictions=f"{total:,}",
        accuracy=f"{accuracy_int}%",
        global_credibility=f"{score:.2f}",
        correct_predictions=f"{correct:,}",
        tracked_since=tracked_since,
        category_count=str(len(cats)),
        raw_prediction_rows=pred_rows_html,
        raw_category_rows=cat_rows_html,
        raw_canonical=f"<link rel='canonical' href='{canonical_url}'>",
        raw_og=og_meta,
        raw_jsonld=f"<script type='application/ld+json'>{jsonld_payload}</script>",
    )


# NOTE: /sitemap.xml and /robots.txt are NOT defined here anymore.
#
# The canonical handlers live in server.py (seo_sitemap_xml / seo_robots_txt).
# The sitemap is served at an obscure, non-guessable URL (server._SITEMAP_PATH,
# currently /497951413996680578.xml) and submitted directly in Google Search
# Console rather than advertised — so we must NOT register a /sitemap.xml route
# that would hand out the full public-page roadmap to any anonymous fetch.
#
# These previously duplicated server.py's routes; server.py's registered first
# and won /robots.txt, while this module's /sitemap.xml shadowed server.py's.
# Both are removed so server.py is the single source of truth and no roadmap is
# exposed at the guessable /sitemap.xml path.


# ── FEATURE 8: Market view tracking (enqueues resolution notifications) ──


@app.post("/api/markets/{market_slug}/track-view")
async def api_track_market_view(request: Request, market_slug: str):
    """Called from the dashboard when a user opens a market detail panel.

    Records a UserMarketView row so that when the market eventually
    resolves, the notification job knows who to email.
    """
    user = current_user(request)
    if not user:
        return JSONResponse({"tracked": False})
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT id, view_count FROM user_market_views WHERE user_id = ? AND market_slug = ?",
            (user["user_id"], market_slug),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE user_market_views SET last_viewed_at = ?, view_count = view_count + 1 WHERE id = ?",
                (now, row["id"]),
            )
        else:
            c.execute(
                "INSERT INTO user_market_views (user_id, market_slug, first_viewed_at, last_viewed_at, view_count, notified_on_resolution) "
                "VALUES (?, ?, ?, ?, 1, 0)",
                (user["user_id"], market_slug, now, now),
            )
    return JSONResponse({"tracked": True})


@app.post("/admin/markets/{market_slug}/mark-resolved")
async def admin_mark_market_resolved(request: Request, market_slug: str, outcome: str = Form("YES")):
    """Admin utility — resolve a market and enqueue the notification job.

    In production the resolver script calls enqueue_job directly; this is
    the manual fallback used from the admin panel or CLI.
    """
    from server import _require_admin_user  # avoid import-time cycle
    _require_admin_user(request)
    await enqueue_job(
        "send_market_resolution_notifications",
        market_slug=market_slug,
        outcome=outcome,
        market_question=market_slug.replace("-", " ").title(),
    )
    return JSONResponse({"enqueued": True})


# ── FEATURE 9: Weekly digest — admin trigger (cron fires automatically) ──


@app.post("/admin/jobs/weekly-digest/run")
async def admin_run_weekly_digest(request: Request):
    from server import _require_admin_user
    _require_admin_user(request)
    job_id = await enqueue_job("send_weekly_digest_batch")
    return JSONResponse({"enqueued": True, "job_id": job_id})


# ── FEATURE 10: Job monitoring admin endpoints ───────────────────────────


@app.get("/admin/api/jobs/status")
async def admin_jobs_status(request: Request):
    from server import _require_admin_user
    _require_admin_user(request)
    return JSONResponse(get_worker_status())


@app.get("/admin/api/jobs/recent")
async def admin_jobs_recent(request: Request, status: str = "", limit: int = 50):
    from server import _require_admin_user
    _require_admin_user(request)
    rows = list_recent_jobs(limit=limit, status_filter=status or None)
    return JSONResponse({"jobs": rows, "count": len(rows)})


@app.post("/admin/api/jobs/{job_id}/retry")
async def admin_jobs_retry(request: Request, job_id: int):
    from server import _require_admin_user
    _require_admin_user(request)
    ok = await retry_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse({"retried": True})


# ═════════════════════════════════════════════════════════════════════════
# FEATURE 11-14: User-facing features (global search, saved predictions,
#                source following, historical odds chart)
# ═════════════════════════════════════════════════════════════════════════


def _require_auth(request: Request) -> dict:
    """Shared auth shim — raise 401 if no session."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _prediction_row_to_dict(row) -> dict:
    """Normalise a predictions row (with or without the credibility join) into JSON."""
    return {
        "id": row["id"] if "id" in row.keys() else row["prediction_id"],
        "content": row["content"],
        "source_handle": row["source_handle"],
        "category": row["category"],
        "market_id": row["market_id"],
        "direction": row["direction"],
        "predicted_probability": row["predicted_probability"],
        "source_url": row["source_url"] if "source_url" in row.keys() else None,
        "extracted_at": row["extracted_at"],
        "resolved": bool(row["resolved"]) if row["resolved"] is not None else False,
        "resolved_correct": bool(row["resolved_correct"]) if row["resolved_correct"] is not None else None,
        "credibility": row["global_credibility"],
        "accuracy_unlocked": bool(row["accuracy_unlocked"]) if row["accuracy_unlocked"] is not None else False,
    }


# ── FEATURE 11: Global search (SQLite FTS5) ──────────────────────────────


@app.get("/api/search")
async def api_search(request: Request, q: str = "", type: str = "all", limit: int = 20):
    """Cross-entity FTS5 search.

    Returns predictions, sources, and markets that match ``q``. ``type`` can
    narrow to a single entity ("predictions" | "sources" | "markets" | "all").
    Limit is clamped to [1, 50] to bound the response size.
    """
    _require_auth(request)  # auth side effect — user dict not used below
    q = (q or "").strip()
    if not q:
        return JSONResponse({"query": q, "results": {"predictions": [], "sources": [], "markets": []}, "total": 0, "took_ms": 0.0})
    if len(q) > 200:
        return JSONResponse({"error": "Query too long"}, status_code=400)
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20

    start = time.perf_counter()
    out = {"predictions": [], "sources": [], "markets": []}

    if type in ("all", "predictions"):
        for row in db.search_predictions(q, limit=limit):
            d = _prediction_row_to_dict(row)
            d["highlight"] = row["highlight"]  # already contains <mark> tags from FTS snippet()
            out["predictions"].append(d)

    if type in ("all", "sources"):
        for row in db.search_sources(q, limit=limit):
            out["sources"].append({
                "handle": row["source_handle"],
                "global_credibility": row["global_credibility"],
                "accuracy_unlocked": bool(row["accuracy_unlocked"]),
                "total_predictions": row["total_predictions"],
                "correct_predictions": row["correct_predictions"],
                "decay_weighted_accuracy": row["decay_weighted_accuracy"],
            })

    if type in ("all", "markets"):
        for row in db.search_markets(q, limit=limit):
            out["markets"].append({
                "market_slug": row["market_slug"],
                "market_question": row["market_question"],
                "category": row["category"],
                "yes_price": row["yes_price"],
                "snapshotted_at": row["snapshotted_at"],
                "highlight": row["highlight"],
            })

    took_ms = round((time.perf_counter() - start) * 1000, 2)
    total = len(out["predictions"]) + len(out["sources"]) + len(out["markets"])
    return JSONResponse({
        "query": q,
        "results": out,
        "total": total,
        "took_ms": took_ms,
    })


# ── FEATURE 12: Saved predictions / watchlist ────────────────────────────


@app.post("/api/saved/{prediction_id}")
async def api_save_prediction(request: Request, prediction_id: int):
    user = _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Free-form notes — normalise through clean_text so pasted unicode
    # and stray null bytes become either valid text or a predictable 400.
    from security.input_hygiene import clean_text
    raw_notes = body.get("notes") if isinstance(body, dict) else None
    notes = clean_text(raw_notes, max_len=2000, field="notes")
    saved_id = db.save_prediction(user["user_id"], prediction_id, notes=notes)
    if saved_id == 0:
        raise HTTPException(status_code=404, detail="Prediction not found")
    return JSONResponse({"saved": True, "id": saved_id})


@app.delete("/api/saved/{prediction_id}")
async def api_unsave_prediction(request: Request, prediction_id: int):
    user = _require_auth(request)
    removed = db.unsave_prediction(user["user_id"], prediction_id)
    return JSONResponse({"removed": bool(removed)})


@app.get("/api/saved")
async def api_list_saved(
    request: Request,
    resolved: str = "all",
    sort: str = "saved_at",
    page: int = 1,
    per_page: int = 50,
):
    """List a user's saved predictions.

    Paginated so a user with 5 000+ saved items doesn't get a single
    50 k-row payload. `resolved` / `sort` are opaque filter strings
    passed to `db.list_saved_predictions`; we normalise them against
    the known value set and fall through to defaults on anything else
    (safer than a 400 when the frontend sends a new value we haven't
    taught the backend about yet).
    """
    user = _require_auth(request)

    # Share the canonical pagination helpers — zero/negative/over-cap
    # inputs collapse to sensible defaults rather than 500-ing through
    # LIMIT/OFFSET.
    from security.input_hygiene import clean_page, clean_per_page
    p = clean_page(page)
    pp = clean_per_page(per_page, default=50, max_per_page=200)

    # Accept only the filter values db.list_saved_predictions understands.
    if resolved not in {"all", "resolved", "unresolved"}:
        resolved = "all"
    if sort not in {"saved_at", "extracted_at", "credibility"}:
        sort = "saved_at"

    all_rows = db.list_saved_predictions(
        user["user_id"], resolved_filter=resolved, sort=sort,
    )
    total = len(all_rows)
    start = (p - 1) * pp
    rows = all_rows[start:start + pp]

    items = []
    for row in rows:
        items.append({
            "saved_id": row["saved_id"],
            "saved_at": row["saved_at"],
            "notes": row["notes"],
            "notified_on_resolution": bool(row["notified_on_resolution"]),
            "prediction": {
                "id": row["prediction_id"],
                "content": row["content"],
                "source_handle": row["source_handle"],
                "category": row["category"],
                "market_id": row["market_id"],
                "direction": row["direction"],
                "predicted_probability": row["predicted_probability"],
                "source_url": row["source_url"],
                "extracted_at": row["extracted_at"],
                "resolved": bool(row["resolved"]),
                "resolved_correct": bool(row["resolved_correct"]) if row["resolved_correct"] is not None else None,
                "resolved_at": row["resolved_at"],
                "credibility": row["global_credibility"],
                "accuracy_unlocked": bool(row["accuracy_unlocked"]) if row["accuracy_unlocked"] is not None else False,
            },
        })
    return JSONResponse({
        "items": items,
        "count": len(items),
        "total": total,
        "page": p,
        "per_page": pp,
        "pages": max(1, (total + pp - 1) // pp),
    })


@app.patch("/api/saved/{prediction_id}")
async def api_update_saved_notes(request: Request, prediction_id: int):
    user = _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    # Collapse the old bespoke "is str? too long?" checks into clean_text
    # — same length cap, but now also NFC-normalises + strips
    # zero-width / bidi / null bytes. allow_empty=True so a deliberate
    # blank note clears the field.
    from security.input_hygiene import clean_text
    notes = clean_text(
        body.get("notes"), max_len=2000, field="notes", allow_empty=True,
    )
    ok = db.update_saved_prediction_notes(user["user_id"], prediction_id, notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Saved prediction not found")
    return JSONResponse({"updated": True, "notes": notes})


@app.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    from server import _role_badge  # type: ignore
    admin_link = '<a href="/admin" class="nav-item">Admin</a>' if user.get("is_admin") else ""
    nav_role = _role_badge(user)
    _sidebar = render_sidebar(
        request,
        active="",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )
    return render_page(
        "saved",
        request=request,
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
        raw_sidebar=_sidebar,
    )


# ── FEATURE 13: Source following ─────────────────────────────────────────


@app.post("/api/sources/{handle}/follow")
async def api_follow_source(request: Request, handle: str):
    user = _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    notify = bool(body.get("notify_on_prediction", False))
    min_cred = body.get("notify_min_credibility", 0.5)
    try:
        min_cred = float(min_cred)
    except (TypeError, ValueError):
        min_cred = 0.5
    min_cred = max(0.0, min(1.0, min_cred))
    platform = (body.get("platform") or "").strip()[:50]
    fid = db.follow_source(
        user["user_id"], handle,
        platform=platform,
        notify_on_prediction=notify,
        notify_min_credibility=min_cred,
    )
    if fid == 0:
        raise HTTPException(status_code=400, detail="Invalid source handle")
    return JSONResponse({
        "following": True,
        "id": fid,
        "notify_on_prediction": notify,
        "notify_min_credibility": min_cred,
    })


@app.delete("/api/sources/{handle}/follow")
async def api_unfollow_source(request: Request, handle: str):
    user = _require_auth(request)
    removed = db.unfollow_source(user["user_id"], handle)
    return JSONResponse({"unfollowed": bool(removed)})


@app.patch("/api/sources/{handle}/follow")
async def api_update_follow(request: Request, handle: str):
    user = _require_auth(request)
    if not db.is_following_source(user["user_id"], handle):
        raise HTTPException(status_code=404, detail="Not following this source")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    notify = bool(body.get("notify_on_prediction", False))
    try:
        min_cred = float(body.get("notify_min_credibility", 0.5))
    except (TypeError, ValueError):
        min_cred = 0.5
    min_cred = max(0.0, min(1.0, min_cred))
    db.update_follow_preferences(user["user_id"], handle, notify, min_cred)
    return JSONResponse({
        "updated": True,
        "notify_on_prediction": notify,
        "notify_min_credibility": min_cred,
    })


@app.get("/api/sources/following")
async def api_list_following(
    request: Request,
    page: int = 1,
    per_page: int = 100,
):
    """List the sources a user follows.

    Paginated defensively (someone following thousands of sources is
    rare but possible after a bulk-import). The whole list is still
    fetched from the DB — db.list_followed_sources has no LIMIT — and
    sliced in Python. Good enough up to ~50 k rows; if that ever
    bites, push the pagination into the SQL helper.
    """
    user = _require_auth(request)

    from security.input_hygiene import clean_page, clean_per_page
    p = clean_page(page)
    pp = clean_per_page(per_page, default=100, max_per_page=500)

    all_rows = db.list_followed_sources(user["user_id"])
    total = len(all_rows)
    start = (p - 1) * pp
    rows = all_rows[start:start + pp]

    payload = {
        "items": [
            {
                "source_handle": r["source_handle"],
                "platform": r["platform"],
                "followed_at": r["followed_at"],
                "notify_on_prediction": bool(r["notify_on_prediction"]),
                "notify_min_credibility": r["notify_min_credibility"],
                "global_credibility": r["global_credibility"],
                "accuracy_unlocked": bool(r["accuracy_unlocked"]) if r["accuracy_unlocked"] is not None else False,
                "total_predictions": r["total_predictions"],
            }
            for r in rows
        ],
        "count": len(rows),
        "total": total,
        "page": p,
        "per_page": pp,
        "pages": max(1, (total + pp - 1) // pp),
    }
    return JSONResponse(server._forensic_sign(user, payload, "api_sources_following"))


# ── FEATURE 14: Historical odds chart ────────────────────────────────────


@app.get("/api/markets/{slug:path}/chart")
async def api_market_chart(request: Request, slug: str):
    """Return odds history + prediction markers for charting."""
    _require_auth(request)
    slug = slug.strip()
    if not slug:
        raise HTTPException(status_code=404, detail="Market not found")
    latest = db.get_latest_market_snapshot(slug)
    odds_history = db.get_market_history(slug, limit=1000)
    markers_rows = db.get_prediction_markers_for_market(slug)
    if latest is None and not odds_history and not markers_rows:
        raise HTTPException(status_code=404, detail="Market not found")
    market_question = latest["market_question"] if latest else None
    category = latest["category"] if latest else None
    return JSONResponse({
        "market_slug": slug,
        "market_question": market_question,
        "category": category,
        "odds_history": [
            {
                "timestamp": int(row["snapshotted_at"]),
                "yes_price": row["yes_price"],
                "volume": row["volume"],
            }
            for row in odds_history
        ],
        "prediction_markers": [
            {
                "prediction_id": row["id"],
                "timestamp": int(row["extracted_at"]),
                "source_handle": row["source_handle"],
                "content": row["content"],
                "credibility": row["global_credibility"],
                "direction": row["direction"],
                "predicted_probability": row["predicted_probability"],
                "market_yes_price_at_time": row["market_yes_price_at_time"],
            }
            for row in markers_rows
        ],
    })


@app.post("/api/markets/{slug:path}/snapshot")
async def api_ingest_market_snapshot(request: Request, slug: str):
    """Internal ingestion endpoint for dashboard backends.

    Authentication: requires the X-Internal-Key header to match the
    GATEWAY_INTERNAL_KEY env var. This endpoint is NOT user-facing; it's
    how the crypto/sports/weather/etc. dashboards push odds updates into
    the gateway so charts can render. Silently 404s if the internal key
    isn't configured so the endpoint stays invisible unless explicitly
    enabled.
    """
    expected = os.environ.get("GATEWAY_INTERNAL_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=404, detail="Not found")
    provided = (request.headers.get("x-internal-key") or "").strip()
    if not _hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Invalid internal key")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be an object"}, status_code=400)
    try:
        yes_price = float(body.get("yes_price"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "yes_price required (number)"}, status_code=400)
    if not (0.0 <= yes_price <= 1.0):
        return JSONResponse({"error": "yes_price must be in [0, 1]"}, status_code=400)
    snapshot_id = db.insert_market_snapshot(
        market_slug=slug,
        yes_price=yes_price,
        snapshotted_at=body.get("snapshotted_at"),
        market_question=body.get("market_question"),
        category=body.get("category"),
        no_price=body.get("no_price"),
        volume=body.get("volume"),
        source_platform=(body.get("source_platform") or "polymarket"),
    )
    return JSONResponse({"id": snapshot_id, "market_slug": slug})



# ═══════════════════════════════════════════════════════════════════════
# AUTH FLOW (/gate perimeter → /login → /dashboard)
# ═══════════════════════════════════════════════════════════════════════
#
# Flow:
#   1. /gate  (server.py)  — SITE_ACCESS_TOKEN perimeter
#   2. /login (server.py)  — standalone email+password page
#   3. POST /auth/login    — JSON login, issues hardened session cookies
#   4. POST /auth/logout   — revokes hardened + legacy session
#
# Two cookies coexist during the rollout:
#   - narve_session (new, hardened, SHA-256 at rest, 7-day TTL)
#   - pm_gateway_session (old, still written so CSRF/audit work)
# Every login writes to BOTH so legacy helpers keep running.
#
# The old token-first flow (/token, /auth/validate-token, /register,
# /auth/register) was retired 2026-05-15 and the routes deleted in the
# audit #18 MED #3 follow-up. See git history for the prior implementation.

from auth.cookies import (
    SESSION_COOKIE,
    set_session_cookie_hardened,
    clear_session_cookie_hardened,
)
from auth.guards import (
    read_hardened_session,
)


def _is_strong_password(pw: str) -> Optional[str]:
    """Return an error string or None. Matches the register.html rules."""
    if len(pw) < 12:
        return "Password must be at least 12 characters."
    if len(pw) > 256:
        return "Password is too long."
    if not any(c.isupper() for c in pw):
        return "Password must contain at least one uppercase letter."
    if not any(c.isdigit() for c in pw):
        return "Password must contain at least one number."
    if not any(not c.isalnum() for c in pw):
        return "Password must contain at least one special character."
    return None


async def _issue_hardened_session(
    user_id: int,
    request: Request,
    response,
) -> str:
    """Issue BOTH a legacy sessions row AND a hardened user_sessions row.

    Returns the raw hardened token (cookie value). Sets both cookies on the
    response. The legacy session is also marked 2FA-verified so new accounts
    go straight to /dashboards.
    """
    legacy_token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(legacy_token)
    except Exception:
        pass

    ua = request.headers.get("user-agent", "")[:256]
    ip = _get_client_ip(request)
    raw_hardened = db.create_user_session(
        user_id,
        ip_address=ip,
        user_agent=ua,
        legacy_token=legacy_token,
    )

    server.set_session_cookie(response, legacy_token, request)
    set_session_cookie_hardened(response, raw_hardened, request)
    # Rotate the CSRF token on every successful session issuance so any token
    # captured on a public page before login cannot be reused post-auth.
    try:
        server._set_csrf_cookie(response, server._generate_csrf_token(), request)
    except Exception:
        pass  # Cookie rotation is defense-in-depth; never block login on failure.
    return raw_hardened


@app.post("/auth/login")
async def auth_login(request: Request):
    """Direct email + password login (JSON).

    Rewritten 2026-05-15 to drop the invite-token requirement. Accepts
    `{email, password}` JSON, verifies against `users.password_hash`,
    issues `narve_session` + `pm_gateway_session` cookies, returns
    `{success: true, redirect: '/dashboards'}`. CSRF enforced via
    `_csrf` header on the request (the form-submit JS in login.html
    reads the cookie and sends it as a header).
    """
    ip = _get_client_ip(request)
    if server._is_rate_limited(f"{ip}:login-auth", limit=10, window=300):
        return JSONResponse(
            {"error": "Too many attempts."},
            status_code=429,
            headers={"Retry-After": "300"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or "@" not in email or len(email) > 254:
        return JSONResponse(
            {"error": "Enter a valid email.", "field": "email"},
            status_code=400,
        )
    if not password or len(password) > 256:
        return JSONResponse(
            {"error": "Enter your password.", "field": "password"},
            status_code=400,
        )

    # Per-email rate limit (H1): credential-stuffing across rotating IPs.
    # 5 wrong attempts per 10min per email regardless of source IP.
    if server._is_rate_limited(f"email:{email}:login", limit=5, window=600):
        return JSONResponse(
            {"error": "Too many attempts for this account."},
            status_code=429,
            headers={"Retry-After": "600"},
        )

    user = db.get_user_by_email(email)
    # Audit #20 HIGH #2 fix: ALWAYS verify password before branching on user
    # state (suspended / shell / unknown). Otherwise distinct error messages
    # leak which emails exist on the platform. Constant-time-ish: always run
    # verify_password even on missing user so timing doesn't distinguish
    # enumeration from wrong-password.
    dummy_hash = "0" * 64
    dummy_salt = "0" * 32
    if not user:
        db.verify_password(password, dummy_hash, dummy_salt)
        log.info("auth.login: unknown email %s", db.mask_email(email))
        return JSONResponse(
            {"error": "Invalid email or password."},
            status_code=401,
        )

    user_id = user["id"]

    # Verify password FIRST — before any state-dependent branching.
    # Shell users (empty password_hash) will reliably fail this check since
    # an empty hash can't match anything, so they'll get the generic
    # "Invalid email or password" until they redeem their reset link.
    if not db.verify_password(password, user["password_hash"] or "", user["password_salt"] or ""):
        log.info("auth.login: wrong password for user_id=%d", user_id)
        security_log.warning(
            "login.failure user_id=%d ip=%s ua_prefix=%s",
            user_id, ip, (request.headers.get("user-agent", "")[:64]),
        )
        return JSONResponse(
            {"error": "Invalid email or password."},
            status_code=401,
        )

    # Password verified — NOW it's safe to surface specific account-state
    # errors. The caller has proven they own the credentials, so revealing
    # suspended/shell status doesn't help an enumeration attacker.
    if user["suspended"]:
        return JSONResponse(
            {"error": "This account has been suspended."},
            status_code=403,
        )

    if not user["password_hash"]:
        # Shell user (created via subproduct signup magic-link) hasn't set
        # a password yet. In practice unreachable because verify_password
        # above will fail against an empty hash, but kept as defense in
        # depth in case verify_password behaviour ever changes.
        return JSONResponse(
            {"error": "Account not finished setup — check your email for the reset link."},
            status_code=401,
        )

    # Opportunistic PBKDF2 iteration upgrade: if this user's hash was written
    # before the iteration-count bump, re-hash at the current cost now that we
    # have the plaintext in hand.
    try:
        if db.password_needs_rehash(password, user["password_hash"], user["password_salt"]):
            new_hash, new_salt = db._hash_password(password)
            with db.conn() as c:
                c.execute(
                    "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                    (new_hash, new_salt, user_id),
                )
            log.info("auth.login: upgraded PBKDF2 iterations for user_id=%d", user_id)
    except Exception as exc:
        log.warning("auth.login: rehash-on-login failed for user_id=%d: %s", user_id, exc)

    # 2FA was removed — login always completes without a second factor.
    has_2fa = False

    response = JSONResponse({
        "success": True,
        "requires_2fa": has_2fa,
        "redirect": "/dashboards",
    })
    await _issue_hardened_session(user_id, request, response)

    log.info("auth.login: user_id=%d success (2fa=%s)", user_id, has_2fa)
    security_log.info(
        "login.success user_id=%d ip=%s 2fa=%s", user_id, ip, has_2fa,
    )
    return response


@app.post("/auth/logout")
@rate_limit(
    limit=20,
    window_seconds=60,
    error_message="Too many logout requests.",
)
async def auth_logout(request: Request):
    """Revoke the hardened session AND the legacy session cookie.

    Rate-limited per-IP (20/min) so an attacker can't spam the endpoint
    to burn CSRF cycles or fill the security-event log. 20/min is
    generous for legitimate multi-tab / multi-device sign-out storms.
    """
    ip = _get_client_ip(request)
    if server._is_rate_limited(f"{ip}:logout", limit=20, window=60):
        # Don't give the spammer a signal; still clear the client-side
        # cookies so a single legitimate click in a spam storm doesn't
        # leave them locked out. Log once per throttle event.
        log.warning("auth.logout: rate-limited ip=%s", ip)
        response = JSONResponse({"ok": True}, status_code=429)
        response.headers["Retry-After"] = "60"
        clear_session_cookie_hardened(response, request)
        clear_session_cookie(response, request)
        return response

    raw_hardened = request.cookies.get(SESSION_COOKIE, "")
    if raw_hardened:
        try:
            db.revoke_user_session_by_token(raw_hardened)
        except Exception:
            pass
    legacy_token = request.cookies.get(COOKIE_NAME)
    if legacy_token:
        try:
            db.delete_session(legacy_token)
        except Exception:
            pass
    response = JSONResponse({"ok": True})
    clear_session_cookie_hardened(response, request)
    clear_session_cookie(response, request)
    return response


@app.get("/api/auth/sessions")
async def api_auth_sessions_list(request: Request):
    """List the current user's active sessions."""
    user = read_hardened_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.list_user_sessions(user["user_id"])
    current_hash = user.get("session_token_hash")
    out = []
    for r in rows:
        ua = r["user_agent"] or ""
        browser = "Unknown"
        osname = "Unknown"
        ua_low = ua.lower()
        if "chrome" in ua_low and "edg" not in ua_low:
            browser = "Chrome"
        elif "firefox" in ua_low:
            browser = "Firefox"
        elif "safari" in ua_low and "chrome" not in ua_low:
            browser = "Safari"
        elif "edg" in ua_low:
            browser = "Edge"
        if "mac os" in ua_low or "macintosh" in ua_low:
            osname = "macOS"
        elif "windows" in ua_low:
            osname = "Windows"
        elif "linux" in ua_low:
            osname = "Linux"
        elif "android" in ua_low:
            osname = "Android"
        elif "iphone" in ua_low or "ipad" in ua_low or "ios" in ua_low:
            osname = "iOS"
        out.append({
            "id": r["id"],
            "browser": browser,
            "os": osname,
            "ip_masked": (r["ip_address"] or "").split(".")[0] + ".…" if r["ip_address"] else "",
            "created_at": r["created_at"],
            "last_active_at": r["last_active_at"],
            "is_current": r["token_hash"] == current_hash,
        })
    return JSONResponse({"sessions": out, "count": len(out)})


@app.delete("/api/auth/sessions/{session_id}")
async def api_auth_sessions_revoke(request: Request, session_id: int):
    """Revoke a specific session (cannot revoke the current one)."""
    user = read_hardened_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Don't let a user kill their own current session via this endpoint.
    with db.conn() as c:
        row = c.execute(
            "SELECT token_hash FROM user_sessions WHERE id = ? AND user_id = ?",
            (session_id, user["user_id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["token_hash"] == user.get("session_token_hash"):
        raise HTTPException(status_code=400, detail="Use /auth/logout to end the current session")
    ok = db.revoke_user_session(session_id, user["user_id"])
    return JSONResponse({"revoked": ok})


@app.delete("/api/auth/sessions")
async def api_auth_sessions_revoke_all(request: Request):
    """Revoke every session except the current one."""
    user = read_hardened_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    count = db.revoke_all_other_user_sessions(
        user["user_id"], user.get("session_token_hash", "")
    )
    return JSONResponse({"revoked": count})
