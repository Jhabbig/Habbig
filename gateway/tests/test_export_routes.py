"""Tests for gateway/export_routes.py — focused on the CRITICAL FIX:

  1. ``_export_secret()`` no longer falls back to a guessable derivation
     of ``DATA_EXPORT_DIR``. With no signing key set:
       - in production: refuses to run (RuntimeError),
       - in dev: refuses to run (RuntimeError) — loud failure beats a
         silent vuln shipping to prod.
  2. ``api_download_export`` requires a valid signature AND a session
     that owns the export (or is admin). A forged signature returns 403.
     A valid signature from the wrong user's session returns 403. The
     owner downloads the file (200).

We DO NOT exercise the full FastAPI server here — instead we mount only
``export_routes`` on a minimal FastAPI app and patch ``current_user`` so
the test never needs the impersonation / SessionMiddleware stack.
"""

from __future__ import annotations

USES_TESTDB = True

import contextlib
import io
import json
import os
import sys
import time
import unittest
import zipfile
from pathlib import Path
from typing import Optional

from tests import _testdb  # noqa: F401 — installs shared in-memory DB

import db  # noqa: E402


# Stable signing key for every test in this file. The fix removed the
# guessable fallback; tests opt in to a known key here so signature
# round-trips work the same way in CI as they would in prod.
os.environ.setdefault("DATA_EXPORT_SIGNING_KEY", "test-export-signing-key-32-chars-min")
os.environ.setdefault("DATA_EXPORT_DIR", "/tmp/narve-exports-tests")


import export_routes  # noqa: E402  — imported after env is primed


# ── Test helpers ─────────────────────────────────────────────────────────


