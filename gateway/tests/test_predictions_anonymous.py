"""Tests for the deanonymisation audit fix.

Two leak vectors were closed:

1.  ``GET /predictions/public/{user_id}`` rendered the owning user's
    ``username`` in the page header (and shipped ``email`` into the
    template context as an unused-but-loaded footgun) even when ALL of
    that user's public predictions were marked ``is_anonymous=1``. The
    URL→user_id binding meant an attacker walking IDs could read back
    the handle behind every "anonymous" entry on the page.

2.  ``GET /api/leaderboard`` filled in the display handle with
    ``f"user_{r['user_id']}"`` whenever a leaderboard opt-in user hadn't
    picked a public handle. That string echoes the raw internal user_id
    straight back to every caller of the leaderboard endpoint, turning
    the leaderboard into a user-id directory. The fallback now renders
    as ``"anonymous"``.

The tests below cover both fixes with no dependency on the full
FastAPI/server import. ``user_prediction_routes._render`` and
``routes_referrals._current_user`` are monkeypatched so the assertions
can inspect the template kwargs / response payload directly without
spinning up the gateway.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest import mock

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import db_referrals as dbr  # noqa: E402
import routes_referrals  # noqa: E402
import user_prediction_routes  # noqa: E402


_USER_PREDICTIONS_PRESENT = all(
    hasattr(db, fn) for fn in (
        "create_user_prediction",
        "get_user_by_id",
        "list_public_user_predictions",
    )
)


def _mk_user(username: str) -> int:
    """Insert a user the same way the existing test_user_predictions does."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, "
            "                   created_at) "
            "VALUES (?, ?, 'h', 's', ?)",
            (username, f"{username}@anon.test", int(time.time())),
        )
        return c.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()["id"]


