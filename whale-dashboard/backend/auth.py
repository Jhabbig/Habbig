from __future__ import annotations
"""Gateway SSO — same pattern as midterm-dashboard/backend/main.py:135-158.

The gateway verifies the user's session and subscription, then forwards the
request with three trusted headers:
    x-gateway-secret           HMAC shared secret
    x-gateway-user-id          UUID
    x-gateway-user-email
    x-gateway-user-tier        "free" | "premium" | "admin"
    x-gateway-user-display-name (optional)
"""

import hmac
import os
from typing import Dict

from fastapi import HTTPException, Request

TIER_RANK = {"free": 0, "premium": 1, "admin": 2}


async def require_auth(request: Request) -> Dict:
    """Return the current user dict or raise 401."""
    sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    provided = request.headers.get("x-gateway-secret", "")
    if sso_secret and hmac.compare_digest(provided, sso_secret):
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        gw_tier = request.headers.get("x-gateway-user-tier", "free")
        gw_display = request.headers.get("x-gateway-user-display-name", "")
        if gw_id and gw_email:
            return {
                "id": gw_id,
                "email": gw_email,
                "tier": gw_tier,
                "display_name": gw_display or gw_email.split("@")[0],
            }
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_tier(request: Request, tier: str) -> Dict:
    user = await require_auth(request)
    if TIER_RANK.get(user.get("tier", "free"), 0) < TIER_RANK.get(tier, 99):
        raise HTTPException(status_code=403, detail=f"Requires {tier} tier")
    return user