def _build_app_with_fake_user():
    """Spin up a minimal FastAPI app with export_routes registered and
    a fake ``current_user`` that reads from an x-test-user-json header.

    Returns (app, TestClient, restore_callable). The restore callable
    swaps back the real ``current_user`` so other tests in the suite
    aren't affected.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    # Patch _current_user on the export_routes module so the handler
    # reads our test header instead of poking server.py. We deliberately
    # do NOT mount the real server — the auth surface here is the only
    # part we care about for this fix.
    orig = export_routes._current_user

    def _fake_current_user(request):
        header = request.headers.get("x-test-user-json")
        if not header:
            return None
        try:
            return json.loads(header)
        except Exception:
            return None

    export_routes._current_user = _fake_current_user
    export_routes.register(app)
    client = TestClient(app)

    def _restore():
        export_routes._current_user = orig

    return app, client, _restore


def _user_header(*, user_id: int, is_admin: bool = False) -> dict:
    return {
        "x-test-user-json": json.dumps({
            "user_id": user_id,
            "email": f"u{user_id}@test.example",
            "is_admin": is_admin,
            "is_super_admin": False,
            "admin_level": 1 if is_admin else 0,
        }),
    }


def _make_ready_export(user_id: int, file_bytes: bytes = b"PK\x05\x06" + b"\x00" * 18) -> tuple[int, Path, int]:
    """Insert a data_export_request row in the 'ready' state with a real
    zip-shaped file on disk. Returns (export_id, file_path, expires_at).
    """
    eid = db.create_data_export_request(user_id)
    export_dir = Path(os.environ["DATA_EXPORT_DIR"])
    export_dir.mkdir(parents=True, exist_ok=True)
    fpath = export_dir / f"test-export-{eid}-u{user_id}.zip"
    fpath.write_bytes(file_bytes)
    expires_at = int(time.time()) + 3600
    db.update_data_export_request(
        eid,
        status="ready",
        completed_at=int(time.time()),
        file_path=str(fpath),
        file_size_bytes=len(file_bytes),
        expires_at=expires_at,
        download_url="__signed__",
    )
    return eid, fpath, expires_at


# ── Tests: signing key hygiene ───────────────────────────────────────────


class TestExportSecretRefusesGuessableFallback(unittest.TestCase):
    """The legacy ``_export_secret`` derived a key from
    ``f'dataexport:{EXPORT_DIR}'`` when no env var was set. The default
    EXPORT_DIR is ``/tmp/narve-exports`` — fully guessable. The fix is
    to refuse to operate without an explicit key.
    """

    def setUp(self):
        # Snapshot env so we can flip both keys off and restore.
        self._saved = {
            "DATA_EXPORT_SIGNING_KEY": os.environ.get("DATA_EXPORT_SIGNING_KEY"),
            "GATEWAY_COOKIE_SECRET": os.environ.get("GATEWAY_COOKIE_SECRET"),
            "PRODUCTION": os.environ.get("PRODUCTION"),
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _clear_keys(self):
        os.environ.pop("DATA_EXPORT_SIGNING_KEY", None)
        os.environ.pop("GATEWAY_COOKIE_SECRET", None)

    def test_no_key_in_production_raises_runtimeerror(self):
        """Production with no signing material must hard-fail rather
        than derive a guessable key from EXPORT_DIR."""
        self._clear_keys()
        os.environ["PRODUCTION"] = "1"
        with self.assertRaises(RuntimeError) as ctx:
            export_routes._export_secret()
        self.assertIn("DATA_EXPORT_SIGNING_KEY", str(ctx.exception))

    def test_no_key_in_dev_also_raises_runtimeerror(self):
        """We also refuse to operate in dev without an explicit key —
        otherwise the dev path silently uses a guessable key and the
        bug never surfaces locally before shipping."""
        self._clear_keys()
        os.environ.pop("PRODUCTION", None)
        with self.assertRaises(RuntimeError):
            export_routes._export_secret()

    def test_explicit_data_export_signing_key_works(self):
        self._clear_keys()
        os.environ["DATA_EXPORT_SIGNING_KEY"] = "dev-signing-key-xyzzy-123456789"
        # Should not raise; returns 32 bytes (sha256 digest).
        key = export_routes._export_secret()
        self.assertEqual(len(key), 32)

    def test_falls_back_to_gateway_cookie_secret(self):
        self._clear_keys()
        os.environ["GATEWAY_COOKIE_SECRET"] = "gateway-cookie-secret-x-32-chars"
        key = export_routes._export_secret()
        self.assertEqual(len(key), 32)

    def test_dedicated_key_preferred_over_cookie_secret(self):
        """When both are set the dedicated key wins, so operators can
        rotate it without invalidating session cookies."""
        self._clear_keys()
        os.environ["DATA_EXPORT_SIGNING_KEY"] = "dedicated-key-zzz"
        os.environ["GATEWAY_COOKIE_SECRET"] = "cookie-secret-yyy"
        key_dedicated = export_routes._export_secret()

        os.environ.pop("DATA_EXPORT_SIGNING_KEY", None)
        key_fallback = export_routes._export_secret()
        self.assertNotEqual(key_dedicated, key_fallback)


# ── Tests: download authorization ────────────────────────────────────────


class TestDownloadAuthorization(unittest.TestCase):
    """End-to-end checks on the /api/account/export/{id}/download
    handler — the route that the legacy guessable key allowed an
    attacker to forge URLs for.
    """

    @classmethod
    def setUpClass(cls):
        cls.app, cls.client, cls._restore = _build_app_with_fake_user()

        # Two distinct users for the cross-user matrix.
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-owner@test.com'"
            ).fetchone()
        if row:
            cls.owner_id = row["id"]
        else:
            cls.owner_id = db.create_user(
                "export-owner@test.com", "ExpOwn1!Pass", "export_owner"
            )

        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-attacker@test.com'"
            ).fetchone()
        if row:
            cls.attacker_id = row["id"]
        else:
            cls.attacker_id = db.create_user(
                "export-attacker@test.com", "ExpAtk1!Pass", "export_attacker"
            )

        # Admin user — admins are allowed to download anyone's export.
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-admin@test.com'"
            ).fetchone()
        if row:
            cls.admin_id = row["id"]
        else:
            cls.admin_id = db.create_user(
                "export-admin@test.com", "ExpAdm1!Pass", "export_admin",
            )

    @classmethod
    def tearDownClass(cls):
        cls._restore()

    def setUp(self):
        # Clean export rows between tests so create_data_export_request
        # IDs are deterministic per test.
        with db.conn() as c:
            c.execute(
                "DELETE FROM data_export_requests WHERE user_id IN (?, ?, ?)",
                (self.owner_id, self.attacker_id, self.admin_id),
            )

    # ── Forged signature ────────────────────────────────────────────────

    def test_forged_signature_returns_403(self):
        """Attacker guesses a URL with a fabricated signature — must 403."""
        eid, _path, expires_at = _make_ready_export(self.owner_id)
        # Owner session, but a garbage signature.
        forged_sig = "deadbeef" * 4  # 32 hex chars, valid shape, wrong key.
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": forged_sig},
            headers=_user_header(user_id=self.owner_id),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("signature", r.json().get("detail", "").lower())

    def test_forged_signature_with_no_session_still_403(self):
        """Even without a session, forged signatures must die at the
        signature check (defence in depth, not signature dependent on
        being logged in)."""
        eid, _path, expires_at = _make_ready_export(self.owner_id)
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": "0" * 32},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    # ── Valid signature, wrong user session ─────────────────────────────

    def test_valid_signature_wrong_user_returns_403(self):
        """The CRITICAL FIX: a leaked email link, valid signature and
        all, must not work for someone else's session. Owner's signed
        URL + attacker's session → 403."""
        eid, _path, expires_at = _make_ready_export(self.owner_id)
        valid_sig = export_routes._sign(eid, self.owner_id, expires_at)
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": valid_sig},
            headers=_user_header(user_id=self.attacker_id),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("not your export", r.json().get("detail", "").lower())

    def test_valid_signature_no_session_returns_401(self):
        """Old behaviour was 200 here — link was shareable from email.
        New behaviour: 401, because the signature alone is not enough."""
        eid, _path, expires_at = _make_ready_export(self.owner_id)
        valid_sig = export_routes._sign(eid, self.owner_id, expires_at)
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": valid_sig},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 401)

    # ── Happy path ──────────────────────────────────────────────────────

    def test_owner_with_valid_signature_returns_200(self):
        """Sanity check: legitimate owner with valid signed URL still
        downloads the file."""
        payload = b"PK\x05\x06" + b"\x00" * 18  # smallest valid zip EOCD.
        eid, _path, expires_at = _make_ready_export(self.owner_id, payload)
        valid_sig = export_routes._sign(eid, self.owner_id, expires_at)
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": valid_sig},
            headers=_user_header(user_id=self.owner_id),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200, msg=f"body={r.text!r}")
        self.assertEqual(r.headers.get("content-type"), "application/zip")
        self.assertEqual(r.content, payload)

    def test_admin_can_download_other_user_export(self):
        """Admins are explicitly allowed (audit / debug). The session
        check should let them through with a valid signature."""
        eid, _path, expires_at = _make_ready_export(self.owner_id)
        valid_sig = export_routes._sign(eid, self.owner_id, expires_at)
        r = self.client.get(
            f"/api/account/export/{eid}/download",
            params={"u": self.owner_id, "e": expires_at, "s": valid_sig},
            headers=_user_header(user_id=self.admin_id, is_admin=True),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200, msg=f"body={r.text!r}")


