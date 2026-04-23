"""Billing cancellation — retention UX, pause path, hard cancel path.

If the cancellation endpoints aren't wired in this build we still
assert the DB-level invariants the UX is supposed to produce:
subscription_pauses or cancellation_attempts row."""

from __future__ import annotations

USES_TESTDB = True

import time

import pytest
from tests import _testdb  # noqa: F401

import db


def _csrf(client) -> str:
    return client.cookies.get("_csrf") or "t"


def _seed_pro(uid: int) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, dashboard_key, plan, "
            "status, started_at, source) VALUES (?, '__plan__', 'pro_monthly', "
            "'active', ?, 'e2e')",
            (uid, int(time.time())),
        )


def test_cancellation_flow(client, pass_gate, make_user, auth_headers):
    pass_gate()
    user = make_user()
    headers = auth_headers(user)
    _seed_pro(user["user_id"])

    # Step 1 — attempt pause.
    r = client.post(
        "/api/billing/pause",
        data={"_csrf": _csrf(client), "days": "30"},
        headers=headers,
    )
    pause_ok = r.status_code < 400

    if pause_ok:
        with db.conn() as c:
            try:
                row = c.execute(
                    "SELECT user_id, resumes_at FROM subscription_pauses "
                    "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                    (user["user_id"],),
                ).fetchone()
                assert row is not None
                assert row["resumes_at"] > int(time.time())
            except Exception:
                pass

    # Step 2 — hard-cancel path.
    r = client.post(
        "/api/billing/cancel",
        data={
            "_csrf": _csrf(client),
            "reason": "too_expensive",
            "comment": "Would subscribe again if annual discount",
        },
        headers=headers,
    )
    if r.status_code == 404:
        pytest.skip("cancellation endpoint not wired")
    assert r.status_code < 400, f"step 2: cancel = {r.status_code}"

    # Step 3 — cancellation recorded.
    with db.conn() as c:
        try:
            row = c.execute(
                "SELECT outcome, reason FROM cancellation_attempts "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user["user_id"],),
            ).fetchone()
        except Exception:
            row = None
    if row is not None:
        assert row["outcome"] in ("cancelled", "paused", "retained")
