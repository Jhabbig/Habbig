"""Admin /admin/test-emails — preview + send-to-self for every template.

Lets an admin verify a template's render before triggering it on real users.
Three endpoints land here:

    GET  /admin/test-emails                          HTML page (admin shell)
    GET  /admin/test-emails/preview/{template_name}  rendered HTML (iframe-safe)
    POST /admin/test-emails/send                     enqueue a test email to self

Registered as a side-effect of being imported at the bottom of
``server.py`` — mirrors :mod:`admin_cost_alerts_routes` so the import-order
contract that keeps these routes above the catch-all stays intact.

Auth model
----------
Every handler goes through ``server._require_admin_user``. The send
endpoint is rate-limited to 20 sends/hour per admin so a slipped CSRF
token can't be turned into an outbound spam loop. CSRF is enforced for
the POST by the global middleware (no exemption is registered).

The preview endpoint serves rendered template HTML with
``X-Frame-Options: DENY`` set explicitly so the page's own preview pane
(when added) must same-origin iframe — defends against clickjacking of a
template that might contain dangerous links if a context override is
abused. The admin UI today shows the preview in a separate tab.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
from admin_shell import render_admin_page
from security.rate_limiter import rate_limit, get_client_ip


log = logging.getLogger("admin_test_emails")


_TEMPLATES_DIR = Path(__file__).parent / "email_system" / "templates"

# Templates that aren't user-facing on their own. ``base.html`` is the
# layout shell every child template extends — rendering it directly is
# meaningless because the ``{{ content }}`` slot stays empty.
_HIDDEN_TEMPLATES = {"base"}


def _admin_key(request: Request) -> str:
    """Key the rate limiter on the admin user id (not IP) so a compromised
    admin can't rotate IPs around the cap."""
    user = server.current_user(request)
    if user and user.get("is_admin"):
        return f"admin_test_emails:{user['user_id']}"
    return f"admin_test_emails:anon:{get_client_ip(request)}"


def _list_templates() -> list[str]:
    """Enumerate template stems from ``email_system/templates/*.html``.

    Filters out ``base.html`` (layout-only). Sorted alphabetically so the
    UI is stable across page loads.
    """
    if not _TEMPLATES_DIR.is_dir():
        return []
    out: list[str] = []
    for path in sorted(_TEMPLATES_DIR.glob("*.html")):
        stem = path.stem
        if stem in _HIDDEN_TEMPLATES:
            continue
        out.append(stem)
    return out


