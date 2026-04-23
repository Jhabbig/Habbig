"""Follow-up tests for the public API polish pass (C + D + B bridge).

Covers:
  - X-RateLimit-* headers appear on 2xx responses, not just on 429
  - GET /api/public/v1/predictions/{id}
      * owner can fetch their own prediction
      * non-owner sees 404 for a private row (we never leak existence)
      * non-owner sees the row when is_public=1
      * is_anonymous scrubs user_id from non-owner response
  - realtime.hub.register_after_broadcast is exposed + fires on broadcast
  - webhook_disabled template renders through EmailService.send_template
    under EMAIL_DRY_RUN=true (no network)
"""

from __future__ import annotations

import asyncio
import os
import unittest

from tests import _testdb  # noqa: F401

# Non-production so the subproduct middleware doesn't demand CF headers.
os.environ["PRODUCTION"] = "0"
os.environ.setdefault("EMAIL_DRY_RUN", "true")

import db
import api_v1


def _mk_user(email: str) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0])


def _mint_key(user_id: int, *, scopes: str = "read,write", rate_limit: int = 100):
    raw, key_id = api_v1.create_api_key(user_id=user_id, name="t", tier="standard")
    with db.conn() as c:
        c.execute(
            "UPDATE api_keys SET scopes = ?, rate_limit_hour = ? WHERE id = ?",
            (scopes, rate_limit, key_id),
        )
    return raw, key_id


def _client():
    import server
    from fastapi.testclient import TestClient
    return TestClient(server.app)


_HOST = {"host": "narve.ai"}


# ── C: rate-limit headers + GET /predictions/{id} ─────────────────────


class TestRateLimitHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"rlh_{id(cls)}@t.com")
        cls.raw, _ = _mint_key(cls.uid, rate_limit=50)
        cls.c = _client()

    def test_headers_present_on_200(self):
        r = self.c.get(
            "/api/public/v1/sources",
            headers={**_HOST, "authorization": f"Bearer {self.raw}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("X-RateLimit-Limit", r.headers)
        self.assertIn("X-RateLimit-Remaining", r.headers)
        self.assertIn("X-RateLimit-Reset", r.headers)

    def test_remaining_decrements(self):
        hdr = {**_HOST, "authorization": f"Bearer {self.raw}"}
        first = self.c.get("/api/public/v1/usage", headers=hdr)
        second = self.c.get("/api/public/v1/usage", headers=hdr)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertGreater(
            int(first.headers["X-RateLimit-Remaining"]),
            int(second.headers["X-RateLimit-Remaining"]),
        )


class TestGetPrediction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = _mk_user(f"own_{id(cls)}@t.com")
        cls.other = _mk_user(f"oth_{id(cls)}@t.com")
        cls.owner_key, _ = _mint_key(cls.owner)
        cls.other_key, _ = _mint_key(cls.other)
        cls.c = _client()

        import queries.predictions as qp
        cls.private_id = qp.create_user_prediction(
            user_id=cls.owner, market_slug="x", market_question="Q?",
            category="other", predicted_outcome="YES", predicted_probability=0.7,
            reasoning=None, market_price_at_prediction=None,
            is_public=False, is_anonymous=False,
        )
        cls.public_id = qp.create_user_prediction(
            user_id=cls.owner, market_slug="y", market_question="Q2?",
            category="other", predicted_outcome="NO", predicted_probability=0.3,
            reasoning=None, market_price_at_prediction=None,
            is_public=True, is_anonymous=False,
        )
        cls.anon_public_id = qp.create_user_prediction(
            user_id=cls.owner, market_slug="z", market_question="Q3?",
            category="other", predicted_outcome="YES", predicted_probability=0.55,
            reasoning=None, market_price_at_prediction=None,
            is_public=True, is_anonymous=True,
        )

    def _hdr(self, raw):
        return {**_HOST, "authorization": f"Bearer {raw}"}

    def test_owner_sees_private_prediction(self):
        r = self.c.get(f"/api/public/v1/predictions/{self.private_id}",
                       headers=self._hdr(self.owner_key))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["is_owner"])
        self.assertEqual(body["prediction"]["id"], self.private_id)

    def test_non_owner_404s_on_private(self):
        r = self.c.get(f"/api/public/v1/predictions/{self.private_id}",
                       headers=self._hdr(self.other_key))
        self.assertEqual(r.status_code, 404)

    def test_non_owner_sees_public_prediction(self):
        r = self.c.get(f"/api/public/v1/predictions/{self.public_id}",
                       headers=self._hdr(self.other_key))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["is_owner"])
        self.assertEqual(body["prediction"]["id"], self.public_id)

    def test_anonymous_public_hides_user_id_from_non_owner(self):
        r = self.c.get(f"/api/public/v1/predictions/{self.anon_public_id}",
                       headers=self._hdr(self.other_key))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["prediction"]["user_id"])

    def test_anonymous_public_shows_user_id_to_owner(self):
        r = self.c.get(f"/api/public/v1/predictions/{self.anon_public_id}",
                       headers=self._hdr(self.owner_key))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prediction"]["user_id"], self.owner)


# ── B: hub after-broadcast bridge ──────────────────────────────────────


class TestHubBridge(unittest.TestCase):
    def test_register_after_broadcast_exists(self):
        from realtime.hub import hub
        self.assertTrue(hasattr(hub, "register_after_broadcast"))

    def test_callback_fires_on_broadcast(self):
        from realtime.hub import hub
        received: list = []

        def sink(channel, message):
            received.append((channel, message))

        hub.register_after_broadcast(sink)
        try:
            # Zero subscribers is fine — bridge must still fire.
            asyncio.run(hub.broadcast("best_bets", {"hello": "world"}))
        finally:
            # De-register so we don't leak into other tests.
            hub._after_broadcast.remove(sink)

        self.assertEqual(len(received), 1)
        ch, msg = received[0]
        self.assertEqual(ch, "best_bets")
        self.assertEqual(msg["hello"], "world")

    def test_coroutine_callback_is_scheduled(self):
        """Registering a coroutine-returning callback must not crash the
        broadcast path. We drive the whole loop inline so there are no
        leftover system tasks to drain."""
        from realtime.hub import hub
        fired: list = []

        async def async_sink(channel, message):
            fired.append((channel, message))

        hub.register_after_broadcast(async_sink)
        try:
            async def _drive():
                await hub.broadcast("market_resolutions", {"m": 1})
                # Yield once so the scheduled task we just created gets to run.
                await asyncio.sleep(0)
            asyncio.run(_drive())
        finally:
            hub._after_broadcast.remove(async_sink)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "market_resolutions")


# ── D: webhook_disabled template renders ───────────────────────────────


class TestWebhookDisabledEmail(unittest.TestCase):
    def test_template_sends_in_dry_run(self):
        from email_system.service import EmailService
        svc = EmailService()
        ok = asyncio.run(svc.send_template(
            to="target@example.com",
            template="webhook_disabled",
            context={
                "display_name": "Jake",
                "webhook_url": "https://jake.example.com/hook",
                "consecutive_failures": 5,
            },
        ))
        self.assertTrue(ok)  # EMAIL_DRY_RUN=true → True without network

    def test_template_file_exists(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "email_system", "templates", "webhook_disabled.html",
        )
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
