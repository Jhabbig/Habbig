"""Rate limiter unit tests.

The limiter lives in rate_limiter.py. We exercise it via the FastAPI route
stack so we catch wiring bugs (wrong scope, forgot to pass the user dict).
"""

from __future__ import annotations

import pytest

import rate_limiter
from tests.conftest import pro_headers


def _hit_api_index(test_client, headers):
    return test_client.get("/api/index", headers=headers)


def test_60_requests_allowed_per_minute(test_client, paywall_env):
    """First 60 requests from a single pro user must all return 200."""
    headers = pro_headers(user_id=1001)
    for i in range(60):
        r = _hit_api_index(test_client, headers)
        assert r.status_code == 200, f"req {i} unexpected {r.status_code}"


def test_61st_request_returns_429(test_client, paywall_env):
    headers = pro_headers(user_id=1002)
    for _ in range(60):
        r = _hit_api_index(test_client, headers)
        assert r.status_code == 200
    r = _hit_api_index(test_client, headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # Retry-After must be a positive integer (never 0 — clients spin otherwise).
    assert int(r.headers["Retry-After"]) >= 1


def test_limiter_keyed_by_user_id_when_authed(test_client, paywall_env):
    """Two different authed users don't share a bucket."""
    h1 = pro_headers(user_id=2001)
    h2 = pro_headers(user_id=2002)
    for _ in range(60):
        assert _hit_api_index(test_client, h1).status_code == 200
    # user 2001 is exhausted
    assert _hit_api_index(test_client, h1).status_code == 429
    # user 2002 still has a full bucket
    assert _hit_api_index(test_client, h2).status_code == 200


def test_limiter_keyed_by_ip_when_unauth(test_client, paywall_env):
    """Even though /api/index requires auth, prove the key helper splits by
    IP for unauthed requests. Exercise the function directly since there
    is no public unauthed /api route to hit in the limiter's own scope."""
    class _Req:
        def __init__(self, ip: str):
            self.headers = {}
            class _C:
                host = ip
            self.client = _C()

    key_a = rate_limiter.rate_key(_Req("1.2.3.4"), None)
    key_b = rate_limiter.rate_key(_Req("5.6.7.8"), None)
    assert key_a != key_b
    assert key_a.startswith("ip:")

    # Authed beats IP
    authed = rate_limiter.rate_key(_Req("1.2.3.4"), {"id": 99})
    assert authed == "user:99"


def test_fp_flag_has_tighter_budget(test_client, paywall_env):
    """/api/fp-flag is capped at 10 requests per minute (scope=fp_flag).

    The scope namespacing means exhausting fp_flag doesn't touch /api/index.
    """
    headers = pro_headers(user_id=3001)
    for _ in range(10):
        r = test_client.post("/api/fp-flag", json={"target_id": "1"}, headers=headers)
        # 200 (written) or 400/500 (db missing) — but NEVER 429 within budget
        assert r.status_code != 429
    # 11th hits the limit
    r = test_client.post("/api/fp-flag", json={"target_id": "1"}, headers=headers)
    assert r.status_code == 429
    # And /api/index is still fine because the scopes are separate buckets
    assert _hit_api_index(test_client, headers).status_code == 200


def test_429_body_is_json(test_client, paywall_env):
    headers = pro_headers(user_id=4001)
    for _ in range(60):
        _hit_api_index(test_client, headers)
    r = _hit_api_index(test_client, headers)
    assert r.status_code == 429
    body = r.json()
    assert "rate_limit_exceeded" in str(body)


def test_reset_for_tests_clears_state():
    """The autouse fixture relies on this — double-check it actually empties."""
    class _Req:
        def __init__(self):
            self.headers = {}
            class _C:
                host = "9.9.9.9"
            self.client = _C()

    # Fill a bucket
    from fastapi import HTTPException
    user = {"id": 5001}
    for _ in range(60):
        rate_limiter.enforce(_Req(), user,
                             limit=60, window_seconds=60, scope="reset_test")
    with pytest.raises(HTTPException) as exc_info:
        rate_limiter.enforce(_Req(), user,
                             limit=60, window_seconds=60, scope="reset_test")
    assert exc_info.value.status_code == 429

    # Reset wipes the bucket
    rate_limiter.reset_for_tests()
    rate_limiter.enforce(_Req(), user,
                         limit=60, window_seconds=60, scope="reset_test")  # no raise
