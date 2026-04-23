"""Bearer-token auth + per-key hourly rate limiting for the public API.

Sits in front of every /api/public/v1/* handler. Two concerns:

1. Validate Authorization: Bearer narve_<key>
   - SHA-256 the provided key, look it up in api_keys, reject if missing or
     revoked. The raw key is never stored; the hash is the only record.
   - Parse the `scopes` column into a set so handlers can enforce
     scope requirements (e.g. 'write' for POST /predictions).

2. Per-key hourly quota
   - api_usage_hourly is an (api_key_id, hour_bucket) → request_count rollup.
     Each validated request UPSERTs +1. If the post-increment count > the
     key's rate_limit_hour, we return 429 with a Retry-After header that
     points at the start of the next hour.

On any internal DB error we log and let the request continue with a
best-effort attempt — denying real traffic because a write race failed
is worse than allowing one extra request above the quota. The exception
is an actually-invalid key, which always 401s.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request

import db


log = logging.getLogger("api.public.auth")


BEARER_PREFIX = "Bearer "
KEY_PREFIX_REQUIRED = "narve_"


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _hour_bucket(now: Optional[float] = None) -> int:
    now = int(now or time.time())
    return now - (now % 3600)


def _parse_scopes(raw: Optional[str]) -> set[str]:
    """Split comma-separated scopes. Default is always at least {'read'}."""
    if not raw:
        return {"read"}
    scopes = {s.strip() for s in raw.split(",") if s.strip()}
    scopes.add("read")  # read is the baseline — never strip it even if absent
    return scopes


def verify_api_key(request: Request) -> dict:
    """Validate the Bearer token on *request*, UPSERT the usage bucket,
    and return a dict describing the key. Raises HTTPException on failure.

    Attached fields:
      - id, user_id, name, tier, key_prefix
      - rate_limit_hour: the configured hourly cap
      - scopes: parsed set, always contains 'read'
      - usage_this_hour: post-increment count (>= 1 for every successful call)
      - hour_bucket: the integer bucket the increment was against
    """
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if not auth.startswith(BEARER_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="API key required. Send: Authorization: Bearer narve_<key>",
            headers={"WWW-Authenticate": 'Bearer realm="narve-public-api"'},
        )

    raw_key = auth[len(BEARER_PREFIX):].strip()
    if not raw_key.startswith(KEY_PREFIX_REQUIRED):
        # Keys start with "narve_" by construction (see api_v1.create_api_key).
        # A Bearer token that doesn't look like one of ours is almost
        # certainly a copy-paste from another service — 401 rather than
        # burning a DB round-trip.
        raise HTTPException(status_code=401, detail="Invalid API key format")

    key_hash = _hash_key(raw_key)
    row = db.get_api_key_by_hash(key_hash)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    limit = int(row["rate_limit_hour"] or 0)
    bucket = _hour_bucket()

    try:
        count = db.bump_api_usage(row["id"], bucket)
    except Exception as exc:
        # Bucket write failure — log but don't 500 legitimate traffic.
        log.warning("bump_api_usage failed key_id=%s: %s", row["id"], exc)
        count = 1

    if limit and count > limit:
        # Retry-After points at the top of the next hour.
        retry_after = max(1, (bucket + 3600) - int(time.time()))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit}/hour for this key)",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(bucket + 3600),
            },
        )

    try:
        db.touch_api_key_last_used(row["id"])
    except Exception:
        pass  # cosmetic field only — never block on this write

    # Dict-ify so FastAPI can attach this to request.state without pickling a
    # sqlite3.Row (which has no .get() and doesn't survive across thread hops).
    key = {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"] if "name" in row.keys() else "",
        "tier": row["tier"] if "tier" in row.keys() else "standard",
        "key_prefix": row["key_prefix"],
        "rate_limit_hour": limit,
        "scopes": _parse_scopes(row["scopes"] if "scopes" in row.keys() else "read"),
        "usage_this_hour": count,
        "hour_bucket": bucket,
    }

    # Stash on request.state so sign_response + per-endpoint handlers can
    # attribute the call without re-hashing the Bearer header.
    request.state.api_key = key
    return key


def require_scope(scope: str):
    """Dependency factory — enforce a scope BEYOND 'read' (which is
    implicitly granted to every valid key).

        @router.post(..., dependencies=[Depends(require_scope('write'))])

    The verify_api_key step must have already run on this request, or the
    factory will re-run it.
    """
    async def _check(request: Request) -> dict:
        key = getattr(request.state, "api_key", None) or verify_api_key(request)
        if scope not in key["scopes"]:
            raise HTTPException(
                status_code=403,
                detail=f"This API key does not have '{scope}' scope",
            )
        return key

    return _check


def sign_if_available(user_id: int, data, endpoint: str):
    """Best-effort wrapper around forensics.signer.sign_response.

    If the forensic signer module is present (leak-attribution work), wrap
    the response so any screenshot or scrape of this JSON is attributable
    to the calling key's owner. If not present — return data unchanged.
    """
    try:
        from forensics import signer as _signer
        return _signer.sign_response(int(user_id), data, endpoint)
    except Exception:
        return data
