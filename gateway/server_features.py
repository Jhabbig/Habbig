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
    """
    token = (token or "").strip()
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
    resp.set_cookie(
        key=_LANG_COOKIE,
        value=lang,
        max_age=60 * 60 * 24 * 180,  # 180 days
        path="/",
        samesite="lax",
        httponly=False,
        secure=os.environ.get("GATEWAY_COOKIE_SECURE", "0").lower() in ("1", "true"),
    )
    return resp


# ── FEATURE 2: Password reset end-to-end ─────────────────────────────────


def _hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@app.post("/auth/forgot-password")
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
async def auth_reset_password(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
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


def _new_referral_code() -> str:
    """8-char uppercase alphanumeric code."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
    return "".join(secrets.choice(alphabet) for _ in range(8))


@app.post("/api/newsletter")
async def api_newsletter_v2(request: Request, email: str = Form(""), ref: str = Form("")):
    """Waitlist signup — assigns a sequential position + referral code.

    Overrides any earlier signup handler because FastAPI dispatches to the
    most-recently-added matching route.
    """
    email = (email or "").strip().lower()
    ref = (ref or "").strip().upper() or None
    if not email or "@" not in email:
        return JSONResponse({"error": "Invalid email"}, status_code=400)

    # Rate limit — reuse existing gateway helper.
    if server._is_rate_limited(f"nl:{_get_client_ip(request)}", limit=5, window=60):
        return JSONResponse({"error": "Too many requests"}, status_code=429)

    with db.conn() as c:
        existing = c.execute(
            "SELECT * FROM newsletter_subscribers WHERE email = ?", (email,)
        ).fetchone()
        if existing and existing["position"]:
            return JSONResponse({
                "success": True,
                "already": True,
                "position": existing["display_position"] or existing["position"],
                "referral_code": existing["referral_code"],
                "share_url": f"{_APP_URL}?ref={existing['referral_code']}",
            })

        # Assign next position atomically.
        row = c.execute("SELECT COALESCE(MAX(position), 0) + 1 AS next FROM newsletter_subscribers").fetchone()
        next_pos = row["next"]
        code = _new_referral_code()
        # Retry if we happened to collide (extremely unlikely).
        while c.execute("SELECT 1 FROM newsletter_subscribers WHERE referral_code = ?", (code,)).fetchone():
            code = _new_referral_code()

        if existing:
            c.execute(
                "UPDATE newsletter_subscribers SET position = ?, display_position = ?, referral_code = ?, referred_by_code = ? WHERE id = ?",
                (next_pos, next_pos, code, ref, existing["id"]),
            )
        else:
            c.execute(
                "INSERT INTO newsletter_subscribers (email, subscribed_at, source, position, display_position, referral_code, referred_by_code) "
                "VALUES (?, ?, 'prerelease', ?, ?, ?, ?)",
                (email, int(time.time()), next_pos, next_pos, code, ref),
            )
    # newsletter_subscribers.id is internal — the public identifier we
    # report back to the user is the assigned waiting-list position.

    # If they came through a ref link, move the referrer up by 5 positions.
    if ref:
        try:
            await _apply_referral_bump(ref)
        except Exception as e:
            log.warning("referral bump failed: %s", e)

    return JSONResponse({
        "success": True,
        "position": next_pos,
        "referral_code": code,
        "share_url": f"{_APP_URL}?ref={code}",
    })


async def _apply_referral_bump(ref: str) -> None:
    """Move the referrer up by 5 display positions (never below 1) and send
    an email confirming the jump."""
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM newsletter_subscribers WHERE referral_code = ?", (ref,)
        ).fetchone()
        if not row:
            return
        new_disp = max(1, (row["display_position"] or row["position"]) - 5)
        c.execute(
            "UPDATE newsletter_subscribers SET display_position = ? WHERE id = ?",
            (new_disp, row["id"]),
        )
    # Best-effort email. Template is simple text inside welcome layout — skip if no SMTP.
    try:
        await enqueue_email(
            to=row["email"],
            template="welcome",
            context={
                "display_name": row["email"].split("@")[0],
                "tier": "waitlist",
            },
            tags=["referral_bump"],
        )
    except Exception:
        pass


