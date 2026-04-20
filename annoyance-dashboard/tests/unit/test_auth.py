"""Gateway SSO + paywall + admin-tier enforcement."""

from __future__ import annotations

from tests.conftest import (
    GATEWAY_SECRET,
    admin_headers,
    free_headers,
    pro_headers,
)


# /healthz is always public — the other tests rely on this.
def test_healthz_is_public(test_client):
    r = test_client.get("/healthz")
    assert r.status_code == 200


def test_sso_header_missing_returns_402(test_client, paywall_env):
    r = test_client.get("/api/index")
    assert r.status_code == 402
    body = r.json()
    # HTTPException.detail may be under "detail" or "error" depending on
    # FastAPI version — accept either, but require upgrade_url somewhere.
    assert "paywall" in str(body)
    assert "upgrade_url" in str(body)


def test_sso_header_invalid_secret_returns_402(test_client, paywall_env):
    bad = pro_headers()
    bad["X-Gateway-Secret"] = "wrong-secret"
    r = test_client.get("/api/index", headers=bad)
    assert r.status_code == 402


def test_sso_header_valid_free_tier_returns_402(test_client, paywall_env):
    r = test_client.get("/api/index", headers=free_headers())
    assert r.status_code == 402


def test_sso_header_valid_pro_tier_returns_200(test_client, paywall_env):
    r = test_client.get("/api/index", headers=pro_headers())
    assert r.status_code == 200
    assert "hours" in r.json()


def test_sso_header_valid_super_admin_returns_200(test_client, paywall_env):
    r = test_client.get("/api/index", headers=admin_headers())
    assert r.status_code == 200


def test_admin_localhost_check(test_client, paywall_env, monkeypatch):
    """Non-local admin requests are rejected even with a super_admin header."""
    import auth
    monkeypatch.setattr(auth, "_client_host", lambda request: "203.0.113.7")
    r = test_client.post("/admin/trigger?loop=aggregator", headers=admin_headers())
    assert r.status_code == 403


def test_admin_super_admin_required(test_client, paywall_env, as_localhost):
    """A pro-tier user on localhost still can't hit /admin/*."""
    r = test_client.post("/admin/trigger?loop=aggregator", headers=pro_headers())
    assert r.status_code == 403


def test_assert_bound_to_localhost_rejects_public_bind():
    """Startup assertion must fail fast on 0.0.0.0."""
    import pytest

    import auth
    with pytest.raises(RuntimeError):
        auth.assert_bound_to_localhost("0.0.0.0")
    with pytest.raises(RuntimeError):
        auth.assert_bound_to_localhost("")

    # Localhost passes
    auth.assert_bound_to_localhost("127.0.0.1")
    auth.assert_bound_to_localhost("::1")


def test_get_session_user_handles_bad_user_id(test_client, paywall_env):
    """Non-integer X-Gateway-User-ID returns None → 402."""
    headers = pro_headers()
    headers["X-Gateway-User-ID"] = "not-a-number"
    r = test_client.get("/api/index", headers=headers)
    assert r.status_code == 402


def test_compare_digest_used_for_secret():
    """Sanity: auth uses hmac.compare_digest so an attacker can't time-side-channel the secret."""
    import inspect
    import auth
    src = inspect.getsource(auth.get_session_user)
    assert "hmac.compare_digest" in src
