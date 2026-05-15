"""Regression tests for the missing-AuditAction CRITICAL audit-log gap.

Background
----------
`admin_routes.flag_create / flag_save / flag_delete / impersonate_start /
impersonate_end` all reference `AuditAction.FEATURE_FLAG_*` and
`AuditAction.IMPERSONATION_*` constants. The impersonation middleware in
`gateway/server.py` additionally fires `IMPERSONATION_BLOCKED` when a
read-only session attempts a mutating verb. Those constants did not
exist on `security.audit.AuditAction`, and `admin_routes._audit()`
silently swallowed the resulting `AttributeError` with a bare
`except: pass`.

Net effect in production: ZERO audit rows for any feature-flag CRUD and
ZERO audit rows for any impersonation start/end/blocked — a major
compliance gap that the silent except hid for the entire lifetime of
those code paths.

This module locks in the fix:

1. The six constants exist on `AuditAction`
   (FEATURE_FLAG_{CREATE,UPDATE,DELETE} +
   IMPERSONATION_{START,END,BLOCKED}).
2. Calling `admin_routes._audit(action, …)` for each of them writes a
   row to `audit_log`.
3. `_audit()` no longer silently swallows truly broken calls — a missing
   AuditAction attribute re-raises the AttributeError so the next gap
   surfaces in tests instead of in production telemetry months later.
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401  — shared in-memory DB

USES_TESTDB = True

import db  # noqa: E402
from security import audit  # noqa: E402
import admin_routes  # noqa: E402


class _FakeRequest:
    """Minimal Request stand-in for audit._get_ip / _get_user_agent / etc."""

    def __init__(self, ip="9.9.9.9", ua="pytest-audit/1.0", req_id="req-flag"):
        class _C:
            host = ip
        self.client = _C()
        self.headers = {
            "user-agent": ua,
            "x-request-id": req_id,
            "x-forwarded-for": ip,
        }


def _make_admin(email: str) -> dict:
    """Create an admin row in the shared test DB and return the
    ``current_user``-shaped dict the admin_routes helpers expect."""
    uid = db.create_user(email, "TestPass123!", username=email.split("@")[0], is_admin=True)
    return {"user_id": uid, "email": email, "is_admin": 1}


# ── 1. Constants exist on AuditAction ────────────────────────────────────────


class TestAuditActionConstantsExist(unittest.TestCase):
    """All six constants the audit found missing must be defined."""

    def test_feature_flag_constants(self):
        self.assertEqual(audit.AuditAction.FEATURE_FLAG_CREATE, "feature_flag.create")
        self.assertEqual(audit.AuditAction.FEATURE_FLAG_UPDATE, "feature_flag.update")
        self.assertEqual(audit.AuditAction.FEATURE_FLAG_DELETE, "feature_flag.delete")

    def test_impersonation_constants(self):
        self.assertEqual(audit.AuditAction.IMPERSONATION_START, "impersonation.start")
        self.assertEqual(audit.AuditAction.IMPERSONATION_END, "impersonation.end")
        # IMPERSONATION_BLOCKED is fired from the impersonation middleware
        # in server.py when a read-only session attempts a mutating verb.
        # Same swallow class as the other constants — define it explicitly.
        self.assertEqual(audit.AuditAction.IMPERSONATION_BLOCKED, "impersonation.blocked")

    def test_constants_are_in_all_actions(self):
        """ALL_ACTIONS drives the audit-log filter dropdown — make sure
        every newly-added constant is enumerated so admins can filter
        by it from the UI."""
        for name in (
            audit.AuditAction.FEATURE_FLAG_CREATE,
            audit.AuditAction.FEATURE_FLAG_UPDATE,
            audit.AuditAction.FEATURE_FLAG_DELETE,
            audit.AuditAction.IMPERSONATION_START,
            audit.AuditAction.IMPERSONATION_END,
            audit.AuditAction.IMPERSONATION_BLOCKED,
        ):
            self.assertIn(name, audit.ALL_ACTIONS)

    def test_constants_have_action_labels(self):
        """The audit-log page renders ACTION_LABELS for human-readable
        descriptions. Missing labels show the raw key — ugly but not
        broken — so this is a soft check; we still assert they're
        present so the UI stays polished."""
        for name in (
            audit.AuditAction.FEATURE_FLAG_CREATE,
            audit.AuditAction.FEATURE_FLAG_UPDATE,
            audit.AuditAction.FEATURE_FLAG_DELETE,
            audit.AuditAction.IMPERSONATION_START,
            audit.AuditAction.IMPERSONATION_END,
            audit.AuditAction.IMPERSONATION_BLOCKED,
        ):
            self.assertIn(name, audit.ACTION_LABELS)
            self.assertTrue(audit.ACTION_LABELS[name])


# ── 2. _audit() actually writes rows for these actions ───────────────────────


class TestFeatureFlagAuditRows(unittest.TestCase):
    """Toggling a flag must write an audit_log row — the bug fixed here."""

    @classmethod
    def setUpClass(cls):
        cls.admin = _make_admin("flag_audit_admin@test.com")

    def _count_action(self, action: str) -> int:
        _rows, total = db.query_audit_log(action=action)
        return total

    def test_feature_flag_create_writes_row(self):
        before = self._count_action(audit.AuditAction.FEATURE_FLAG_CREATE)
        admin_routes._audit(
            audit.AuditAction.FEATURE_FLAG_CREATE,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="feature_flag",
            target_id="test_flag_create",
            target_description="A test flag",
        )
        after = self._count_action(audit.AuditAction.FEATURE_FLAG_CREATE)
        self.assertEqual(after, before + 1,
                         "Creating a feature flag must write exactly one audit_log row")

    def test_feature_flag_update_writes_row(self):
        before = self._count_action(audit.AuditAction.FEATURE_FLAG_UPDATE)
        admin_routes._audit(
            audit.AuditAction.FEATURE_FLAG_UPDATE,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="feature_flag",
            target_id="test_flag_update",
            after={"enabled_globally": True, "rollout_percentage": 25},
        )
        after = self._count_action(audit.AuditAction.FEATURE_FLAG_UPDATE)
        self.assertEqual(after, before + 1)

    def test_feature_flag_delete_writes_row(self):
        before = self._count_action(audit.AuditAction.FEATURE_FLAG_DELETE)
        admin_routes._audit(
            audit.AuditAction.FEATURE_FLAG_DELETE,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="feature_flag",
            target_id="test_flag_delete",
        )
        after = self._count_action(audit.AuditAction.FEATURE_FLAG_DELETE)
        self.assertEqual(after, before + 1)

    def test_feature_flag_row_captures_request_metadata(self):
        """The audit row should not just count — it should preserve the
        IP, user agent and request-id the admin acted from."""
        admin_routes._audit(
            audit.AuditAction.FEATURE_FLAG_UPDATE,
            admin=self.admin,
            request=_FakeRequest(ip="10.20.30.40", ua="ff-audit/2", req_id="req-meta"),
            target_type="feature_flag",
            target_id="meta_flag",
        )
        rows, _total = db.query_audit_log(action=audit.AuditAction.FEATURE_FLAG_UPDATE)
        meta_rows = [r for r in rows if r["target_id"] == "meta_flag"]
        self.assertTrue(meta_rows, "Expected at least one row with target_id='meta_flag'")
        row = meta_rows[0]
        self.assertEqual(row["ip_address"], "10.20.30.40")
        self.assertEqual(row["user_agent"], "ff-audit/2")
        self.assertEqual(row["request_id"], "req-meta")
        self.assertEqual(row["admin_email"], "flag_audit_admin@test.com")


class TestImpersonationAuditRows(unittest.TestCase):
    """Starting/ending impersonation must each write an audit_log row."""

    @classmethod
    def setUpClass(cls):
        cls.admin = _make_admin("imp_audit_admin@test.com")

    def _count_action(self, action: str) -> int:
        _rows, total = db.query_audit_log(action=action)
        return total

    def test_impersonation_start_writes_row(self):
        before = self._count_action(audit.AuditAction.IMPERSONATION_START)
        admin_routes._audit(
            audit.AuditAction.IMPERSONATION_START,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="user",
            target_id=42,
            target_description="victim@example.com",
            notes="reason=investigating billing complaint",
        )
        after = self._count_action(audit.AuditAction.IMPERSONATION_START)
        self.assertEqual(after, before + 1)

    def test_impersonation_end_writes_row(self):
        before = self._count_action(audit.AuditAction.IMPERSONATION_END)
        admin_routes._audit(
            audit.AuditAction.IMPERSONATION_END,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="user",
            target_id=42,
            notes="session_id=99",
        )
        after = self._count_action(audit.AuditAction.IMPERSONATION_END)
        self.assertEqual(after, before + 1)

    def test_impersonation_blocked_writes_row(self):
        """The middleware-fired BLOCKED action must also reach audit_log."""
        before = self._count_action(audit.AuditAction.IMPERSONATION_BLOCKED)
        admin_routes._audit(
            audit.AuditAction.IMPERSONATION_BLOCKED,
            admin=self.admin,
            request=_FakeRequest(),
            target_type="user",
            target_id=42,
            target_description="victim@example.com",
            notes="POST /admin/users/42",
        )
        after = self._count_action(audit.AuditAction.IMPERSONATION_BLOCKED)
        self.assertEqual(after, before + 1)


# ── 3. _audit() no longer silently swallows truly broken calls ───────────────


class TestAuditDoesNotSilentlySwallow(unittest.TestCase):
    """The bare ``except: pass`` is gone.

    Specifically: referencing a nonexistent ``AuditAction`` attribute used
    to raise AttributeError, which was swallowed and the call site got a
    None back with no signal. After the fix, the AttributeError surfaces
    at the call site (i.e. *before* _audit is even entered, because the
    arg is computed first), and any other programming error inside
    _audit re-raises so it can't be silently dropped.
    """

    @classmethod
    def setUpClass(cls):
        cls.admin = _make_admin("strict_audit_admin@test.com")

    def test_missing_action_attribute_raises_at_call_site(self):
        """Reading a nonexistent attribute on AuditAction blows up
        BEFORE _audit() is entered — this is what the missing constants
        used to do, hidden by the swallow."""
        with self.assertRaises(AttributeError):
            _ = audit.AuditAction.THIS_ATTRIBUTE_DOES_NOT_EXIST  # type: ignore[attr-defined]

    def test_audit_reraises_on_truly_broken_call(self):
        """If ``security.audit.log_action`` itself raises (e.g. a
        programming error wired into the helper), _audit must re-raise
        so the caller knows the audit row was lost, instead of silently
        eating the exception the way the old ``except: pass`` did."""
        import security.audit as _audit_mod

        original = _audit_mod.log_action

        def broken_log_action(**_kwargs):
            raise RuntimeError("simulated programming error inside log_action")

        _audit_mod.log_action = broken_log_action
        try:
            with self.assertRaises(RuntimeError):
                admin_routes._audit(
                    audit.AuditAction.FEATURE_FLAG_CREATE,
                    admin=self.admin,
                    request=_FakeRequest(),
                    target_type="feature_flag",
                    target_id="never_logged",
                )
        finally:
            _audit_mod.log_action = original


if __name__ == "__main__":
    unittest.main()
