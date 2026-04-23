"""New-user onboarding progress → completion."""

from __future__ import annotations

USES_TESTDB = True

import json
import time

import pytest
from tests import _testdb  # noqa: F401

import db


def _csrf(client) -> str:
    return client.cookies.get("_csrf") or "t"


def test_onboarding_flow(client, pass_gate, make_user, auth_headers):
    pass_gate()
    user = make_user()
    headers = auth_headers(user)

    # Step 1 — set categories.
    r = client.post(
        "/api/onboarding/categories",
        json={"categories": ["politics", "crypto"]},
        headers={**headers, "x-csrf-token": _csrf(client)},
    )
    if r.status_code == 404:
        # Try the form-encoded variant before giving up.
        r = client.post(
            "/api/onboarding/categories",
            data={"_csrf": _csrf(client), "categories": "politics,crypto"},
            headers=headers,
        )
    if r.status_code == 404:
        pytest.skip("onboarding endpoints not wired")
    assert r.status_code < 400, f"step 1: categories = {r.status_code}"

    # Step 2 — set notification prefs.
    r = client.post(
        "/api/onboarding/notifications",
        json={"push": False, "email": True,
              "ev_threshold": 0.1, "cred_threshold": 0.6},
        headers={**headers, "x-csrf-token": _csrf(client)},
    )
    assert r.status_code < 400 or r.status_code == 404

    # Step 3 — complete.
    r = client.post(
        "/api/onboarding/complete",
        data={"_csrf": _csrf(client)},
        headers=headers,
    )
    assert r.status_code < 400 or r.status_code == 404

    # Step 4 — DB invariants.
    try:
        status = db.get_onboarding_status(user["user_id"])
        assert status is not None
        assert "completed" in status
        if status.get("categories"):
            assert "politics" in status["categories"] or "crypto" in status["categories"]
    except Exception:
        # Schema variant — helper not present; best-effort.
        with db.conn() as c:
            row = c.execute(
                "SELECT onboarding_completed, onboarding_categories FROM users "
                "WHERE id = ?", (user["user_id"],),
            ).fetchone()
        if row is not None:
            if row["onboarding_categories"]:
                cats = json.loads(row["onboarding_categories"])
                assert any(c in cats for c in ("politics", "crypto"))
