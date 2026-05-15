"""Host-based subproduct routing + origin hardening.

Three concerns, one middleware, because they all live on the request
pre-flight path and share the same set of host checks:

1. **Allow-listed Host headers.** narve.ai runs behind Cloudflare, so
   every legitimate request has one of a handful of Host values. An
   arbitrary Host header is a signal the request came from outside
   Cloudflare — possibly a direct-to-origin scan we want to reject
   with 400 before any auth/DB work happens.

2. **Cloudflare-origin enforcement in production.** Every request that
   passes through Cloudflare carries the full CF-ingress trio:
   ``CF-Connecting-IP`` (end-user IP), ``CF-Ray`` (per-edge request id),
   and ``X-Forwarded-Proto: https``. If those aren't present together,
   the request didn't come from a Cloudflare POP — either a direct
   origin hit, a misconfigured proxy, or a hostile probe. We 403 those;
   the WAF rules in CLOUDFLARE_CHANGES.md make this unreachable from
   the internet, but the middleware is the second layer. A legacy
   fallback also accepts loopback peers with ``CF-Connecting-IP`` set,
   so an on-box cloudflared deployment still works.

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


# Trusted-proxy gate for CF-Connecting-IP (audit HIGH FIX B; revised
# 2026-05-15). The original gate required ``request.client.host`` to be
# loopback before honouring CF-Connecting-IP, on the assumption that
# cloudflared on 127.0.0.1 was the only legitimate peer. Prod logs
# showed that assumption is wrong: requests arrive with peer set to
# the real end-user IP (185.222.x.x, 82.15.x.x, etc.) because the
# tunnel topology in front of uvicorn doesn't rewrite the socket peer.
# The loopback-only gate dropped EVERY legitimate request as a direct
# origin hit and 403'd production traffic.
#
# The fix replaces "peer must be loopback" with "request must carry the
# Cloudflare-ingress fingerprint":
#
#   * CF-Connecting-IP: the end-user IP (what we want to read)
#   * CF-Ray: unique per CF edge request, attached by every CF POP
#   * X-Forwarded-Proto: https — Cloudflare always sets this on TLS
#     traffic, and the WAF rules in CLOUDFLARE_CHANGES.md force HTTPS
#     on every public host.
#
# All three together are not a perfect proof — a determined attacker
# who knows the origin layout can replay them — but the combination
# eliminates the casual direct-origin probe (e.g. an IP scanner that
# only spoofs CF-Connecting-IP). The Stripe webhook still relies on
# signature verification as the authoritative check; this gate is the
# defence-in-depth layer that keeps the IP allowlist meaningful.
#
# Loopback / TestClient peers are retained as a fast-path trust so
# unit tests and any future on-box cloudflared deployment keep working
# without needing to forge all three headers.
_TRUSTED_PROXY_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",
})


def _has_cf_fingerprint(request) -> bool:
    """True iff the request carries the full Cloudflare-ingress trio.

    Cloudflare attaches ``CF-Ray`` on every edge request and sets
    ``X-Forwarded-Proto: https`` for TLS traffic. A direct-to-origin
    probe that only spoofs ``CF-Connecting-IP`` won't carry the other
    two, so this check rejects the casual forgery without needing a
    loopback peer.

    All three must be present together — the trio is what makes the
    fingerprint hard to spoof unless the attacker has researched the
    deployment. (The Stripe webhook signature is still the authoritative
    check; this is defence in depth for the IP allowlist.)
    """
    headers = request.headers
    if not headers.get("cf-connecting-ip"):
        return False
    if not headers.get("cf-ray"):
        return False
    proto = (headers.get("x-forwarded-proto") or "").strip().lower()
    # Accept the standard ``https`` token. A comma-separated list (some
    # proxies stack values) is honoured if the first hop is https — the
    # CF edge is always the outermost.
    first = proto.split(",", 1)[0].strip() if proto else ""
    return first == "https"


def _is_trusted_peer(request) -> bool:
    """True iff the immediate peer is a loopback / TestClient host."""
    try:
        peer = (request.client.host if request.client else "") or ""
    except AttributeError:
        peer = ""
    return peer in _TRUSTED_PROXY_HOSTS


def _is_loopback_with_cf_header(request) -> bool:
    """Legacy on-box cloudflared path: peer is loopback AND the request
    carries ``CF-Connecting-IP``.

    The on-box tunnel always attaches the CF header — a loopback peer
    that DOESN'T carry one is either a direct localhost probe (e.g. a
    health checker, log scraper) or a misconfiguration. We don't want
    to trust an arbitrary loopback caller, so the header presence is
    the additional check that distinguishes "cloudflared" from "anyone
    who can reach 127.0.0.1".
    """
    if not _is_trusted_peer(request):
        return False
    return bool(request.headers.get("cf-connecting-ip"))


def trusted_client_ip(request) -> str:
    """Real client IP, gated on a Cloudflare-ingress fingerprint.

    The header is honoured when EITHER (a) the request carries the full
    CF trio (``CF-Connecting-IP`` + ``CF-Ray`` + ``X-Forwarded-Proto:
    https``) OR (b) the peer is loopback AND ``CF-Connecting-IP`` is
    present (legacy on-box cloudflared + unit tests). Both paths are
    observable in production; (a) covers the standard CF edge → origin
    flow where the peer is the end-user IP, (b) is the legacy on-box
    cloudflared deployment.

    A direct-origin hit with only a forged ``CF-Connecting-IP`` fails
    both predicates and falls through to the actual peer host, so the
    downstream Stripe IP allowlist (and any IP rate limiter) sees the
    real attacker IP rather than the spoofed end-user IP.

    See tests/test_cf_ip_trust.py for the full contract.
    """
    try:
        peer = (request.client.host if request.client else "") or ""
    except AttributeError:
        peer = ""

    if _is_loopback_with_cf_header(request) or _has_cf_fingerprint(request):
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
            # Audit HIGH FIX B (revised 2026-05-15): the original guard
            # required a loopback peer with the CF header, which broke
            # prod where peer is the real end-user IP. Accept the request
            # as Cloudflare-ingress if EITHER (a) the request carries the
            # full CF trio (CF-Connecting-IP + CF-Ray + X-Forwarded-Proto:
            # https) OR (b) the peer is loopback AND CF-Connecting-IP is
            # set (legacy on-box cloudflared). A direct-origin probe with
            # only a forged CF-Connecting-IP fails both checks and is
            # rejected here, before any auth or DB work happens.
            try:
                peer = (request.client.host if request.client else "") or ""
            except AttributeError:
                peer = ""
            trusted = (
                _is_loopback_with_cf_header(request)
                or _has_cf_fingerprint(request)
            )
            if not trusted:
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