def _default_context(template: str, admin_email: str) -> dict[str, Any]:
    """Sensible per-template test context.

    Values are picked to exercise every variant of the template's
    branching (e.g. welcome has three mutually-exclusive ``is_*_welcome``
    paths). The admin's own email is plumbed through so anything that
    renders ``{{ email }}`` looks coherent in the inbox.
    """
    common: dict[str, Any] = {
        "display_name": "Test Admin",
        "email": admin_email,
        "app_url": server.APP_URL if hasattr(server, "APP_URL") else "https://narve.ai",
        "subject": None,
    }
    extras: dict[str, dict[str, Any]] = {
        "welcome": {
            "tier": "Pro",
            "dashboard_url": f"{common['app_url']}/dashboards",
            "is_pro_welcome": True,
        },
        "token_delivery": {
            "token": "test-token-abc123",
            "invite_url": f"{common['app_url']}/redeem/test-token-abc123",
        },
        "password_reset": {
            "reset_url": f"{common['app_url']}/reset/test-reset",
            "expires_at": "2026-05-15 12:00 UTC",
        },
        "payment_failed": {
            "amount": "$29.00",
            "retry_url": f"{common['app_url']}/settings/billing",
        },
        "subscription_cancelled": {
            "plan": "Pro",
            "ends_at": "2026-05-31",
        },
        "account_deletion_confirmation": {
            "cancel_url": f"{common['app_url']}/settings/account/cancel-deletion",
            "deletion_at": "2026-05-21",
        },
        "weekly_digest": {
            "signals_count": 42,
            "top_market": "Will rates be cut in June?",
            "digest_url": f"{common['app_url']}/digest",
            "items": [],
        },
        "market_resolved": {
            "market_question": "Will rates be cut in June?",
            "outcome": "Yes",
            "market_url": f"{common['app_url']}/markets/test",
        },
        "enquiry_notification": {
            "enquirer_email": "investor@example.com",
            "message": "Hello — interested in the enterprise tier.",
        },
        "morning_briefing": {
            "date": "2026-05-14",
            "top_signals": [],
        },
        "market_mover_alert": {
            "market_question": "Will rates be cut in June?",
            "price_change": "+18%",
            "market_url": f"{common['app_url']}/markets/test",
        },
        "newsletter_confirm": {
            "confirm_url": f"{common['app_url']}/newsletter/confirm/test",
        },
        "2fa_email_otp": {
            "code": "428917",
            "ip": "203.0.113.10",
            "user_agent": "Mozilla/5.0",
        },
        "2fa_locked": {
            "unlock_url": f"{common['app_url']}/settings/security",
            "ip": "203.0.113.10",
        },
        "winback_7d": {
            "return_url": f"{common['app_url']}/dashboards",
        },
        "winback_30d": {
            "return_url": f"{common['app_url']}/dashboards",
        },
        "weekly_intelligence": {
            "headlines": [],
            "report_url": f"{common['app_url']}/reports/weekly",
        },
        "saved_prediction_resolved": {
            "market_question": "Will rates be cut in June?",
            "outcome": "Yes",
            "market_url": f"{common['app_url']}/markets/test",
        },
        "incident_created": {
            "incident_title": "Scraper degraded",
            "incident_url": f"{common['app_url']}/status",
            "severity": "minor",
        },
        "incident_update": {
            "incident_title": "Scraper degraded",
            "incident_url": f"{common['app_url']}/status",
            "update_body": "Investigating.",
        },
        "incident_resolved": {
            "incident_title": "Scraper degraded",
            "incident_url": f"{common['app_url']}/status",
        },
        "webhook_disabled": {
            "endpoint_url": "https://example.com/hook",
            "reason": "exceeded retry budget",
        },
        "admin_cost_alert": {
            "threshold_usd": "50.00",
            "total_cost_usd": "72.50",
            "alert_date": "2026-05-13",
        },
        "admin_subscription_drift": {
            "drift_count": 3,
        },
        "admin_security_alert": {
            "headline": "Test security alert",
            "detail": "This is a synthetic alert generated from /admin/test-emails.",
        },
        "admin_forensic_alert": {
            "watermark_id": "wm_test_abc123",
            "recipient": admin_email,
        },
        "affiliate_payout_threshold": {
            "amount": "$250.00",
            "payout_url": f"{common['app_url']}/affiliates",
        },
        "referral_invite": {
            "inviter_name": "Test Admin",
            "invite_url": f"{common['app_url']}/r/test",
        },
        "referral_reward": {
            "reward_amount": "$25.00",
            "balance_url": f"{common['app_url']}/settings/referrals",
        },
        "data_export_ready": {
            "download_url": f"{common['app_url']}/exports/test.zip",
            "expires_at": "2026-05-21",
        },
        "account_deleted": {},
        "unsubscribe_confirmation": {},
    }
    ctx = dict(common)
    ctx.update(extras.get(template, {}))
    return ctx


def _is_known_template(name: str) -> bool:
    """Cheap allowlist + path-traversal guard.

    ``name`` reaches us as a path param. We never trust it as a filename
    directly — the renderer does the I/O off ``TEMPLATES_DIR / f"{name}.html"``
    so any caller-supplied ``..`` or absolute path is structurally rejected
    by the dir-list comparison below.
    """
    return name in set(_list_templates())


# ── HTML page ────────────────────────────────────────────────────────────