@app.get("/api/newsletter/position")
async def api_newsletter_position(request: Request, email: str = ""):
    email = (email or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    if server._is_rate_limited(f"pos:{_get_client_ip(request)}", limit=5, window=60):
        return JSONResponse({"error": "Too many requests"}, status_code=429)
    with db.conn() as c:
        row = c.execute(
            "SELECT position, display_position, referral_code FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)
        referrals = c.execute(
            "SELECT COUNT(*) AS n FROM newsletter_subscribers WHERE referred_by_code = ?",
            (row["referral_code"],),
        ).fetchone()
    return JSONResponse({
        "position": row["display_position"] or row["position"],
        "referral_code": row["referral_code"],
        "referrals_count": referrals["n"] if referrals else 0,
    })


# ── FEATURE 6: Account deletion with 30-day recovery ─────────────────────


@app.post("/api/account/delete")
async def api_account_delete(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
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
    cred = db.get_source_credibility(handle) if hasattr(db, "get_source_credibility") else None
    if not cred or not cred["accuracy_unlocked"]:
        return HTMLResponse(
            "<!DOCTYPE html><html><head><title>Source not found — narve.ai</title></head>"
            "<body><h1>Source not rated yet</h1>"
            "<p>This source has not made enough qualifying predictions to be rated publicly.</p>"
            "<p><a href='/'>Back to narve.ai</a></p></body></html>",
            status_code=404,
        )

    cats = db.get_all_category_credibilities(handle) if hasattr(db, "get_all_category_credibilities") else []
    preds = db.list_recent_predictions(limit=10) if hasattr(db, "list_recent_predictions") else []
    total = cred["total_predictions"] or 0
    correct = cred["correct_predictions"] or 0
    accuracy = int(100 * correct / total) if total else 0
    score = round(cred["global_credibility"], 2)
    tracked_since = _dt.datetime.fromtimestamp(cred["last_computed_at"]).strftime("%B %Y")
    category_rows = "".join(
        f"<tr><td style='padding:10px 0;color:var(--text-primary)'>{_html.escape(c['category'].title())}</td>"
        f"<td style='padding:10px 0;color:var(--text-secondary);text-align:right'>{round(c['category_credibility'], 2)}</td>"
        f"<td style='padding:10px 0;color:var(--text-tertiary);text-align:right'>"
        f"{c['prediction_count']} preds · {int(100 * c['correct_count'] / max(c['prediction_count'], 1))}%</td></tr>"
        for c in cats
    ) or "<tr><td colspan='3' style='padding:14px 0;color:var(--text-tertiary)'>No category breakdown yet.</td></tr>"
    pred_rows = "".join(
        f"<tr><td style='padding:10px 14px;color:var(--text-primary);font-size:13px'>{_html.escape((p['content'] or '')[:160])}</td>"
        f"<td style='padding:10px 14px;color:var(--text-tertiary);font-size:12px'>{_html.escape(p['category'] or '')}</td>"
        f"<td style='padding:10px 14px;color:var(--text-tertiary);font-size:12px'>"
        f"{'resolved' if p['resolved'] else 'open'}</td></tr>"
        for p in preds[:10]
    ) or "<tr><td colspan='3' style='padding:14px 0;color:var(--text-tertiary)'>No recent predictions.</td></tr>"

    meta_desc = (
        f"@{handle} has a credibility score of {score} on narve.ai. "
        f"{accuracy}% accuracy across {total} tracked predictions on Polymarket markets."
    )

    body = f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>@{_html.escape(handle)} Prediction Credibility Score — narve.ai</title>
<meta name='description' content='{_html.escape(meta_desc)}'>
<link rel='canonical' href='{_APP_URL}/sources/{_html.escape(handle)}'>
<meta name='robots' content='index, follow'>
<meta property='og:type' content='profile'>
<meta property='og:site_name' content='narve.ai'>
<meta property='og:title' content='@{_html.escape(handle)} on narve.ai'>
<meta property='og:description' content='{_html.escape(meta_desc)}'>
<meta property='og:url' content='{_APP_URL}/sources/{_html.escape(handle)}'>
<meta property='og:image' content='{_APP_URL}/og/source/{_html.escape(handle)}'>
<meta property='og:image:width' content='1200'>
<meta property='og:image:height' content='630'>
<meta name='twitter:card' content='summary_large_image'>
<meta name='twitter:site' content='@narveai'>
<meta name='twitter:title' content='@{_html.escape(handle)} — narve.ai'>
<meta name='twitter:description' content='{_html.escape(meta_desc)}'>
<meta name='twitter:image' content='{_APP_URL}/og/source/{_html.escape(handle)}'>
<script type='application/ld+json'>
{{
  "@context": "https://schema.org",
  "@type": "Person",
  "name": "@{handle}",
  "description": "{meta_desc}",
  "url": "{_APP_URL}/sources/{handle}"
}}
</script>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=5'>
<style>
.wrap{{max-width:760px;margin:0 auto;padding:56px 24px 80px;font-family:var(--font-ui);color:var(--text-primary)}}
.handle{{font-family:var(--font-display);font-size:36px;font-weight:500;margin:0 0 8px;letter-spacing:-0.02em}}
.cred-card{{background:var(--bg-surface);border:1px solid var(--border-default);border-radius:12px;padding:28px 32px;margin:24px 0}}
.cred-score{{font-family:var(--font-display);font-size:52px;font-weight:500;margin:0;letter-spacing:-0.02em}}
.cred-label{{font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:var(--text-tertiary);margin:0 0 4px}}
.meta-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin:24px 0}}
.meta-grid div{{padding:14px 18px;background:var(--bg-raised);border:1px solid var(--border-default);border-radius:8px}}
.meta-grid strong{{display:block;font-size:18px;margin-bottom:2px}}
.meta-grid span{{font-size:11px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-tertiary);padding:8px 0;border-bottom:1px solid var(--border-default)}}
.cta{{display:inline-block;background:var(--text-primary);color:var(--interactive-text);padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:500;margin-top:24px}}
</style></head><body style='background:var(--bg-base);margin:0'><div class='wrap'>
<a href='/' style='font-size:12px;color:var(--text-tertiary);text-decoration:none'>← narve.ai</a>
<h1 class='handle'>@{_html.escape(handle)}</h1>
<p style='color:var(--text-secondary);font-size:14px'>Public credibility profile</p>

