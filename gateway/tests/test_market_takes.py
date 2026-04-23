"""End-to-end tests for the Community Takes feature (migrations 122–124).

Covers every spec bullet:
  - paid-only posting (free/anon users blocked)
  - duplicate take rejected
  - 24h edit window (passes within, blocks after)
  - one vote per user per take (re-vote overwrites, clear deletes)
  - shadow-hide when downvotes ≥ 3 AND quality < -5
  - report creates a queue entry; idempotent on repeat
  - /admin/moderation routes: list + resolve + hard-delete
  - resolution job: position-matches-outcome flips resolved_correct and
    recomputes quality_score with the correctness multiplier

Uses the shared in-memory DB from ``tests._testdb`` so migrations 122/123/
124 actually run at module import.
"""

from __future__ import annotations

USES_TESTDB = True

import os
import secrets
import time
import unittest

import pytest

os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import db
import db_takes
import server
from fastapi.testclient import TestClient


client = TestClient(server.app)


_CSRF_TOKEN = "test-csrf-token-takes"


# ── Fixtures ────────────────────────────────────────────────────────────────


def _uniq(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(3)}"


def _make_user(
    prefix: str = "u",
    *,
    is_admin: bool = False,
    paid: bool = False,
) -> int:
    """Create a user. `paid=True` grants an active pro subscription so
    the paid gate lets them post takes.
    """
    uname = _uniq(prefix)
    uid = db.create_user(
        f"{uname}@test.local", "TestPw!!1234", username=uname, is_admin=is_admin,
    )
    if paid:
        # Give them an active pro subscription on the default dashboard.
        try:
            db.upsert_subscription(
                uid, dashboard_key="all", plan="pro",
                status="active", source="test",
            )
        except Exception:
            # Schema may use a different helper; fall back to a direct insert.
            with db.conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO subscriptions "
                    "(user_id, dashboard_key, plan, status, started_at, source) "
                    "VALUES (?, 'all', 'pro', 'active', ?, 'test')",
                    (uid, int(time.time())),
                )
    return uid


def _session_cookies(uid: int) -> dict:
    token = db.create_session(uid)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return {
        server.COOKIE_NAME: token,
        server.CSRF_COOKIE_NAME: _CSRF_TOKEN,
    }


def _csrf_headers() -> dict:
    return {server.CSRF_HEADER_NAME: _CSRF_TOKEN}


def _good_reasoning() -> str:
    # 60+ chars so it clears the 50-char minimum with room to spare.
    return (
        "Q1 data plus recent policy signals make this outcome almost "
        "certain. Market is mispricing uncertainty here."
    )


# ── 1. Paid gate ────────────────────────────────────────────────────────────


class TestPaidGate(unittest.TestCase):
    def test_anonymous_cannot_post(self):
        r = client.post(
            "/api/v1/markets/poly:test-anon/takes",
            json={"position": "yes", "reasoning": _good_reasoning(), "confidence": 7},
            headers=_csrf_headers(),
            cookies={server.CSRF_COOKIE_NAME: _CSRF_TOKEN},
        )
        self.assertEqual(r.status_code, 401)

    def test_free_user_cannot_post(self):
        uid = _make_user("free", paid=False)
        r = client.post(
            "/api/v1/markets/poly:test-free/takes",
            json={"position": "yes", "reasoning": _good_reasoning(), "confidence": 7},
            headers=_csrf_headers(),
            cookies=_session_cookies(uid),
        )
        self.assertEqual(r.status_code, 402)
        self.assertIn("subscription", r.json()["detail"].lower())

    def test_paid_user_can_post(self):
        uid = _make_user("paid", paid=True)
        r = client.post(
            "/api/v1/markets/poly:test-paid/takes",
            json={"position": "yes", "reasoning": _good_reasoning(), "confidence": 7},
            headers=_csrf_headers(),
            cookies=_session_cookies(uid),
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["position"], "yes")
        self.assertEqual(body["confidence"], 7)
        self.assertTrue(body["can_edit"])

    def test_admin_can_post_without_subscription(self):
        admin = _make_user("admin", is_admin=True, paid=False)
        r = client.post(
            "/api/v1/markets/poly:test-admin/takes",
            json={"position": "no", "reasoning": _good_reasoning()},
            headers=_csrf_headers(),
            cookies=_session_cookies(admin),
        )
        self.assertEqual(r.status_code, 201, r.text)


