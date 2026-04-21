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
    if (target["is_admin"] or 0) >= (admin.get("admin_level") or 0):
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
    user = _current_user(request)
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


# ── Registration ─────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire all admin routes into the given FastAPI app.

    Called once during server.py import. Idempotent — FastAPI dedupes by
    (path, method) so re-registering just no-ops (with a logged warning).
    """
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
