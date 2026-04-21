"""HTTP + SSE routes for the in-app notification bell.

Registered into the main FastAPI ``app`` by being imported at the bottom
of ``server.py`` (same pattern as ``server_features.py``). Keep this module
free of top-level side-effects beyond route registration — it's safe to
reload under pytest's collection order.

Routes exposed:
    GET    /api/notifications                list
    GET    /api/notifications/unread_count   {"count": int}
    POST   /api/notifications/{id}/read      mark single read
    POST   /api/notifications/read-all       mark all read
    POST   /api/notifications/{id}/archive   archive
    DELETE /api/notifications/{id}           hard delete
    GET    /api/notifications/preferences    prefs dict
    PATCH  /api/notifications/preferences    merge-update prefs
    GET    /api/notifications/stream         SSE stream (text/event-stream)

Every route goes through ``server._require_authenticated`` — the bell is
per-user by construction. Rate limits: generous, the UI polls /unread_count
every 30 s by default. Hard DELETE and POST operations additionally enforce
CSRF via the global ``CSRFMiddleware`` (no exemption) so an attacker can't
mass-archive someone else's feed.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import db
import notifications
import server  # for ``app`` + ``_require_authenticated``
from security.rate_limiter import rate_limit


log = logging.getLogger("notification_routes")


# ── Small helpers ────────────────────────────────────────────────────────────

def _user_key(request: Request) -> str:
    """Rate-limit bucket key: one bucket per authed user (IP fallback for
    the rare unauth request — those will 401 anyway)."""
    user = server.current_user(request)
    if user:
        return f"notif:user:{user['user_id']}"
    # Anonymous hits will 401 before the handler runs, but fall back to IP
    # so a misbehaving client can't burn through the decorator's hit counter
    # by rotating bodies.
    from security.rate_limiter import get_client_ip
    return f"notif:anon:{get_client_ip(request)}"


# ── List + badge count ──────────────────────────────────────────────────────

@server.app.get("/api/notifications")
@rate_limit(limit=120, window_seconds=60, key_func=_user_key)
async def api_notifications_list(
    request: Request,
    unread_only: bool = False,
    type: Optional[str] = None,
    limit: int = 20,
    before_id: Optional[int] = None,
    include_archived: bool = False,
) -> JSONResponse:
    """Paginated feed. Newest first. Keyset pagination via ``before_id``.

    Returns ``{"notifications": [...], "unread_count": int, "next_before_id": int|null}``
    so the client can render the dropdown and the badge in a single round-trip.
    """
    user = server._require_authenticated(request)

    # SECURITY (H10): ``before_id`` is an attacker-controlled integer
    # used for keyset pagination. If db.get_notifications trusts it to
    # resolve a timestamp WITHOUT also constraining the row to the
    # current user, an attacker can pass a victim's notification id as
    # ``before_id`` to enumerate the victim's feed position. Defence:
    # before we pass it to the DB layer, verify the referenced
    # notification belongs to us. The 404 response is identical to
    # "notification does not exist" so we also avoid a cross-user
    # existence oracle.
    if before_id is not None:
        try:
            with db.conn() as c:
                row = c.execute(
                    "SELECT user_id FROM notifications WHERE id = ?",
                    (before_id,),
                ).fetchone()
        except Exception:
            row = None
        if not row or int(row["user_id"]) != int(user["user_id"]):
            # Don't disclose the reason — just behave as "no such cursor".
            raise HTTPException(status_code=404, detail="Invalid pagination cursor")

    try:
        rows = db.get_notifications(
            user_id=user["user_id"],
            unread_only=unread_only,
            type=type or None,
            limit=limit,
            before_id=before_id,
            include_archived=include_archived,
        )
    except Exception:
        log.exception("api_notifications_list failed user=%s", user["user_id"])
        rows = []
    try:
        unread_count = db.get_unread_count(user["user_id"])
    except Exception:
        unread_count = 0
    next_before = rows[-1]["id"] if rows and len(rows) >= limit else None
    return JSONResponse({
        "notifications":   rows,
        "unread_count":    unread_count,
        "next_before_id":  next_before,
    })


@server.app.get("/api/notifications/unread_count")
@rate_limit(limit=240, window_seconds=60, key_func=_user_key)
async def api_notifications_unread_count(request: Request) -> JSONResponse:
    """Cheap badge poller. Runs every 30 s from the bell JS."""
    user = server._require_authenticated(request)
    try:
        count = db.get_unread_count(user["user_id"])
    except Exception:
        count = 0
    return JSONResponse({"count": count})


# ── Read / archive / delete ─────────────────────────────────────────────────

@server.app.post("/api/notifications/{notif_id}/read")
@rate_limit(limit=120, window_seconds=60, key_func=_user_key)
async def api_notification_mark_read(request: Request, notif_id: int) -> JSONResponse:
    user = server._require_authenticated(request)

    # SECURITY (L18): defence-in-depth ownership check. The DB function
    # is expected to filter with ``WHERE id = ? AND user_id = ?`` but
    # we can't edit db.py here, so we also verify ownership at the
    # route level. A mismatched owner returns 404 — identical to
    # "does not exist" so the handler is not a cross-user existence
    # oracle. If the notifications row is gone entirely we still call
    # the DB (its WHERE-clause filter will no-op safely).
    try:
        with db.conn() as c:
            owner_row = c.execute(
                "SELECT user_id FROM notifications WHERE id = ?",
                (notif_id,),
            ).fetchone()
    except Exception:
        owner_row = None
    if owner_row is not None and int(owner_row["user_id"]) != int(user["user_id"]):
        raise HTTPException(status_code=404, detail="Notification not found")

    changed = db.mark_notification_read(notif_id, user["user_id"])
    return JSONResponse({"ok": True, "changed": bool(changed)})


@server.app.post("/api/notifications/read-all")
@rate_limit(limit=20, window_seconds=60, key_func=_user_key)
async def api_notifications_mark_all_read(request: Request) -> JSONResponse:
    user = server._require_authenticated(request)
    count = db.mark_all_notifications_read(user["user_id"])
    return JSONResponse({"ok": True, "marked": count})


@server.app.post("/api/notifications/{notif_id}/archive")
@rate_limit(limit=120, window_seconds=60, key_func=_user_key)
async def api_notification_archive(request: Request, notif_id: int) -> JSONResponse:
    user = server._require_authenticated(request)
    changed = db.archive_notification(notif_id, user["user_id"])
    return JSONResponse({"ok": True, "changed": bool(changed)})


@server.app.delete("/api/notifications/{notif_id}")
@rate_limit(limit=60, window_seconds=60, key_func=_user_key)
async def api_notification_delete(request: Request, notif_id: int) -> JSONResponse:
    user = server._require_authenticated(request)
    changed = db.delete_notification(notif_id, user["user_id"])
    if not changed:
        raise HTTPException(status_code=404, detail="Notification not found")
    return JSONResponse({"ok": True})


# ── Preferences ─────────────────────────────────────────────────────────────

@server.app.get("/api/notifications/preferences")
@rate_limit(limit=60, window_seconds=60, key_func=_user_key)
async def api_notifications_prefs_get(request: Request) -> JSONResponse:
    user = server._require_authenticated(request)
    prefs = db.get_notification_preferences(user["user_id"])
    return JSONResponse(prefs)


@server.app.patch("/api/notifications/preferences")
@rate_limit(limit=30, window_seconds=60, key_func=_user_key)
async def api_notifications_prefs_patch(request: Request) -> JSONResponse:
    """Partial update. Body keys: ``inapp_enabled`` / ``push_enabled`` /
    ``email_enabled`` (bools) and/or ``types`` (dict of type→bool).
    Unknown keys are ignored. Returns the full prefs dict post-update."""
    user = server._require_authenticated(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    kwargs: dict = {}
    for k in ("inapp_enabled", "push_enabled", "email_enabled"):
        if k in body:
            kwargs[k] = bool(body[k])
    types = body.get("types")
    if isinstance(types, dict):
        kwargs["types"] = {str(k): bool(v) for k, v in types.items()}
    try:
        updated = db.set_notification_preferences(user["user_id"], **kwargs)
    except Exception:
        log.exception("prefs update failed user=%s", user["user_id"])
        raise HTTPException(status_code=500, detail="Failed to save preferences")
    return JSONResponse(updated)


# ── Server-Sent Events ──────────────────────────────────────────────────────

@server.app.get("/api/notifications/stream")
async def api_notifications_stream(request: Request) -> StreamingResponse:
    """Long-lived SSE stream — one connection per authenticated client tab.

    Why no @rate_limit on this route: the limiter counts every yield tick
    through the decorator, which would throttle the stream itself. SSE
    bandwidth is self-rate-limited by the subscriber queue's maxsize=100
    in notifications.py.
    """
    user = server._require_authenticated(request)
    gen = notifications.sse_stream(user["user_id"])
    # Note: ``X-Accel-Buffering: no`` prevents nginx from swallowing the
    # stream. Cache-Control=no-cache keeps proxies from collapsing updates.
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering":  "no",
            "Connection":         "keep-alive",
        },
    )


# ── Full-page /notifications route ──────────────────────────────────────────

@server.app.get("/notifications")
async def notifications_page(request: Request):
    """Return the full notification history page. Auth guard redirects
    anonymous users to login (same pattern as /profile, /billing).

    Uses ``server.render_page`` so the shared sidebar / avatar / admin-link
    template vars match every other authed page.
    """
    from fastapi.responses import RedirectResponse
    user = server.current_user(request)
    if not user:
        return RedirectResponse("/login?next=/notifications", status_code=302)
    username = user.get("username") or (user.get("email") or "").split("@")[0] or "user"
    avatar_letter = (username[:1] or "?").upper()
    admin_link = '<a href="/admin" class="nav-item">Admin</a>' if user.get("is_admin") else ""
    return server.render_page(
        "notifications",
        request=request,
        username=username,
        avatar_letter=avatar_letter,
        raw_admin_link=admin_link,
        _is_admin=user.get("is_admin"),
    )