# ── 2. Input validation ────────────────────────────────────────────────────


class TestInputValidation(unittest.TestCase):
    def _post(self, uid, slug, payload):
        return client.post(
            f"/api/v1/markets/{slug}/takes",
            json=payload,
            headers=_csrf_headers(),
            cookies=_session_cookies(uid),
        )

    def _err_text(self, r) -> str:
        """Extract the human-readable error from either response shape.

        The validation-error response format was tightened post-ship to
        return `{"error": "…", "field": "…"}` instead of the FastAPI-
        default `{"detail": "…"}`. Read both so this test survives either
        version.
        """
        body = r.json() or {}
        return str(body.get("error") or body.get("detail") or body)

    def test_short_reasoning_rejected(self):
        uid = _make_user("val_short", paid=True)
        r = self._post(uid, "poly:val-short", {
            "position": "yes", "reasoning": "too short", "confidence": 5,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("50", self._err_text(r))

    def test_long_reasoning_rejected(self):
        uid = _make_user("val_long", paid=True)
        r = self._post(uid, "poly:val-long", {
            "position": "yes", "reasoning": "x" * 2001, "confidence": 5,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("2000", self._err_text(r))

    def test_invalid_position_rejected(self):
        uid = _make_user("val_pos", paid=True)
        r = self._post(uid, "poly:val-pos", {
            "position": "maybe", "reasoning": _good_reasoning(),
        })
        self.assertEqual(r.status_code, 400)

    def test_invalid_confidence_rejected(self):
        uid = _make_user("val_conf", paid=True)
        r = self._post(uid, "poly:val-conf", {
            "position": "yes", "confidence": 99, "reasoning": _good_reasoning(),
        })
        self.assertEqual(r.status_code, 400)


# ── 3. Duplicates + edit window + soft-delete ──────────────────────────────


class TestDuplicatesAndEditing(unittest.TestCase):
    def test_duplicate_take_rejected(self):
        uid = _make_user("dup", paid=True)
        slug = "poly:dup-test"
        r1 = client.post(
            f"/api/v1/markets/{slug}/takes",
            json={"position": "yes", "reasoning": _good_reasoning()},
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r1.status_code, 201)
        r2 = client.post(
            f"/api/v1/markets/{slug}/takes",
            json={"position": "no", "reasoning": _good_reasoning()},
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("already have a take", r2.json()["detail"])

    def test_edit_within_window_works(self):
        uid = _make_user("ewin", paid=True)
        tid = db_takes.create_take(
            user_id=uid, market_slug="poly:edit-win",
            position="yes", reasoning=_good_reasoning(), confidence=5,
        )
        r = client.patch(
            f"/api/v1/takes/{tid}",
            json={"confidence": 9},
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["confidence"], 9)

    def test_edit_after_window_blocked(self):
        uid = _make_user("ewin_exp", paid=True)
        tid = db_takes.create_take(
            user_id=uid, market_slug="poly:edit-expired",
            position="yes", reasoning=_good_reasoning(),
        )
        # Backdate the take to 25h ago so the 24h window has closed.
        with db.conn() as c:
            c.execute(
                "UPDATE market_takes SET created_at = ? WHERE id = ?",
                (int(time.time()) - (25 * 3600), tid),
            )
        r = client.patch(
            f"/api/v1/takes/{tid}",
            json={"confidence": 9},
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r.status_code, 409)
        self.assertIn("edit window", r.json()["detail"])

    def test_edit_by_non_owner_forbidden(self):
        owner = _make_user("owner", paid=True)
        other = _make_user("other", paid=True)
        tid = db_takes.create_take(
            user_id=owner, market_slug="poly:owner-only",
            position="yes", reasoning=_good_reasoning(),
        )
        r = client.patch(
            f"/api/v1/takes/{tid}",
            json={"confidence": 3},
            headers=_csrf_headers(), cookies=_session_cookies(other),
        )
        self.assertEqual(r.status_code, 403)

    def test_soft_delete_then_repost(self):
        uid = _make_user("redelete", paid=True)
        slug = "poly:redelete"
        tid = db_takes.create_take(
            user_id=uid, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        r = client.delete(
            f"/api/v1/takes/{tid}",
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r.status_code, 200)
        # Can now post a fresh take because the partial uniq index excludes
        # soft-deleted rows.
        r2 = client.post(
            f"/api/v1/markets/{slug}/takes",
            json={"position": "no", "reasoning": _good_reasoning()},
            headers=_csrf_headers(), cookies=_session_cookies(uid),
        )
        self.assertEqual(r2.status_code, 201)


# ── 4. Rate limit ──────────────────────────────────────────────────────────


class TestPostingRateLimit(unittest.TestCase):
    def test_eleventh_take_in_24h_blocked(self):
        uid = _make_user("rl", paid=True)
        for i in range(db_takes.MAX_TAKES_PER_DAY):
            db_takes.create_take(
                user_id=uid, market_slug=f"poly:rl-{i}",
                position="yes", reasoning=_good_reasoning(),
            )
        with self.assertRaises(ValueError) as cm:
            db_takes.create_take(
                user_id=uid, market_slug="poly:rl-over",
                position="yes", reasoning=_good_reasoning(),
            )
        self.assertIn("rate limit", str(cm.exception).lower())


# ── 5. Voting ──────────────────────────────────────────────────────────────


class TestVoting(unittest.TestCase):
    def _mk_take(self, slug="poly:vote-test"):
        author = _make_user("voteauth", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        return author, tid

    def test_upvote_increments_counts(self):
        author, tid = self._mk_take("poly:vote-up")
        voter = _make_user("voter_up")
        r = client.post(
            f"/api/v1/takes/{tid}/vote",
            json={"vote": 1},
            headers=_csrf_headers(), cookies=_session_cookies(voter),
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["upvotes"], 1)
        self.assertEqual(r.json()["downvotes"], 0)

    def test_revote_replaces_old_vote(self):
        author, tid = self._mk_take("poly:vote-flip")
        voter = _make_user("voter_flip")
        # First: up
        db_takes.cast_vote(tid, voter, 1)
        # Then: down. Must REPLACE, not add.
        up, down = db_takes.cast_vote(tid, voter, -1)
        self.assertEqual(up, 0)
        self.assertEqual(down, 1)

    def test_clear_vote_removes_row(self):
        author, tid = self._mk_take("poly:vote-clear")
        voter = _make_user("voter_clear")
        db_takes.cast_vote(tid, voter, 1)
        up, down = db_takes.clear_vote(tid, voter)
        self.assertEqual(up, 0)
        self.assertEqual(down, 0)

    def test_self_vote_is_noop(self):
        author, tid = self._mk_take("poly:vote-self")
        up, down = db_takes.cast_vote(tid, author, 1)
        self.assertEqual(up, 0)
        self.assertEqual(down, 0)

    def test_one_vote_per_user_per_take(self):
        author, tid = self._mk_take("poly:one-vote")
        voter = _make_user("voter_one")
        # Up twice — should end at 1 up, 0 down.
        db_takes.cast_vote(tid, voter, 1)
        up, down = db_takes.cast_vote(tid, voter, 1)
        self.assertEqual(up, 1)
        self.assertEqual(down, 0)


# ── 6. Shadow-hide ─────────────────────────────────────────────────────────


class TestShadowHideNotification(unittest.TestCase):
    """Spec: "Any take with ≥ 3 downvotes AND quality_score < -5 is shadow-
    hidden … Author notified." Tests cover the notification wiring."""

    def _count_notifications_for(self, user_id: int) -> int:
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def test_first_shadow_hide_creates_one_notification(self):
        author = _make_user("shn_author", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:shn-test",
            position="yes", reasoning=_good_reasoning(),
        )
        self.assertEqual(self._count_notifications_for(author), 0)

        for i in range(10):
            voter = _make_user(f"shn_voter_{i}")
            db_takes.cast_vote(tid, voter, -1)

        self.assertTrue(int(db_takes.get_take(tid)["shadow_hidden"]))
        # Exactly one notification even though many votes triggered the
        # recompute — the notification is edge-triggered on the 0→1 flip.
        self.assertEqual(self._count_notifications_for(author), 1)

        with db.conn() as c:
            n = c.execute(
                "SELECT title, link_url, type FROM notifications WHERE user_id = ?",
                (author,),
            ).fetchone()
        self.assertEqual(n["type"], "system")
        self.assertIn("hidden", (n["title"] or "").lower())
        self.assertIn("poly:shn-test", n["link_url"] or "")
        self.assertIn(f"take-{tid}", n["link_url"] or "")

    def test_subsequent_downvotes_while_hidden_dont_renotify(self):
        author = _make_user("shn_dup", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:shn-dup",
            position="yes", reasoning=_good_reasoning(),
        )
        # First batch: tip into shadow.
        for i in range(10):
            db_takes.cast_vote(tid, _make_user(f"shn_dup_v1_{i}"), -1)
        self.assertEqual(self._count_notifications_for(author), 1)
        # Second batch: already hidden → no new notification.
        for i in range(5):
            db_takes.cast_vote(tid, _make_user(f"shn_dup_v2_{i}"), -1)
        self.assertEqual(self._count_notifications_for(author), 1)


class TestShadowHide(unittest.TestCase):
    def test_three_downvotes_and_quality_below_minus_five_hides(self):
        author = _make_user("shauthor", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:shadow-test",
            position="yes", reasoning=_good_reasoning(),
        )
        # Need enough downvotes to push quality below -5 with the default
        # author cred (0.5) → score = net * 0.75. So net must be < -6.66.
        # Use 10 downvoters to make it unambiguous.
        for i in range(10):
            voter = _make_user(f"sh_voter_{i}")
            db_takes.cast_vote(tid, voter, -1)

        row = db_takes.get_take(tid)
        self.assertEqual(int(row["downvotes"]), 10)
        self.assertTrue(int(row["shadow_hidden"]))
        self.assertLess(float(row["quality_score"]), -5.0)

        # Author still sees their own hidden take in list_market_takes.
        seen_by_author = db_takes.list_market_takes(
            "poly:shadow-test", viewer_user_id=author,
        )
        self.assertEqual(len(seen_by_author), 1)
        # Someone else does NOT see it.
        other = _make_user("sh_other")
        seen_by_other = db_takes.list_market_takes(
            "poly:shadow-test", viewer_user_id=other,
        )
        self.assertEqual(len(seen_by_other), 0)


# ── 7. Reporting + moderation queue ────────────────────────────────────────


class TestReporting(unittest.TestCase):
    def test_report_creates_queue_entry(self):
        author = _make_user("rep_author", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:rep-test",
            position="yes", reasoning=_good_reasoning(),
        )
        reporter = _make_user("rep_reporter")
        r = client.post(
            f"/api/v1/takes/{tid}/report",
            json={"reason": "spam", "details": "link dump"},
            headers=_csrf_headers(), cookies=_session_cookies(reporter),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["report_id"])

        # Admin sees it in the queue.
        reports = db_takes.list_open_reports()
        self.assertTrue(any(int(r2["take_id"]) == tid for r2 in reports))

    def test_duplicate_report_is_idempotent(self):
        author = _make_user("rep_dup_auth", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:rep-dup",
            position="yes", reasoning=_good_reasoning(),
        )
        reporter = _make_user("rep_dup")
        r1 = client.post(
            f"/api/v1/takes/{tid}/report",
            json={"reason": "spam"},
            headers=_csrf_headers(), cookies=_session_cookies(reporter),
        )
        r2 = client.post(
            f"/api/v1/takes/{tid}/report",
            json={"reason": "spam"},
            headers=_csrf_headers(), cookies=_session_cookies(reporter),
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Second call returns null report_id because the UNIQUE constraint
        # short-circuits the insert.
        self.assertIsNone(r2.json()["report_id"])

    def test_cannot_report_own_take(self):
        author = _make_user("rep_self", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:rep-self",
            position="yes", reasoning=_good_reasoning(),
        )
        r = client.post(
            f"/api/v1/takes/{tid}/report",
            json={"reason": "spam"},
            headers=_csrf_headers(), cookies=_session_cookies(author),
        )
        self.assertEqual(r.status_code, 400)

    def test_invalid_reason_rejected(self):
        author = _make_user("rep_bad", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:rep-bad",
            position="yes", reasoning=_good_reasoning(),
        )
        reporter = _make_user("rep_bad_reporter")
        r = client.post(
            f"/api/v1/takes/{tid}/report",
            json={"reason": "explosion"},
            headers=_csrf_headers(), cookies=_session_cookies(reporter),
        )
        self.assertEqual(r.status_code, 400)


class TestAdminModeration(unittest.TestCase):
    def _seed(self):
        author = _make_user("mod_auth", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:mod-test",
            position="yes", reasoning=_good_reasoning(),
        )
        reporter = _make_user("mod_reporter")
        rid = db_takes.create_report(
            take_id=tid, reporter_user_id=reporter,
            reason="spam", details="link",
        )
        return author, tid, rid

    def test_non_admin_blocked_from_moderation_page(self):
        user = _make_user("not_admin")
        r = client.get(
            "/admin/moderation",
            cookies=_session_cookies(user),
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_sees_moderation_page(self):
        _author, _tid, _rid = self._seed()
        admin = _make_user("mod_admin", is_admin=True)
        r = client.get(
            "/admin/moderation",
            cookies=_session_cookies(admin),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("Moderation queue", r.text)

    def test_resolve_report_with_delete_also_deletes_take(self):
        _author, tid, rid = self._seed()
        admin = _make_user("mod_admin_act", is_admin=True)
        r = client.post(
            f"/api/v1/admin/reports/{rid}/resolve",
            json={"action": "deleted", "take_id": tid},
            headers=_csrf_headers(), cookies=_session_cookies(admin),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["resolved"])
        self.assertTrue(r.json()["take_deleted"])
        # Take is soft-deleted.
        row = db_takes.get_take(tid, include_deleted=True)
        self.assertEqual(int(row["is_deleted"]), 1)

    def test_resolve_report_dismiss_keeps_take(self):
        _author, tid, rid = self._seed()
        admin = _make_user("mod_admin_dis", is_admin=True)
        r = client.post(
            f"/api/v1/admin/reports/{rid}/resolve",
            json={"action": "dismissed", "take_id": tid},
            headers=_csrf_headers(), cookies=_session_cookies(admin),
        )
        self.assertEqual(r.status_code, 200)
        # Take is NOT deleted.
        row = db_takes.get_take(tid)
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_deleted"]), 0)

    def test_admin_delete_auto_closes_sibling_reports(self):
        author = _make_user("mod_sib_author", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug="poly:mod-siblings",
            position="yes", reasoning=_good_reasoning(),
        )
        # Three different users report the same take.
        for i in range(3):
            reporter = _make_user(f"sibrep_{i}")
            db_takes.create_report(
                take_id=tid, reporter_user_id=reporter, reason="spam",
            )
        self.assertEqual(
            len([r for r in db_takes.list_open_reports() if r["take_id"] == tid]),
            3,
        )
        admin = _make_user("mod_sib_admin", is_admin=True)
        # Pass `json={}` so TestClient sets Content-Type: application/json —
        # the CSRF middleware only reads the x-csrf-token header when the
        # request has a JSON content type. Without a body the header is
        # silently ignored and we'd get a 403.
        r = client.post(
            f"/api/v1/admin/takes/{tid}/delete",
            json={},
            headers=_csrf_headers(), cookies=_session_cookies(admin),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reports_closed"], 3)
        # Queue no longer lists this take's reports.
        self.assertEqual(
            len([r for r in db_takes.list_open_reports() if r["take_id"] == tid]),
            0,
        )


# ── 8. Resolution job ──────────────────────────────────────────────────────


class TestResolutionJob(unittest.TestCase):
    def _seed_resolved_prediction(
        self, market_id: str, direction: str, correct: int,
    ) -> None:
        """Insert one resolved prediction onto a market so the resolver
        can infer the outcome."""
        now = int(time.time())
        src = _uniq("resolver_src")
        with db.conn() as c:
            c.execute(
                "INSERT INTO predictions "
                "(source_handle, market_id, category, direction, content, "
                " extracted_at, resolved, resolved_correct, resolved_at) "
                "VALUES (?, ?, 'test', ?, 'x', ?, 1, ?, ?)",
                (src, market_id, direction, now, correct, now),
            )

    def test_yes_outcome_flips_yes_takes_correct(self):
        slug = "poly:resolve-yes"
        uid_a = _make_user("r_yes_a", paid=True)
        uid_b = _make_user("r_yes_b", paid=True)
        tid_yes = db_takes.create_take(
            user_id=uid_a, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        tid_no = db_takes.create_take(
            user_id=uid_b, market_slug=slug, position="no",
            reasoning=_good_reasoning(),
        )
        # Mark the market resolved YES via an oracle prediction.
        self._seed_resolved_prediction(slug, direction="YES", correct=1)

        import asyncio
        from jobs.take_resolution_jobs import resolve_takes_for_finished_markets
        result = asyncio.run(resolve_takes_for_finished_markets())

        self.assertEqual(result["takes_resolved"], 2)
        self.assertEqual(result["takes_correct"], 1)
        self.assertEqual(result["takes_incorrect"], 1)

        self.assertEqual(int(db_takes.get_take(tid_yes)["resolved_correct"]), 1)
        self.assertEqual(int(db_takes.get_take(tid_no)["resolved_correct"]), 0)

    def test_quality_score_applies_correctness_multiplier(self):
        slug = "poly:resolve-q"
        author = _make_user("r_q_author", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        # Two upvotes to give a positive baseline score.
        for i in range(2):
            voter = _make_user(f"r_q_voter_{i}")
            db_takes.cast_vote(tid, voter, 1)
        pre_q = float(db_takes.get_take(tid)["quality_score"])
        self.assertGreater(pre_q, 0)

        self._seed_resolved_prediction(slug, direction="YES", correct=1)
        import asyncio
        from jobs.take_resolution_jobs import resolve_takes_for_finished_markets
        asyncio.run(resolve_takes_for_finished_markets())

        post_q = float(db_takes.get_take(tid)["quality_score"])
        # 1.2× multiplier for correct resolution.
        self.assertAlmostEqual(post_q, pre_q * 1.2, places=3)

    def test_ambiguous_market_skipped(self):
        # Predictions both YES-correct AND NO-correct → outcome ambiguous
        # → resolver leaves takes untouched.
        slug = "poly:resolve-ambig"
        author = _make_user("r_ambig", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        self._seed_resolved_prediction(slug, direction="YES", correct=1)
        self._seed_resolved_prediction(slug, direction="YES", correct=0)

        import asyncio
        from jobs.take_resolution_jobs import resolve_takes_for_finished_markets
        asyncio.run(resolve_takes_for_finished_markets())

        # Take still un-scored.
        self.assertIsNone(db_takes.get_take(tid)["resolved_correct"])

    def test_neutral_position_stays_unresolved(self):
        slug = "poly:resolve-neutral"
        author = _make_user("r_neutral", paid=True)
        tid = db_takes.create_take(
            user_id=author, market_slug=slug, position="neutral",
            reasoning=_good_reasoning(),
        )
        self._seed_resolved_prediction(slug, direction="YES", correct=1)
        import asyncio
        from jobs.take_resolution_jobs import resolve_takes_for_finished_markets
        asyncio.run(resolve_takes_for_finished_markets())
        self.assertIsNone(db_takes.get_take(tid)["resolved_correct"])


# ── 9. Quality score formula ───────────────────────────────────────────────


class TestQualityScore(unittest.TestCase):
    def _take_row(self, **overrides):
        """Build a dict that walks like a sqlite3.Row for compute_quality_score."""
        base = {
            "user_id": 1,
            "upvotes": 0,
            "downvotes": 0,
            "resolved_correct": None,
        }
        base.update(overrides)

        class _Row:
            def __init__(self, data):
                self._data = data
            def __getitem__(self, k):
                return self._data[k]
            def keys(self):
                return self._data.keys()

        return _Row(base)

    def test_net_votes_with_default_cred(self):
        row = self._take_row(upvotes=10, downvotes=2)
        q = db_takes.compute_quality_score(row, author_cred=0.5)
        # net=8, multiplier=0.5+0.25=0.75 → 6.0
        self.assertAlmostEqual(q, 6.0, places=3)

    def test_high_cred_amplifies_score(self):
        row = self._take_row(upvotes=10, downvotes=2)
        q_low = db_takes.compute_quality_score(row, author_cred=0.1)
        q_hi = db_takes.compute_quality_score(row, author_cred=0.9)
        self.assertGreater(q_hi, q_low)

    def test_correct_multiplier_amplifies(self):
        row = self._take_row(upvotes=10, downvotes=2, resolved_correct=1)
        q = db_takes.compute_quality_score(row, author_cred=0.5)
        self.assertAlmostEqual(q, 6.0 * 1.2, places=3)

    def test_incorrect_multiplier_dampens(self):
        row = self._take_row(upvotes=10, downvotes=2, resolved_correct=0)
        q = db_takes.compute_quality_score(row, author_cred=0.5)
        self.assertAlmostEqual(q, 6.0 * 0.7, places=3)


# ── 10. Listing: sort + filter + shadow-hide ───────────────────────────────


class TestListing(unittest.TestCase):
    def test_list_filters_by_position(self):
        slug = "poly:list-filter"
        u1 = _make_user("lf_u1", paid=True)
        u2 = _make_user("lf_u2", paid=True)
        u3 = _make_user("lf_u3", paid=True)
        db_takes.create_take(user_id=u1, market_slug=slug, position="yes",
                             reasoning=_good_reasoning())
        db_takes.create_take(user_id=u2, market_slug=slug, position="no",
                             reasoning=_good_reasoning())
        db_takes.create_take(user_id=u3, market_slug=slug, position="neutral",
                             reasoning=_good_reasoning())

        r = client.get(f"/api/v1/markets/{slug}/takes?position=yes")
        self.assertEqual(r.status_code, 200)
        out = r.json()["takes"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["position"], "yes")

    def test_list_sorts_by_newest(self):
        slug = "poly:list-newest"
        u1 = _make_user("ln_u1", paid=True)
        u2 = _make_user("ln_u2", paid=True)
        tid_old = db_takes.create_take(
            user_id=u1, market_slug=slug, position="yes",
            reasoning=_good_reasoning(),
        )
        tid_new = db_takes.create_take(
            user_id=u2, market_slug=slug, position="no",
            reasoning=_good_reasoning(),
        )
        # Create and re-post time resolution is 1s; backdate the first
        # take so the ORDER BY is unambiguous (both otherwise land in
        # the same second).
        with db.conn() as c:
            c.execute(
                "UPDATE market_takes SET created_at = created_at - 60 WHERE id = ?",
                (tid_old,),
            )
        r = client.get(f"/api/v1/markets/{slug}/takes?sort=newest")
        ids = [t["id"] for t in r.json()["takes"]]
        self.assertEqual(ids[0], tid_new)
        self.assertEqual(ids[1], tid_old)


# ── 11. Blended credibility + public profile ─────────────────────────────


class TestBlendedCredibility(unittest.TestCase):
    def _resolve_take(self, tid: int, correct: int) -> None:
        with db.conn() as c:
            c.execute(
                "UPDATE market_takes SET resolved_correct = ? WHERE id = ?",
                (correct, tid),
            )

    def test_no_resolved_takes_returns_base_credibility(self):
        uid = _make_user("bc_nothing", paid=True)
        # No user_accuracy row yet → base defaults to 0.5.
        self.assertAlmostEqual(db_takes.get_blended_credibility(uid), 0.5, places=3)
        self.assertIsNone(db_takes.get_user_take_accuracy(uid))

    def test_perfect_take_record_nudges_up(self):
        uid = _make_user("bc_perfect", paid=True)
        # Author has a middling global accuracy.
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_accuracy "
                "(user_id, accuracy_score, total_predictions, correct_predictions, "
                " last_computed_at) VALUES (?, 0.50, 10, 5, ?)",
                (uid, int(time.time())),
            )
        # Three correct takes, zero wrong.
        for i in range(3):
            tid = db_takes.create_take(
                user_id=uid, market_slug=f"poly:bc-perfect-{i}",
                position="yes", reasoning=_good_reasoning(),
            )
            self._resolve_take(tid, 1)

        self.assertEqual(db_takes.get_user_take_accuracy(uid), 1.0)
        blended = db_takes.get_blended_credibility(uid)
        # 0.85·0.5 + 0.15·1.0 = 0.575 — a small nudge above the base 0.5.
        self.assertAlmostEqual(blended, 0.575, places=3)
        self.assertGreater(blended, 0.5)
        self.assertLess(blended, 0.6, "nudge must stay SMALL, not dominant")

    def test_wrong_take_record_nudges_down(self):
        uid = _make_user("bc_wrong", paid=True)
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_accuracy "
                "(user_id, accuracy_score, total_predictions, correct_predictions, "
                " last_computed_at) VALUES (?, 0.80, 10, 8, ?)",
                (uid, int(time.time())),
            )
        for i in range(4):
            tid = db_takes.create_take(
                user_id=uid, market_slug=f"poly:bc-wrong-{i}",
                position="yes", reasoning=_good_reasoning(),
            )
            self._resolve_take(tid, 0)

        self.assertEqual(db_takes.get_user_take_accuracy(uid), 0.0)
        # 0.85·0.8 + 0.15·0.0 = 0.68 — a small dip below the base 0.8.
        self.assertAlmostEqual(db_takes.get_blended_credibility(uid), 0.68, places=3)

    def test_neutral_takes_dont_count_toward_accuracy(self):
        uid = _make_user("bc_neutral", paid=True)
        # Neutral takes stay unresolved (resolved_correct IS NULL), so they
        # shouldn't pull the denominator in either direction.
        for i in range(5):
            db_takes.create_take(
                user_id=uid, market_slug=f"poly:bc-neutral-{i}",
                position="neutral", reasoning=_good_reasoning(),
            )
        self.assertIsNone(db_takes.get_user_take_accuracy(uid))


class TestPublicProfile(unittest.TestCase):
    def _opt_in(self, uid: int) -> None:
        with db.conn() as c:
            c.execute(
                "UPDATE users SET leaderboard_participation = 1 WHERE id = ?",
                (uid,),
            )

    def test_profile_404_if_not_opted_in(self):
        uid = _make_user("prof_priv", paid=True)
        db_takes.create_take(
            user_id=uid, market_slug="poly:prof-priv",
            position="yes", reasoning=_good_reasoning(),
        )
        r = client.get(f"/u/{uid}/takes")
        self.assertEqual(r.status_code, 404)

    def test_profile_renders_when_opted_in(self):
        uid = _make_user("prof_pub", paid=True)
        self._opt_in(uid)
        tid = db_takes.create_take(
            user_id=uid, market_slug="poly:prof-pub",
            position="yes", reasoning=_good_reasoning(),
        )
        # Get a non-null quality score so the take shows up in best_takes.
        other = _make_user("prof_pub_voter")
        db_takes.cast_vote(tid, other, 1)
        r = client.get(f"/u/{uid}/takes")
        self.assertEqual(r.status_code, 200, r.text)
        # Handle + market slug + reasoning snippet all present.
        self.assertIn("Best takes", r.text)
        self.assertIn("poly:prof-pub", r.text)

    def test_profile_404_for_unknown_user(self):
        r = client.get("/u/999999/takes")
        self.assertEqual(r.status_code, 404)

    def test_best_takes_filter_excludes_shadow_hidden(self):
        uid = _make_user("prof_shadow", paid=True)
        self._opt_in(uid)
        tid = db_takes.create_take(
            user_id=uid, market_slug="poly:prof-shadow",
            position="yes", reasoning=_good_reasoning(),
        )
        # Push the take into shadow-hidden state with 10 downvotes.
        for i in range(10):
            voter = _make_user(f"prof_shadow_v_{i}")
            db_takes.cast_vote(tid, voter, -1)
        self.assertTrue(int(db_takes.get_take(tid)["shadow_hidden"]))

        best = db_takes.list_user_best_takes(uid)
        self.assertEqual(len(best), 0, "shadow-hidden takes must not leak to profile")

    def test_best_takes_requires_non_null_quality(self):
        uid = _make_user("prof_noscore", paid=True)
        db_takes.create_take(
            user_id=uid, market_slug="poly:prof-noscore",
            position="yes", reasoning=_good_reasoning(),
        )
        # Brand-new take, nobody voted → quality_score is NULL.
        best = db_takes.list_user_best_takes(uid)
        self.assertEqual(len(best), 0)


if __name__ == "__main__":
    unittest.main()
