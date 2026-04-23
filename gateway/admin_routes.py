"""Admin routes for impersonation, feature flags, and email templates.

Registered from server.py via `admin_routes.register(app)` during module
import. Kept in a separate file so the three-feature surface (routes, form
parsers, small render helpers) doesn't balloon server.py further. All
cross-references back into server.py go through `_deps()` below, which
lazily grabs the names from the already-imported server module — this
avoids circular imports at startup.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import re
import time
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import features
import impersonation as _imp


log = logging.getLogger("gateway.admin_routes")


# ── Deferred lookups into server.py ─────────────────────────────────────


def _srv():
    """Return the already-imported server module."""
    import sys
    return sys.modules.get("server") or sys.modules["__main__"]


def _require_admin_user(request, *, page: bool = False):
    return _srv()._require_admin_user(request, page=page)


def _real_admin_user(request):
    return _srv()._real_admin_user(request)


def _denied_response(request):
    return _srv()._denied_response(request)


def _render_page(name, **context):
    return _srv().render_page(name, **context)


def _role_badge(user):
    return _srv()._role_badge(user)


def _set_imp_cookie(response, token, request):
    return _srv()._set_impersonation_cookie(response, token, request)


def _clear_imp_cookie(response, request):
    return _srv()._clear_impersonation_cookie(response, request)


def _client_ip(request):
    return _srv()._get_client_ip(request)


def _current_user(request):
    return _srv().current_user(request)


# ── Shared helpers ───────────────────────────────────────────────────────


def _audit(action, *, admin, request, target_type=None, target_id=None,
           target_description=None, before=None, after=None, notes=None):
    try:
        from security import audit as _a
        _a.log_action(
            admin_user_id=(admin or {}).get("user_id"),
            admin_email=(admin or {}).get("email"),
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_description=target_description,
            before=before, after=after,
            request=request, notes=notes,
        )
    except Exception:
        pass


def _fmt_ts(ts, fmt="%Y-%m-%d %H:%M:%S UTC"):
    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime(fmt)


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# ── Impersonation routes ─────────────────────────────────────────────────


async def impersonate_start(request: Request, user_id: int, reason: str = Form("")):
    admin = _require_admin_user(request)
    reason = (reason or "").strip()
    if not reason or len(reason) < 4:
        raise HTTPException(status_code=400, detail="Reason is required (min 4 chars)")
    if len(reason) > 500:
        reason = reason[:500]

    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot impersonate yourself")
    # C6: admin_level MUST be explicitly present on the actor — missing = fail
    # closed. Without this, a session dict lacking admin_level would silently
    # fall back to 0 and allow weird comparisons.
    admin_level = admin.get("admin_level")
    if admin_level is None or admin_level < 1:
        raise HTTPException(status_code=403, detail="Admin role not verified")
    target_level = target["is_admin"] or 0
    if target_level >= admin_level:
        # Block impersonating a peer-or-higher admin to prevent privilege
        # laundering (admin A → impersonate admin B → take admin B's actions).
        raise HTTPException(status_code=403, detail="Cannot impersonate an equal-or-higher admin")

    imp = db.create_impersonation_session(
        admin_user_id=admin["user_id"],
        target_user_id=user_id,
        reason=reason,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )

    from security import audit as _a
    _audit(
        _a.AuditAction.IMPERSONATION_START,
        admin=admin, request=request,
        target_type="user", target_id=user_id,
        target_description=target["email"],
        notes=f"reason={reason[:200]}",
    )
    log.info("Admin %s started impersonating user_id=%d reason=%r", admin["email"], user_id, reason[:80])

    response = RedirectResponse("/dashboards", status_code=302)
    _set_imp_cookie(response, imp["cookie_token"], request)
    return response


async def impersonate_end(request: Request):
    """Idempotent — a stale cookie always clears on the way out."""
    imp_state = getattr(request.state, "impersonation", None)
    admin = _real_admin_user(request)

    if imp_state:
        try:
            db.end_impersonation_session(imp_state["session_id"], end_reason="admin_ended")
        except Exception as exc:
            log.warning("end_impersonation_session failed: %s", exc)
        from security import audit as _a
        try:
            _a.log_action(
                admin_user_id=imp_state["admin_user_id"],
                admin_email=imp_state.get("admin_email"),
                action=_a.AuditAction.IMPERSONATION_END,
                target_type="user", target_id=imp_state["target_user_id"],
                request=request,
                notes=f"session_id={imp_state['session_id']}",
            )
        except Exception:
            pass

    target_path = "/admin/impersonations" if admin and admin.get("is_admin") else "/login"
    response = RedirectResponse(target_path, status_code=302)
    _clear_imp_cookie(response, request)
    return response


async def impersonations_list(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    sessions = db.list_impersonation_sessions(limit=200)
    rows = []
    for s in sessions:
        started = _fmt_ts(s["started_at"], "%Y-%m-%d %H:%M UTC")
        if s["ended_at"]:
            dur_s = int(s["ended_at"]) - int(s["started_at"])
            status_badge = '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Ended</span>'
        else:
            dur_s = int(time.time()) - int(s["started_at"])
            status_badge = '<span class="badge" style="background:rgba(245,158,11,0.12);color:#f59e0b">Active</span>'

        rows.append(
            f'<a class="admin-row" href="/admin/impersonations/{s["id"]}" '
            f'style="display:flex;text-decoration:none;color:inherit">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">{html.escape(s["admin_email"] or "(deleted)")} → '
            f'<strong>{html.escape(s["target_email"] or "(deleted)")}</strong> {status_badge}</div>'
            f'<div class="admin-row-meta">{html.escape((s["reason"] or "")[:200])} &middot; '
            f'{started} &middot; {_fmt_duration(dur_s)} &middot; '
            f'{int(s["action_count"] or 0)} actions</div>'
            f'</div></a>'
        )

    body = "".join(rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No impersonation sessions yet.</div></div></div>'
    return _render_page(
        "admin-impersonations",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        raw_sessions=body,
    )


async def impersonation_detail(request: Request, session_id: int):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    s = db.get_impersonation_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    admin_row = db.get_user_by_id(s["admin_user_id"]) if s["admin_user_id"] else None
    target_row = db.get_user_by_id(s["target_user_id"]) if s["target_user_id"] else None

    actions = db.list_impersonation_actions(session_id, limit=1000)
    action_rows = []
    for a in actions:
        ts = _fmt_ts(a["timestamp"], "%H:%M:%S")
        status = a["status_code"] if a["status_code"] is not None else "—"
        row_style = ""
        tag = ""
        if a["was_blocked"]:
            row_style = "background:rgba(239,68,68,0.08)"
            tag = ' <span class="badge" style="background:rgba(239,68,68,0.18);color:#ef4444">BLOCKED</span>'
        action_rows.append(
            f'<div class="admin-row" style="{row_style}">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main" style="font-family:ui-monospace,monospace;font-size:12px">'
            f'<span style="color:var(--text-muted);margin-right:10px">{ts}</span>'
            f'<strong>{html.escape(a["method"])}</strong> {html.escape(a["path"])}{tag}'
            f'</div>'
            f'<div class="admin-row-meta">Status: {status}</div>'
            f'</div></div>'
        )

    admin_display = admin_row["email"] if admin_row else "(deleted)"
    target_display = target_row["email"] if target_row else "(deleted)"
    summary = (
        f'<div class="stat-card"><div class="stat-label">Admin</div>'
        f'<div class="stat-value" style="font-size:14px">{html.escape(admin_display)}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Target</div>'
        f'<div class="stat-value" style="font-size:14px">{html.escape(target_display)}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Started</div>'
        f'<div class="stat-value" style="font-size:13px">{_fmt_ts(s["started_at"])}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Ended</div>'
        f'<div class="stat-value" style="font-size:13px">{_fmt_ts(s["ended_at"])}</div></div>'
        f'<div class="stat-card"><div class="stat-label">Actions</div>'
        f'<div class="stat-value">{int(s["action_count"] or 0)}</div></div>'
    )

    return _render_page(
        "admin-impersonation-detail",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        session_id=str(session_id),
        reason=s["reason"] or "",
        ip_address=s["ip_address"] or "",
        raw_summary_cards=summary,
        raw_action_rows="".join(action_rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No actions recorded.</div></div></div>',
    )


# ── Feature flag routes ─────────────────────────────────────────────────


_FLAG_TIERS = ["free", "trader", "pro", "enterprise"]


def _flag_tier_input(current: list) -> str:
    parts = []
    for t in _FLAG_TIERS:
        checked = "checked" if t in current else ""
        parts.append(
            f'<label style="display:inline-flex;align-items:center;gap:6px;margin-right:14px">'
            f'<input type="checkbox" name="tiers" value="{html.escape(t)}" {checked}> '
            f'{html.escape(t)}</label>'
        )
    return "".join(parts)


def _parse_flag_form(form) -> dict:
    def _csv(value):
        if not value:
            return []
        out = []
        for part in str(value).replace("\n", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                pass
        return out

    tiers = form.getlist("tiers") if hasattr(form, "getlist") else []
    kwargs = {
        "name": (form.get("name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "enabled_globally": bool(form.get("enabled_globally")),
        "enabled_for_tiers": [t for t in tiers if t],
        "enabled_for_user_ids": _csv(form.get("enabled_user_ids") or ""),
        "disabled_for_user_ids": _csv(form.get("disabled_user_ids") or ""),
    }
    try:
        kwargs["rollout_percentage"] = int(form.get("rollout_percentage") or 0)
    except ValueError:
        kwargs["rollout_percentage"] = 0
    return kwargs


async def flags_page(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    flags = db.list_feature_flags()
    rows = []
    for f in flags:
        data = features.flag_to_dict(f)
        tiers = ", ".join(data["enabled_for_tiers"]) or "—"
        status = (
            '<span class="badge" style="background:rgba(34,197,94,0.12);color:#22c55e">Enabled</span>'
            if f["enabled_globally"] else
            '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Disabled</span>'
        )
        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main"><code>{html.escape(f["key"])}</code> &middot; '
            f'<strong>{html.escape(f["name"])}</strong> {status}</div>'
            f'<div class="admin-row-meta">Tiers: {html.escape(tiers)} &middot; '
            f'Rollout: {int(f["rollout_percentage"] or 0)}%</div></div>'
            f'<div class="admin-row-actions"><a class="btn btn-primary-outline" style="font-size:11px" '
            f'href="/admin/flags/{html.escape(f["key"])}">Edit</a></div>'
            f'</div>'
        )

    body = "".join(rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No flags yet. Create the first one below.</div></div></div>'
    return _render_page(
        "admin-flags",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        raw_flag_rows=body,
    )


async def flag_create(request: Request):
    admin = _require_admin_user(request)
    form = await request.form()
    key = (form.get("key") or "").strip()
    name = (form.get("name") or "").strip()
    if not key or not re.fullmatch(r"[a-z0-9_\-]{1,80}", key):
        raise HTTPException(status_code=400, detail="Key must be lowercase [a-z0-9_-], ≤80 chars")
    if not name:
        name = key
    if db.get_feature_flag(key):
        raise HTTPException(status_code=409, detail="A flag with that key already exists")

    db.create_feature_flag(
        key=key, name=name,
        description=(form.get("description") or "").strip(),
        updated_by_admin_id=admin["user_id"],
    )
    from security import audit as _a
    _audit(
        _a.AuditAction.FEATURE_FLAG_CREATE,
        admin=admin, request=request,
        target_type="feature_flag", target_id=key,
        target_description=name,
    )
    return RedirectResponse(f"/admin/flags/{key}", status_code=302)


async def flag_edit_page(request: Request, key: str):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    row = db.get_feature_flag(key)
    if not row:
        raise HTTPException(status_code=404, detail="Flag not found")
    data = features.flag_to_dict(row)

    return _render_page(
        "admin-flag-edit",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        flag_key=data["key"],
        flag_name=data["name"],
        flag_description=data["description"],
        enabled_checked="checked" if data["enabled_globally"] else "",
        rollout_percentage=str(data["rollout_percentage"]),
        enabled_user_ids=", ".join(str(x) for x in data["enabled_for_user_ids"]),
        disabled_user_ids=", ".join(str(x) for x in data["disabled_for_user_ids"]),
        raw_tier_checkboxes=_flag_tier_input(data["enabled_for_tiers"]),
    )


async def flag_save(request: Request, key: str):
    admin = _require_admin_user(request)
    row = db.get_feature_flag(key)
    if not row:
        raise HTTPException(status_code=404, detail="Flag not found")
    form = await request.form()
    kwargs = _parse_flag_form(form)
    kwargs["updated_by_admin_id"] = admin["user_id"]
    db.update_feature_flag(key, **kwargs)

    # Feature flags gate what the feed materialises for which tier/user.
    # Flush the feed namespace after any flag change so users don't see
    # stale rows until the 60s TTL expires.
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_feature_flag_change()
    except Exception:
        log.warning("ttl_invalidate on_feature_flag_change failed", exc_info=True)

    from security import audit as _a
    _audit(
        _a.AuditAction.FEATURE_FLAG_UPDATE,
        admin=admin, request=request,
        target_type="feature_flag", target_id=key,
        after=kwargs,
    )
    return RedirectResponse("/admin/flags", status_code=302)


async def flag_delete(request: Request, key: str):
    admin = _require_admin_user(request)
    if not db.delete_feature_flag(key):
        raise HTTPException(status_code=404, detail="Flag not found")
    from security import audit as _a
    _audit(
        _a.AuditAction.FEATURE_FLAG_DELETE,
        admin=admin, request=request,
        target_type="feature_flag", target_id=key,
    )
    return RedirectResponse("/admin/flags", status_code=302)


async def flag_evaluate_api(request: Request, key: str):
    # C5: require authentication — flags leak rollout state and should never
    # be enumerable by anonymous traffic.
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    enabled = features.is_feature_enabled(key, user)
    return JSONResponse({"key": key, "enabled": enabled})


# ── Email template routes ───────────────────────────────────────────────


EDITABLE_EMAIL_TEMPLATES = [
    ("welcome", "Welcome email (sent after signup)"),
    ("token_delivery", "Access-token delivery (invite flow)"),
    ("password_reset", "Password reset link"),
    ("payment_failed", "Payment failed notification"),
    ("subscription_cancelled", "Subscription cancellation"),
    ("account_deletion_confirmation", "Account deletion confirmation"),
    ("account_deleted", "Account deleted notice"),
    ("weekly_digest", "Weekly signal digest"),
    ("market_resolved", "Market resolved notification"),
    ("unsubscribe_confirmation", "Unsubscribe confirmation"),
    ("enquiry_notification", "Enquiry notification (admin alert)"),
    ("morning_briefing", "Daily morning briefing"),
    ("market_mover_alert", "Market mover alert"),
]


def _default_email_variables(key: str) -> list:
    common = ["display_name", "email", "app_url"]
    extras = {
        "welcome": ["tier", "dashboard_url"],
        "token_delivery": ["token", "invite_url"],
        "password_reset": ["reset_url", "expires_at"],
        "payment_failed": ["amount", "retry_url"],
        "subscription_cancelled": ["plan", "ends_at"],
        "account_deletion_confirmation": ["cancel_url", "deletion_at"],
        "account_deleted": [],
        "weekly_digest": ["signals_count", "top_market", "digest_url"],
        "market_resolved": ["market_question", "outcome", "market_url"],
        "unsubscribe_confirmation": [],
        "enquiry_notification": ["enquirer_email", "message"],
        "morning_briefing": ["date", "top_signals"],
        "market_mover_alert": ["market_question", "price_change", "market_url"],
    }.get(key, [])
    seen = set()
    out = []
    for v in common + extras:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def emails_page(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    existing = {r["key"]: r for r in db.list_email_templates()}
    rows = []
    for key, description in EDITABLE_EMAIL_TEMPLATES:
        row = existing.get(key)
        if row:
            ts = _fmt_ts(row["updated_at"], "%b %-d %H:%M UTC")
            status_html = (
                '<span class="badge" style="background:rgba(34,197,94,0.12);color:#22c55e">Custom</span>'
                if row["is_active"] else
                '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Draft</span>'
            )
            subject = row["subject"]
        else:
            ts = "—"
            status_html = '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Default</span>'
            subject = ""

        meta_parts = [html.escape(description)]
        if row:
            meta_parts.append(f"Last edited {html.escape(ts)}")
        if subject:
            meta_parts.append(f"Subject: {html.escape(subject[:80])}")

        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main"><code>{html.escape(key)}</code> {status_html}</div>'
            f'<div class="admin-row-meta">{" &middot; ".join(meta_parts)}</div></div>'
            f'<div class="admin-row-actions"><a class="btn btn-primary-outline" style="font-size:11px" '
            f'href="/admin/emails/{html.escape(key)}">Edit</a></div>'
            f'</div>'
        )

    return _render_page(
        "admin-emails",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        raw_template_rows="".join(rows),
    )


async def email_edit_page(request: Request, key: str):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    known_keys = {k for k, _ in EDITABLE_EMAIL_TEMPLATES}
    if key not in known_keys:
        raise HTTPException(status_code=404, detail="Unknown template key")

    row = db.get_email_template(key)
    variables = _default_email_variables(key)

    if row:
        subject = row["subject"] or ""
        body_html_str = row["body_html"] or ""
        body_text = row["body_text"] or ""
        is_active = bool(row["is_active"])
        try:
            saved_vars = json.loads(row["variables"] or "[]")
            if saved_vars:
                variables = saved_vars
        except (ValueError, TypeError):
            pass
    else:
        from email_system.service import _SUBJECTS as _DEFAULT_SUBJECTS
        subject = _DEFAULT_SUBJECTS.get(key, "narve.ai")
        body_html_str = ""
        body_text = ""
        is_active = True

    var_chips = "".join(
        f'<code style="margin-right:6px;padding:2px 6px;background:var(--surface-hover);'
        f'border-radius:4px;font-size:11px">{{{{ {html.escape(v)} }}}}</code>'
        for v in variables
    )

    return _render_page(
        "admin-email-edit",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        template_key=key,
        subject=subject,
        body_html_text=body_html_str,
        body_text=body_text,
        is_active_checked="checked" if is_active else "",
        has_override="1" if row else "0",
        raw_variable_chips=var_chips,
    )


async def email_save(request: Request, key: str):
    admin = _require_admin_user(request)
    known_keys = {k for k, _ in EDITABLE_EMAIL_TEMPLATES}
    if key not in known_keys:
        raise HTTPException(status_code=404, detail="Unknown template key")

    form = await request.form()
    subject = (form.get("subject") or "").strip()
    body_html_str = form.get("body_html") or ""
    body_text_str = form.get("body_text") or ""
    is_active = bool(form.get("is_active"))
    if not subject:
        raise HTTPException(status_code=400, detail="Subject cannot be empty")
    if not body_html_str.strip():
        raise HTTPException(status_code=400, detail="Body HTML cannot be empty")

    db.upsert_email_template(
        key=key,
        subject=subject,
        body_html=body_html_str,
        body_text=body_text_str,
        variables=_default_email_variables(key),
        is_active=is_active,
        updated_by_admin_id=admin["user_id"],
    )
    from security import audit as _a
    _audit(
        _a.AuditAction.EMAIL_TEMPLATE_UPDATE,
        admin=admin, request=request,
        target_type="email_template", target_id=key,
        notes=f"is_active={is_active}",
    )
    return RedirectResponse("/admin/emails", status_code=302)


async def email_preview(request: Request, key: str):
    _require_admin_user(request)
    form = await request.form()
    subject = form.get("subject") or ""
    body_html_str = form.get("body_html") or ""
    from email_system.service import render_preview
    preview = render_preview(
        subject=subject,
        body_html=body_html_str,
        variables=_default_email_variables(key),
    )
    return JSONResponse(preview)


async def email_reset(request: Request, key: str):
    admin = _require_admin_user(request)
    existed = db.delete_email_template(key)
    if existed:
        from security import audit as _a
        _audit(
            _a.AuditAction.EMAIL_TEMPLATE_RESET,
            admin=admin, request=request,
            target_type="email_template", target_id=key,
        )
    return RedirectResponse("/admin/emails", status_code=302)


# ── Cache admin ──────────────────────────────────────────────────────────
#
# Surfaces the sync TTL cache (cache/ttl.py) to admins: total items, live
# vs expired, hit rate per key prefix, eviction count. The "Clear cache"
# button hits a POST-only endpoint so it can't be triggered via <img> or
# GET-prefetch. Admin-gated via the standard `_require_admin_user` guard.


async def cache_page(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    from cache import ttl_cache
    stats = ttl_cache.stats()

    # Render the per-prefix hit table. Keep styling inline so it inherits
    # the dashboard shell without needing a new /static asset.
    rows = []
    for r in stats["per_prefix"]:
        rate = f"{r['hit_rate'] * 100:.1f}%"
        rows.append(
            f"<tr>"
            f"<td><code>{html.escape(r['prefix'])}</code></td>"
            f"<td class='num'>{r['hits']:,}</td>"
            f"<td class='num'>{r['misses']:,}</td>"
            f"<td class='num'>{r['sets']:,}</td>"
            f"<td class='num'>{rate}</td>"
            f"</tr>"
        )
    per_prefix_html = "".join(rows) or (
        "<tr><td colspan='5' class='muted'>No cache activity yet.</td></tr>"
    )
    hit_rate_pct = f"{stats['hit_rate'] * 100:.2f}%"

    body = f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'><title>Cache — narve admin</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>
body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:1100px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
margin:0 0 8px;letter-spacing:-0.02em}}
.meta{{color:var(--text-tertiary);font-size:12px;font-family:var(--font-mono);
text-transform:uppercase;letter-spacing:0.1em;margin-bottom:32px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:32px}}
.card{{background:var(--bg-raised);border:1px solid var(--border-default);
border-radius:12px;padding:16px}}
.card-label{{font-size:11px;color:var(--text-tertiary);text-transform:uppercase;
letter-spacing:0.08em;margin:0 0 8px;font-family:var(--font-mono)}}
.card-value{{font-size:28px;font-weight:500;margin:0;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border-subtle)}}
th{{color:var(--text-tertiary);font-size:11px;text-transform:uppercase;letter-spacing:0.08em;
font-family:var(--font-mono);font-weight:500}}
.num{{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--font-mono)}}
.muted{{color:var(--text-tertiary);text-align:center;padding:24px;font-style:italic}}
form.clear{{margin-top:32px;padding:20px;background:var(--bg-raised);
border:1px solid var(--border-default);border-radius:12px}}
button.danger{{background:transparent;color:var(--text-primary);
border:1px solid var(--border-default);border-radius:6px;padding:8px 16px;
font-family:var(--font-mono);font-size:11px;text-transform:uppercase;
letter-spacing:0.08em;cursor:pointer}}
button.danger:hover{{border-color:var(--text-primary)}}
</style></head><body>
<h1>Cache</h1>
<p class='meta'>Process-local TTL cache · read hits that skip SQLite</p>

<div class='grid'>
  <div class='card'><p class='card-label'>Live entries</p>
    <p class='card-value'>{stats['live']:,}</p></div>
  <div class='card'><p class='card-label'>Total (live + expired)</p>
    <p class='card-value'>{stats['total']:,} / {stats['max_items']:,}</p></div>
  <div class='card'><p class='card-label'>Hit rate</p>
    <p class='card-value'>{hit_rate_pct}</p></div>
  <div class='card'><p class='card-label'>Evictions</p>
    <p class='card-value'>{stats['evictions']:,}</p></div>
</div>

<h2 style='font-family:var(--font-display);font-style:italic;font-size:24px;margin:32px 0 8px'>
  Per-prefix activity</h2>
<table>
  <thead><tr>
    <th>Prefix</th><th class='num'>Hits</th><th class='num'>Misses</th>
    <th class='num'>Sets</th><th class='num'>Hit rate</th>
  </tr></thead>
  <tbody>{per_prefix_html}</tbody>
</table>

<form class='clear' method='post' action='/admin/cache/clear'
      onsubmit='return confirm(\"Clear ALL cache entries? Reads will hit SQLite until caches rebuild.\")'>
  <p style='margin:0 0 12px;color:var(--text-secondary);font-size:13px'>
    <strong>Danger zone.</strong> Clearing drops every cached entry in this
    process. Reads immediately revert to hitting SQLite; next-read will
    repopulate. Invalidation is normally automatic — only clear when you
    need a hard flush.</p>
  <button class='danger' type='submit'>Clear cache</button>
</form>
</body></html>"""
    return HTMLResponse(body)


