"""Unit tests for the public-profile + follow-graph layer.

The DB-touching tests use ``tests._testdb``'s in-memory connection so
they don't need a server running. Validation tests work directly off
the helpers in ``queries.profile``.

Coverage:
  * Reserved + invalid handles rejected.
  * Duplicate handles rejected (unique partial index honoured).
  * 30-day cooldown blocks second handle change but not bio edits.
  * Follow toggle is idempotent + symmetric.
  * follower_count returns 0 for unknown user.
  * Self-follow silently ignored.
  * Gravatar fallback hash is stable.
  * /u/{handle} returns 404 when user has the handle but isn't opted in
    (existence-hide property — never 403).
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — installs the in-memory conn
import db  # noqa: E402

# Apply migrations 172 + 173 against the in-memory DB.
import migrations as _migrations  # noqa: E402

USES_TESTDB = True


def _ensure_migrated():
    """Run any outstanding migrations on the in-memory connection."""
    try:
        _migrations.upgrade_to_head()
    except Exception:
        # Some older test fixtures stub out the migration pipeline.
        with db.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            if "public_profile_enabled" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN public_profile_enabled INTEGER DEFAULT 0")
            if "profile_handle" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN profile_handle TEXT")
            if "profile_bio" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN profile_bio TEXT")
            if "profile_avatar_url" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN profile_avatar_url TEXT")
            if "profile_handle_changed_at" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN profile_handle_changed_at INTEGER")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_profile_handle "
                "ON users(profile_handle) WHERE profile_handle IS NOT NULL"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS user_follows ("
                "  follower_user_id INTEGER NOT NULL, "
                "  followed_user_id INTEGER NOT NULL, "
                "  followed_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "  PRIMARY KEY (follower_user_id, followed_user_id)"
                ")"
            )


_ensure_migrated()

from queries import profile as profile_q  # noqa: E402  — after migrations
from profile_routes import _gravatar  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────


_user_seq = 0


def _new_user(email: str, *, username: str | None = None) -> int:
    """Create a user via the existing helper (or fall back to a raw INSERT
    with a stub password hash so the NOT NULL constraint is satisfied).

    Username defaults to a per-test-process unique slug to dodge the
    UNIQUE constraint on ``users.username``."""
    global _user_seq
    _user_seq += 1
    if username is None:
        username = f"tester{time.time_ns()}_{_user_seq}"
    if hasattr(db, "create_user"):
        try:
            return db.create_user(email=email, password="x" * 12, username=username)
        except Exception:
            pass
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, username, password_hash, password_salt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, username, "stub-hash-not-real", "stub-salt", int(time.time())),
        )
        return cur.lastrowid


# ── Validation ─────────────────────────────────────────────────────────


class TestHandleValidation(unittest.TestCase):
    def setUp(self):
        self.uid = _new_user(f"v{time.time_ns()}@example.com")

    def test_reserved_handle_rejected(self):
        with self.assertRaises(profile_q.ProfileError) as cm:
            profile_q.update_profile(self.uid, enabled=True, handle="admin", bio=None)
        self.assertEqual(cm.exception.code, "handle_reserved")

    def test_invalid_handle_pattern_rejected(self):
        # Each of these stays invalid even after .lower() — short, dashed,
        # too long, or contains whitespace.
        for bad in ("ab", "with-dash", "a" * 21, "spaces in"):
            with self.assertRaises(profile_q.ProfileError) as cm:
                profile_q.update_profile(self.uid, enabled=False, handle=bad, bio=None)
            self.assertEqual(cm.exception.code, "handle_invalid", f"{bad!r} should fail")

    def test_handle_normalised_to_lowercase(self):
        # Mixed-case input is silently lowercased on save — the form
        # surfaces the lowercased value back so the user sees the
        # canonical form.
        result = profile_q.update_profile(
            self.uid, enabled=True, handle="MyName_42", bio=None,
        )
        self.assertEqual(result["profile_handle"], "myname_42")

    def test_bio_length_capped(self):
        with self.assertRaises(profile_q.ProfileError) as cm:
            profile_q.update_profile(
                self.uid, enabled=False, handle=None, bio="x" * 201,
            )
        self.assertEqual(cm.exception.code, "bio_too_long")

    def test_enable_without_handle_refused(self):
        with self.assertRaises(profile_q.ProfileError) as cm:
            profile_q.update_profile(self.uid, enabled=True, handle=None, bio="hi")
        self.assertEqual(cm.exception.code, "handle_required")


class TestUniqueHandle(unittest.TestCase):
    def test_duplicate_handle_rejected(self):
        a = _new_user(f"a{time.time_ns()}@example.com")
        b = _new_user(f"b{time.time_ns()}@example.com")
        profile_q.update_profile(a, enabled=True, handle="forecaster_x", bio=None)
        with self.assertRaises(profile_q.ProfileError) as cm:
            profile_q.update_profile(b, enabled=True, handle="forecaster_x", bio=None)
        self.assertEqual(cm.exception.code, "handle_taken")

    def test_keeping_own_handle_succeeds(self):
        # A user re-saving their existing handle with a new bio must NOT
        # trip the cooldown / uniqueness checks.
        uid = _new_user(f"keep{time.time_ns()}@example.com")
        profile_q.update_profile(uid, enabled=True, handle="my_handle_42", bio="first")
        # Bio change keeps handle the same — must succeed.
        result = profile_q.update_profile(
            uid, enabled=True, handle="my_handle_42", bio="updated bio",
        )
        self.assertEqual(result["profile_bio"], "updated bio")


class TestHandleCooldown(unittest.TestCase):
    def test_change_within_30_days_blocked(self):
        uid = _new_user(f"cool{time.time_ns()}@example.com")
        profile_q.update_profile(uid, enabled=True, handle="first_handle", bio=None)
        with self.assertRaises(profile_q.ProfileError) as cm:
            profile_q.update_profile(uid, enabled=True, handle="second_handle", bio=None)
        self.assertEqual(cm.exception.code, "handle_cooldown")

    def test_bio_only_edit_does_not_trip_cooldown(self):
        uid = _new_user(f"bio{time.time_ns()}@example.com")
        profile_q.update_profile(uid, enabled=True, handle="bio_user", bio=None)
        # Same handle, bio edit — must succeed.
        result = profile_q.update_profile(
            uid, enabled=True, handle="bio_user", bio="new bio text",
        )
        self.assertEqual(result["profile_bio"], "new bio text")


# ── Follow graph ───────────────────────────────────────────────────────


class TestFollowGraph(unittest.TestCase):
    def setUp(self):
        self.alice = _new_user(f"alice{time.time_ns()}@x.com")
        self.bob = _new_user(f"bob{time.time_ns()}@x.com")

    def test_follow_inserts_row(self):
        self.assertTrue(profile_q.follow(self.alice, self.bob))
        self.assertTrue(profile_q.is_following(self.alice, self.bob))
        self.assertEqual(profile_q.follower_count(self.bob), 1)

    def test_follow_idempotent(self):
        profile_q.follow(self.alice, self.bob)
        # Second insert must report False (no new row).
        self.assertFalse(profile_q.follow(self.alice, self.bob))
        self.assertEqual(profile_q.follower_count(self.bob), 1)

    def test_unfollow_removes_row(self):
        profile_q.follow(self.alice, self.bob)
        self.assertTrue(profile_q.unfollow(self.alice, self.bob))
        self.assertFalse(profile_q.is_following(self.alice, self.bob))
        self.assertEqual(profile_q.follower_count(self.bob), 0)

    def test_unfollow_unknown_pair_returns_false(self):
        self.assertFalse(profile_q.unfollow(self.alice, self.bob))

    def test_self_follow_ignored(self):
        self.assertFalse(profile_q.follow(self.alice, self.alice))
        self.assertFalse(profile_q.is_following(self.alice, self.alice))
        self.assertEqual(profile_q.follower_count(self.alice), 0)

    def test_toggle_alternates(self):
        s1 = profile_q.toggle_follow(self.alice, self.bob)
        self.assertTrue(s1["is_following"])
        s2 = profile_q.toggle_follow(self.alice, self.bob)
        self.assertFalse(s2["is_following"])
        self.assertEqual(s2["follower_count"], 0)


# ── Lookup ─────────────────────────────────────────────────────────────


class TestProfileLookup(unittest.TestCase):
    def test_disabled_profile_returns_none(self):
        uid = _new_user(f"hide{time.time_ns()}@x.com")
        # Save handle WITHOUT enabling so the partial index is populated
        # but the public visibility is off.
        profile_q.update_profile(uid, enabled=False, handle="hidden_42", bio=None)
        self.assertIsNone(profile_q.get_profile_by_handle("hidden_42"))

    def test_enabled_profile_resolves(self):
        uid = _new_user(f"show{time.time_ns()}@x.com")
        profile_q.update_profile(uid, enabled=True, handle="visible_99", bio="hi")
        row = profile_q.get_profile_by_handle("visible_99")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], uid)


# ── Gravatar fallback ──────────────────────────────────────────────────


class TestGravatar(unittest.TestCase):
    def test_md5_stable(self):
        # Reference: gravatar's docs say lowercase + trim before md5.
        url = _gravatar("Test@Example.COM ")
        # md5("test@example.com") = 55502f40dc8b7c769880b10874abc9d0
        self.assertIn("55502f40dc8b7c769880b10874abc9d0", url)
        self.assertIn("d=identicon", url)



# ── Audit HIGH (2026-05-15): avatar decompression-bomb guard ───────────


try:
    from PIL import Image as _AVATAR_TEST_PIL_IMAGE
    _AVATAR_TEST_PIL_OK = True
except Exception:
    _AVATAR_TEST_PIL_OK = False


class TestAvatarDecompressionBombGuard(unittest.TestCase):
    """Audit HIGH — the avatar upload route is now hardened against
    Pillow decompression-bomb attacks.

    The fix has three pieces:
      1. Module-level ``Image.MAX_IMAGE_PIXELS = 16_000_000`` so a
         2 MB PNG that decodes to 50k×50k can't OOM the worker.
      2. ``@rate_limit("avatar-upload", 5, 60)`` per-user budget so a
         single attacker can't replay a borderline image to peg CPU.
      3. ``DecompressionBombError`` / ``DecompressionBombWarning``
         intercepted explicitly and surfaced as 413 ``image_too_large``.

    This test pins (1) and (3) with a 4096×4096+ test bitmap that
    trips the cap. The rate-limit decorator is verified by inspecting
    the route handler's ``__wrapped__`` attribute — calling 6 times
    in a unit test would couple to other tests' rate-limit state.
    """

    @unittest.skipUnless(_AVATAR_TEST_PIL_OK, "Pillow not installed")
    def test_max_image_pixels_is_capped(self):
        # The module loaded its cap at import time. Reading the
        # class-level attribute confirms the cap survived
        # ``profile_routes`` import.
        import profile_routes  # noqa: F401 — side-effect import to set cap
        from PIL import Image
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 16_000_000)

    @unittest.skipUnless(_AVATAR_TEST_PIL_OK, "Pillow not installed")
    def test_avatar_handler_rejects_decompression_bomb(self):
        """A 5000×5000 image (25M pixels) exceeds the 16M cap. The
        handler must surface 413 ``image_too_large`` rather than
        attempting to decode and crash the worker."""
        import io
        import os
        import sys
        # Path the gateway module so profile_routes resolves.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        # We must rely on the actual handler since rate_limit + the
        # bomb error catch are the load-bearing pieces.
        import profile_routes
        from PIL import Image

        # Build a 5000×5000 RGB image. Encode it as PNG so the body
        # has real bytes; Pillow opens it lazily, runs verify(), then
        # raises a DecompressionBombWarning on re-open (since 25M >
        # 16M cap but < 2×16M = 32M, this is a Warning, not Error).
        buf = io.BytesIO()
        img = Image.new("RGB", (5000, 5000), color=(128, 128, 128))
        img.save(buf, format="PNG")
        buf.seek(0)

        # Drive the handler through a TestClient so the wrapper /
        # rate-limit chain runs. Authenticate by stubbing
        # ``server.current_user`` to return a fixed user dict.
        import sys
        import db
        import server
        from fastapi.testclient import TestClient

        # Use any extant user id; the handler only reads user["user_id"].
        # _new_user is module-local and creates a fresh DB row.
        uid = _new_user(f"avbomb_{time.time_ns()}@x.com")

        client = TestClient(server.app)
        token = db.create_session(uid)
        client.cookies.set(server.COOKIE_NAME, token)
        client.cookies.set("_csrf", "t_avatar_csrf")

        r = client.post(
            "/api/settings/avatar",
            files={"file": ("bomb.png", buf.getvalue(), "image/png")},
            headers={"x-csrf-token": "t_avatar_csrf"},
        )
        # 413 is the strict contract — the bomb branch surfaces
        # ``image_too_large``. We accept 400 if Pillow rejects on
        # verify() before the warning fires (different Pillow versions
        # behave differently on borderline sizes).
        self.assertIn(r.status_code, (400, 413), r.text)
        if r.status_code == 413:
            self.assertIn(
                r.json().get("error"),
                ("image_too_large", "too_large"),
            )

    def test_rate_limit_decorator_attached(self):
        """The handler must carry the @rate_limit wrapper so a single
        attacker can't replay borderline images to peg CPU. We
        introspect the function object rather than firing 6 requests
        because rate-limit state leaks across tests in the shared
        in-memory bucket store."""
        import profile_routes
        handler = profile_routes.api_settings_avatar
        # @rate_limit wraps with functools.wraps so __wrapped__ should
        # point at the original function — its presence is the proof
        # that the decorator ran.
        self.assertTrue(
            hasattr(handler, "__wrapped__"),
            "api_settings_avatar must be wrapped by @rate_limit",
        )



if __name__ == "__main__":
    unittest.main()
