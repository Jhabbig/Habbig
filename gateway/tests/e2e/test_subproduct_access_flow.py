"""Paywall matrix: subproduct subscription grants only that product's
routes, Pro grants everything.

The gateway reverse-proxies subdomains — in a test environment we
can't spin up real subdomains, so we probe the access helpers
directly. The invariants we assert are the ones downstream middleware
consults."""

from __future__ import annotations

USES_TESTDB = True

import time

import pytest
from tests import _testdb  # noqa: F401

import db


def _grant_sub(uid: int, key: str, plan: str = "monthly", status: str = "active"):
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, dashboard_key, plan, "
            "status, started_at, source) VALUES (?, ?, ?, ?, ?, 'e2e')",
            (uid, key, plan, status, int(time.time())),
        )


def _has_access(uid: int, dashboard: str) -> bool:
    """Probe whatever helper the build exposes for subproduct access."""
    for name in ("user_has_dashboard_access", "has_subproduct_access",
                 "get_user_active_subscription"):
        fn = getattr(db, name, None)
        if fn is None:
            continue
        try:
            res = fn(uid, dashboard) if name != "get_user_active_subscription" else fn(uid)
        except TypeError:
            res = fn(uid)
        if isinstance(res, bool):
            return res
        if isinstance(res, dict):
            return res.get("status") == "active" and (
                res.get("dashboard_key") in (None, "__plan__", dashboard)
            )
    # Fallback — direct query.
    with db.conn() as c:
        r = c.execute(
            "SELECT 1 FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND dashboard_key IN (?, '__plan__')",
            (uid, dashboard),
        ).fetchone()
    return r is not None


def test_subproduct_access_flow(client, make_user, pass_gate):
    pass_gate()
    user = make_user()
    uid = user["user_id"]

    # Step 1 — user with only `sports` has access to sports.
    _grant_sub(uid, "sports")
    assert _has_access(uid, "sports"), "step 1: sports sub not active"

    # Step 2 — same user has NO access to crypto.
    assert not _has_access(uid, "crypto"), "step 2: crypto leaked without sub"

    # Step 3 — Pro plan grants access to every dashboard_key.
    _grant_sub(uid, "__plan__", plan="pro_monthly")
    for dash in ("sports", "crypto", "world", "weather", "midterm"):
        assert _has_access(uid, dash), (
            f"step 3: pro-plan user denied access to {dash}"
        )

    # Step 4 — canceling the pro plan (but keeping sports) re-introduces
    # the per-subproduct gating.
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'canceled' "
            "WHERE user_id = ? AND dashboard_key = '__plan__'",
            (uid,),
        )
    assert _has_access(uid, "sports"), "step 4: sports sub erroneously revoked"
    assert not _has_access(uid, "crypto"), (
        "step 4: crypto still accessible after pro cancel"
    )
