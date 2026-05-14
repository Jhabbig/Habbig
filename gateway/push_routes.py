"""HTTP routes for Web Push subscription management.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``notification_routes``). Keep module free of top-level side-effects
beyond route registration so pytest's reload cycle stays clean.

Routes:
    GET    /api/push/vapid-key       public VAPID key (unauthed)
    POST   /api/push/subscribe       persist a PushSubscription for this user
    POST   /api/push/unsubscribe     remove one by endpoint
    POST   /api/push/test            fire a self-addressed test notification
    GET    /api/push/subscriptions   list (for settings UI / debugging)

The subscribe endpoint is CSRF-protected via the global middleware, same
as every other POST in this app. The vapid-key endpoint is unauthed since
it's a public constant browsers need before ``pushManager.subscribe()``.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import push
import server
from security.rate_limiter import rate_limit, get_client_ip


log = logging.getLogger("push_routes")


# Canonical push-service hosts. Reject anything else so attackers can't
# coerce the server into sending VAPID-signed payloads to arbitrary HTTPS
# endpoints (SSRF + push-spam vector). Entries prefixed with "*." match
# any host ending in that suffix.
_PUSH_HOST_ALLOWLIST = frozenset({
    # Google FCM (Chrome / Android / Edge)
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.googleapis.com",
    # Mozilla Autopush (Firefox)
    "updates.push.services.mozilla.com",
    "updates-autopush.push.services.mozilla.com",
    # Apple WebPush (Safari)
    "web.push.apple.com",
    "api.push.apple.com",
    # Microsoft WNS (Edge legacy / Windows)
    "*.notify.windows.com",
})


def _is_allowed_push_host(endpoint: str) -> bool:
    """Return True iff ``endpoint`` is https:// and points at a known
    push service host (exact or wildcard-suffix match)."""
    try:
        u = urlparse(endpoint)
    except Exception:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    # Exact match
    if host in _PUSH_HOST_ALLOWLIST:
        return True
    # Wildcard suffix match (entries like "*.notify.windows.com")
    for entry in _PUSH_HOST_ALLOWLIST:
        if entry.startswith("*."):
            suffix = entry[1:]  # ".notify.windows.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
    return False


def _user_key(request: Request) -> str:
    user = server.current_user(request)
    if user:
        return f"push:user:{user['user_id']}"
    return f"push:anon:{get_client_ip(request)}"


# ── Public: VAPID key ────────────────────────────────────────────────────
@server.app.get("/api/push/vapid-key")
@rate_limit(limit=60, window_seconds=60, key_func=lambda r: f"vapid:{get_client_ip(r)}")
async def api_push_vapid_key(request: Request) -> JSONResponse:
    """Return the site's VAPID public key (base64url, uncompressed P-256).

    Browsers pass this into ``pushManager.subscribe({applicationServerKey: ...})``.
    It rotates only when the private key is replaced — extremely rare in
    practice — so clients can cache it.
    """
    try:
        key = push.vapid_public_key()
    except push.PushNotAvailable as exc:
        log.warning("vapid-key: push unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Push not configured")
    return JSONResponse(
        {"publicKey": key},
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── Authed: subscribe ────────────────────────────────────────────────────
@server.app.post("/api/push/subscribe")
@rate_limit(limit=30, window_seconds=60, key_func=_user_key)
async def api_push_subscribe(request: Request) -> JSONResponse:
    """Persist a PushSubscription sent by the browser.

    Body shape mirrors ``PushSubscription.toJSON()``::

        {
          "endpoint": "...",
          "keys": {"p256dh": "...", "auth": "..."}
        }

    Idempotent: the endpoint URL is unique, so re-subscribing overwrites
    the keys + rebinds to the current user. CSRF-protected by the global
    middleware.
    """
    user = server._require_authenticated(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body or {}).get("endpoint")
    keys = (body or {}).get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (isinstance(endpoint, str) and isinstance(p256dh, str) and isinstance(auth, str)):
        raise HTTPException(status_code=400, detail="Missing endpoint or keys")
    if not endpoint.startswith(("https://",)):
        raise HTTPException(status_code=400, detail="endpoint must be https")
    if not _is_allowed_push_host(endpoint):
        raise HTTPException(status_code=422, detail="Unsupported push service host")

    ua = request.headers.get("user-agent", "")[:500] or None
    try:
        push.save_subscription(
            user_id=user["user_id"],
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=ua,
        )
    except Exception:
        log.exception("push.save_subscription failed user=%s", user["user_id"])
        raise HTTPException(status_code=500, detail="Could not save subscription")
    return JSONResponse({"ok": True})


# ── Authed: unsubscribe ──────────────────────────────────────────────────
@server.app.post("/api/push/unsubscribe")
@rate_limit(limit=30, window_seconds=60, key_func=_user_key)
async def api_push_unsubscribe(request: Request) -> JSONResponse:
    user = server._require_authenticated(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body or {}).get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")
    try:
        removed = push.delete_subscription(user["user_id"], endpoint)
    except Exception:
        log.exception("push.delete_subscription failed user=%s", user["user_id"])
        raise HTTPException(status_code=500, detail="Could not remove subscription")
    return JSONResponse({"ok": True, "removed": removed})


# ── Authed: send a test to self ──────────────────────────────────────────
@server.app.post("/api/push/test")
@rate_limit(limit=5, window_seconds=60, key_func=_user_key)
async def api_push_test(request: Request) -> JSONResponse:
    """Useful for the settings UI: "Send yourself a test push" button.

    Returns a summary. If pywebpush isn't installed, surfaces as 503
    — the client then shows a "feature unavailable" notice.
    """
    user = server._require_authenticated(request)
    try:
        result = push.send_to_user(
            user["user_id"],
            title="narve.ai",
            body="Test push — you're all set.",
            url="/dashboards",
            tag="narve-test",
        )
    except push.PushNotAvailable as exc:
        log.warning("push.test: push unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Push not configured")
    except Exception:
        log.exception("push.test failed user=%s", user["user_id"])
        raise HTTPException(status_code=500, detail="Send failed")
    return JSONResponse(result)


# ── Authed: list subscriptions ───────────────────────────────────────────
@server.app.get("/api/push/subscriptions")
@rate_limit(limit=60, window_seconds=60, key_func=_user_key)
async def api_push_subscriptions(request: Request) -> JSONResponse:
    user = server._require_authenticated(request)
    rows = push.list_subscriptions(user["user_id"])
    # Strip key material — callers only need to identify rows, not use them.
    return JSONResponse({
        "subscriptions": [
            {
                "id": r["id"],
                "endpoint": r["endpoint"],
                "user_agent": r["user_agent"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
                "failure_count": r["failure_count"],
            }
            for r in rows
        ]
    })