async def cache_stats_json(request: Request):
    """JSON snapshot of cache stats. Useful for dashboards / curl checks."""
    _require_admin_user(request)
    from cache import ttl_cache
    return JSONResponse(ttl_cache.stats())


async def cache_clear(request: Request):
    admin = _require_admin_user(request)
    from cache import ttl_invalidate
    removed = ttl_invalidate.everything()

    # Audit trail — a cache clear can mask bugs, we want the record.
    try:
        from security import audit as _a
        _audit(
            _a.AuditAction.SYSTEM_CONFIG_CHANGE,
            admin=admin, request=request,
            target_type="cache", target_id="all",
            after={"removed": removed},
        )
    except Exception:
        log.warning("audit of cache_clear failed", exc_info=True)

    return RedirectResponse("/admin/cache", status_code=302)


# ── /admin/churn ─────────────────────────────────────────────────────────
#
# Reads from the ``churn_signals`` table (populated nightly by
# jobs/compute_churn_signals.py) and the ``cancellation_attempts`` funnel
# log (written by the 3-step cancel flow in billing_routes.py).
#
# Rendered server-side — the numbers are small (one row per subscriber,
# handful of cancel attempts/day) so there's no need for a JSON API +
# client-side chart. Keep everything inline so it stays parseable.


def _churn_risk_distribution() -> list[tuple[str, int]]:
    """Return [(tier, count)] across 'healthy', 'at_risk', 'critical'.

    Missing tiers render as zero so the pie chart is always three slices.
    """
    counts = {"healthy": 0, "at_risk": 0, "critical": 0}
    try:
        with db.conn() as c:
            for row in c.execute(
                "SELECT risk_tier, COUNT(*) AS n FROM churn_signals "
                "WHERE risk_tier IS NOT NULL GROUP BY risk_tier"
            ):
                tier = row["risk_tier"]
                if tier in counts:
                    counts[tier] = int(row["n"])
    except Exception as exc:
        log.warning("admin/churn: risk distribution failed: %s", exc)
    return [(t, counts[t]) for t in ("healthy", "at_risk", "critical")]


