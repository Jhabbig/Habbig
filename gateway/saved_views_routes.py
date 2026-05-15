"""HTTP surface for saved views — CRUD + preview count + share-token resolve.

Registered via ``register(app)``:

  JSON API (auth required)
    POST   /api/saved-views                    — create
    GET    /api/saved-views                    — list mine (?scope=markets optional)
    GET    /api/saved-views/pinned             — sidebar helper
    GET    /api/saved-views/default            — ?scope=markets → the user's default
    GET    /api/saved-views/{id}               — get one (owner only)
    PATCH  /api/saved-views/{id}               — partial update
    DELETE /api/saved-views/{id}               — delete
    POST   /api/saved-views/preview            — ?scope + filters → {count, cached}
    POST   /api/saved-views/{id}/clone         — clone a shared view into mine

  Unauthenticated share surface
    GET    /v/{token}                          — redirects to scope URL with
                                                 the view's filters applied as
                                                 query params; renders an "add
                                                 to my views" banner.

Preview count uses the existing in-memory ``cache`` with a 30s TTL
keyed on (user, scope, filters) per the spec. Unknown filter keys are
dropped, never raised, so a malformed URL never 500s.
"""

from __future__ import annotations

import html as _html
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import saved_views_db as views
import saved_views_schema as schema
from auth.cookies import cookie_domain_for


log = logging.getLogger("saved_views_routes")


# ── Auth helpers — lazy-imported to avoid circular import at module load ────


def _require_user(request: Request) -> dict:
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    return user


def _optional_user(request: Request) -> Optional[dict]:
    import server
    return server.current_user(request)


def _require_subscription(user_id: int) -> None:
    """Saved views are a paid feature. A lapsed sub can still read existing
    views (so share links keep working for link viewers on the read path)
    but cannot create or mutate."""
    if hasattr(db, "has_any_active_subscription"):
        if not db.has_any_active_subscription(user_id):
            raise HTTPException(
                status_code=403, detail="Active subscription required",
            )


# ── Scope URL mapping ───────────────────────────────────────────────────────
#
# Each scope has a canonical landing URL that accepts filter query params.
# The /v/{token} handler redirects to this URL with the saved filters
# spliced in. If your app adds a /dashboard/markets tab later, just change
# the mapping here — no other code needs to move.

_SCOPE_URL = {
    "markets":     "/dashboards",           # no dedicated markets tab yet
    "feed":        "/signal-search",
    "sources":     "/leaderboard",          # source list lives there
    "predictions": "/predictions",
}


def _scope_url_for(scope: str, filters: dict, view_id: Optional[int] = None) -> str:
    base = _SCOPE_URL.get(scope, "/dashboards")
    q = schema.filters_to_query(filters or {})
    if view_id is not None:
        q["view_id"] = str(view_id)
    if not q:
        return base
    return f"{base}?{urlencode(q, doseq=False)}"


# ── Preview cache ──────────────────────────────────────────────────────────
#
# Lazy-imported so a failed cache import (e.g. in tests that don't spin up
# the full stack) never takes the routes down. Failure falls back to a
# no-op memoiser.

def _cache_get_or_compute(key: str, factory, ttl: int = 30):
    try:
        from cache import cache  # type: ignore
        return cache.get_or_compute(key, factory, ttl_seconds=ttl)
    except Exception:
        return factory()


# ── Preview count compiler ─────────────────────────────────────────────────


def _preview_count(scope: str, filters: dict) -> dict:
    """Compile filters to SQL and run COUNT(*). Never raises on malformed
    input — returns {"count": 0, "error": ...} if anything goes sideways."""
    where_sql, params, joins, having = schema.build_where(scope, filters)

    base_map = {
        "markets": ("markets m", []),
        # Feed = predictions joined to markets + credibility.
        "feed": (
            "predictions p "
            "LEFT JOIN markets m ON m.market_id = p.market_id "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle",
            [],
        ),
        "sources": ("source_credibility sc", []),
        "predictions": (
            "predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle",
            [],
        ),
    }

    if scope not in base_map:
        return {"count": 0, "total": 0, "error": "unknown_scope"}

    base_from, _ = base_map[scope]
    join_sql = " ".join(joins) if joins else ""

    where_clause = (" WHERE 1=1 " + where_sql) if where_sql else ""
    having_clause = (" GROUP BY 1 HAVING " + " AND ".join(having)) if having else ""

    sql = (
        f"SELECT COUNT(*) AS n FROM ("
        f"SELECT 1 FROM {base_from} {join_sql}{where_clause}{having_clause}"
        f") AS sub"
    )
    total_sql = f"SELECT COUNT(*) AS n FROM {base_from}"

    try:
        with db.conn() as c:
            row = c.execute(sql, params).fetchone()
            total_row = c.execute(total_sql).fetchone()
    except Exception as exc:
        log.warning("preview_count sql failed: scope=%s err=%s", scope, exc)
        return {"count": 0, "total": 0, "error": "sql_failed"}

    return {
        "count": row["n"] if row else 0,
        "total": total_row["n"] if total_row else 0,
    }


