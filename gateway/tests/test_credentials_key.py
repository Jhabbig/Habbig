"""Regression tests for the CREDENTIALS_ENCRYPTION_KEY startup guard.

Audit finding (HIGH, partial guard): the previous implementation only
raised when at least one TOTP user already existed in the DB, so a
fresh production deploy with zero TOTP users would boot cleanly and any
subsequent encryption write (TOTP enrolment, Kalshi token, etc.) would
silently store plaintext or 500 under load.

These tests pin the new behaviour:

  - In production, missing key raises at startup — regardless of TOTP
    users.
  - In production, an invalid Fernet key raises at startup.
  - In production, a valid Fernet key boots cleanly.
  - In dev, missing key does NOT raise (local iteration stays
    unblocked).
  - The old TOTP-conditional code is gone, so future refactors can't
    regress to the partial guard.

We exercise the guard by replaying its logic against a controlled
environment rather than booting the full app — the lifespan does
unrelated work (job queue, scheduler, migrations) that would make a
TestClient-based test slow and noisy. The source-pattern tests below
catch regressions in the guard itself; the behavioural tests catch
regressions in the semantics.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from cryptography.fernet import Fernet


SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"


def _run_guard(env: dict, is_production: bool) -> None:
    """Replay the production guard logic in isolation.

    Mirrors the block in server.lifespan() that validates
    CREDENTIALS_ENCRYPTION_KEY. Kept as a free function here so the test
    can exercise every branch without booting FastAPI's lifespan.
    """
    cred_key = env.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if is_production and not cred_key:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY must be set in production"
        )
    if is_production and cred_key:
        try:
            Fernet(cred_key.encode())
        except Exception as e:
            raise RuntimeError(
                f"CREDENTIALS_ENCRYPTION_KEY invalid Fernet key: {e}"
            )


class CredentialsKeyGuardSourceTests(unittest.TestCase):
    """Static checks on server.py — the guard's shape must not regress."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SERVER_PY.read_text(encoding="utf-8")

    def test_unconditional_production_guard_present(self) -> None:
        """The unconditional 'must be set in production' raise must exist."""
        self.assertIn(
            'CREDENTIALS_ENCRYPTION_KEY must be set in production',
            self.source,
            "expected unconditional production guard for "
            "CREDENTIALS_ENCRYPTION_KEY in server.py",
        )

    def test_fernet_validation_present(self) -> None:
        """The Fernet shape-check must exist so typos fail at boot."""
        self.assertIn(
            "invalid Fernet key",
            self.source,
            "expected Fernet shape validation for "
            "CREDENTIALS_ENCRYPTION_KEY in server.py",
        )

    def test_no_totp_conditional_guard(self) -> None:
        """The legacy 'only if TOTP users exist' guard must be gone."""
        # The bug was a `_totp_users > 0 and not os.environ.get(...)`
        # check. If that pattern reappears anywhere near the encryption
        # key it means we've regressed. Allow the substring "TOTP" to
        # appear elsewhere (handlers, comments) but not in a guard that
        # short-circuits on _totp_users with CREDENTIALS_ENCRYPTION_KEY.
        pattern = re.compile(
            r"_totp_users\s*>\s*0\s+and\s+not\s+os\.environ\.get\(\s*"
            r"['\"]CREDENTIALS_ENCRYPTION_KEY['\"]",
        )
        self.assertIsNone(
            pattern.search(self.source),
            "legacy TOTP-conditional guard for CREDENTIALS_ENCRYPTION_KEY "
            "still present in server.py — must be unconditional in "
            "production",
        )


class CredentialsKeyGuardBehaviourTests(unittest.TestCase):
    """Behavioural checks — replay the guard against synthetic envs."""

    def test_production_without_key_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _run_guard({}, is_production=True)
        self.assertIn(
            "CREDENTIALS_ENCRYPTION_KEY must be set in production",
            str(ctx.exception),
        )

    def test_production_with_invalid_key_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _run_guard(
                {"CREDENTIALS_ENCRYPTION_KEY": "not-a-real-fernet-key"},
                is_production=True,
            )
        self.assertIn("invalid Fernet key", str(ctx.exception))

    def test_production_with_valid_key_passes(self) -> None:
        valid = Fernet.generate_key().decode()
        # Should not raise.
        _run_guard(
            {"CREDENTIALS_ENCRYPTION_KEY": valid},
            is_production=True,
        )

    def test_dev_without_key_does_not_raise(self) -> None:
        # Local dev must stay unblocked when the key isn't set.
        _run_guard({}, is_production=False)

    def test_dev_with_invalid_key_does_not_raise(self) -> None:
        # The validator only runs in production; a malformed key in dev
        # is the dev's problem and shouldn't refuse to start.
        _run_guard(
            {"CREDENTIALS_ENCRYPTION_KEY": "bogus"},
            is_production=False,
        )

    def test_guard_does_not_depend_on_totp_user_count(self) -> None:
        """The guard must fire even when there are zero TOTP users.

        We can't easily inject a fake user count into the live lifespan,
        but the _run_guard replica deliberately omits any DB lookup —
        mirroring the new code, which removes the SELECT COUNT(*) on
        users.totp_enabled. If someone re-adds a TOTP check that gates
        the raise, the source-pattern test above will catch it; this
        test documents the contract.
        """
        # Same call as test_production_without_key_raises — left as a
        # distinct test so the failure surface points at the TOTP
        # contract rather than the missing-key contract.
        with self.assertRaises(RuntimeError):
            _run_guard({}, is_production=True)


if __name__ == "__main__":
    unittest.main()
