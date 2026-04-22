"""/ws WebSocket endpoint + /admin/realtime/stats JSON feed.

Why the endpoint lives inside the realtime package rather than in
server.py: the WebSocket catch-all proxy (``websocket_proxy``) in
server.py hard-consumes every subdomain WS path. Registering ``/ws`` as a
dedicated route BEFORE that catch-all (via ``register(app)`` at module
import time) is the only way the two coexist without the proxy
swallowing our upgrades.

Auth rules:
    * Cookie-based session only — never trust query-string tokens.
    * Impersonation cookie respected (admin session is still tied to the
      ADMIN user for the purpose of ``user:{id}`` channel gating).
    * Same Origin-header check the proxy already enforces in production.
    * Gate cookie enforced when ``SITE_ACCESS_TOKEN`` is configured.

Rate limits (see ``channels.py`` for the constants):
    * Max 3 concurrent WebSockets per user — oldest is closed on the 4th.
    * Max 50 channel subscriptions per connection.
    * Max 30 client messages per second per connection. A rolling
      30-message bucket keeps the check cheap.

The admin observability panel hits ``/admin/realtime/stats`` for a live
JSON snapshot; the HTML page itself lives in static/realtime-admin.html
and polls that endpoint every 2 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import deque
from typing import Any, Deque
from urllib.parse import urlparse as _urlparse

from fastapi import Request, WebSocket, WebSocketDisconnect

import db

from . import channels
from .hub import hub


log = logging.getLogger("gateway.realtime.routes")

_WS_DENY_POLICY = 1008          # policy-violation close code
_WS_AUTH_REQUIRED = 4401        # custom code matching spec
_WS_RATE_LIMITED = 4429         # custom code — "too many requests"

# Close-code → disconnect reason string. Kept short so the admin panel
# can show them in a pie chart without wrapping.
_REASON_BY_CODE = {
    1000: "client_closed",
    1001: "going_away",
    1006: "abnormal",
    1008: "policy_violation",
    _WS_AUTH_REQUIRED: "auth_required",
    _WS_RATE_LIMITED: "rate_limited",
}


def _srv():
    """Return the already-imported server module (shared helpers/constants)."""
    return sys.modules.get("server") or sys.modules["__main__"]


def _ws_origin_allowed(ws: WebSocket) -> bool:
    """Reject cross-origin WS upgrades in production. In dev (``IS_PRODUCTION=0``)
    we skip the check so localhost test harnesses can connect without faking
    an Origin header.
    """
    srv = _srv()
    if not getattr(srv, "IS_PRODUCTION", False):
        return True
    origin = (ws.headers.get("origin") or "").lower().strip()
    if not origin:
        return False
    try:
        origin_host = (_urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    for apex in getattr(srv, "ALLOWED_DOMAINS", ()):
        if origin_host == apex or origin_host.endswith("." + apex):
            return True
    return False


def _ws_gate_allowed(ws: WebSocket) -> bool:
    """Mirror the HTTP GateMiddleware for WS upgrades."""
    srv = _srv()
    site_token = getattr(srv, "SITE_ACCESS_TOKEN", None)
    if not site_token:
        return True
    gate_cookie = ws.cookies.get(getattr(srv, "GATE_COOKIE_NAME", "narve_gate_access"), "")
    try:
        return bool(srv._gate_cookie_is_valid(gate_cookie))
    except Exception:
        return False


def _user_from_ws(ws: WebSocket) -> dict | None:
    """Resolve the session cookie on a WebSocket upgrade to a user dict.

    We can't use ``current_user(request)`` because WebSocket upgrades don't
    run the HTTP middleware stack that populates ``request.state.user``.
    Read the cookie directly and walk the same session → user lookup
    ``db.get_session`` uses for HTTP requests.

    Dev bypass is preserved for localhost so tests + the `/admin/realtime`
    page can open a socket without a real login.
    """
    srv = _srv()
    cookie_name = getattr(srv, "COOKIE_NAME", "pm_gateway_session")
    token = ws.cookies.get(cookie_name)
    session = db.get_session(token) if token else None
    if session:
        admin_level = session["is_admin"] or 0
        return {
            "user_id": session["user_id"],
            "email": session["email"] if "email" in session.keys() else "",
            "is_admin": bool(admin_level),
            "admin_level": admin_level,
        }
    # Localhost dev bypass — only when PRODUCTION=0.
    if not getattr(srv, "IS_PRODUCTION", False):
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host in ("localhost", "127.0.0.1") or host.endswith(".localhost"):
            try:
                user_id = srv.ensure_dev_user()
            except Exception:
                return None
            row = db.get_user_by_id(user_id)
            if not row:
                return None
            admin_level = row["is_admin"] or 0
            return {
                "user_id": user_id,
                "email": row["email"] if "email" in row.keys() else "",
                "is_admin": bool(admin_level),
                "admin_level": admin_level,
                "_dev_bypass": True,
            }
    return None


def _log_event(user_id: int | None, event: str, *, channel: str | None = None,
               code: int | None = None, reason: str | None = None, ip: str | None = None) -> None:
    """Fire-and-forget insert into realtime_connection_events (migration 100).

    Called on every connect/disconnect/subscribe/denied event. Kept synchronous
    because sqlite3 is already blocking and the row is tiny; if this ever
    shows up as hot on the profile we can enqueue via the job queue.
    """
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO realtime_connection_events "
                "(user_id, event, channel, code, reason, ip) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, event, channel, code, reason, ip),
            )
    except Exception as exc:  # pragma: no cover — metrics row, don't break WS
        log.debug("realtime event log failed: %s", exc)


# ── WebSocket endpoint ─────────────────────────────────────────────────────


async def ws_endpoint(ws: WebSocket) -> None:
    """Single multiplexed WebSocket endpoint at ``/ws``."""
    ip = (ws.client.host if ws.client else None) or ws.headers.get("x-forwarded-for", "")

    if not _ws_origin_allowed(ws):
        await ws.close(code=_WS_DENY_POLICY, reason="Cross-origin upgrade denied")
        _log_event(None, "denied", code=_WS_DENY_POLICY, reason="origin", ip=ip)
        return
    if not _ws_gate_allowed(ws):
        await ws.close(code=_WS_DENY_POLICY, reason="Gate access required")
        _log_event(None, "denied", code=_WS_DENY_POLICY, reason="gate", ip=ip)
        return

    user = _user_from_ws(ws)
    if not user:
        await ws.close(code=_WS_AUTH_REQUIRED, reason="Not authenticated")
        _log_event(None, "denied", code=_WS_AUTH_REQUIRED, reason="auth", ip=ip)
        return

    # Per-user concurrency cap: close the oldest connections above the limit.
    try:
        to_evict = await hub.evict_oldest_for_user(
            user["user_id"], channels.MAX_CONNECTIONS_PER_USER,
        )
    except Exception:
        to_evict = []
    for old_ws in to_evict:
        try:
            await old_ws.close(code=_WS_RATE_LIMITED, reason="Replaced by newer connection")
        except Exception:
            pass
        await hub.unsubscribe_all(old_ws, reason="replaced")

    await ws.accept()
    await hub.register_connection(ws, user_id=user["user_id"], ip=ip)
    _log_event(user["user_id"], "connect", ip=ip)

    # Message-rate bucket: deque of timestamps, one per received client frame.
    recent: Deque[float] = deque(maxlen=channels.MAX_MESSAGES_PER_SEC)
    close_reason = "client_closed"

    try:
        await ws.send_json({
            "op": "hello",
            "user_id": user["user_id"],
            "server_ts": int(time.time() * 1000),
            "limits": {
                "max_channels": channels.MAX_CHANNELS_PER_CONN,
                "max_messages_per_sec": channels.MAX_MESSAGES_PER_SEC,
            },
        })

        async for raw in ws.iter_text():
            now = time.time()
            recent.append(now)
            # When the bucket is full and all timestamps are within 1s, this
            # client is over-rate. Close rather than throttle so the backoff
            # logic on the client kicks in.
            if len(recent) == recent.maxlen and (recent[-1] - recent[0]) < 1.0:
                close_reason = "rate_limited"
                await ws.close(code=_WS_RATE_LIMITED, reason="Message rate exceeded")
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"op": "error", "error": "invalid_json"})
                continue
            if not isinstance(msg, dict):
                await ws.send_json({"op": "error", "error": "invalid_shape"})
                continue

            op = msg.get("op")
            if op == "ping":
                await ws.send_json({"op": "pong", "server_ts": int(time.time() * 1000)})
                continue

            if op == "subscribe":
                channel = msg.get("channel") or ""
                if len(hub.ws_channels.get(ws, set())) >= channels.MAX_CHANNELS_PER_CONN:
                    await ws.send_json({
                        "op": "denied",
                        "channel": channel,
                        "reason": "channel_cap_reached",
                    })
                    _log_event(user["user_id"], "denied", channel=channel, reason="channel_cap", ip=ip)
                    continue
                if channels.is_channel_allowed(user, channel):
                    await hub.subscribe(ws, channel)
                    await ws.send_json({"op": "subscribed", "channel": channel})
                    _log_event(user["user_id"], "subscribe", channel=channel, ip=ip)
                else:
                    await ws.send_json({"op": "denied", "channel": channel})
                    _log_event(user["user_id"], "denied", channel=channel, reason="auth", ip=ip)
                continue

            if op == "unsubscribe":
                channel = msg.get("channel") or ""
                await hub.unsubscribe(ws, channel)
                await ws.send_json({"op": "unsubscribed", "channel": channel})
                _log_event(user["user_id"], "unsubscribe", channel=channel, ip=ip)
                continue

            await ws.send_json({"op": "error", "error": "unknown_op"})

    except WebSocketDisconnect as exc:
        close_reason = _REASON_BY_CODE.get(exc.code, f"code_{exc.code}")
    except Exception as exc:  # pragma: no cover
        log.warning("ws endpoint failed for user=%s: %s", user.get("user_id"), exc)
        close_reason = "server_error"
    finally:
        await hub.unsubscribe_all(ws, reason=close_reason)
        _log_event(user["user_id"], "disconnect", reason=close_reason, ip=ip)


# ── Admin stats JSON ───────────────────────────────────────────────────────


async def admin_realtime_stats(request: Request):
    """Live stats snapshot for the admin observability page. Admin-only."""
    from fastapi.responses import JSONResponse
    srv = _srv()
    user = srv._require_admin_user(request)
    if isinstance(user, type(None)):  # redirect/HTTPException already handled
        return user  # pragma: no cover
    return JSONResponse(hub.stats())


async def admin_realtime_page(request: Request):
    """Render the admin observability dashboard."""
    srv = _srv()
    user = srv._require_admin_user(request, page=True)
    if user is None:
        return srv._denied_response(request)
    from fastapi.responses import RedirectResponse
    if isinstance(user, RedirectResponse):
        return user
    return srv.render_page(
        "realtime-admin",
        request=request,
        _is_admin=True,
    )


# ── Registration ───────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire /ws, /admin/realtime, /admin/realtime/stats into the app.

    Registration order matters — the module-level ``websocket_proxy``
    catch-all in server.py is decorated during import, which happens
    before ``register()`` runs. Inserting our WebSocket route at the
    front of ``app.router.routes`` ensures Starlette's path matcher
    considers ``/ws`` first.
    """
    from fastapi.responses import HTMLResponse
    from starlette.routing import WebSocketRoute

    ws_route = WebSocketRoute("/ws", ws_endpoint, name="realtime_ws")
    # Prepend so the catch-all ``@app.websocket("/{full_path:path}")`` that
    # was registered at import time doesn't eat this path first.
    app.router.routes.insert(0, ws_route)

    app.add_api_route(
        "/admin/realtime", admin_realtime_page,
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/admin/realtime/stats", admin_realtime_stats,
        methods=["GET"], include_in_schema=False,
    )