def _top_at_risk_users(limit: int = 20) -> list[dict]:
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT cs.user_id, cs.risk_score, cs.risk_tier, cs.engagement_trend,
                       cs.days_since_last_active, cs.computed_at,
                       u.email, u.username
                FROM churn_signals cs
                LEFT JOIN users u ON u.id = cs.user_id
                WHERE cs.risk_tier IN ('at_risk', 'critical')
                ORDER BY cs.risk_score DESC, cs.user_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except Exception as exc:
        log.warning("admin/churn: top at-risk query failed: %s", exc)
        return []
    return [dict(r) for r in rows]


def _cancellation_funnel() -> dict:
    """Return counts per outcome across all cancellation_attempts rows."""
    out = {"total": 0, "retained": 0, "paused": 0, "cancelled": 0, "in_flight": 0}
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT COALESCE(outcome, 'in_flight') AS o, COUNT(*) AS n "
                "FROM cancellation_attempts GROUP BY o"
            ).fetchall()
        for r in rows:
            key = r["o"] if r["o"] in out else "in_flight"
            out[key] += int(r["n"])
            out["total"] += int(r["n"])
    except Exception as exc:
        log.warning("admin/churn: cancel funnel failed: %s", exc)
    return out


def _recent_cancellations(limit: int = 20) -> list[dict]:
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT ca.id, ca.user_id, ca.reason, ca.reached_step, ca.outcome,
                       ca.pause_days, ca.started_at, ca.completed_at,
                       u.email, u.username
                FROM cancellation_attempts ca
                LEFT JOIN users u ON u.id = ca.user_id
                ORDER BY ca.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except Exception as exc:
        log.warning("admin/churn: recent cancels query failed: %s", exc)
        return []
    return [dict(r) for r in rows]


