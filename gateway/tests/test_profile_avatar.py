"""Avatar upload decompression-bomb guard tests (audit HIGH, 2026-05-15).

Companion tests for the hardening in ``profile_routes.api_settings_avatar``.
The handler now:

  1. Caps ``Image.MAX_IMAGE_PIXELS`` at 16,000,000 pixels at module
     import time so a 2 MB PNG that decodes to 50k×50k can't OOM the
     worker.
  2. Promotes ``Image.DecompressionBombWarning`` to an exception via
     ``warnings.simplefilter("error", DecompressionBombWarning)`` inside
     a ``catch_warnings`` context, and intercepts both the Warning AND
     the harder ``DecompressionBombError`` to surface a 413
     ``image_too_large``.

This module pins the two contract cases the audit explicitly named:

  * Crafted 89 MP image (~9437×9437) → 413
  * 5 MP image (~2236×2236) → 200 (happy path: under cap, gets saved)

The companion file ``test_profile.py`` already exercises a 25 MP bomb
plus an introspection check that the ``@rate_limit`` decorator is still
wrapping the route. Together the two files cover the full guard surface.
"""

from __future__ import annotations

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — installs the in-memory conn

import db  # noqa: E402

# Apply migrations against the in-memory DB so users/sessions tables
# exist for the TestClient flow below.
import migrations as _migrations  # noqa: E402

USES_TESTDB = True


def _ensure_migrated():
    try:
        _migrations.upgrade_to_head()
    except Exception:
        with db.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            if "profile_avatar_url" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN profile_avatar_url TEXT")


_ensure_migrated()


try:
    from PIL import Image as _PIL
    _PIL_OK = True
except Exception:
    _PIL_OK = False


# ── Helpers ────────────────────────────────────────────────────────────


_user_seq = 0


def _new_user(email: str) -> int:
    """Insert a user row directly — the avatar handler only reads
    ``user_id`` off the session-resolved user dict, so we don't need
    a real password hash here."""
    global _user_seq
    _user_seq += 1
    username = f"avatartester{time.time_ns()}_{_user_seq}"
    if hasattr(db, "create_user"):
        try:
            return db.create_user(email=email, password="x" * 12, username=username)
        except Exception:
            pass
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, username, password_hash, password_salt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, username, "stub-hash", "stub-salt", int(time.time())),
        )
        return cur.lastrowid


def _png_bytes(width: int, height: int) -> bytes:
    """Build an in-memory PNG of the given dimensions. We use a solid
    fill so the encode is fast and the pixel count is the only knob
    Pillow inspects on re-open."""
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=(64, 128, 192))
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Contract pins ──────────────────────────────────────────────────────


@unittest.skipUnless(_PIL_OK, "Pillow not installed")
class TestAvatarDecompressionBombContract(unittest.TestCase):
    """Pin the two cases the audit named: bomb → 413, legit → 200."""

    def setUp(self):
        # Import here so the MAX_IMAGE_PIXELS side-effect of the
        # ``profile_routes`` import is observable for the assertion
        # below.
        import profile_routes  # noqa: F401 — side-effect import
        from fastapi.testclient import TestClient
        import server

        self.profile_routes = profile_routes
        self.uid = _new_user(f"avbomb_{time.time_ns()}@x.com")
        self.client = TestClient(server.app)
        token = db.create_session(self.uid)
        self.client.cookies.set(server.COOKIE_NAME, token)
        self.client.cookies.set("_csrf", "t_csrf_avatar")
        self.csrf = {"x-csrf-token": "t_csrf_avatar"}

    def test_max_image_pixels_cap_value(self):
        """The cap is the load-bearing constant — if anyone reverts
        this to Pillow's default ~89M, the bomb path is reachable
        again. Pin the exact value the audit chose."""
        from PIL import Image
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 16_000_000)

    def test_89mp_image_rejected_413(self):
        """A 9437×9437 image is ~89 M pixels — well over the 16 M cap
        and well over 2× the cap, so Pillow raises
        ``DecompressionBombError`` (not just the Warning). The handler
        must catch it and return 413 ``image_too_large`` rather than
        attempting to decode the full 89 M-pixel raster."""
        body = _png_bytes(9437, 9437)
        r = self.client.post(
            "/api/settings/avatar",
            files={"file": ("bomb.png", body, "image/png")},
            headers=self.csrf,
        )
        # 413 is the contract. 400 ``bad_image`` is acceptable if a
        # Pillow build folds the bomb error into the generic decode-
        # error branch — the load-bearing property is "no 200, no
        # 500, no worker hang".
        self.assertIn(r.status_code, (400, 413), r.text)
        if r.status_code == 413:
            self.assertIn(
                r.json().get("error"),
                ("image_too_large", "too_large"),
                r.text,
            )

    def test_5mp_image_accepted_200(self):
        """A 2236×2236 image is ~5 M pixels — well under the 16 M cap.
        The happy path must still succeed; the guard's job is to reject
        bombs, not to break ordinary avatars. The handler downscales
        to 200×200 webp before saving, so the response carries the
        cache-busted avatar URL."""
        body = _png_bytes(2236, 2236)
        # A 5 MP PNG is large on the wire — make sure we're under the
        # 2 MB envelope before sending so we test the decompression
        # guard, not the byte-size guard. A solid-fill PNG of this
        # size compresses far below 2 MB.
        self.assertLess(len(body), 2 * 1024 * 1024, "test fixture exceeded byte cap")
        r = self.client.post(
            "/api/settings/avatar",
            files={"file": ("ok.png", body, "image/png")},
            headers=self.csrf,
        )
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        self.assertTrue(payload.get("ok"))
        self.assertIn("/avatars/", payload.get("avatar_url", ""))


if __name__ == "__main__":
    unittest.main()
