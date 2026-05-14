"""Tests for the Intelligence assistant — context builder + DB layer."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TestIntelligenceConversations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()
        cls.user_id = db.create_user("intel@test.com", "TestPass123!", username="inteluser")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_create_conversation_returns_id(self):
        conv_id = db.create_intelligence_conversation(self.user_id)
        self.assertGreater(conv_id, 0)

    def test_append_message_increments_count(self):
        conv_id = db.create_intelligence_conversation(self.user_id)
        db.append_intelligence_message(conv_id, "user", "Hello")
        db.append_intelligence_message(conv_id, "assistant", "Hi there")
        msgs = db.list_intelligence_messages(conv_id)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_first_user_message_becomes_title(self):
        conv_id = db.create_intelligence_conversation(self.user_id)
        db.append_intelligence_message(conv_id, "user", "What's the best bet on US elections?")
        conv = db.get_intelligence_conversation(conv_id, self.user_id)
        self.assertIn("US elections", conv["title"])

    def test_delete_conversation_cascades(self):
        conv_id = db.create_intelligence_conversation(self.user_id)
        db.append_intelligence_message(conv_id, "user", "test")
        db.delete_intelligence_conversation(conv_id, self.user_id)
        self.assertEqual(db.list_intelligence_messages(conv_id), [])

    def test_user_isolation(self):
        other_id = db.create_user("other@test.com", "TestPass123!", username="otheruser")
        conv_id = db.create_intelligence_conversation(self.user_id)
        # Other user cannot see/delete this conversation.
        self.assertIsNone(db.get_intelligence_conversation(conv_id, other_id))
        self.assertFalse(db.delete_intelligence_conversation(conv_id, other_id))

    def test_count_messages_today(self):
        conv_id = db.create_intelligence_conversation(self.user_id)
        before = db.count_intelligence_messages_today(self.user_id)
        db.append_intelligence_message(conv_id, "user", "msg 1")
        db.append_intelligence_message(conv_id, "user", "msg 2")
        after = db.count_intelligence_messages_today(self.user_id)
        self.assertEqual(after, before + 2)

    def test_history_limited_to_last_20(self):
        from intelligence.claude_client import _build_messages
        history = [{"role": "user", "content": f"msg {i}"} for i in range(30)]
        messages = _build_messages(history, "current question")
        # Last 20 from history + 1 current = 21
        self.assertEqual(len(messages), 21)
        self.assertEqual(messages[-1]["content"], "current question")
        # Earliest preserved should be msg 10 (30 - 20).
        self.assertEqual(messages[0]["content"], "msg 10")


class TestContextBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()
        cls.user_id = db.create_user("ctx@test.com", "TestPass123!", username="ctxuser")
        # Seed a credibility row + prediction so the context builder has data.
        db.upsert_source_credibility(
            source_handle="cryptoanalyst",
            global_credibility=0.81,
            accuracy_unlocked=1,
            decay_weighted_accuracy=0.78,
            total_predictions=12,
            correct_predictions=9,
            categories_active=4,
        )
        db.create_prediction(
            source_handle="cryptoanalyst",
            content="Bitcoin ETF approval inevitable by April",
            category="crypto",
        )

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_context_includes_user_profile(self):
        from intelligence import build_intelligence_context
        user = {"user_id": self.user_id, "is_admin": False}
        result = _run(build_intelligence_context(user, "hello", []))
        self.assertIn("User profile", result["text"])

    def test_handle_query_loads_source_profile(self):
        from intelligence import build_intelligence_context
        user = {"user_id": self.user_id, "is_admin": False}
        result = _run(build_intelligence_context(user, "tell me about @cryptoanalyst", []))
        self.assertIn("@cryptoanalyst", result["text"])
        self.assertIn("Global credibility", result["text"])

    def test_best_bets_query_includes_recent_predictions(self):
        from intelligence import build_intelligence_context
        user = {"user_id": self.user_id, "is_admin": False}
        result = _run(build_intelligence_context(user, "what are today's best bets?", []))
        self.assertIn("Recent high-signal predictions", result["text"])

    def test_category_query_loads_category_predictions(self):
        from intelligence import build_intelligence_context
        user = {"user_id": self.user_id, "is_admin": False}
        result = _run(build_intelligence_context(user, "what's happening with crypto markets?", []))
        # Either the recent-predictions block or the crypto category block matches.
        self.assertIn("crypto", result["text"].lower())


class TestIntelligenceAddonAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()
        cls.user_id = db.create_user("noaddon@test.com", "TestPass123!", username="noaddon")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_user_without_addon_returns_false(self):
        self.assertFalse(db.get_user_intelligence_addon_active(self.user_id))

    def test_set_intelligence_addon_active(self):
        db.set_user_intelligence_addon(self.user_id, True, period_end=None)
        self.assertTrue(db.get_user_intelligence_addon_active(self.user_id))

    def test_set_intelligence_addon_inactive(self):
        db.set_user_intelligence_addon(self.user_id, False, None)
        self.assertFalse(db.get_user_intelligence_addon_active(self.user_id))


if __name__ == "__main__":
    unittest.main()
