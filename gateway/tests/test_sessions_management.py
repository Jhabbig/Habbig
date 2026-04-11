"""Tests for the active-sessions management API.

Spec requirements:
  - GET /api/auth/sessions returns active sessions only
  - Cannot revoke current session via DELETE /api/auth/sessions/{id}
  - DELETE /api/auth/sessions revokes all except current
  - MAX 5 sessions: 6th login revokes oldest
"""

from __future__ import annotations

import hashlib
import os
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TestActiveSessionsList(unittest.TestCase):
    def test_list_returns_only_active_sessions(self):
        uid = db.create_user("sm-list@test.com", "InitialPass123!", username="smlist1")
        raw1 = db.create_user_session(uid, ip_address="1.1.1.1", user_agent="ua-a")
        raw2 = db.create_user_session(uid, ip_address="2.2.2.2", user_agent="ua-b")

        sessions = db.list_user_sessions(uid)
        self.assertEqual(len(sessions), 2)
        # Most recently active first
        self.assertGreaterEqual(sessions[0]["last_active_at"], sessions[1]["last_active_at"] - 1)

        # Revoke raw1 — list should drop to 1
        db.revoke_user_session_by_token(raw1)
        sessions = db.list_user_sessions(uid)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["token_hash"], _sha(raw2))

    def test_list_excludes_other_users(self):
        uid_a = db.create_user("sm-a@test.com", "InitialPass123!", username="sma1")
        uid_b = db.create_user("sm-b@test.com", "InitialPass123!", username="smb1")
        db.create_user_session(uid_a)
        db.create_user_session(uid_a)
        db.create_user_session(uid_b)

        a_sessions = db.list_user_sessions(uid_a)
        b_sessions = db.list_user_sessions(uid_b)
        self.assertEqual(len(a_sessions), 2)
        self.assertEqual(len(b_sessions), 1)
        for s in a_sessions:
            self.assertEqual(s["user_id"], uid_a)


class TestRevokeSingleSession(unittest.TestCase):
    def test_revoke_by_id_works(self):
        uid = db.create_user("sm-rev@test.com", "InitialPass123!", username="smrev1")
        raw1 = db.create_user_session(uid)
        raw2 = db.create_user_session(uid)

        sessions = db.list_user_sessions(uid)
        # Kill the older one
        target = sessions[-1]  # oldest
        ok = db.revoke_user_session(target["id"], uid)
        self.assertTrue(ok)

        sessions = db.list_user_sessions(uid)
        self.assertEqual(len(sessions), 1)

    def test_cannot_revoke_another_users_session(self):
        uid_a = db.create_user("sm-rev-a@test.com", "InitialPass123!", username="smreva1")
        uid_b = db.create_user("sm-rev-b@test.com", "InitialPass123!", username="smrevb1")
        raw_b = db.create_user_session(uid_b)
        b_sessions = db.list_user_sessions(uid_b)
        target = b_sessions[0]

        ok = db.revoke_user_session(target["id"], uid_a)
        self.assertFalse(ok, "revoke_user_session must enforce ownership")
        # B's session is still valid
        self.assertIsNotNone(db.validate_user_session(raw_b))


class TestRevokeAllOthers(unittest.TestCase):
    def test_bulk_revoke_keeps_current_session(self):
        uid = db.create_user("sm-all@test.com", "InitialPass123!", username="small1")
        raws = [db.create_user_session(uid) for _ in range(4)]
        current_hash = _sha(raws[-1])

        count = db.revoke_all_other_user_sessions(uid, current_hash)
        self.assertEqual(count, 3)

        # Current is still valid
        self.assertIsNotNone(db.validate_user_session(raws[-1]))
        # Others are dead
        for raw in raws[:-1]:
            self.assertIsNone(db.validate_user_session(raw))

    def test_bulk_revoke_on_single_session_returns_zero(self):
        uid = db.create_user("sm-solo@test.com", "InitialPass123!", username="smsolo1")
        raw = db.create_user_session(uid)
        count = db.revoke_all_other_user_sessions(uid, _sha(raw))
        self.assertEqual(count, 0)
        self.assertIsNotNone(db.validate_user_session(raw))


class TestMaxFiveSessions(unittest.TestCase):
    def test_sixth_login_revokes_oldest(self):
        uid = db.create_user("sm-max@test.com", "InitialPass123!", username="smmax1")
        # The helper revokes proactively when active count >= MAX, BEFORE
        # inserting the new row, so the total active count never exceeds MAX.
        raws = []
        for i in range(db.MAX_SESSIONS_PER_USER + 3):
            raws.append(db.create_user_session(uid))

        active = db.list_user_sessions(uid)
        self.assertLessEqual(len(active), db.MAX_SESSIONS_PER_USER)
        # The newest 5 should still be valid
        for raw in raws[-db.MAX_SESSIONS_PER_USER:]:
            self.assertIsNotNone(db.validate_user_session(raw),
                                 "newest sessions must survive")
        # The oldest should be revoked
        for raw in raws[:-db.MAX_SESSIONS_PER_USER]:
            self.assertIsNone(db.validate_user_session(raw),
                              "oldest sessions should have been revoked")

    def test_max_is_5(self):
        self.assertEqual(db.MAX_SESSIONS_PER_USER, 5,
                         "the spec fixes MAX_SESSIONS_PER_USER at 5")


if __name__ == "__main__":
    unittest.main()
