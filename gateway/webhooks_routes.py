"""Settings + admin pages for outbound webhook subscriptions.

Routes:
  GET  /settings/webhooks             — user's own list + create form
  POST /settings/webhooks             — create a new subscription
  POST /settings/webhooks/{id}/delete — delete (session-auth'd, owner-only)
  POST /settings/webhooks/{id}/test   — fire a synthetic test.ping
  GET  /admin/webhooks                — admin-only: all users' subscriptions

The actual delivery logic lives in gateway/webhooks.py; these handlers
just perform CRUD and admin views over the subscription/delivery tables.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import sys
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db


log = logging.getLogger("gateway.webhooks_routes")


# Max subscriptions per user. Keeps a runaway script from pointing a
# thousand subs at their own endpoint. Enterprise can raise this via
# a direct DB edit; no settings-page knob exists by design.
MAX_WEBHOOKS_PER_USER = 10

_VALID_EVENTS = {
    "best_bet.new",
    "market.resolved",
    "source.credibility_updated",
    "insider_signal.new",
    "user.prediction.resolved",
}


# ── Deferred lookups ───────────────────────────────────────────────────


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


def _current_user(request):
    return _srv().current_user(request)


def _render(name, request, **ctx):
    return _srv().render_page(name, request=request, **ctx)


def _role_badge(user):
    return _srv()._role_badge(user) if hasattr(_srv(), "_role_badge") else ""


def _require_admin(request):
    return _srv()._require_admin_user(request, page=True) if hasattr(_srv(), "_require_admin_user") else None


# ── Helpers ────────────────────────────────────────────────────────────


def _validate_url(url: str) -> str:
    """Return the URL if it's a safe outbound target, else raise 400.

    Rules:
      - Must be https (http only in dev for localhost)
      - No RFC1918 / loopback / metadata hosts in prod
    """
    url = (url or "").strip()
    if len(url) > 500 or not url:
        raise HTTPException(400, "url required (max 500 chars)")
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(400, "Invalid URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "URL must be http(s)://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "URL must include a host")

    # SSRF guard: no loopback / link-local / metadata / RFC1918.
    # Cheap regex rather than resolving — we want users to notice their
    # own mistake immediately, not race a DNS rebinding attack later.
    blocked_host_patterns = [
        r"^localhost$", r"^127\.", r"^0\.0\.0\.0$",
        r"^169\.254\.",                 # link-local (AWS metadata etc.)
        r"^10\.",                        # RFC1918
        r"^192\.168\.",                  # RFC1918
        r"^172\.(1[6-9]|2[0-9]|3[0-1])\.",  # RFC1918
        r"^\[?::1\]?$",                  # IPv6 loopback
        r"^\[?f[cd][0-9a-f]{2}:",        # IPv6 ULA
    ]
    import os as _os
    if _os.environ.get("PRODUCTION", "0") == "1":
        for pat in blocked_host_patterns:
            if re.match(pat, host):
                raise HTTPException(400, f"URL host not allowed: {host}")
    if parsed.scheme == "http" and _os.environ.get("PRODUCTION", "0") == "1":
        raise HTTPException(400, "Production webhooks must use https://")
    return url


def _parse_events(form) -> list[str]:
    """Collect checked events from form. Raise 400 if none or invalid."""
    if hasattr(form, "getlist"):
        picked = form.getlist("events")
    else:
        picked = []
    picked = [e for e in (picked or []) if e in _VALID_EVENTS]
    if not picked:
        raise HTTPException(400, "Select at least one event")
    return picked


def _fmt_ts(ts) -> str:
    import datetime as _dt
    if not ts:
        return "never"
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── User routes ────────────────────────────────────────────────────────


async def webhooks_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/settings/webhooks", status_code=302)

    rows = db.list_webhooks_for_user(user["user_id"])
    row_html: list[str] = []
    for r in rows:
        try:
            events = json.loads(r["events"] or "[]")
        except (ValueError, TypeError):
            events = []
        badges = "".join(
            f'<span class="badge">{html.escape(e)}</span>' for e in events
        )
        active = bool(r["is_active"])
        status_html = (
            '<span class="badge badge-ok">Active</span>' if active
            else '<span class="badge badge-muted">Disabled</span>'
        )
        deliveries = db.list_webhook_deliveries(r["id"], limit=5)
        latest_status = (
            f'last status <code>{deliveries[0]["status_code"] or "—"}</code> '
            f'after {deliveries[0]["attempts"]} attempt(s)'
            if deliveries else "no deliveries yet"
        )
        row_html.append(
            '<div class="row">'
            '<div class="row-main">'
            f'  <div><code>{html.escape(r["url"])}</code> {status_html}</div>'
            f'  <div class="row-meta">Events: {badges} · '
            f'  Last delivered: {_fmt_ts(r["last_delivered_at"])} · '
            f'  {latest_status} · '
            f'  Consecutive failures: {r["consecutive_failures"]}</div>'
            '</div>'
            f'<div class="row-actions">'
            f'<form method="post" action="/settings/webhooks/{r["id"]}/test" style="display:inline">'
            f'<button class="btn" type="submit">Test</button></form> '
            f'<form method="post" action="/settings/webhooks/{r["id"]}/delete" style="display:inline" '
            f'onsubmit="return confirm(\'Delete this subscription? This is permanent.\')">'
            f'<button class="btn btn-danger" type="submit">Delete</button></form>'
            f'</div>'
            '</div>'
        )
    if not row_html:
        row_html.append(
            '<div class="row"><div class="row-main"><div class="row-meta">'
            'No webhooks yet. Create one below.</div></div></div>'
        )

    # Event checkboxes for create form.
    event_choices = "".join(
        f'<label class="event-choice">'
        f'<input type="checkbox" name="events" value="{e}"> '
        f'<code>{e}</code></label>'
        for e in sorted(_VALID_EVENTS)
    )

    at_limit = len(rows) >= MAX_WEBHOOKS_PER_USER
    return _render(
        "settings_webhooks",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        raw_webhook_rows="".join(row_html),
        raw_event_choices=event_choices,
        auto_secret=secrets.token_urlsafe(32),
        create_disabled="disabled" if at_limit else "",
        create_note=(
            f"At webhook limit ({MAX_WEBHOOKS_PER_USER}). Delete an existing one first."
            if at_limit else ""
        ),
    )


async def webhooks_create(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    rows = db.list_webhooks_for_user(user["user_id"])
    if len(rows) >= MAX_WEBHOOKS_PER_USER:
        raise HTTPException(409, "At webhook limit for this account")

    form = await request.form()
    url = _validate_url(form.get("url") or "")
    events = _parse_events(form)
    secret = (form.get("secret") or "").strip() or secrets.token_urlsafe(32)
    if len(secret) > 256:
        raise HTTPException(400, "secret too long")

    try:
        wid = db.create_webhook_subscription(
            user_id=user["user_id"], url=url, events=events, secret=secret,
        )
    except Exception as exc:
        log.exception("create_webhook_subscription failed user=%s: %s", user["user_id"], exc)
        raise HTTPException(500, "Could not save subscription")

    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user["user_id"], admin_email=user["email"],
            action="webhook.create",
            target_type="webhook", target_id=wid,
            request=request, notes=f"url={url[:120]} events={','.join(events)}",
        )
    except Exception:
        pass
    return RedirectResponse("/settings/webhooks", status_code=302)


async def webhooks_delete(request: Request, webhook_id: int):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    ok = db.delete_webhook_subscription(webhook_id, user["user_id"])
    if ok:
        try:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user["user_id"], admin_email=user["email"],
                action="webhook.delete",
                target_type="webhook", target_id=webhook_id,
                request=request,
            )
        except Exception:
            pass
    return RedirectResponse("/settings/webhooks", status_code=302)


async def webhooks_test(request: Request, webhook_id: int):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    sub = db.get_webhook_subscription(webhook_id)
    if not sub or sub["user_id"] != user["user_id"]:
        raise HTTPException(404, "Webhook not found")
    try:
        import webhooks as _w
        result = await _w.fire_test_payload(webhook_id)
    except Exception as exc:
        log.exception("fire_test_payload failed wh=%s: %s", webhook_id, exc)
        result = {"ok": False, "error": str(exc)[:200]}
    return JSONResponse(result)


# ── Admin view ─────────────────────────────────────────────────────────


async def admin_webhooks_page(request: Request):
    admin = _require_admin(request)
    if admin is None:
        if hasattr(_srv(), "_denied_response"):
            return _srv()._denied_response(request)
        raise HTTPException(403, "Admin required")

    rows = db.list_all_webhooks(limit=500)
    row_html: list[str] = []
    for r in rows:
        try:
            events = json.loads(r["events"] or "[]")
        except (ValueError, TypeError):
            events = []
        status_html = (
            '<span class="badge badge-ok">Active</span>' if r["is_active"]
            else '<span class="badge badge-muted">Disabled</span>'
        )
        row_html.append(
            '<div class="row">'
            '<div class="row-main">'
            f'  <div><strong>{html.escape(r["owner_email"] or "(deleted)")}</strong> '
            f'  → <code>{html.escape(r["url"])}</code> {status_html}</div>'
            f'  <div class="row-meta">Events: {", ".join(html.escape(e) for e in events)} · '
            f'  Last delivered: {_fmt_ts(r["last_delivered_at"])} · '
            f'  Consec. failures: {r["consecutive_failures"]} · '
            f'  Total failures: {r["failure_count"]}</div>'
            '</div>'
            '</div>'
        )
    if not row_html:
        row_html.append(
            '<div class="row"><div class="row-main"><div class="row-meta">'
            'No webhooks registered.</div></div></div>'
        )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/webhooks.html",
        page_title="Webhooks",
        active_route="webhooks",
        breadcrumb=[("Admin", "/admin"), ("Webhooks", "/admin/webhooks")],
        raw_webhook_rows="".join(row_html),
    )


# ── Registration ───────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/settings/webhooks", webhooks_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/settings/webhooks", webhooks_create,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/settings/webhooks/{webhook_id}/delete", webhooks_delete,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/settings/webhooks/{webhook_id}/test", webhooks_test,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/admin/webhooks", admin_webhooks_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