def _render_risk_pie(dist: list[tuple[str, int]]) -> str:
    """SVG donut — each slice coloured by tier. Pure string template so
    we don't pull in a charting library for 3 numbers."""
    total = sum(n for _, n in dist) or 1
    colors = {"healthy": "#10b981", "at_risk": "#f59e0b", "critical": "#ef4444"}
    segs = []
    offset = 0.0
    circ = 2 * 3.14159 * 40  # circumference of r=40 circle
    for tier, n in dist:
        frac = n / total
        dash = frac * circ
        segs.append(
            f'<circle r="40" cx="50" cy="50" fill="transparent" '
            f'stroke="{colors.get(tier, "#888")}" stroke-width="16" '
            f'stroke-dasharray="{dash:.2f} {circ - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 50 50)" />'
        )
        offset += dash
    legend = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;font-size:12px">'
        f'<span style="display:inline-block;width:10px;height:10px;background:{colors.get(t, "#888")};border-radius:2px"></span>'
        f'<span style="color:var(--text-primary)">{t.replace("_", " ").title()}</span>'
        f'<span style="color:var(--text-muted);margin-left:auto;font-variant-numeric:tabular-nums">{n}</span>'
        f'</div>'
        for t, n in dist
    )
    return (
        '<div style="display:flex;gap:24px;align-items:center">'
        f'<svg viewBox="0 0 100 100" width="140" height="140" style="flex:none">{"".join(segs)}</svg>'
        f'<div style="flex:1;display:flex;flex-direction:column;gap:6px;min-width:160px">{legend}</div>'
        '</div>'
    )