# ── JSON API handlers ──────────────────────────────────────────────────────


async def _parse_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def api_create(request: Request):
    user = _require_user(request)
    _require_subscription(user["user_id"])
    body = await _parse_body(request)

    scope = (body.get("scope") or "").strip().lower()
    if scope not in schema.SCOPES:
        raise HTTPException(status_code=400, detail="Invalid scope")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")

    filters = schema.validate_filters(scope, body.get("filters") or {})
    is_default = bool(body.get("is_default"))
    is_pinned = bool(body.get("is_pinned"))

    row = views.create_view(
        user["user_id"], scope, name, filters,
        is_default=is_default, is_pinned=is_pinned,
    )
    if not row:
        raise HTTPException(
            status_code=403,
            detail=f"View limit reached ({views.MAX_VIEWS_PER_USER_PER_SCOPE} per scope)",
        )
    log.info("saved_view created: user=%s scope=%s name=%s",
             user["user_id"], scope, name[:50])
    return JSONResponse({"view": row}, status_code=201)


async def api_list_mine(request: Request):
    user = _require_user(request)
    scope = request.query_params.get("scope")
    if scope and scope not in schema.SCOPES:
        return JSONResponse({"views": []})
    rows = views.list_user_views(user["user_id"], scope)
    return JSONResponse({
        "views": rows,
        "limit_per_scope": views.MAX_VIEWS_PER_USER_PER_SCOPE,
    })


async def api_list_pinned(request: Request):
    user = _require_user(request)
    rows = views.list_pinned(user["user_id"])
    return JSONResponse({"views": rows})


async def api_get_default(request: Request):
    user = _require_user(request)
    scope = request.query_params.get("scope", "")
    if scope not in schema.SCOPES:
        raise HTTPException(status_code=400, detail="Invalid scope")
    row = views.get_default(user["user_id"], scope)
    return JSONResponse({"view": row})


async def api_get(request: Request, id: int):
    user = _require_user(request)
    row = views.get_user_view(user["user_id"], id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"view": row})


async def api_update(request: Request, id: int):
    user = _require_user(request)
    _require_subscription(user["user_id"])
    body = await _parse_body(request)

    name = body.get("name")
    raw_filters = body.get("filters")
    filters: Optional[dict] = None
    if raw_filters is not None:
        # Look up the view to find its scope — filter validation is per-scope.
        existing = views.get_user_view(user["user_id"], id)
        if not existing:
            raise HTTPException(status_code=404, detail="Not found")
        filters = schema.validate_filters(existing["scope"], raw_filters)

    is_default = body.get("is_default")
    is_pinned = body.get("is_pinned")
    row = views.update_view(
        user["user_id"], id,
        name=(name.strip() if isinstance(name, str) else None),
        filters=filters,
        is_default=bool(is_default) if is_default is not None else None,
        is_pinned=bool(is_pinned) if is_pinned is not None else None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"view": row})


async def api_delete(request: Request, id: int):
    user = _require_user(request)
    ok = views.delete_view(user["user_id"], id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"ok": True})


async def api_preview(request: Request):
    """POST /api/saved-views/preview — returns the count without listing rows.

    Body: {"scope": "markets", "filters": {…}}
    Response: {"count": 47, "total": 1284, "cached": false}
    """
    user = _optional_user(request)  # Preview is cheap; allow anon for marketing.
    body = await _parse_body(request)
    scope = (body.get("scope") or "").strip().lower()
    if scope not in schema.SCOPES:
        raise HTTPException(status_code=400, detail="Invalid scope")
    filters = schema.validate_filters(scope, body.get("filters") or {})

    uid = user["user_id"] if user else None
    ck = schema.cache_key(scope, filters, user_id=uid)
    result = _cache_get_or_compute(ck, lambda: _preview_count(scope, filters))
    return JSONResponse({**result, "scope": scope, "filters": filters})