<div class='cred-card'>
  <p class='cred-label'>Global credibility</p>
  <p class='cred-score'>{score}</p>
  <p style='color:var(--text-tertiary);font-size:13px;margin:8px 0 0'>
    Rated · {accuracy}% accuracy across {total} tracked predictions
  </p>
</div>

<div class='meta-grid'>
  <div><strong>{total}</strong><span>Predictions tracked</span></div>
  <div><strong>{correct}</strong><span>Correct</span></div>
  <div><strong>{accuracy}%</strong><span>Accuracy</span></div>
  <div><strong>{tracked_since}</strong><span>Tracked since</span></div>
</div>

<h2 style='font-family:var(--font-display);font-size:22px;margin:40px 0 12px;font-weight:500'>Category scores</h2>
<table>
<thead><tr><th>Category</th><th style='text-align:right'>Score</th><th style='text-align:right'>Accuracy</th></tr></thead>
<tbody>{category_rows}</tbody>
</table>

<h2 style='font-family:var(--font-display);font-size:22px;margin:40px 0 12px;font-weight:500'>Recent predictions</h2>
<table style='background:var(--bg-surface);border:1px solid var(--border-default);border-radius:8px'>
<tbody>{pred_rows}</tbody>
</table>

<a class='cta' href='{_APP_URL}'>View live on narve.ai →</a>
</div></body></html>"""
    return HTMLResponse(body)


@app.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    # Sub-brand subdomains get their own minimal sitemap canonical to
    # themselves — each subdomain is a separate Google property and the
    # branded landing is the only public URL there today.
    sub = server.get_subdomain(request)
    if sub:
        from subproduct import SUBPRODUCTS as _SP
        if sub in _SP:
            base = f"https://{sub}.narve.ai"
            parts = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
                f"<url><loc>{base}/</loc><priority>1.0</priority><changefreq>weekly</changefreq></url>",
                "</urlset>",
            ]
            return Response(content="\n".join(parts), media_type="application/xml")
    # Prefer the generated file on disk (written by the generate_sitemap job).
    sitemap_path = server.STATIC_DIR / "sitemap.xml"
    if sitemap_path.exists():
        return Response(content=sitemap_path.read_text(), media_type="application/xml")
    # Fall back to a live-generated sitemap when no cron run has happened yet.
    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url, p, cf in [("/", "1.0", "daily"), ("/terms", "0.5", "monthly"),
                       ("/privacy", "0.5", "monthly"), ("/pricing", "0.8", "weekly")]:
        parts.append(f"<url><loc>{_APP_URL}{url}</loc><priority>{p}</priority><changefreq>{cf}</changefreq></url>")
    try:
        sources = db.list_all_source_credibilities() if hasattr(db, "list_all_source_credibilities") else []
        for s in sources:
            if s["accuracy_unlocked"]:
                parts.append(
                    f"<url><loc>{_APP_URL}/sources/{s['source_handle']}</loc>"
                    f"<priority>0.7</priority><changefreq>weekly</changefreq></url>"
                )
    except Exception:
        pass
    parts.append("</urlset>")
    return Response(content="\n".join(parts), media_type="application/xml")


@app.get("/robots.txt")
async def robots_txt(request: Request):
    sub = server.get_subdomain(request)
    if sub:
        from subproduct import SUBPRODUCTS as _SP
        if sub in _SP:
            body = (
                "User-agent: *\n"
                "Allow: /\n"
                "Disallow: /admin/\n"
                "Disallow: /api/\n"
                "Disallow: /dashboard/\n"
                "Disallow: /gate\n"
                f"Sitemap: https://{sub}.narve.ai/sitemap.xml\n"
            )
            return PlainTextResponse(body)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /sources/\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/\n"
        "Disallow: /gate\n"
        f"Sitemap: {_APP_URL}/sitemap.xml\n"
    )
    return PlainTextResponse(body)


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
    notes = (body.get("notes") or None) if isinstance(body, dict) else None
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
):
    user = _require_auth(request)
    rows = db.list_saved_predictions(user["user_id"], resolved_filter=resolved, sort=sort)
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
    return JSONResponse({"items": items, "count": len(items)})


@app.patch("/api/saved/{prediction_id}")
async def api_update_saved_notes(request: Request, prediction_id: int):
    user = _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        return JSONResponse({"error": "notes must be a string or null"}, status_code=400)
    if isinstance(notes, str) and len(notes) > 2000:
        return JSONResponse({"error": "notes too long (max 2000 chars)"}, status_code=400)
    ok = db.update_saved_prediction_notes(user["user_id"], prediction_id, notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Saved prediction not found")
    return JSONResponse({"updated": True, "notes": notes})


@app.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    from server import _role_badge  # type: ignore
    admin_link = '<a href="/admin" class="nav-item">Admin</a>' if user.get("is_admin") else ""
    return render_page(
        "saved",
        request=request,
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user),
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
async def api_list_following(request: Request):
    user = _require_auth(request)
    rows = db.list_followed_sources(user["user_id"])
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
# TOKEN-FIRST AUTH FLOW (/token → /register or /login → /dashboard)
# ═══════════════════════════════════════════════════════════════════════
#
# Flow:
#   1. /token                   — single input, validates invite token
#   2. POST /auth/validate-token → sets pending_token cookie, returns
#      {valid, claimed, email_hint?}
#   3. /register                — requires pending_token, shows form
#   4. POST /auth/register      — creates user, issues session, clears
#      pending_token
#   5. /login                   — requires pending_token, shows form
#   6. POST /auth/login         — verifies password, issues session,
#      clears pending_token
#
# Two cookies coexist during the rollout:
#   - narve_session (new, hardened, SHA-256 at rest, 7-day TTL)
#   - pm_gateway_session (old, still written so CSRF/2FA/audit work)
# Every login/register writes to BOTH so legacy helpers keep running.

from auth.cookies import (
    SESSION_COOKIE,
    set_pending_token_cookie,
    clear_pending_token_cookie,
    read_pending_token,
    set_session_cookie_hardened,
    clear_session_cookie_hardened,
)
from auth.guards import (
    read_hardened_session,
    require_pending_token,
)


@app.get("/token", response_class=HTMLResponse)
async def token_page(request: Request):
    """The only entry point to the site for unauthenticated users."""
    sub = server.get_subdomain(request)
    if sub:
        return await server.proxy_request(request, "/token")
    # If already authenticated, short-circuit to the dashboard.
    if read_hardened_session(request) or server.current_user(request):
        return RedirectResponse("/dashboards", status_code=302)
    return render_page("token", request=request, error="")


@app.post("/auth/validate-token")
async def auth_validate_token(request: Request):
    """Check an invite token and issue a pending_token cookie.

    Rate limit: 10 attempts per minute per IP.
    Response: {valid: bool, claimed: bool, email_hint?: str}
    """
    ip = _get_client_ip(request)
    # Per-IP cap tightened to 5/min (H4): shared IP floods + token guessing.
    if server._is_rate_limited(f"{ip}:token-validate", limit=5, window=60):
        return JSONResponse(
            {"valid": False, "error": "Too many attempts. Wait 60 seconds."},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_token = (body.get("token") or "").strip()
    if not raw_token or len(raw_token) > 128:
        return JSONResponse({"valid": False}, status_code=400)

    # Per-token bucket so a single token cannot be hammered by a botnet.
    if server._is_rate_limited(f"token-validate:{raw_token[:32]}", limit=10, window=600):
        return JSONResponse(
            {"valid": False, "error": "Too many attempts for this token."},
            status_code=429,
            headers={"Retry-After": "600"},
        )

    invite = db.get_invite_token(raw_token)
    if not invite or invite["status"] == "revoked":
        return JSONResponse({"valid": False})

    claimed = invite["status"] == "claimed"
    email_hint = ""
    if claimed and invite["claimed_by_email"]:
        email_hint = db.mask_email(invite["claimed_by_email"])

    resp = JSONResponse({
        "valid": True,
        "claimed": claimed,
        "email_hint": email_hint,
    })
    set_pending_token_cookie(resp, raw_token, request)
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Account creation — requires a valid pending_token cookie."""
    sub = server.get_subdomain(request)
    if sub:
        return await server.proxy_request(request, "/register")
    # Already logged in → dashboard
    if read_hardened_session(request) or server.current_user(request):
        return RedirectResponse("/dashboards", status_code=302)
    redirect = require_pending_token(request)
    if redirect:
        return redirect
    raw_token = read_pending_token(request)
    invite = db.get_invite_token(raw_token) if raw_token else None
    if not invite or invite["status"] != "unclaimed":
        # Claimed tokens go to /login instead
        if invite and invite["status"] == "claimed":
            return RedirectResponse("/login", status_code=302)
        return RedirectResponse("/token", status_code=302)
    target_email = ""
    try:
        target_email = invite["target_email"] or ""
    except (KeyError, IndexError):
        target_email = ""
    return render_page(
        "register",
        request=request,
        error="",
        target_email=target_email,
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
    clear_pending_token_cookie(response, request)
    # Rotate the CSRF token on every successful session issuance so any token
    # captured on a public page before login cannot be reused post-auth.
    try:
        server._set_csrf_cookie(response, server._generate_csrf_token(), request)
    except Exception:
        pass  # Cookie rotation is defense-in-depth; never block login on failure.
    return raw_hardened


@app.post("/auth/register")
async def auth_register(request: Request):
    """Create the user bound to the pending_token's invite token."""
    ip = _get_client_ip(request)
    if server._is_rate_limited(f"{ip}:register", limit=5, window=600):
        return JSONResponse({"error": "Too many registration attempts."}, status_code=429)

    redirect = require_pending_token(request)
    if redirect:
        return JSONResponse({"error": "Session expired. Start again from /token."}, status_code=401)
    raw_token = read_pending_token(request) or ""

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    display_name = (body.get("display_name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    confirm_password = body.get("confirm_password") or ""

    if not display_name or len(display_name) < 2 or len(display_name) > 40:
        return JSONResponse(
            {"error": "Display name must be 2-40 characters.", "field": "display_name"},
            status_code=400,
        )
    if not email or "@" not in email or len(email) > 254:
        return JSONResponse({"error": "Enter a valid email.", "field": "email"}, status_code=400)
    if password != confirm_password:
        return JSONResponse(
            {"error": "Passwords do not match.", "field": "password"},
            status_code=400,
        )
    pw_err = _is_strong_password(password)
    if pw_err:
        return JSONResponse({"error": pw_err, "field": "password"}, status_code=400)

    # Token must still be valid and unclaimed at write time.
    invite = db.get_invite_token(raw_token)
    if not invite or invite["status"] != "unclaimed":
        return JSONResponse(
            {"error": "This token has already been claimed."},
            status_code=409,
        )

    if db.get_user_by_email(email):
        return JSONResponse(
            {"error": "An account with that email already exists.", "field": "email"},
            status_code=400,
        )

    # display_name reuses the username slot — must match USERNAME_RE roughly
    username_base = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in display_name)[:20]
    if len(username_base) < 3:
        username_base = email.split("@")[0][:20]
    # Ensure uniqueness
    username = username_base
    suffix = 1
    while db.get_user_by_username(username):
        suffix += 1
        username = f"{username_base[:18]}{suffix}"

    user_id = db.create_user(email, password, username=username)
    if not db.claim_invite_token(raw_token, user_id, email):
        return JSONResponse(
            {"error": "This token was just claimed by someone else."},
            status_code=409,
        )

    # Auto-activate subscription if the token came from a purchase
    try:
        note = invite["note"] or ""
    except Exception:
        note = ""
    if note.startswith("Subscription:"):
        parts = note.replace("Subscription:", "").strip().split("(")
        sub_plan = parts[0].strip().lower() if parts else ""
        sub_interval = parts[1].rstrip(")").strip().lower() if len(parts) > 1 else "monthly"
        if sub_plan in ("trader", "pro"):
            duration = 30 if sub_interval == "monthly" else 365
            if sub_plan == "pro":
                for key in server.DASHBOARDS:
                    db.upsert_subscription(
                        user_id=user_id, dashboard_key=key,
                        plan=f"pro_{sub_interval}", duration_days=duration,
                        source="subscribe_checkout",
                    )
            else:
                db.upsert_subscription(
                    user_id=user_id, dashboard_key="__plan__",
                    plan=f"trader_{sub_interval}", duration_days=duration,
                    source="subscribe_checkout",
                )

    response = JSONResponse({"success": True, "user_id": user_id})
    await _issue_hardened_session(user_id, request, response)
    log.info("auth.register: user_id=%d email=%s via token=%s...", user_id, email, raw_token[:8])
    return response


@app.post("/auth/login")
async def auth_login(request: Request):
    """Password check against the user bound to the pending_token."""
    ip = _get_client_ip(request)
    if server._is_rate_limited(f"{ip}:login-auth", limit=10, window=300):
        return JSONResponse(
            {"error": "Too many attempts."},
            status_code=429,
            headers={"Retry-After": "300"},
        )

    redirect = require_pending_token(request)
    if redirect:
        return JSONResponse({"error": "Session expired. Start again from /token."}, status_code=401)
    raw_token = read_pending_token(request) or ""

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    password = body.get("password") or ""
    if not password or len(password) > 256:
        return JSONResponse({"error": "Incorrect password."}, status_code=401)

    invite = db.get_invite_token(raw_token)
    if not invite or invite["status"] != "claimed":
        return JSONResponse(
            {"error": "This token is not linked to an account."},
            status_code=401,
        )

    user_id = invite["claimed_by_user_id"]
    user = db.get_user_by_id(user_id)
    if not user:
        return JSONResponse({"error": "Account not found."}, status_code=401)

    # Per-email rate limit (H1): credential-stuffing across rotating IPs.
    # 5 wrong attempts per 10min per email regardless of source IP.
    try:
        email_key = (user["email"] or "").strip().lower()
    except (KeyError, IndexError):
        email_key = ""
    if email_key and server._is_rate_limited(f"email:{email_key}:login", limit=5, window=600):
        return JSONResponse(
            {"error": "Too many attempts for this account."},
            status_code=429,
            headers={"Retry-After": "600"},
        )

    if user["suspended"]:
        return JSONResponse({"error": "This account has been suspended."}, status_code=403)

    if not db.verify_password(password, user["password_hash"], user["password_salt"]):
        log.info("auth.login: wrong password for user_id=%d", user_id)
        security_log.warning(
            "login.failure user_id=%d ip=%s ua_prefix=%s",
            user_id, ip, (request.headers.get("user-agent", "")[:64]),
        )
        return JSONResponse({"error": "Incorrect password."}, status_code=401)

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
    })
    await _issue_hardened_session(user_id, request, response)

    log.info("auth.login: user_id=%d success (2fa=%s)", user_id, has_2fa)
    security_log.info(
        "login.success user_id=%d ip=%s 2fa=%s", user_id, ip, has_2fa,
    )
    return response


@app.post("/auth/logout")
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
        clear_pending_token_cookie(response, request)
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
    clear_pending_token_cookie(response, request)
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
