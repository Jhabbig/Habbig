"""
Integration tests for decision #4 "hard paywall" via decision #5 "crypto-
dashboard SSO pattern". Proves:

  * /api/* → 402 without valid gateway headers
  * /api/* → 402 for free tier
  * /api/* → 200 for pro + super_admin
  * /admin/* → 403 for any client whose host isn't in LOCAL_HOSTS, even
                with a valid super_admin token
  * /admin/* → 200 on localhost, super_admin (synthetic or real)
"""

from __future__ import annotations

import pytest

from tests.conftest import pro_headers, admin_headers, free_headers, GATEWAY_SECRET


pytestmark = pytest.mark.integration


# ── /api/* paywall ───────────────────────────────────────────────────────────

def test_api_without_gateway_headers_402(test_client, paywall_env):
    for endpoint in ("/api/index", "/api/spikes", "/api/entities/top",
                     "/api/entity/Tesla", "/api/sources"):
        r = test_client.get(endpoint)
        assert r.status_code == 402, f"{endpoint} should be paywalled"


def test_api_with_wrong_gateway_secret_402(test_client, paywall_env):
    bad = pro_headers()
    bad["X-Gateway-Secret"] = "wrong-secret"
    r = test_client.get("/api/index", headers=bad)
    assert r.status_code == 402


def test_api_with_missing_user_id_402(test_client, paywall_env):
    h = pro_headers()
    h.pop("X-Gateway-User-ID")
    r = test_client.get("/api/index", headers=h)
    assert r.status_code == 402


def test_api_free_tier_402(test_client, paywall_env):
    r = test_client.get("/api/index", headers=free_headers())
    assert r.status_code == 402


def test_api_pro_tier_200(test_client, paywall_env):
    r = test_client.get("/api/index", headers=pro_headers())
    assert r.status_code == 200


def test_api_super_admin_tier_200(test_client, paywall_env):
    r = test_client.get("/api/index", headers=admin_headers())
    assert r.status_code == 200


def test_api_without_gateway_secret_env_set_fails_closed(test_client, monkeypatch):
    """Without GATEWAY_SSO_SECRET in env, get_session_user returns None → 402."""
    monkeypatch.delenv("GATEWAY_SSO_SECRET", raising=False)
    r = test_client.get("/api/index", headers=pro_headers())
    assert r.status_code == 402


# ── /admin/* localhost gate ──────────────────────────────────────────────────

def test_admin_trigger_rejects_non_localhost(test_client, paywall_env):
    """TestClient's default client host is 'testclient' — not in LOCAL_HOSTS."""
    r = test_client.post("/admin/trigger?loop=aggregator", headers=admin_headers())
    assert r.status_code == 403


def test_admin_trigger_allows_localhost_without_sso(test_client, paywall_env, as_localhost):
    """On localhost, even without SSO headers, auth.require_admin synthesises
    an admin user — matches the 'operators running on the box' pattern."""
    r = test_client.post("/admin/trigger?loop=aggregator")
    assert r.status_code == 200


def test_admin_trigger_allows_localhost_with_super_admin(test_client, paywall_env, as_localhost):
    r = test_client.post("/admin/trigger?loop=aggregator", headers=admin_headers())
    assert r.status_code == 200


def test_admin_trigger_rejects_localhost_with_pro_token(test_client, paywall_env, as_localhost):
    """Localhost + valid pro token (not super_admin) → still 403."""
    r = test_client.post("/admin/trigger?loop=aggregator", headers=pro_headers())
    assert r.status_code == 403


def test_admin_cost_summary_respects_localhost_gate(test_client, paywall_env, as_localhost):
    r = test_client.get("/admin/cost-summary", headers=admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert "ceiling_cents" in body
    assert "today_cents" in body


def test_admin_cost_summary_rejects_non_localhost(test_client, paywall_env):
    r = test_client.get("/admin/cost-summary", headers=admin_headers())
    assert r.status_code == 403


def test_admin_reclassify_respects_localhost_gate(test_client, paywall_env, as_localhost):
    r = test_client.post("/admin/reclassify?limit=10", headers=admin_headers())
    assert r.status_code == 200


def test_admin_reclassify_rejects_non_localhost(test_client, paywall_env):
    r = test_client.post("/admin/reclassify?limit=10", headers=admin_headers())
    assert r.status_code == 403


# ── admin/trigger loop names ─────────────────────────────────────────────────

def test_admin_trigger_unknown_loop_400(test_client, paywall_env, as_localhost):
    r = test_client.post("/admin/trigger?loop=not_a_real_loop")
    assert r.status_code == 400


def test_admin_trigger_aggregator(test_client, paywall_env, as_localhost):
    r = test_client.post("/admin/trigger?loop=aggregator")
    assert r.status_code == 200
    assert r.json()["loop"] == "aggregator"


def test_admin_trigger_retention(test_client, paywall_env, as_localhost):
    r = test_client.post("/admin/trigger?loop=retention")
    assert r.status_code == 200
    body = r.json()
    assert body["loop"] == "retention"
    assert body["ttl_days"] == 30