class _RenderRecorder:
    """Captures the kwargs passed to ``_render`` so tests can assert on
    which fields the public-profile page exposes to the template."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, name, request, **ctx):
        self.calls.append({"name": name, **ctx})
        return "<rendered>"


@unittest.skipUnless(
    _USER_PREDICTIONS_PRESENT,
    "user_predictions surface not present on this branch",
)
class TestPublicProfileAnonymity(unittest.TestCase):
    """``/predictions/public/{user_id}`` must not name a user whose public
    predictions are anonymous, and must never push email PII into the
    template context."""

    def setUp(self) -> None:
        self.recorder = _RenderRecorder()
        self._render_patch = mock.patch.object(
            user_prediction_routes, "_render", self.recorder
        )
        self._render_patch.start()

    def tearDown(self) -> None:
        self._render_patch.stop()

    def _run(self, user_id: int):
        return asyncio.run(
            user_prediction_routes.public_profile_page(
                request=mock.MagicMock(), user_id=user_id
            )
        )

    def test_anonymous_prediction_omits_user_attribution(self):
        """Walking the URL must not deanonymise the prediction author."""
        uid = _mk_user("anon_owner")
        db.create_user_prediction(
            user_id=uid,
            market_slug="poly:anon-1",
            market_question="Will the test pass?",
            category="other",
            predicted_outcome="YES",
            predicted_probability=0.7,
            is_public=True,
            is_anonymous=True,
        )

        self._run(uid)

        self.assertEqual(len(self.recorder.calls), 1)
        ctx = self.recorder.calls[0]
        # Header / page title must render the placeholder, not the handle.
        self.assertEqual(ctx["username"], "Anonymous")
        self.assertNotIn("anon_owner", ctx["username"])
        # The username column on the user row must not surface anywhere
        # else in the template context either — defence-in-depth against
        # a future template author adding {{ ... }} that re-echoes it.
        for value in ctx.values():
            if isinstance(value, str):
                self.assertNotIn("anon_owner", value)
                self.assertNotIn("@anon.test", value)
        # Footgun fix — the unused email kwarg should be gone outright.
        self.assertNotIn("email", ctx)

    def test_non_anonymous_prediction_renders_username(self):
        """Opt-in non-anonymous profiles still surface the handle so the
        feature stays useful for users who actively want attribution."""
        uid = _mk_user("named_owner")
        db.create_user_prediction(
            user_id=uid,
            market_slug="poly:named-1",
            market_question="Will the test pass?",
            category="other",
            predicted_outcome="NO",
            predicted_probability=0.4,
            is_public=True,
            is_anonymous=False,
        )

        self._run(uid)

        ctx = self.recorder.calls[0]
        self.assertEqual(ctx["username"], "named_owner")
        # Email PII still must not leak even on non-anonymous pages —
        # the template doesn't read it; passing it is the footgun.
        self.assertNotIn("email", ctx)

    def test_mixed_visibility_falls_back_to_anonymous(self):
        """If even one row on the page is anonymous, naming the user at
        the page level deanonymises that row by URL→handle binding."""
        uid = _mk_user("mixed_owner")
        db.create_user_prediction(
            user_id=uid,
            market_slug="poly:mixed-1",
            market_question="Will the test pass?",
            category="other",
            predicted_outcome="YES",
            predicted_probability=0.6,
            is_public=True,
            is_anonymous=False,
        )
        db.create_user_prediction(
            user_id=uid,
            market_slug="poly:mixed-2",
            market_question="Will the test pass?",
            category="other",
            predicted_outcome="NO",
            predicted_probability=0.3,
            is_public=True,
            is_anonymous=True,
        )

        self._run(uid)

        ctx = self.recorder.calls[0]
        self.assertEqual(ctx["username"], "Anonymous")


class TestLeaderboardHandleFallback(unittest.TestCase):
    """``GET /api/leaderboard`` must not echo back the raw user_id when
    an opt-in user hasn't chosen a public handle."""

    def setUp(self) -> None:
        # A real authenticated caller is required to pass the 401 and
        # 402 gates at the top of api_leaderboard. The caller's identity
        # is irrelevant to what we're asserting — we just need
        # _current_user and _require_paid_user to return something
        # truthy so we can reach the handle-rendering code path.
        self.caller_id = _mk_user("lb_caller")
        caller = {
            "user_id": self.caller_id,
            "email": "lb@test",
            "is_admin": 1,  # short-circuits the subscription check
        }
        self._cu_patch = mock.patch.object(
            routes_referrals, "_current_user", return_value=caller,
        )
        self._rp_patch = mock.patch.object(
            routes_referrals, "_require_paid_user",
            return_value={**caller, "tier": "pro"},
        )
        self._cu_patch.start()
        self._rp_patch.start()

    def tearDown(self) -> None:
        self._rp_patch.stop()
        self._cu_patch.stop()

    def _make_ranked_user(
        self, username: str, *, handle: str | None
    ) -> int:
        """Seed a user opted into the leaderboard with the given handle
        (or NULL) plus enough ``user_accuracy`` rows to clear the
        ``accuracy_all_time IS NOT NULL`` filter."""
        uid = _mk_user(username)
        with db.conn() as c:
            c.execute(
                "UPDATE users SET leaderboard_participation = 1, "
                "leaderboard_handle = ? WHERE id = ?",
                (handle, uid),
            )
        dbr.upsert_user_accuracy(
            uid,
            total=10,
            correct=7,
            accuracy_all=0.7,
            accuracy_90d=0.7,
            accuracy_30d=0.7,
            accuracy_7d=0.7,
        )
        return uid

    def test_no_handle_renders_as_anonymous(self):
        """The pre-fix fallback was f'user_{user_id}' — that string
        leaked the internal id. After the fix, rows without a public
        handle render as 'anonymous'."""
        unhandled_id = self._make_ranked_user("lb_unhandled", handle=None)

        response = asyncio.run(routes_referrals.api_leaderboard(
            request=mock.MagicMock(), period="all", limit=100,
        ))

        # Decode the JSONResponse body. The leaderboard is a list of
        # rows; pull out the one we care about by user_id.
        import json
        payload = json.loads(response.body.decode())
        # Find the row corresponding to our test user. The endpoint
        # doesn't expose user_id, so we match on the only row that
        # could be ours given our seeded set.
        rows = payload["rows"]
        # The caller's user row has no user_accuracy entry → filtered out;
        # only the unhandled user is on the board.
        self.assertEqual(len(rows), 1, payload)
        row = rows[0]
        self.assertEqual(row["handle"], "anonymous")
        # Explicitly assert the regressed format doesn't reappear.
        self.assertNotEqual(row["handle"], f"user_{unhandled_id}")
        self.assertNotIn(str(unhandled_id), row["handle"])

    def test_blank_handle_renders_as_anonymous(self):
        """A whitespace-only handle hits the same fallback path."""
        self._make_ranked_user("lb_blank", handle="   ")

        response = asyncio.run(routes_referrals.api_leaderboard(
            request=mock.MagicMock(), period="all", limit=100,
        ))

        import json
        payload = json.loads(response.body.decode())
        rows = payload["rows"]
        self.assertEqual(len(rows), 1, payload)
        self.assertEqual(rows[0]["handle"], "anonymous")

    def test_explicit_handle_passes_through(self):
        """Users who set a public handle still see it on the leaderboard."""
        self._make_ranked_user("lb_named", handle="topranker")

        response = asyncio.run(routes_referrals.api_leaderboard(
            request=mock.MagicMock(), period="all", limit=100,
        ))

        import json
        payload = json.loads(response.body.decode())
        rows = payload["rows"]
        self.assertEqual(len(rows), 1, payload)
        self.assertEqual(rows[0]["handle"], "topranker")


if __name__ == "__main__":
    unittest.main()
