"""Deprecation plumbing for the API versioning scheme.

Every route is canonically served at `/api/v1/...`. The unversioned
`/api/...` paths still work for a transition window but redirect to
the v1 equivalent and carry a `Sunset` + `X-API-Deprecated` pair of
response headers that RFC-8594-compatible clients (and humans reading
curl output) will notice.

Usage:
    from api.deprecation import (
        SUNSET_DATE,
        deprecation_headers,
        log_deprecated_hit,
        deprecated,
    )

The `@deprecated` decorator is provided so route handlers registered
*explicitly* under the legacy /api/... prefix (the very few that cannot
be middleware-redirected) can still advertise deprecation. All other
legacy hits are handled by `APIVersionMiddleware` in server.py.
"""

from __future__ import annotations

import functools
import logging
from typing import Callable, Mapping

log = logging.getLogger("api.deprecation")


# ── Public contract ──────────────────────────────────────────────────────────

# ISO 8601 / RFC 8594 Sunset date.
# Unversioned /api/ routes stop responding after this date — they'll return 410 Gone.
SUNSET_DATE = "2026-12-31"

# RFC 7231 HTTP-date form of SUNSET_DATE — used in the `Sunset` header.
SUNSET_HTTP_DATE = "Thu, 31 Dec 2026 23:59:59 GMT"

# Human-readable deprecation message — echoed in X-API-Deprecated.
DEPRECATION_MSG = (
    f"This endpoint will be removed on {SUNSET_DATE}. "
    "Use the /api/v1/ equivalent instead."
)

# URL to the migration guide (links to the OpenAPI docs for now;
# swap to a dedicated /docs/migration page when one exists).
DOCS_URL = "https://narve.ai/api/v1/docs"


def deprecation_headers(v1_target: str | None = None) -> dict[str, str]:
    """Return the standard header set for a deprecated response.

    Headers:
      * `Deprecation: true` — RFC draft-ietf-httpapi-deprecation-header
      * `Sunset: <HTTP-date>` — RFC 8594
      * `X-API-Deprecated: <human-readable message>`
      * `Link: <v1>; rel="successor-version"` — if *v1_target* given
    """
    headers: dict[str, str] = {
        "Deprecation": "true",
        "Sunset": SUNSET_HTTP_DATE,
        "X-API-Deprecated": DEPRECATION_MSG,
    }
    if v1_target:
        # RFC 5988 Link with successor-version relation
        headers["Link"] = f'<{v1_target}>; rel="successor-version"'
    return headers


def log_deprecated_hit(
    *,
    legacy_path: str,
    v1_path: str,
    method: str,
    user_agent: str = "",
    ip: str = "",
) -> None:
    """Record a hit on a deprecated endpoint.

    Emits a structured WARNING log the ops pipeline can aggregate.
    Never raises — observability must not break the request path.
    """
    try:
        log.warning(
            "api.deprecated_hit path=%s method=%s successor=%s ip=%s ua=%s",
            legacy_path,
            method,
            v1_path,
            ip,
            (user_agent or "")[:120],
        )
    except Exception:
        pass


def deprecated(v1_target: str | None = None) -> Callable:
    """Decorator marking a FastAPI handler as deprecated.

    The wrapped handler still runs normally; on success, the deprecation
    headers are merged into the response. Use this only for handlers that
    must live at a legacy `/api/...` path (e.g. because they diverged
    from their v1 twin). All other legacy routes are handled by
    `APIVersionMiddleware` in server.py and do not need this decorator.

    Example:
        @app.get("/api/old-thing")
        @deprecated(v1_target="/api/v1/new-thing")
        async def old_thing():
            return {"ok": True}
    """
    def _decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def _wrapped(*args, **kwargs):
            from starlette.responses import Response  # local import — keeps module cold-start light
            result = await fn(*args, **kwargs)
            if isinstance(result, Response):
                for k, v in deprecation_headers(v1_target).items():
                    # Don't clobber caller-set headers
                    if k not in result.headers:
                        result.headers[k] = v
            return result
        # Preserve FastAPI's reflection on the original signature
        _wrapped.__deprecated__ = True  # type: ignore[attr-defined]
        _wrapped.__v1_target__ = v1_target  # type: ignore[attr-defined]
        return _wrapped
    return _decorate


# ── /api/version payload builder ─────────────────────────────────────────────


def version_payload(supported: tuple[str, ...] = ("v1",),
                    deprecated_versions: tuple[str, ...] = ()) -> Mapping:
    """JSON payload for GET /api/version and /api/v1/version."""
    return {
        "current": "v1",
        "supported": list(supported),
        "deprecated": list(deprecated_versions),
        "docs_url": DOCS_URL,
        "sunset_unversioned": SUNSET_DATE,
    }
