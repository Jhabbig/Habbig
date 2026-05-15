"""Host-based subproduct routing + origin hardening.

Three concerns, one middleware, because they all live on the request
pre-flight path and share the same set of host checks:

1. **Allow-listed Host headers.** narve.ai runs behind Cloudflare, so
   every legitimate request has one of a handful of Host values. An
   arbitrary Host header is a signal the request came from outside
   Cloudflare — possibly a direct-to-origin scan we want to reject
   with 400 before any auth/DB work happens.

2. **Cloudflare-origin enforcement in production.** Every request that
   passes through Cloudflare carries a ``CF-Connecting-IP`` header.
   If it's missing in production, the request came from something that
   isn't Cloudflare (direct origin hit, misconfigured reverse proxy).
   We 403 those; the WAF rules in CLOUDFLARE_CHANGES.md make this
   unreachable from the internet, but the middleware is the second
   layer.

3. **Attach the subproduct to request.state.** Downstream route handlers
   and templates read ``request.state.subproduct`` to decide which
   wordmark, tabs, and content filter to render. ``None`` means the
   apex narve.ai or a www./api./admin./staging. host — the main product.

The middleware intentionally does NOT do access checks. ``require_subproduct_access``
(in ``subproduct/access.py``) is a FastAPI dependency, not a middleware,
because per-route access depends on the authenticated user — which this
middleware runs before the session layer attaches.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

try:  # The subproduct catalogue lives in the top-level module that was
      # already here before this middleware. We import it lazily so tests
      # that stub the module still work.
    from subproduct import SUBPRODUCTS as _CATALOG
except Exception:  # pragma: no cover — degraded mode for import-time edge
    _CATALOG = {}


log = logging.getLogger("middleware.subproduct")


# Stripe price IDs per subproduct are held in env vars so staging can
# point at test-mode price objects without a code change. The Stripe
# Checkout route reads these by the key in SUBPRODUCTS[slug]["env_price_id"]
# (see gateway/subproduct.py). This middleware doesn't need to know
# them; it only routes.

# Canonical product hosts — same for production and staging. The
# ``staging.`` host is part of the prod allowlist because prod sits in
# front of it (see StagingProxyMiddleware in server.py).
_APEX_HOSTS = {
    "narve.ai",
    "www.narve.ai",
    "api.narve.ai",
    "admin.narve.ai",
    "staging.narve.ai",
}

# Dev loopback — used by pytest + local uvicorn. Not in production allow.
_DEV_HOSTS = {"localhost", "127.0.0.1", "testserver"}


# Trusted-proxy gate for CF-Connecting-IP (audit HIGH FIX B). Only the
# loopback peer (cloudflared in prod) can attach a trustworthy
# CF-Connecting-IP. Anything else is attacker-controlled.
_TRUSTED_PROXY_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",
})


def trusted_client_ip(request) -> str:
    """Real client IP, gating CF-Connecting-IP on a trusted peer.

    See tests/test_cf_ip_trust.py — off-tunnel peer with a forged
    CF-Connecting-IP returns the peer host (not the forged header), so
    the downstream Stripe IP allowlist sees the real off-tunnel IP and
    rejects.
    """
    try:
        peer = (request.client.host if request.client else "") or ""
    except AttributeError:
        peer = ""
    if peer in _TRUSTED_PROXY_HOSTS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def _subproduct_hosts() -> set[str]:
    """Every {slug}.narve.ai host for a configured subproduct."""
    return {f"{slug}.narve.ai" for slug in _CATALOG}


def allowed_hosts() -> set[str]:
    """All hosts the gateway will serve. Exposed so tests can assert
    against the allowlist without duplicating the set."""
    return _APEX_HOSTS | _subproduct_hosts() | _DEV_HOSTS


def _strip_port(host: str) -> str:
    # uvicorn + TestClient include a port; Cloudflare strips it. Either
    # way we normalise to just the hostname for the allowlist check.
    return host.split(":", 1)[0].strip().lower()


def subproduct_for_host(host: str) -> Optional[str]:
    """Return the subproduct slug for a ``Host`` header, or None.

    ``None`` covers apex, www, api, admin, staging, and dev loopbacks —
    none of those render a subproduct brand.
    """
    h = _strip_port(host)
    if h in _APEX_HOSTS or h in _DEV_HOSTS:
        return None
    if not h.endswith(".narve.ai"):
        return None
    head = h[: -len(".narve.ai")]
    if head in _CATALOG:
        return head
    return None


def _is_production() -> bool:
    # Re-read each request so tests can flip PRODUCTION via monkeypatch
    # without reimporting. Cheap: one env lookup.
    return os.environ.get("PRODUCTION", "0") == "1"


class SubproductMiddleware(BaseHTTPMiddleware):
    """Validate Host, verify Cloudflare origin, attach subproduct slug."""

    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        host = _strip_port(host_header)
        allow = allowed_hosts()
        if host and host not in allow:
            log.info("subproduct: rejecting unknown host %r", host_header)
            return JSONResponse(
                {"error": "Invalid Host header"}, status_code=400,
            )

        if _is_production():
            # Audit HIGH FIX B: gate on a trusted loopback peer attaching
            # the CF header. Off-tunnel ingress with a forged header is
            # rejected here instead of silently bypassing IP rate limits.
            try:
                peer = (request.client.host if request.client else "") or ""
            except AttributeError:
                peer = ""
            cf_header = request.headers.get("cf-connecting-ip")
            if peer not in _TRUSTED_PROXY_HOSTS or not cf_header:
                log.warning(
                    "subproduct: direct-origin request rejected host=%s path=%s peer=%s",
                    host, request.url.path, peer or "?",
                )
                return JSONResponse(
                    {"error": "Forbidden"}, status_code=403,
                )

        # Attach state even for apex hosts so route handlers can
        # `getattr(request.state, "subproduct", None)` safely.
        request.state.subproduct = subproduct_for_host(host)
        return await call_next(request)