async def api_clone(request: Request, id: int):
    """POST /api/saved-views/{id}/clone — duplicate someone else's view
    into mine. Used by the /v/{token} "Save to my views" button."""
    user = _require_user(request)
    _require_subscription(user["user_id"])
    body = await _parse_body(request)
    name = body.get("name") if isinstance(body.get("name"), str) else None
    row = views.clone_view(user["user_id"], id, name=name)
    if not row:
        raise HTTPException(status_code=404, detail="View not found or limit reached")
    return JSONResponse({"view": row}, status_code=201)


# ── Share link resolver ────────────────────────────────────────────────────


async def share_view(request: Request, token: str):
    """GET /v/{token} — authless share link.

    Decodes the HMAC, looks up the view, redirects to the scope URL with
    filter params. If the view is gone or token is bad, renders a plain
    error page — no 500.
    """
    view_id = views.verify_view_token(token)
    if view_id is None:
        return HTMLResponse(_share_error_page("This share link is invalid."), status_code=200)

    row = views.get_view(view_id)
    if not row:
        return HTMLResponse(
            _share_error_page("This view has been deleted by its owner."),
            status_code=200,
        )

    # Strip any default/pinned flags the owner set — the recipient should
    # see a neutral preview.
    target = _scope_url_for(row["scope"], row["filters"], view_id=view_id)
    # Attach a one-time flash so the scope page can render an "Added view?"
    # banner. Implementation: cookie set here, read by the scope page JS.
    response = RedirectResponse(url=target, status_code=302)
    # AUDIT #4 HIGH #3 — gate on Secure in production. Cookie is
    # deliberately non-HttpOnly (JS reads it for the flash banner) but
    # a HTTP-downgrade should not leak its value.
    import os as _os
    _is_prod = _os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
    response.set_cookie(
        "narve_shared_view",
        json.dumps({"id": view_id, "name": row["name"][:80], "scope": row["scope"]}),
        max_age=300, httponly=False, samesite="lax", secure=_is_prod,
        domain=cookie_domain_for(request),
    )
    return response


def _share_error_page(msg: str) -> str:
    # Minimal standalone page — no dependency on render_page so it stays
    # cheap and the error path never fails because of a missing template.
    safe = _html.escape(msg)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>narve.ai</title>"
        "<link rel='stylesheet' href='/_gateway_static/gateway.css'>"
        "</head><body>"
        f"<main style='max-width:480px;margin:120px auto;padding:0 24px;text-align:center;color:var(--text-secondary)'>"
        f"<h1 style='font-size:22px;color:var(--text-primary);margin-bottom:12px'>Share link unavailable</h1>"
        f"<p>{safe}</p>"
        "<p><a href='/' style='color:var(--text-primary);text-decoration:underline'>Back to narve.ai</a></p>"
        "</main></body></html>"
    )


# ── /settings/saved-views page ─────────────────────────────────────────────


async def page_settings(request: Request):
    """Manage-views dashboard. Pure HTML shell; the table + toggles are
    driven client-side by fetch calls to /api/saved-views.
    """
    import server
    user = server.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    username = user.get("username") or (user.get("email") or "").split("@")[0]
    role_badge = ""
    if hasattr(server, "_role_badge"):
        try:
            role_badge = server._role_badge(user)
        except Exception:
            pass
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""

    try:
        from sidebar import render_sidebar as _render_sidebar
        sidebar_html = _render_sidebar(
            request, active="settings",
            username=username,
            raw_admin_link=admin_link,
            raw_nav_role=role_badge,
        )
    except Exception:
        sidebar_html = ""

    return server.render_page(
        "settings_saved_views",
        request=request,
        username=username,
        raw_nav_role=role_badge,
        raw_admin_link=admin_link,
        raw_sidebar=sidebar_html,
        _is_admin=user.get("is_admin"),
    )


# ── Registration ───────────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/api/saved-views",               api_create,      methods=["POST"])
    app.add_api_route("/api/saved-views",               api_list_mine,   methods=["GET"])
    app.add_api_route("/api/saved-views/pinned",        api_list_pinned, methods=["GET"])
    app.add_api_route("/api/saved-views/default",       api_get_default, methods=["GET"])
    app.add_api_route("/api/saved-views/preview",       api_preview,     methods=["POST"])
    app.add_api_route("/api/saved-views/{id}",          api_get,         methods=["GET"])
    app.add_api_route("/api/saved-views/{id}",          api_update,      methods=["PATCH"])
    app.add_api_route("/api/saved-views/{id}",          api_delete,      methods=["DELETE"])
    app.add_api_route("/api/saved-views/{id}/clone",    api_clone,       methods=["POST"])
    app.add_api_route("/v/{token}",                     share_view,      methods=["GET"])
    app.add_api_route("/settings/saved-views",          page_settings,   methods=["GET"])