# ── Tests: signing round-trip ────────────────────────────────────────────


class TestSigningRoundTrip(unittest.TestCase):
    """The HMAC helpers themselves — independent of the route."""

    def test_sign_and_verify_match(self):
        expires_at = int(time.time()) + 3600
        sig = export_routes._sign(42, 7, expires_at)
        self.assertTrue(export_routes._verify(42, 7, expires_at, sig))

    def test_sig_rejects_different_export_id(self):
        expires_at = int(time.time()) + 3600
        sig = export_routes._sign(42, 7, expires_at)
        self.assertFalse(export_routes._verify(43, 7, expires_at, sig))

    def test_sig_rejects_different_user_id(self):
        expires_at = int(time.time()) + 3600
        sig = export_routes._sign(42, 7, expires_at)
        self.assertFalse(export_routes._verify(42, 8, expires_at, sig))

    def test_sig_rejects_different_expiry(self):
        expires_at = int(time.time()) + 3600
        sig = export_routes._sign(42, 7, expires_at)
        self.assertFalse(export_routes._verify(42, 7, expires_at + 60, sig))

    def test_sig_rejects_empty(self):
        expires_at = int(time.time()) + 3600
        self.assertFalse(export_routes._verify(42, 7, expires_at, ""))


if __name__ == "__main__":
    unittest.main()