@server.app.get("/admin/test-emails", response_class=HTMLResponse)
async def admin_test_emails_page(request: Request):
    """Render the /admin/test-emails page inside the admin shell."""
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    templates = _list_templates()
    admin_email = (user.get("email") or "").strip() or "admin@narve.ai"

    # Default context preview (JSON for the override textarea). We show
    # the welcome template's context as the starting point because it's
    # the canonical "happy-path" template with the richest variable surface.
    default_ctx = _default_context("welcome", admin_email)
    default_ctx_json = html.escape(
        json.dumps(default_ctx, indent=2, sort_keys=True, default=str)
    )

    cards = []
    for name in templates:
        cards.append(
            '<div class="test-emails__card" data-template="'
            f'{html.escape(name)}">'
            '<div class="test-emails__card-head">'
            f'<code class="test-emails__card-name">{html.escape(name)}</code>'
            "</div>"
            '<div class="test-emails__card-actions">'
            f'<a class="test-emails__btn test-emails__btn--ghost" '
            f'href="/admin/test-emails/preview/{html.escape(name)}" '
            'target="_blank" rel="noopener">Preview HTML</a>'
            f'<button type="button" class="test-emails__btn" '
            f'data-action="send" data-template="{html.escape(name)}">'
            "Send test to me</button>"
            "</div>"
            "</div>"
        )
    cards_html = "".join(cards) if cards else (
        '<div class="test-emails__empty">No email templates found in '
        "<code>gateway/email_system/templates/</code>.</div>"
    )

    csrf_token = (
        request.cookies.get(server.CSRF_COOKIE_NAME)
        or getattr(getattr(request, "state", None), "csrf_token", None)
        or server._generate_csrf_token()
    )

    return render_admin_page(
        request,
        "admin/test_emails.html",
        page_title="Test email templates",
        active_route="emails",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Email templates", "/admin/emails"),
            ("Test templates", "/admin/test-emails"),
        ],
        admin_email=admin_email,
        template_count=str(len(templates)),
        raw_template_cards=cards_html,
        raw_default_context_json=default_ctx_json,
        csrf_token=html.escape(csrf_token),
    )


# ── Preview endpoint ─────────────────────────────────────────────────────


@server.app.get("/admin/test-emails/preview/{template_name}")
@rate_limit(limit=120, window_seconds=60, key_func=_admin_key)
async def admin_test_emails_preview(request: Request, template_name: str):
    """Return rendered template HTML for iframe / new-tab preview.

    ``Content-Type: text/html`` so the browser paints it like the real
    email would render. ``X-Frame-Options: DENY`` + ``Content-Security-
    Policy: frame-ancestors 'none'`` defend against any other site framing
    a preview page — admins click through to a same-origin window, never
    a third-party.
    """
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")

    if not _is_known_template(template_name):
        raise HTTPException(status_code=404, detail="Unknown email template")

    admin_email = (user.get("email") or "").strip() or "admin@narve.ai"
    ctx = _default_context(template_name, admin_email)

    try:
        from email_system.renderer import render as render_template
        rendered = render_template(template_name, ctx)
    except Exception as exc:
        log.warning("preview render failed for %s: %s", template_name, exc)
        rendered = (
            "<!DOCTYPE html><html><body style='font-family:sans-serif;"
            "padding:24px;color:#900'>"
            f"<h1>Preview render failed</h1><pre>{html.escape(str(exc))}</pre>"
            "</body></html>"
        )

    headers = {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
    }
    return HTMLResponse(rendered, headers=headers)


# ── POST: send test email to self ────────────────────────────────────────


@server.app.post("/admin/test-emails/send")
@rate_limit(limit=20, window_seconds=3600, key_func=_admin_key)
async def admin_test_emails_send(request: Request):
    """Enqueue a test render of ``template`` to the calling admin's inbox.

    Body (JSON): ``{"template": "welcome", "context": {...}}``. ``context``
    is optional — when omitted the per-template default kicks in. ``context``
    is merged on top of the defaults so callers only need to override the
    keys they care about.

    Returns ``{"queued": true, "template": ..., "recipient": ...}``.
    """
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    template = (body.get("template") or "").strip()
    if not template:
        raise HTTPException(status_code=400, detail="Missing 'template'")
    if not _is_known_template(template):
        raise HTTPException(status_code=404, detail="Unknown email template")

    override = body.get("context")
    if override is not None and not isinstance(override, dict):
        raise HTTPException(status_code=400, detail="'context' must be an object")

    admin_email = (user.get("email") or "").strip()
    if not admin_email:
        raise HTTPException(status_code=400, detail="Admin user has no email on file")

    ctx = _default_context(template, admin_email)
    if override:
        ctx.update(override)
    # Always force the recipient context to the admin themselves so an
    # override can't redirect the test send to an arbitrary user.
    ctx["email"] = admin_email

    try:
        from jobs.email_jobs import enqueue_email
        job_id = await enqueue_email(
            to=admin_email,
            template=template,
            context=ctx,
            tags=["admin_test"],
        )
    except Exception as exc:
        log.exception("test email enqueue failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to enqueue email")

    log.info(
        "Admin %s queued test email template=%s job_id=%s",
        admin_email, template, job_id,
    )
    return JSONResponse({
        "queued": True,
        "template": template,
        "recipient": admin_email,
        "job_id": job_id,
    })
