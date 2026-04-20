"""
Integration tests for the FastAPI surface. Exercises the full ASGI stack
with lifespan + dependencies wired (paywall, rate-limiter, etc.) but with
background loops replaced by no-ops so nothing touches Reddit/Claude.
"""

from __future__ import annotations

import pytest

from tests.conftest import pro_headers, admin_headers


pytestmark = pytest.mark.integration


# ── /healthz — no auth required ──────────────────────────────────────────────

def test_healthz_public(test_client):
    r = test_client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "db" in body
    assert "has_api_key" in body


# ── Index page — no auth required ────────────────────────────────────────────

def test_root_serves_index_html(test_client):
    r = test_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


# ── /api/* — paywall enforced ────────────────────────────────────────────────

def test_api_index_without_auth_returns_402(test_client, paywall_env):
    r = test_client.get("/api/index")
    assert r.status_code == 402
    body = r.json()
    assert body["detail"]["error"] == "paywall"


def test_api_index_with_pro_returns_200(test_client, paywall_env):
    r = test_client.get("/api/index", headers=pro_headers())
    assert r.status_code == 200
    assert r.json() == {"hours": []}  # fresh DB, no data


def test_api_spikes_with_pro_returns_empty_list(test_client, paywall_env):
    r = test_client.get("/api/spikes", headers=pro_headers())
    assert r.status_code == 200
    assert r.json() == {"spikes": []}


def test_api_top_entities_with_pro_empty(test_client, paywall_env):
    r = test_client.get("/api/entities/top", headers=pro_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["entities"] == []
    assert body["hour"] is None


def test_api_entity_detail_with_pro_returns_empty(test_client, paywall_env):
    r = test_client.get("/api/entity/Tesla", headers=pro_headers())
    assert r.status_code == 200
    assert r.json() == {"entity": "Tesla", "history": []}


def test_api_sources_with_pro(test_client, paywall_env):
    r = test_client.get("/api/sources", headers=pro_headers())
    assert r.status_code == 200
    assert "sources" in r.json()


# ── /api/* — free tier blocked ───────────────────────────────────────────────

def test_api_free_tier_is_paywalled(test_client, paywall_env):
    from tests.conftest import free_headers
    r = test_client.get("/api/index", headers=free_headers())
    assert r.status_code == 402


# ── Seeded data round-trip ───────────────────────────────────────────────────

def test_api_index_returns_seeded_data(test_client, paywall_env):
    """After seed_test_data runs in conftest.seeded_db, /api/index returns rows."""
    # Seed manually inside this test — test_client uses fresh_db which is empty.
    import seed_test_data
    seed_test_data.seed(num_posts=50, hours=12)
    r = test_client.get("/api/index?hours=24", headers=pro_headers())
    assert r.status_code == 200
    hours = r.json()["hours"]
    assert len(hours) > 0
    # Every row carries the expected keys
    assert all("score" in h and "post_count" in h and "sources" in h for h in hours)


def test_api_spikes_hydrates_sample_posts(test_client, paywall_env):
    """The synthetic spike seeded by seed_test_data should include sample_posts."""
    import seed_test_data
    seed_test_data.seed(num_posts=30, hours=4)
    r = test_client.get("/api/spikes", headers=pro_headers())
    assert r.status_code == 200
    spikes = r.json()["spikes"]
    assert len(spikes) >= 1
    first = spikes[0]
    assert "entity" in first
    assert "sample_posts" in first
    # At least the seeded spike's sample_post_ids should have hydrated
    assert isinstance(first["sample_posts"], list)


# ── Parameter clamping ───────────────────────────────────────────────────────

def test_api_index_clamps_hours(test_client, paywall_env):
    # hours=0 gets clamped to 1, hours=9999 gets clamped to 336
    r = test_client.get("/api/index?hours=0", headers=pro_headers())
    assert r.status_code == 200
    r = test_client.get("/api/index?hours=99999", headers=pro_headers())
    assert r.status_code == 200


def test_api_spikes_clamps_limit(test_client, paywall_env):
    r = test_client.get("/api/spikes?limit=0", headers=pro_headers())
    assert r.status_code == 200
    r = test_client.get("/api/spikes?limit=9999", headers=pro_headers())
    assert r.status_code == 200


# ── FP flag write endpoint ───────────────────────────────────────────────────

def test_fp_flag_requires_paywall(test_client, paywall_env):
    r = test_client.post("/api/fp-flag", json={"target_id": "1", "target_type": "spike"})
    assert r.status_code == 402


def test_fp_flag_rejects_missing_target(test_client, paywall_env):
    r = test_client.post("/api/fp-flag", headers=pro_headers(), json={})
    assert r.status_code == 400


def test_fp_flag_rejects_non_integer_target(test_client, paywall_env):
    r = test_client.post(
        "/api/fp-flag", headers=pro_headers(),
        json={"target_id": "not-an-int", "target_type": "spike"},
    )
    assert r.status_code == 400


def test_fp_flag_round_trip(test_client, paywall_env):
    """Insert a spike, then flag it via /api/fp-flag, then verify it lands
    on the review queue via db.list_fp_queue."""
    import db
    sid = db.insert_spike(
        entity="Tesla", detected_hour=db.current_hour_iso(),
        z_score=4.0, multiple_of_baseline=4.5, avg_annoyance=80.0,
        count=10, sample_post_ids=[],
    )
    r = test_client.post(
        "/api/fp-flag", headers=pro_headers(),
        json={"target_id": str(sid), "target_type": "spike", "reason": "dup"},
    )
    assert r.status_code == 200
    queue = db.list_fp_queue()
    assert len(queue) == 1
    assert queue[0]["spike_id"] == sid
    assert queue[0]["reason"] == "dup"
