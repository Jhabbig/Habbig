"""Gateway SSO + hard paywall for the annoyance dashboard.

The dashboard sits behind the narve.ai gateway reverse proxy. Every
request arrives with gateway-injected headers:

  X-Gateway-Secret    shared HMAC secret (must match GATEWAY_SSO_SECRET)
  X-Gateway-User-ID   integer user id
  X-Gateway-User-Email  user email
  X-Gateway-User-Tier   one of {"free", "pro", "super_admin"}

Tier policy (decision #4 — hard paywall):
  - free            → 402 Payment Required (upgrade_url to narve.ai/billing)
  - pro             → allow /api/* (except /admin/*)
  - super_admin     → allow everything, including /admin/*
                      but only if the request also originates from localhost

Adapted from crypto-dashboard/server.py:81-127.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


log = logging.getLogger("annoyance.auth")


PAYWALL_UPGRADE_URL = os.environ.get(
    "PAYWALL_UPGRADE_URL", "https://narve.ai/billing"
).strip()

ALLOWED_TIERS_API = ("pro", "super_admin")
ADMIN_TIER = "super_admin"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def paywall_response() -> JSONResponse:
    """Canonical 402 payload. Kept in one place so tests and routes agree."""
    return JSONResponse(
        {"error": "paywall", "upgrade_url": PAYWALL_UPGRADE_URL},
        status_code=402,
    )


def _gateway_secret() -> str:
    """Read the shared secret at call time so tests can override env vars."""
    return os.environ.get("GATEWAY_SSO_SECRET", "").strip()


def get_session_user(request: Request) -> Optional[dict]:
    """Return the authenticated user from gateway headers, or None.

    The gateway proves the request came from a trusted narve.ai process by
    sending the shared ``GATEWAY_SSO_SECRET`` in the ``X-Gateway-Secret``
    header. ``hmac.compare_digest`` guarantees the comparison is constant
    time so an attacker cannot infer the secret byte-by-byte.

    Returns None if the secret is missing/invalid or if the gateway did not
    attach the user id / email headers.
    """
    expected = _gateway_secret()
    if not expected:
        log.warning("get_session_user: GATEWAY_SSO_SECRET not set")
        return None

    provided = request.headers.get("x-gateway-secret", "")
    if not provided:
        return None
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        log.warning("get_session_user: invalid gateway secret")
        return None

    gw_id = request.headers.get("x-gateway-user-id", "").strip()
    gw_email = request.headers.get("x-gateway-user-email", "").strip()
    gw_tier = request.headers.get("x-gateway-user-tier", "free").strip().lower()

    if not gw_id or not gw_email:
        return None

    try:
        uid = int(gw_id)
    except ValueError:
        return None

    if gw_tier not in ("free", "pro", "super_admin"):
        gw_tier = "free"

    return {
        "id": uid,
        "email": gw_email,
        "tier": gw_tier,
        "display_name": gw_email.split("@")[0],
    }


def require_paid_user(request: Request) -> dict:
    """FastAPI dependency — returns user dict or raises 402.

    Use on every /api/* route except /healthz:

        @app.get("/api/something")
        async def handler(request: Request):
            user = require_paid_user(request)
            ...
    """
    user = get_session_user(request)
    if user is None:
        raise HTTPException(status_code=402, detail={
            "error": "paywall",
            "upgrade_url": PAYWALL_UPGRADE_URL,
        })
    if user["tier"] not in ALLOWED_TIERS_API:
        raise HTTPException(status_code=402, detail={
            "error": "paywall",
            "upgrade_url": PAYWALL_UPGRADE_URL,
        })
    return user


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def require_admin(request: Request) -> dict:
    """FastAPI dependency — require super_admin tier AND localhost origin.

    Admin routes are additionally gated on localhost because even with a
    valid super_admin token, they should never be hit from arbitrary
    gateway traffic (the gateway shouldn't be proxying /admin/* anyway).
    """
    host = _client_host(request)
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="admin routes are localhost only")
    user = get_session_user(request)
    if user is not None and user["tier"] == ADMIN_TIER:
        return user
    # Localhost fallback for operators running commands on the box directly
    # (no gateway in the loop). This matches the admin_trigger behaviour the
    # existing dashboard already shipped with.
    if host in LOCAL_HOSTS and user is None:
        return {
            "id": 0,
            "email": "localhost",
            "tier": ADMIN_TIER,
            "display_name": "System",
        }
    raise HTTPException(status_code=403, detail="super_admin required")


def assert_bound_to_localhost(host: str) -> None:
    """Startup check — refuse to boot if the server is listening on 0.0.0.0.

    The gateway reverse proxy terminates TLS and injects auth headers. If
    the dashboard were reachable directly on the public internet, anyone
    could bypass the paywall by hitting the box's IP. Fail fast.
    """
    if host in ("0.0.0.0", "::", ""):
        raise RuntimeError(
            f"annoyance dashboard refuses to bind to {host!r}. "
            "Set HOST=127.0.0.1 so only the gateway can reach it."
        )
