"""
Security suite — P5.2 — paywall bypass.

Iterates every GET/POST route on the app and verifies it enforces auth
against permutations of manipulated SSO headers. Discovery happens via
app.routes so new endpoints can't escape coverage.

Matrix covered per /api/* route:
  * No SSO headers               → 402 (paywall)
  * SSO with tier=free           → 402
  * SSO + tampered HMAC          → 402 (current design — treats invalid
                                        secret as "not authenticated";
                                        distinct 403-for-tampering is a
                                        product decision, not a bug)
  * SSO + valid pro tier         → 200 (sanity check — happy path works)
  * SSO + spoofed X-Forwarded-For: 127.0.0.1 without real SSO → 402
    (verifies the gateway doesn't trust XFF for auth)

For /admin/* routes: the default test client is non-localhost, so all
admin routes 403 regardless of SSO. Covered in test_paywall.py already.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from tests.conftest import pro_headers, admin_headers, free_headers


pytestmark = pytest.mark.integration


_SKIP_PREFIXES = ("/static", "/admin")
_SKIP_PATHS = {"/healthz", "/", "/api/me"}  # public routes


def _collect_api_routes(app) -> list[tuple[str, list[str], str]]:
    """Return (path, methods, endpoint_name) for every /api/* route."""
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if path in _SKIP_PATHS:
            continue
        if not path.startswith("/api/"):
            continue
        methods = [m for m in route.methods or [] if m in ("GET", "POST", "PUT", "DELETE", "PATCH")]
        out.append((path, methods, route.name))
    return out


def _fill_path_params(path: str) -> str:
    """Replace {name} / {name:type} path params with a harmless placeholder
    so TestClient will route the request instead of 422'ing on missing params."""
    import re
    return re.sub(r"\{[^}]+\}", "Tesla", path)


def _do_request(client, method: str, path: str, headers: dict | None = None, body: dict | None = None):
    if method == "GET":
        return client.get(path, headers=headers or {})
    if method == "POST":
        return client.post(path, headers=headers or {}, json=body or {})
    if method == "PUT":
        return client.put(path, headers=headers or {}, json=body or {})
    if method == "DELETE":
        return client.delete(path, headers=headers or {})
    raise ValueError(f"unsupported method {method}")


# ── Route discovery sanity check ─────────────────────────────────────────────

def test_api_routes_discovered(test_client, paywall_env):
    routes = _collect_api_routes(test_client.app)
    # Spot-check a few known endpoints from server.py
    paths = [p for p, _, _ in routes]
    assert "/api/index" in paths
    assert "/api/spikes" in paths
    assert "/api/entities/top" in paths
    # At least 5 protected routes
    assert len(routes) >= 5


# ── No SSO → 402 everywhere ──────────────────────────────────────────────────

def test_every_api_route_rejects_missing_sso(test_client, paywall_env):
    fails = []
    for path, methods, _ in _collect_api_routes(test_client.app):
        concrete = _fill_path_params(path)
        for method in methods:
            r = _do_request(test_client, method, concrete)
            if r.status_code not in (401, 402, 403):
                fails.append(f"{method} {concrete} → {r.status_code} (expected 401/402/403)")
    assert not fails, "Routes leaking without SSO:\n" + "\n".join(fails)


# ── Tier=free → 402 ──────────────────────────────────────────────────────────

def test_every_api_route_rejects_free_tier(test_client, paywall_env):
    fails = []
    for path, methods, _ in _collect_api_routes(test_client.app):
        concrete = _fill_path_params(path)
        for method in methods:
            r = _do_request(test_client, method, concrete, headers=free_headers())
            if r.status_code not in (401, 402, 403):
                fails.append(f"{method} {concrete} free → {r.status_code}")
    assert not fails, "Routes leaking to free tier:\n" + "\n".join(fails)


# ── Tampered HMAC → 402 ──────────────────────────────────────────────────────

def test_every_api_route_rejects_tampered_hmac(test_client, paywall_env):
    """Forging the secret must fail closed. Current design returns 402; if
    the product later wants 403-specific-to-tampering, tighten auth.py and
    update this expectation."""
    bad = pro_headers()
    bad["X-Gateway-Secret"] = "definitely-not-the-real-secret"
    fails = []
    for path, methods, _ in _collect_api_routes(test_client.app):
        concrete = _fill_path_params(path)
        for method in methods:
            r = _do_request(test_client, method, concrete, headers=bad)
            if r.status_code not in (401, 402, 403):
                fails.append(f"{method} {concrete} tampered → {r.status_code}")
    assert not fails, "Routes accepting tampered HMAC:\n" + "\n".join(fails)


# ── Spoofed X-Forwarded-For → 402 ────────────────────────────────────────────

def test_xff_spoof_does_not_bypass_paywall(test_client, paywall_env):
    """An attacker behind a proxy who sets X-Forwarded-For: 127.0.0.1
    must NOT gain /api access. Paywall is SSO-keyed, not IP-keyed —
    XFF has no effect. Verifies the P1.1 fix."""
    spoof = {"X-Forwarded-For": "127.0.0.1"}
    fails = []
    for path, methods, _ in _collect_api_routes(test_client.app):
        concrete = _fill_path_params(path)
        for method in methods:
            r = _do_request(test_client, method, concrete, headers=spoof)
            if r.status_code not in (401, 402, 403):
                fails.append(f"{method} {concrete} XFF-spoof → {r.status_code}")
    assert not fails, "Routes bypassing via XFF:\n" + "\n".join(fails)


def test_xff_with_bad_sso_still_blocks(test_client, paywall_env):
    """XFF=127.0.0.1 + tampered SSO secret must still fail."""
    headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Gateway-Secret": "forged",
        "X-Gateway-User-ID": "42",
        "X-Gateway-User-Email": "hacker@evil.test",
        "X-Gateway-User-Tier": "super_admin",
    }
    r = test_client.get("/api/index", headers=headers)
    assert r.status_code in (401, 402, 403)


def test_cf_connecting_ip_spoof_does_not_bypass(test_client, paywall_env):
    """Same as XFF but via CF-Connecting-IP header."""
    spoof = {"CF-Connecting-IP": "127.0.0.1"}
    r = test_client.get("/api/index", headers=spoof)
    assert r.status_code in (401, 402, 403)


# ── Valid pro → 200 (happy path sanity) ──────────────────────────────────────

def test_pro_tier_accepted_on_read_routes(test_client, paywall_env):
    """Without this test, the rejects-* tests could pass trivially by having
    all routes 500. Assert the happy path works."""
    for endpoint in ("/api/index", "/api/spikes", "/api/entities/top"):
        r = test_client.get(endpoint, headers=pro_headers())
        assert r.status_code == 200, f"{endpoint} broken under valid pro: {r.status_code} {r.text[:200]}"


# ── Gateway secret env missing → fail closed ─────────────────────────────────

def test_missing_gateway_secret_env_fails_closed(test_client, monkeypatch):
    """If GATEWAY_SSO_SECRET is unset in env, get_session_user returns None
    → every /api/* route returns 402. Operators can't accidentally run
    without the secret and get public reads."""
    monkeypatch.delenv("GATEWAY_SSO_SECRET", raising=False)
    for endpoint in ("/api/index", "/api/spikes"):
        r = test_client.get(endpoint, headers=pro_headers())
        assert r.status_code in (401, 402, 403), (
            f"{endpoint} leaked without GATEWAY_SSO_SECRET: {r.status_code}"
        )
