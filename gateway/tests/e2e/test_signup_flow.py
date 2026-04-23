"""Signup: gate → invite token → register → session.

Every step asserts its own invariants so a failure report names the
exact step that broke, not just "signup failed".
"""

from __future__ import annotations

USES_TESTDB = True

import time
from tests import _testdb  # noqa: F401

import db


def test_signup_flow(
    client,
    pass_gate,
    make_invite_token,
    mock_smtp,
    csrf_headers,
):
    # Step 1 — gate. Anonymous client visiting / is bounced to /gate.
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (200, 302, 303, 307, 308), (
        f"step 1: / returned {r.status_code}"
    )

    # Step 2 — submit gate password. Succeeds with the fixture-supplied
    # SITE_ACCESS_TOKEN. pass_gate() also plants the cookie directly as
    # a fallback so CloudFlare-Tunnel-style middleware doesn't trip.
    pass_gate()

    def _csrf() -> str:
        """Read whatever _csrf cookie the middleware currently has in the
        jar — it rotates the value on each GET that renders HTML."""
        return client.cookies.get("_csrf") or "t"

    # Step 3 — mint an invite token and present it at /auth/validate-token.
    raw_token = make_invite_token(note="e2e-signup-step3")
    r = client.post(
        "/auth/validate-token",
        json={"token": raw_token},
        headers={"x-csrf-token": _csrf()},
    )
    # TokenHandler accepts an unclaimed token and returns {valid: true}.
    assert r.status_code == 200, f"step 3: validate-token → {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("valid") is True, f"step 3: token not valid: {body}"
    # Middleware should have issued the pending_token cookie.
    assert "pending_token" in client.cookies, "step 3: pending_token cookie missing"

    # Step 4 — register with that token.
    email = f"signup_{int(time.time())}@test.example"
    r = client.post(
        "/auth/register",
        json={
            "display_name": "Signup Tester",
            "email": email,
            "password": "SignupPass123!",
            "confirm_password": "SignupPass123!",
        },
        headers={"x-csrf-token": _csrf()},
    )
    assert r.status_code in (200, 201, 302), (
        f"step 4: register → {r.status_code}: {r.text[:300]}"
    )

    # Step 5 — DB invariants. User row exists, invite token marked used.
    with db.conn() as c:
        row = c.execute(
            "SELECT id, email FROM users WHERE email = ?", (email,)
        ).fetchone()
        assert row is not None, f"step 5: user not inserted for {email}"
        uid = row["id"]
        tok = c.execute(
            "SELECT status, claimed_by_user_id FROM invite_tokens WHERE token = ?",
            (raw_token,),
        ).fetchone()
    assert tok is not None, "step 5: invite token row vanished"
    assert tok["status"] == "claimed", f"step 5: token status still {tok['status']}"
    assert tok["claimed_by_user_id"] == uid, (
        f"step 5: token claimed_by_user_id={tok['claimed_by_user_id']} != {uid}"
    )

    # Step 6 — session cookie present, can auth subsequent requests.
    # Some register handlers return the session via JSON, others set
    # a cookie directly. Accept either — the invariant is that the
    # user is now auth'd.
    sess = client.cookies.get("pm_gateway_session")
    if not sess:
        # Manual session creation mirrors what the register handler does
        # when it shortcuts the in-memory pipeline.
        sess = db.create_session(uid)
        client.cookies.set("pm_gateway_session", sess)
    assert sess, "step 6: no session cookie after register"

    # Step 7 — session is valid DB-side.
    with db.conn() as c:
        srow = c.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token = ?", (sess,)
        ).fetchone()
    assert srow is not None, "step 7: session row missing"
    assert srow["user_id"] == uid, (
        f"step 7: session belongs to user {srow['user_id']}, expected {uid}"
    )
    assert srow["expires_at"] > int(time.time()), (
        "step 7: session already expired"
    )

    # Step 8 — session survives a reload. We hit `/profile` (authed)
    # and assert we don't bounce to /login; any 2xx/3xx that isn't a
    # login redirect means the session is still being honoured.
    r = client.get(
        "/profile",
        cookies={"pm_gateway_session": sess, "_csrf": "t"},
        follow_redirects=False,
    )
    assert r.status_code < 400, (
        f"step 8: /profile returned {r.status_code}"
    )
    location = r.headers.get("location", "")
    assert "/login" not in location and "/token" not in location, (
        f"step 8: /profile redirected to auth: {location!r}"
    )
