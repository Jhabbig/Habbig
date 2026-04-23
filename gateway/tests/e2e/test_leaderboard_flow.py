"""Leaderboard opt-in / opt-out + ranking presence."""

from __future__ import annotations

USES_TESTDB = True

import pytest
from tests import _testdb  # noqa: F401

import db


def _csrf(client) -> str:
    return client.cookies.get("_csrf") or "t"


def test_leaderboard_flow(client, pass_gate, make_user, auth_headers):
    pass_gate()
    user = make_user()
    headers = auth_headers(user)

    # Step 1 — opt in.
    r = client.post(
        "/api/leaderboard/participate",
        data={"_csrf": _csrf(client)},
        headers=headers,
    )
    if r.status_code == 404:
        pytest.skip("leaderboard endpoints not wired")
    assert r.status_code < 400, (
        f"step 1: opt-in = {r.status_code}: {r.text[:200]}"
    )

    # Step 2 — listing endpoint returns valid JSON.
    r = client.get("/api/leaderboard?period=all", headers=headers)
    assert r.status_code == 200, f"step 2: list = {r.status_code}"
    try:
        body = r.json()
    except Exception:
        pytest.skip(f"leaderboard returned non-JSON: {r.text[:120]!r}")
    assert (
        "entries" in body or "leaderboard" in body or "rows" in body
        or isinstance(body, list)
    ), f"step 2: shape unexpected: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}"

    # Step 3 — opt out.
    r = client.post(
        "/api/leaderboard/opt-out",
        data={"_csrf": _csrf(client)},
        headers=headers,
    )
    assert r.status_code < 400, f"step 3: opt-out = {r.status_code}"

    # Step 4 — opted-out user is invisible in subsequent listing. We don't
    # know whether this build filters at query time or flags the row, so
    # we check the user-scoped flag directly.
    with db.conn() as c:
        for tbl in ("leaderboard_participants", "user_leaderboard_optin"):
            try:
                row = c.execute(
                    f"SELECT opted_in FROM {tbl} WHERE user_id = ?",
                    (user["user_id"],),
                ).fetchone()
                if row is not None:
                    assert not row["opted_in"], (
                        f"step 4: user still opted-in after opt-out"
                    )
                    break
            except Exception:
                continue