def _fmt_pct(numerator: int, denom: int) -> str:
    if not denom:
        return "—"
    return f"{round(100 * numerator / denom)}%"


async def churn_dashboard(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    # _require_admin_user returns a RedirectResponse when 2FA is required.
    # Pass it through directly — don't try to index it like a user dict.
    if not isinstance(admin, dict):
        return admin

    dist = _churn_risk_distribution()
    top = _top_at_risk_users(limit=20)
    funnel = _cancellation_funnel()
    recent = _recent_cancellations(limit=20)

    # Risk distribution pie
    risk_pie_html = _render_risk_pie(dist)

    # Top at-risk table
    if top:
        rows = []
        for u in top:
            label = html.escape(u.get("email") or f"user#{u['user_id']}")
            score = float(u.get("risk_score") or 0.0)
            tier_class = (
                "background:rgba(239,68,68,0.12);color:#ef4444"
                if u.get("risk_tier") == "critical"
                else "background:rgba(245,158,11,0.12);color:#f59e0b"
            )
            days = u.get("days_since_last_active")
            days_str = f"{days}d" if days is not None else "—"
            trend = html.escape(u.get("engagement_trend") or "unknown")
            rows.append(
                f'<tr>'
                f'<td style="padding:8px 12px">{label}</td>'
                f'<td style="padding:8px 12px;font-variant-numeric:tabular-nums">{score:.2f}</td>'
                f'<td style="padding:8px 12px"><span class="badge" style="{tier_class};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{html.escape(u.get("risk_tier") or "")}</span></td>'
                f'<td style="padding:8px 12px;color:var(--text-muted)">{trend}</td>'
                f'<td style="padding:8px 12px;color:var(--text-muted)">{days_str}</td>'
                f'</tr>'
            )
        top_html = (
            '<table style="width:100%;border-collapse:collapse">'
            '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">'
            '<th style="padding:6px 12px">User</th>'
            '<th style="padding:6px 12px">Score</th>'
            '<th style="padding:6px 12px">Tier</th>'
            '<th style="padding:6px 12px">Trend</th>'
            '<th style="padding:6px 12px">Idle</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        top_html = (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);font-size:13px">'
            'No at-risk or critical users yet.</div>'
        )

    # Funnel
    total = funnel["total"] or 1
    funnel_html = (
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">'
        f'<div><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase">Total</div>'
        f'<div style="font-size:24px;font-weight:700;font-variant-numeric:tabular-nums">{funnel["total"]}</div></div>'
        f'<div><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase">Retained</div>'
        f'<div style="font-size:24px;font-weight:700;color:#10b981;font-variant-numeric:tabular-nums">{_fmt_pct(funnel["retained"], total)}</div>'
        f'<div style="font-size:11px;color:var(--text-muted)">{funnel["retained"]} of {funnel["total"]}</div></div>'
        f'<div><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase">Paused</div>'
        f'<div style="font-size:24px;font-weight:700;color:#f59e0b;font-variant-numeric:tabular-nums">{_fmt_pct(funnel["paused"], total)}</div>'
        f'<div style="font-size:11px;color:var(--text-muted)">{funnel["paused"]} of {funnel["total"]}</div></div>'
        f'<div><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase">Cancelled</div>'
        f'<div style="font-size:24px;font-weight:700;color:#ef4444;font-variant-numeric:tabular-nums">{_fmt_pct(funnel["cancelled"], total)}</div>'
        f'<div style="font-size:11px;color:var(--text-muted)">{funnel["cancelled"]} of {funnel["total"]}</div></div>'
        '</div>'
    )

    # Recent cancellations
    if recent:
        rrows = []
        for ca in recent:
            email = html.escape(ca.get("email") or f"user#{ca['user_id']}")
            outcome = ca.get("outcome") or "in_flight"
            outcome_color = {
                "retained": "#10b981",
                "paused": "#f59e0b",
                "cancelled": "#ef4444",
                "in_flight": "var(--text-muted)",
            }.get(outcome, "var(--text-muted)")
            reason = html.escape((ca.get("reason") or "").replace("_", " "))
            started = html.escape(str(ca.get("started_at") or ""))
            pause_info = f" · {ca.get('pause_days')}d pause" if ca.get("pause_days") else ""
            rrows.append(
                f'<tr>'
                f'<td style="padding:8px 12px">{email}</td>'
                f'<td style="padding:8px 12px;color:var(--text-muted);font-size:12px">{started}</td>'
                f'<td style="padding:8px 12px;color:var(--text-muted)">{reason}</td>'
                f'<td style="padding:8px 12px;color:var(--text-muted)">Step {ca.get("reached_step", "?")}</td>'
                f'<td style="padding:8px 12px;color:{outcome_color};font-weight:600;text-transform:capitalize">{outcome.replace("_", " ")}{pause_info}</td>'
                f'</tr>'
            )
        recent_html = (
            '<table style="width:100%;border-collapse:collapse">'
            '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">'
            '<th style="padding:6px 12px">User</th>'
            '<th style="padding:6px 12px">Started</th>'
            '<th style="padding:6px 12px">Reason</th>'
            '<th style="padding:6px 12px">Step</th>'
            '<th style="padding:6px 12px">Outcome</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rrows)}</tbody>'
            '</table>'
        )
    else:
        recent_html = (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);font-size:13px">'
            'No cancellation attempts recorded yet.</div>'
        )

    return _render_page(
        "admin-churn",
        request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        raw_risk_pie=risk_pie_html,
        raw_top_users=top_html,
        raw_funnel=funnel_html,
        raw_recent=recent_html,
    )


