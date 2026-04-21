"""Tests for engagement tracking + churn detection + cancel flow + admin churn.

Covers:
  * TestEngagementLog:       fire-and-forget event write hits the DB.
  * TestChurnSignalJob:      every rule in the risk formula, tier buckets.
  * TestPromptEndpoint:      /api/engagement/prompt returns right payload.
  * TestPromptDismissal:     POST dismiss hides banner for 7 days.
  * TestCancelFlow3Step:     step 1 → step 2 → step 3 path writes
                             cancellation_attempts + subscriptions.
  * TestPauseFlow:           POST pause sets subscription_paused_until
                             and dashboards shows the pause screen.
  * TestAdminChurn:          GET /admin/churn renders the sections.

Uses the shared in-memory DB via tests._testdb. All DB access goes through
db.conn() pinned to that connection, so sibling test files that swap
db.conn do not leak into us (USES_TESTDB marker + manual re-pin in setUp).
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Opt into the conftest's shared in-memory DB.
USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import server  # noqa: E402
import engagement  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jobs.compute_churn_signals import compute_churn_signals_sync  # noqa: E402


client = TestClient(server.app)

# Re-pin db.conn to the shared fake so sibling test files can't poison us.
_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client()
        # Purge per-test rows in the three feature tables so test
        # ordering doesn't matter. The shared DB is module-scoped.
        with db.conn() as c:
            c.execute("DELETE FROM engagement_events")
            c.execute("DELETE FROM engagement_prompt_dismissals")
            c.execute("DELETE FROM churn_signals")
            c.execute("DELETE FROM cancellation_attempts")
            c.execute("DELETE FROM subscription_pauses")
            c.execute("UPDATE users SET subscription_paused_until = NULL")
        super().setUp()


def _make_user(email: str, username: str, *, plan: str = "pro", interval: str = "annual", days_left: int = 300) -> tuple[int, str]:
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions "
            "(user_id, dashboard_key, plan, status, started_at, expires_at) "
            "VALUES (?, '__plan__', ?, 'active', ?, ?)",
            (uid, f"{plan}_{interval}", now, now + days_left * 86400),
        )
    token = db.create_session(uid)
    return uid, token


def _csrf_token(session_token: str) -> str:
    """Prime + read the _csrf cookie for POSTs."""
    client.get("/settings/billing", cookies={server.COOKIE_NAME: session_token}, follow_redirects=False)
    return client.cookies.get("_csrf") or ""


def _post_form(path: str, token: str, data: dict | None = None):
    csrf = _csrf_token(token)
    payload = dict(data or {})
    if csrf:
        payload["_csrf"] = csrf
    return client.post(
        path,
        data=payload,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        follow_redirects=False,
    )


def _post_json(path: str, token: str, body: dict):
    csrf = _csrf_token(token)
    return client.post(
        path,
        json=body,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )


def _raw_insert_event(c, uid: int, etype: str, days_ago: int) -> None:
    """Insert an event with a back-dated created_at so we can exercise
    the 7d / 14d / 30d windows without actually sleeping."""
    c.execute(
        "INSERT INTO engagement_events (user_id, event_type, created_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (uid, etype, f"-{int(days_ago)} days"),
    )


# ── Engagement log ───────────────────────────────────────────────────────────


class TestEngagementLog(_Base):
    def test_log_event_writes_row(self):
        uid, _ = _make_user("eng-log@t.com", "eng_log")
        engagement.log_event(uid, "save", metadata={"prediction_id": 1})
        engagement._reset_for_tests()
        with db.conn() as c:
            rows = c.execute(
                "SELECT event_type, metadata FROM engagement_events WHERE user_id = ?",
                (uid,),
            ).fetchall()
        # log-event from create_session already fired a 'login' row in
        # _make_user, so we expect at least the 'save' row we just wrote.
        event_types = {r["event_type"] for r in rows}
        self.assertIn("save", event_types)
        save_row = [r for r in rows if r["event_type"] == "save"][0]
        self.assertEqual(json.loads(save_row["metadata"])["prediction_id"], 1)

    def test_log_event_drops_on_bad_input(self):
        # None / empty args are no-op, don't raise.
        engagement.log_event(None, "save")
        engagement.log_event(5, "")
        engagement._reset_for_tests()

    def test_log_event_survives_non_serializable_metadata(self):
        uid, _ = _make_user("eng-bad-meta@t.com", "eng_bad_meta")
        # `object()` isn't natively JSON-serializable but json.dumps
        # coerces via default=str. Either way, we must write the row —
        # never raise an exception up into the request handler.
        engagement.log_event(uid, "save", metadata={"blob": object()})
        engagement._reset_for_tests()
        with db.conn() as c:
            rows = c.execute(
                "SELECT event_type, metadata FROM engagement_events "
                "WHERE user_id = ? AND event_type = 'save'",
                (uid,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        # metadata is either None (pure-TypeError path) or a coerced
        # string — both are acceptable. The critical property is that
        # the row was written without raising.

    def test_create_session_emits_login(self):
        uid = db.create_user("eng-sess@t.com", "TestPass123!", username="eng_sess")
        token = db.create_session(uid)
        engagement._reset_for_tests()
        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM engagement_events WHERE user_id = ? AND event_type = 'login'",
                (uid,),
            ).fetchone()["n"]
        self.assertGreaterEqual(n, 1, "create_session should log a 'login' event")


# ── Churn signal job ─────────────────────────────────────────────────────────


class TestChurnSignalJob(_Base):
    def test_healthy_user_with_recent_predictions(self):
        uid, _ = _make_user("healthy@t.com", "healthy")
        with db.conn() as c:
            for d in range(6):
                _raw_insert_event(c, uid, "prediction_made", d)
        engagement._reset_for_tests()
        result = compute_churn_signals_sync()
        self.assertGreaterEqual(result["total"], 1)
        with db.conn() as c:
            row = c.execute("SELECT * FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()
        self.assertEqual(row["risk_tier"], "healthy")
        self.assertLess(row["risk_score"], 0.3)
        self.assertEqual(row["engagement_trend"], "rising")  # prior_7d=0, recent_7d>0

    def test_at_risk_user_declining_engagement(self):
        uid, _ = _make_user("at-risk@t.com", "at_risk_u")
        with db.conn() as c:
            # Heavy activity 8-14 days ago, almost none in last 7
            for d in range(8, 15):
                for _ in range(5):
                    _raw_insert_event(c, uid, "feed_view", d)
            _raw_insert_event(c, uid, "feed_view", 2)  # last_active ~2d ago
        engagement._reset_for_tests()
        compute_churn_signals_sync()
        with db.conn() as c:
            row = c.execute("SELECT * FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()
        # Rules that fire: no prediction_made in 30d (+0.2),
        # recent_7d (1) < 0.3 * prior_7d (35) (+0.3).
        # last_active is within 7d so the staleness bumps don't fire.
        # Expected score ~0.5.
        self.assertGreaterEqual(row["risk_score"], 0.3)
        self.assertEqual(row["risk_tier"], "at_risk")
        self.assertEqual(row["engagement_trend"], "declining")

    def test_critical_user_fully_dormant(self):
        uid, _ = _make_user("critical@t.com", "critical_u")
        # _make_user calls create_session which emits a 'login' event —
        # purge it so the user truly has zero events in the 30d window,
        # exercising the fully-dormant rule path (+0.8 floor).
        with db.conn() as c:
            c.execute("DELETE FROM engagement_events WHERE user_id = ?", (uid,))
        compute_churn_signals_sync()
        with db.conn() as c:
            row = c.execute("SELECT * FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["risk_score"], 0.7)
        self.assertEqual(row["risk_tier"], "critical")
        self.assertEqual(row["engagement_trend"], "dormant")

    def test_active_user_bump_reduces_score(self):
        uid, _ = _make_user("active-week@t.com", "active_w")
        with db.conn() as c:
            # Heavy prior-7d activity but ALSO a prediction in the last 7d.
            # Without the active_7d bump, declining rule + stale bump would
            # put this user in at_risk. The -0.2 should pull it back to healthy.
            for d in range(8, 15):
                _raw_insert_event(c, uid, "feed_view", d)
            _raw_insert_event(c, uid, "prediction_made", 2)
        compute_churn_signals_sync()
        with db.conn() as c:
            row = c.execute("SELECT * FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()
        self.assertLess(row["risk_score"], 0.3)
        self.assertEqual(row["risk_tier"], "healthy")

    def test_job_upserts_idempotently(self):
        uid, _ = _make_user("idem@t.com", "idem_u")
        compute_churn_signals_sync()
        compute_churn_signals_sync()
        compute_churn_signals_sync()
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_job_skips_non_subscribers(self):
        # User with no __plan__ sub shouldn't show up.
        uid = db.create_user("free@t.com", "TestPass123!", username="free_u")
        compute_churn_signals_sync()
        with db.conn() as c:
            row = c.execute("SELECT * FROM churn_signals WHERE user_id = ?", (uid,)).fetchone()
        self.assertIsNone(row)


# ── /api/engagement/prompt ──────────────────────────────────────────────────


class TestPromptEndpoint(_Base):
    def test_unauth_returns_null_not_401(self):
        r = client.get("/api/engagement/prompt")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["prompt"])

    def test_healthy_user_gets_null(self):
        uid, token = _make_user("prompt-healthy@t.com", "prompt_h")
        with db.conn() as c:
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score) VALUES (?, 'healthy', 0.1)",
                (uid,),
            )
        r = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["prompt"])

    def test_at_risk_user_gets_suggestion_prompt(self):
        uid, token = _make_user("prompt-ar@t.com", "prompt_ar")
        with db.conn() as c:
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score) VALUES (?, 'at_risk', 0.5)",
                (uid,),
            )
        r = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        body = r.json()
        self.assertIsNotNone(body["prompt"])
        self.assertEqual(body["prompt"]["type"], "suggestion")
        self.assertEqual(body["prompt"]["tier"], "at_risk")
        self.assertTrue(body["prompt"]["cta_url"].startswith("/"))

    def test_critical_user_gets_winback_prompt(self):
        uid, token = _make_user("prompt-crit@t.com", "prompt_crit")
        with db.conn() as c:
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score) VALUES (?, 'critical', 0.9)",
                (uid,),
            )
        r = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        body = r.json()
        self.assertEqual(body["prompt"]["type"], "win_back")
        self.assertEqual(body["prompt"]["tier"], "critical")

    def test_stale_signal_returns_null(self):
        uid, token = _make_user("prompt-stale@t.com", "prompt_stale")
        with db.conn() as c:
            # Back-date computed_at by 30 days.
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score, computed_at) "
                "VALUES (?, 'at_risk', 0.5, datetime('now', '-30 days'))",
                (uid,),
            )
        r = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        self.assertIsNone(r.json()["prompt"])


class TestPromptDismissal(_Base):
    def test_dismiss_hides_prompt(self):
        uid, token = _make_user("dismiss@t.com", "dismiss_u")
        with db.conn() as c:
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score) VALUES (?, 'at_risk', 0.5)",
                (uid,),
            )
        # First request: prompt returned.
        r1 = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        self.assertIsNotNone(r1.json()["prompt"])
        # Dismiss it.
        r2 = _post_json("/api/engagement/prompt/dismiss", token, {"tier": "at_risk"})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["ok"])
        # Second request: prompt suppressed.
        r3 = client.get("/api/engagement/prompt", cookies={server.COOKIE_NAME: token})
        self.assertIsNone(r3.json()["prompt"])

    def test_dismiss_invalid_tier_rejected(self):
        _, token = _make_user("dismiss-bad@t.com", "dismiss_bad")
        r = _post_json("/api/engagement/prompt/dismiss", token, {"tier": "nonsense"})
        self.assertEqual(r.status_code, 400)

    def test_dismiss_unauth_returns_401(self):
        # Without auth we can't prime the CSRF cookie, so just verify
        # the endpoint rejects unauthenticated callers.
        r = client.post(
            "/api/engagement/prompt/dismiss",
            json={"tier": "at_risk"},
            follow_redirects=False,
        )
        # CSRF middleware may 403 first; either rejection is acceptable.
        self.assertIn(r.status_code, (401, 403))


# ── 3-step cancel flow ──────────────────────────────────────────────────────


class TestCancelFlow3Step(_Base):
    def test_step1_records_attempt_and_redirects_to_step2(self):
        uid, token = _make_user("cancel1@t.com", "cancel1")
        r = _post_form("/settings/billing/cancel", token, {"step": "1", "reason": "too_expensive"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/settings/billing/cancel-flow?step=2", r.headers["location"])
        with db.conn() as c:
            row = c.execute(
                "SELECT reason, reached_step, outcome FROM cancellation_attempts WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["reason"], "too_expensive")
        self.assertEqual(row["reached_step"], 1)
        self.assertIsNone(row["outcome"])

    def test_step3_finalizes_cancel_and_flips_subs(self):
        uid, token = _make_user("cancel3@t.com", "cancel3")
        # Step 1 to open the attempt.
        r1 = _post_form("/settings/billing/cancel", token, {"step": "1"})
        loc = r1.headers["location"]
        attempt_id = int(loc.split("attempt_id=")[-1])
        # Step 3 to finalize.
        r2 = _post_form("/settings/billing/cancel", token, {"step": "3", "attempt_id": str(attempt_id)})
        self.assertEqual(r2.status_code, 302)
        self.assertIn("saved=cancelled", r2.headers["location"])
        with db.conn() as c:
            sub = c.execute(
                "SELECT status FROM subscriptions WHERE user_id = ? AND dashboard_key = '__plan__'",
                (uid,),
            ).fetchone()
            attempt = c.execute(
                "SELECT outcome, reached_step FROM cancellation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(sub["status"], "cancelled")
        self.assertEqual(attempt["outcome"], "cancelled")
        self.assertEqual(attempt["reached_step"], 3)

    def test_cancel_flow_page_renders_step1_by_default(self):
        _, token = _make_user("cf-page@t.com", "cf_page")
        r = client.get("/settings/billing/cancel-flow", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Before you cancel", r.text)
        self.assertIn("Keep my subscription", r.text)

    def test_cancel_flow_page_renders_step2_with_attempt_id(self):
        _, token = _make_user("cf-step2@t.com", "cf_step2")
        # Prime an attempt id via step 1.
        r = _post_form("/settings/billing/cancel", token, {"step": "1"})
        attempt_id = int(r.headers["location"].split("attempt_id=")[-1])
        r2 = client.get(
            f"/settings/billing/cancel-flow?step=2&attempt_id={attempt_id}",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Pause for 30 days", r2.text)
        self.assertIn("Pause for 60 days", r2.text)


# ── Pause ────────────────────────────────────────────────────────────────────


class TestPauseFlow(_Base):
    def test_pause_sets_expiry_and_finalizes_attempt(self):
        uid, token = _make_user("pause1@t.com", "pause1")
        r1 = _post_form("/settings/billing/cancel", token, {"step": "1"})
        attempt_id = int(r1.headers["location"].split("attempt_id=")[-1])
        r2 = _post_form("/settings/billing/pause", token, {"days": "30", "attempt_id": str(attempt_id)})
        self.assertEqual(r2.status_code, 302)
        self.assertIn("saved=paused", r2.headers["location"])
        with db.conn() as c:
            user_row = c.execute(
                "SELECT subscription_paused_until FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
            attempt = c.execute(
                "SELECT outcome, pause_days FROM cancellation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            pause_row = c.execute(
                "SELECT resume_at FROM subscription_pauses WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertIsNotNone(user_row["subscription_paused_until"])
        self.assertEqual(attempt["outcome"], "paused")
        self.assertEqual(attempt["pause_days"], 30)
        self.assertIsNotNone(pause_row)

    def test_paused_user_hits_pause_screen_on_dashboards(self):
        uid, token = _make_user("pause-dash@t.com", "pause_dash")
        future_ts = int(time.time()) + 30 * 86400
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subscription_paused_until = datetime(?, 'unixepoch') WHERE id = ?",
                (future_ts, uid),
            )
        r = client.get("/dashboards", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Subscription paused", r.text)
        self.assertIn("Resume now", r.text)

    def test_resume_clears_pause(self):
        uid, token = _make_user("resume@t.com", "resume_u")
        future_ts = int(time.time()) + 30 * 86400
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subscription_paused_until = datetime(?, 'unixepoch') WHERE id = ?",
                (future_ts, uid),
            )
            c.execute(
                "INSERT INTO subscription_pauses (user_id, resume_at) VALUES (?, datetime(?, 'unixepoch'))",
                (uid, future_ts),
            )
        r = _post_form("/settings/billing/resume", token)
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=resumed", r.headers["location"])
        with db.conn() as c:
            user_row = c.execute(
                "SELECT subscription_paused_until FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
            pause_row = c.execute(
                "SELECT resumed_early_at FROM subscription_pauses WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertIsNone(user_row["subscription_paused_until"])
        self.assertIsNotNone(pause_row["resumed_early_at"])

    def test_expired_pause_auto_clears_on_dashboard_hit(self):
        uid, token = _make_user("expired-pause@t.com", "exp_pause")
        past_ts = int(time.time()) - 86400  # yesterday
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subscription_paused_until = datetime(?, 'unixepoch') WHERE id = ?",
                (past_ts, uid),
            )
        r = client.get("/dashboards", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertNotIn("Subscription paused", r.text)
        with db.conn() as c:
            user_row = c.execute(
                "SELECT subscription_paused_until FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertIsNone(user_row["subscription_paused_until"])


# ── Admin /admin/churn ──────────────────────────────────────────────────────


class TestAdminChurn(_Base):
    def test_admin_churn_handler_renders_with_seed_data(self):
        """Invoke the handler directly, bypassing the 2FA gate. This
        verifies the HTML assembly + DB queries; the auth gate is
        exercised separately in test_non_admin_blocked / test_unauth."""
        from admin_routes import churn_dashboard
        import asyncio
        # Seed one of each shape so every section has content.
        admin_uid = db.create_user("admin-churn@t.com", "TestPass123!", username="admin_churn")
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin_uid,))
            c.execute(
                "INSERT INTO churn_signals (user_id, risk_tier, risk_score, engagement_trend, days_since_last_active) "
                "VALUES (?, 'at_risk', 0.5, 'declining', 8)",
                (admin_uid,),
            )
            c.execute(
                "INSERT INTO cancellation_attempts (user_id, reason, reached_step, outcome, completed_at) "
                "VALUES (?, 'too_expensive', 3, 'cancelled', CURRENT_TIMESTAMP)",
                (admin_uid,),
            )
        # Bypass the auth gate by monkey-patching _require_admin_user.
        import admin_routes as _ar
        original = _ar._require_admin_user
        admin_dict = {
            "user_id": admin_uid,
            "email": "admin-churn@t.com",
            "username": "admin_churn",
            "is_admin": True,
        }
        _ar._require_admin_user = lambda request, page=False: admin_dict
        try:
            from starlette.requests import Request as StarletteRequest
            scope = {
                "type": "http", "method": "GET", "path": "/admin/churn",
                "headers": [], "query_string": b"",
            }
            req = StarletteRequest(scope)
            resp = asyncio.run(churn_dashboard(req))
            body = resp.body.decode()
            self.assertIn("Risk distribution", body)
            self.assertIn("Cancellation funnel", body)
            self.assertIn("Top 20 at-risk users", body)
            self.assertIn("at_risk", body)
            self.assertIn("cancelled", body)
        finally:
            _ar._require_admin_user = original

    def test_non_admin_blocked(self):
        _, token = _make_user("not-admin@t.com", "not_admin")
        r = client.get("/admin/churn", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        # Admin gate returns 403, redirects to 2FA, or renders the 403
        # template (200 but without the churn content).
        if r.status_code == 200:
            self.assertNotIn("Risk distribution", r.text)
        else:
            self.assertIn(r.status_code, (302, 303, 403, 404))


if __name__ == "__main__":
    unittest.main()