# ── Admin: /admin/sharing ────────────────────────────────────────────────
#
# Renders the share-loop dashboard: total shares by type, top-shared
# markets + sources, top sharers by attributed conversions, referrer +
# country breakdowns, and a daily time series for the chart card.
#
# Data helpers live in queries/sharing_metrics.py — imported lazily so
# a partial schema tree (migrations 110-114 not yet applied) doesn't
# break admin_routes.py at module load. If the module isn't available
# we render a benign "migrations pending" panel instead of crashing.


def _render_totals_table(rows: list[dict]) -> str:
    """Three-row table: market / source / prediction × views + conversions.
    Stable column order even with zero rows (same invariant
    sharing_metrics.totals_by_type already guarantees on the data
    side) so the card layout doesn't shift between empty and full
    states."""
    if not rows:
        return (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);'
            'font-size:13px">No share activity in this window.</div>'
        )
    body = []
    for r in rows:
        rate_label = f"{r['conversion_rate_pct']:.1f}%" if r["views"] else "—"
        body.append(
            "<tr>"
            f"<td style=\"padding:8px 12px;text-transform:capitalize\">{html.escape(r['share_type'])}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r['views']):,}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r['conversions']):,}</td>"
            f"<td style=\"padding:8px 12px;color:var(--text-muted);font-variant-numeric:tabular-nums\">{rate_label}</td>"
            "</tr>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:0.05em">'
        '<th style="padding:6px 12px">Type</th>'
        '<th style="padding:6px 12px">Views</th>'
        '<th style="padding:6px 12px">Conversions</th>'
        '<th style="padding:6px 12px">Rate</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _render_items_table(rows: list[dict], *, key: str, label: str) -> str:
    """Top-N table for shared_markets / shared_sources. ``key`` is the
    dict column the row label comes from (e.g. 'market_slug')."""
    if not rows:
        return (
            f'<div style="padding:24px;text-align:center;color:var(--text-muted);'
            f'font-size:13px">No shared {label.lower()} yet in this window.</div>'
        )
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f"<td style=\"padding:8px 12px\">{html.escape(str(r.get(key) or '—'))}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r['views']):,}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums;color:var(--text-muted)\">{int(r.get('distinct_shares', 0)):,}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r.get('conversions', 0)):,}</td>"
            "</tr>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:0.05em">'
        f'<th style="padding:6px 12px">{html.escape(label)}</th>'
        '<th style="padding:6px 12px">Views</th>'
        '<th style="padding:6px 12px">Tokens</th>'
        '<th style="padding:6px 12px">Conversions</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _render_sharers_table(rows: list[dict]) -> str:
    if not rows:
        return (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);'
            'font-size:13px">No attributed conversions yet.</div>'
        )
    body = []
    for r in rows:
        who = html.escape(r.get("username") or r.get("email") or f"user#{r['user_id']}")
        body.append(
            "<tr>"
            f"<td style=\"padding:8px 12px\">{who}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r['conversions']):,}</td>"
            "</tr>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:0.05em">'
        '<th style="padding:6px 12px">User</th>'
        '<th style="padding:6px 12px">Conversions</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _render_breakdown(rows: list[dict], *, key: str, label: str) -> str:
    """Referrer + country breakdown renderers share a shape."""
    if not rows:
        return (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);'
            'font-size:13px">No data in this window.</div>'
        )
    body = []
    total_views = sum(int(r["views"]) for r in rows) or 1
    for r in rows:
        label_text = html.escape(str(r.get(key) or "—"))
        views = int(r["views"])
        pct = views / total_views * 100
        body.append(
            "<tr>"
            f"<td style=\"padding:8px 12px\">{label_text}</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{views:,}</td>"
            f"<td style=\"padding:8px 12px;color:var(--text-muted);font-variant-numeric:tabular-nums\">{pct:.1f}%</td>"
            f"<td style=\"padding:8px 12px;font-variant-numeric:tabular-nums\">{int(r.get('conversions', 0)):,}</td>"
            "</tr>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse">'
        '<thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:0.05em">'
        f'<th style="padding:6px 12px">{html.escape(label)}</th>'
        '<th style="padding:6px 12px">Views</th>'
        '<th style="padding:6px 12px">Share</th>'
        '<th style="padding:6px 12px">Conversions</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _render_sparkline(series: list[dict]) -> str:
    """Inline SVG sparkline of daily totals. 320×60 viewbox, single
    stroke path — no external chart library because this card is
    admin-only and every KB counts on cold load. Empty series
    renders a muted 'no data' line."""
    totals = [int(r.get("total", 0)) for r in series]
    if not totals or max(totals) == 0:
        return (
            '<div style="padding:24px;text-align:center;color:var(--text-muted);'
            'font-size:13px">No views recorded in this window.</div>'
        )
    mx = max(totals)
    w = 320
    h = 60
    step = w / max(1, (len(totals) - 1))
    pts = []
    for i, v in enumerate(totals):
        x = i * step
        y = h - (v / mx) * (h - 4) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    last_label = totals[-1]
    peak_label = mx
    # Accessibility: screen readers need the gist of the sparkline.
    # role=img + aria-label summarise peak + current; the visual
    # labels below remain the primary channel for sighted users.
    aria = (
        f"Daily views over {len(totals)} days. "
        f"Peak {peak_label} per day; most recent day {last_label}."
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" '
        f'aria-label="{html.escape(aria)}" '
        'style="display:block;margin-bottom:8px">'
        f'<polyline points="{poly}" fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
        '<div style="display:flex;justify-content:space-between;'
        'color:var(--text-muted);font-size:11px;font-variant-numeric:tabular-nums">'
        f'<span>peak {peak_label:,}/day</span>'
        f'<span>today {last_label:,}</span>'
        '</div>'
    )


def _render_window_tabs(days: int) -> str:
    """Server-rendered tab strip with the active preset already
    marked. Pre-rendering avoids the post-load class-mutation flash
    the pure-JS version showed on cold navigation."""
    presets = (7, 30, 90)
    out = []
    for d in presets:
        cls = "active" if d == days else ""
        out.append(
            f'<a href="?days={d}" class="{cls}" '
            f'aria-current="{"page" if d == days else "false"}">{d}d</a>'
        )
    return "".join(out)


async def sharing_dashboard(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    if not isinstance(admin, dict):
        return admin

    # Window selector — default 30 days, clamped to [1, 90]. The query
    # param is the one page-state control this dashboard needs; adding
    # a full form would be a distraction from the numbers.
    try:
        days = int(request.query_params.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(90, days))

    # Lazy import: the module requires migrations 110-114. On a partial
    # schema tree we render a guidance panel instead of a 500.
    try:
        from queries import sharing_metrics as sm
    except Exception as exc:
        log.warning("admin/sharing: queries.sharing_metrics import failed: %s", exc)
        return _render_page(
            "admin-sharing", request=request,
            email=admin["email"],
            username=admin.get("username", admin["email"]),
            raw_nav_role=_role_badge(admin),
            _is_admin=admin.get("is_admin"),
            days=days,
            raw_window_tabs=_render_window_tabs(days),
            raw_summary="<div style=\"padding:24px;color:var(--text-muted);"
                        "font-size:13px\">Sharing metrics unavailable — migrations "
                        "110-114 may not yet be applied on this host.</div>",
            raw_sparkline="",
            raw_totals="",
            raw_top_markets="",
            raw_top_sources="",
            raw_top_sharers="",
            raw_referrers="",
            raw_countries="",
        )

    try:
        overall = sm.overall_stats(days=days)
        totals = sm.totals_by_type(days=days)
        top_markets = sm.top_shared_markets(days=days, limit=10)
        top_sources = sm.top_shared_sources(days=days, limit=10)
        sharers = sm.top_sharers(days=days, limit=10)
        referrers = sm.referrer_breakdown(days=days)
        countries = sm.country_breakdown(days=days, limit=10)
        series = sm.daily_timeseries(days=days)
    except Exception:
        log.exception("admin/sharing: query failure")
        overall = {"window_days": days, "total_views": 0, "total_conversions": 0,
                   "conversion_rate_pct": 0.0, "distinct_countries": 0}
        totals = top_markets = top_sources = sharers = []
        referrers = countries = series = []

    rate_display = (
        f"{overall['conversion_rate_pct']:.1f}%"
        if overall["total_views"] else "—"
    )
    summary = (
        '<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px">'
        f'<div><div style="font-size:22px;font-weight:600;font-variant-numeric:tabular-nums">'
        f'{int(overall["total_views"]):,}</div>'
        '<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;'
        'letter-spacing:0.05em;margin-top:2px">Total views</div></div>'
        f'<div><div style="font-size:22px;font-weight:600;font-variant-numeric:tabular-nums">'
        f'{int(overall["total_conversions"]):,}</div>'
        '<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;'
        'letter-spacing:0.05em;margin-top:2px">Conversions</div></div>'
        f'<div><div style="font-size:22px;font-weight:600;font-variant-numeric:tabular-nums">'
        f'{rate_display}</div>'
        '<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;'
        'letter-spacing:0.05em;margin-top:2px">Conversion rate</div></div>'
        f'<div><div style="font-size:22px;font-weight:600;font-variant-numeric:tabular-nums">'
        f'{int(overall["distinct_countries"]):,}</div>'
        '<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;'
        'letter-spacing:0.05em;margin-top:2px">Countries</div></div>'
        '</div>'
    )

    return _render_page(
        "admin-sharing", request=request,
        email=admin["email"],
        username=admin.get("username", admin["email"]),
        raw_nav_role=_role_badge(admin),
        _is_admin=admin.get("is_admin"),
        days=days,
        raw_window_tabs=_render_window_tabs(days),
        raw_summary=summary,
        raw_sparkline=_render_sparkline(series),
        raw_totals=_render_totals_table(totals),
        raw_top_markets=_render_items_table(
            top_markets, key="market_slug", label="Market",
        ),
        raw_top_sources=_render_items_table(
            top_sources, key="source_handle", label="Source",
        ),
        raw_top_sharers=_render_sharers_table(sharers),
        raw_referrers=_render_breakdown(referrers, key="referrer", label="Referrer"),
        raw_countries=_render_breakdown(countries, key="country", label="Country"),
    )


# ── Registration ─────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire all admin routes into the given FastAPI app.

    Called once during server.py import. Idempotent — FastAPI dedupes by
    (path, method) so re-registering just no-ops (with a logged warning).
    """
    app.add_api_route(
        "/admin/churn", churn_dashboard,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )

    # Share-loop dashboard — totals, top items, sharers, referrers, country
    # distribution. Data layer: queries/sharing_metrics.py; DB:
    # share_metrics (migration 114) + shared_* tables (110-112).
    app.add_api_route(
        "/admin/sharing", sharing_dashboard,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )

    app.add_api_route(
        "/admin/users/{user_id}/impersonate", impersonate_start,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/impersonations/end", impersonate_end,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/impersonations", impersonations_list,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/impersonations/{session_id}", impersonation_detail,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )

    app.add_api_route(
        "/admin/flags", flags_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/flags", flag_create,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/flags/{key}", flag_edit_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/flags/{key}", flag_save,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/flags/{key}/delete", flag_delete,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/api/flags/evaluate/{key}", flag_evaluate_api,
        methods=["GET"], include_in_schema=False,
    )

    app.add_api_route(
        "/admin/emails", emails_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/emails/{key}", email_edit_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/emails/{key}", email_save,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/emails/{key}/preview", email_preview,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/emails/{key}/reset", email_reset,
        methods=["POST"], include_in_schema=False,
    )

    # Cache observability + nuclear clear button.
    app.add_api_route(
        "/admin/cache", cache_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/cache/stats", cache_stats_json,
        methods=["GET"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/cache/clear", cache_clear,
        methods=["POST"], include_in_schema=False,
    )
