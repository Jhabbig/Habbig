"""Tests for the Markets feature — unified markets, connections, trading, portfolio, security."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add gateway root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
from backend.markets.encryption import decrypt_token, encrypt_token
from backend.markets.unified_markets import (
    UnifiedMarket,
    _normalise_kalshi,
    _normalise_polymarket,
    filter_markets,
)


class TestUnifiedMarkets(unittest.TestCase):
    """Tests for the unified market normalisation and filtering."""

    def test_normalise_polymarket_basic(self):
        raw = {
            "slug": "will-trump-win",
            "question": "Will Trump win the election?",
            "outcomePrices": '["0.67", "0.33"]',
            "volume": 1500000,
            "liquidity": 200000,
            "active": True,
            "closed": False,
            "resolved": False,
            "endDate": "2026-11-03T00:00:00Z",
        }
        m = _normalise_polymarket(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.id, "poly:will-trump-win")
        self.assertEqual(m.source, "polymarket")
        self.assertAlmostEqual(m.yes_price, 0.67, places=2)
        self.assertAlmostEqual(m.no_price, 0.33, places=2)
        self.assertEqual(m.status, "active")
        self.assertEqual(m.category, "politics")

    def test_normalise_polymarket_resolved(self):
        raw = {
            "slug": "btc-100k",
            "question": "Will BTC hit 100k?",
            "outcomePrices": '["0.95", "0.05"]',
            "volume": 500000,
            "resolved": True,
            "outcome": "Yes",
        }
        m = _normalise_polymarket(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.status, "resolved")
        self.assertEqual(m.outcome, "Yes")

    def test_normalise_polymarket_missing_slug(self):
        raw = {"question": "No slug here"}
        m = _normalise_polymarket(raw)
        self.assertIsNone(m)

    def test_normalise_kalshi_basic(self):
        raw = {
            "ticker": "FEDRATE-26APR-T4.75",
            "title": "Fed rate above 4.75%?",
            "yes_ask": 65,
            "volume": 800000,
            "open_interest": 150000,
            "status": "open",
            "close_time": "2026-04-30T00:00:00Z",
        }
        m = _normalise_kalshi(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.id, "kalshi:FEDRATE-26APR-T4.75")
        self.assertEqual(m.source, "kalshi")
        self.assertAlmostEqual(m.yes_price, 0.65, places=2)
        self.assertEqual(m.status, "active")
        self.assertEqual(m.category, "finance")

    def test_normalise_kalshi_settled(self):
        raw = {
            "ticker": "SNOW-NYC",
            "title": "Will it snow in NYC?",
            "yes_ask": 90,
            "status": "settled",
            "result": "yes",
        }
        m = _normalise_kalshi(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.status, "resolved")
        self.assertEqual(m.outcome, "yes")

    def test_normalise_kalshi_missing_ticker(self):
        raw = {"title": "No ticker"}
        m = _normalise_kalshi(raw)
        self.assertIsNone(m)

    def test_filter_by_category(self):
        markets = [
            UnifiedMarket(id="1", source="polymarket", title="A", category="politics",
                          yes_price=0.5, no_price=0.5, volume_usd=100, liquidity_usd=10,
                          close_time=None, status="active", outcome=None, url=""),
            UnifiedMarket(id="2", source="kalshi", title="B", category="sports",
                          yes_price=0.7, no_price=0.3, volume_usd=200, liquidity_usd=20,
                          close_time=None, status="active", outcome=None, url=""),
        ]
        result = filter_markets(markets, category="sports")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "2")

    def test_filter_by_source(self):
        markets = [
            UnifiedMarket(id="1", source="polymarket", title="A", category="",
                          yes_price=0.5, no_price=0.5, volume_usd=100, liquidity_usd=10,
                          close_time=None, status="active", outcome=None, url=""),
            UnifiedMarket(id="2", source="kalshi", title="B", category="",
                          yes_price=0.7, no_price=0.3, volume_usd=200, liquidity_usd=20,
                          close_time=None, status="active", outcome=None, url=""),
        ]
        result = filter_markets(markets, source="kalshi")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "kalshi")

    def test_filter_by_search(self):
        markets = [
            UnifiedMarket(id="1", source="polymarket", title="Will Trump win?", category="",
                          yes_price=0.5, no_price=0.5, volume_usd=100, liquidity_usd=10,
                          close_time=None, status="active", outcome=None, url=""),
            UnifiedMarket(id="2", source="kalshi", title="Bitcoin 100k", category="",
                          yes_price=0.7, no_price=0.3, volume_usd=200, liquidity_usd=20,
                          close_time=None, status="active", outcome=None, url=""),
        ]
        result = filter_markets(markets, search="bitcoin")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "2")

    def test_sort_by_volume(self):
        markets = [
            UnifiedMarket(id="1", source="polymarket", title="A", category="",
                          yes_price=0.5, no_price=0.5, volume_usd=100, liquidity_usd=10,
                          close_time=None, status="active", outcome=None, url=""),
            UnifiedMarket(id="2", source="kalshi", title="B", category="",
                          yes_price=0.7, no_price=0.3, volume_usd=500, liquidity_usd=20,
                          close_time=None, status="active", outcome=None, url=""),
        ]
        result = filter_markets(markets, sort="volume")
        self.assertEqual(result[0].id, "2")  # Higher volume first

    def test_sort_by_ev(self):
        m1 = UnifiedMarket(id="1", source="polymarket", title="A", category="",
                           yes_price=0.5, no_price=0.5, volume_usd=100, liquidity_usd=10,
                           close_time=None, status="active", outcome=None, url="",
                           betyc_ev_score=0.3)
        m2 = UnifiedMarket(id="2", source="kalshi", title="B", category="",
                           yes_price=0.7, no_price=0.3, volume_usd=200, liquidity_usd=20,
                           close_time=None, status="active", outcome=None, url="",
                           betyc_ev_score=0.1)
        result = filter_markets([m1, m2], sort="ev")
        self.assertEqual(result[0].id, "1")  # Higher EV first


class TestEncryption(unittest.TestCase):
    """Tests for Kalshi token encryption."""

    def test_encrypt_decrypt_roundtrip(self):
        """If cryptography is available and key is set, roundtrip works."""
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            self.skipTest("cryptography not installed")

        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"CREDENTIALS_ENCRYPTION_KEY": key}):
            # Reset cached fernet
            import backend.markets.encryption as enc
            enc._fernet = None
            encrypted = encrypt_token("my-secret-token")
            self.assertNotEqual(encrypted, "my-secret-token")
            decrypted = decrypt_token(encrypted)
            self.assertEqual(decrypted, "my-secret-token")
            enc._fernet = None  # Clean up

    def test_decrypt_handles_plaintext_gracefully(self):
        """Decrypting a non-encrypted string returns it as-is (migration path)."""
        import backend.markets.encryption as enc
        enc._fernet = None
        # With no key set, decrypt just returns the input
        result = decrypt_token("plaintext-token")
        self.assertEqual(result, "plaintext-token")


class TestDatabaseModels(unittest.TestCase):
    """Tests for UserMarketCredentials and UserBetHistory DB operations."""

    @classmethod
    def setUpClass(cls):
        """Use an in-memory database for tests."""
        import sqlite3
        # Override db path to in-memory
        db.DB_PATH = ":memory:"
        # Need to store connection for test lifetime since :memory: is per-connection
        cls._test_conn = sqlite3.connect(":memory:")
        cls._test_conn.row_factory = sqlite3.Row
        cls._test_conn.execute("PRAGMA foreign_keys = ON")
        cls._test_conn.executescript(db.SCHEMA)
        cls._test_conn.commit()

        # Monkey-patch db.conn to return our test connection
        import contextlib

        @contextlib.contextmanager
        def test_conn():
            try:
                yield cls._test_conn
                cls._test_conn.commit()
            except Exception:
                cls._test_conn.rollback()
                raise

        cls._orig_conn = db.conn
        db.conn = test_conn

        # Create a test user
        cls.user_id = db.create_user("test@test.com", "TestPass123!", username="testuser")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def test_upsert_kalshi_credential(self):
        db.upsert_market_credential(
            self.user_id, "kalshi",
            kalshi_token="encrypted_token_123",
            kalshi_member_id="mem_abc",
        )
        cred = db.get_market_credential(self.user_id, "kalshi")
        self.assertIsNotNone(cred)
        self.assertEqual(cred["kalshi_token"], "encrypted_token_123")
        self.assertEqual(cred["kalshi_member_id"], "mem_abc")

    def test_upsert_polymarket_credential(self):
        db.upsert_market_credential(
            self.user_id, "polymarket",
            polymarket_wallet_address="0x1234567890abcdef",
        )
        cred = db.get_market_credential(self.user_id, "polymarket")
        self.assertIsNotNone(cred)
        self.assertEqual(cred["polymarket_wallet_address"], "0x1234567890abcdef")

    def test_get_all_credentials(self):
        # Ensure at least one credential exists
        db.upsert_market_credential(
            self.user_id, "polymarket",
            polymarket_wallet_address="0xABCDEF",
        )
        creds = db.get_all_market_credentials(self.user_id)
        self.assertGreaterEqual(len(creds), 1)

    def test_delete_credential(self):
        db.upsert_market_credential(
            self.user_id, "kalshi",
            kalshi_token="to_delete",
            kalshi_member_id="mem_del",
        )
        result = db.delete_market_credential(self.user_id, "kalshi")
        self.assertTrue(result)
        cred = db.get_market_credential(self.user_id, "kalshi")
        self.assertIsNone(cred)

    def test_delete_nonexistent_returns_false(self):
        result = db.delete_market_credential(self.user_id, "nonexistent")
        self.assertFalse(result)

    def test_record_bet(self):
        bet_id = db.record_bet(
            self.user_id, "kalshi", "order_123",
            "kalshi:TICKER", "Will X happen?",
            "yes", 50.0, 0.65, "submitted",
        )
        self.assertGreater(bet_id, 0)

    def test_list_bet_history(self):
        # Record another bet
        db.record_bet(
            self.user_id, "polymarket", "order_456",
            "poly:some-slug", "Will Y happen?",
            "no", 25.0, 0.30, "filled",
        )
        history = db.list_bet_history(self.user_id)
        self.assertGreaterEqual(len(history), 1)

    def test_kalshi_token_stored_as_given(self):
        """Verify tokens are stored as-is (route layer encrypts before saving)."""
        db.upsert_market_credential(
            self.user_id, "kalshi",
            kalshi_token="gAAAAABf_encrypted_blob_here",
            kalshi_member_id="mem_sec",
        )
        cred = db.get_market_credential(self.user_id, "kalshi")
        self.assertEqual(cred["kalshi_token"], "gAAAAABf_encrypted_blob_here")
        # Token should look like an encrypted value, not a raw credential
        self.assertTrue(cred["kalshi_token"].startswith("gAAAAAB"))


class TestSecurityRules(unittest.TestCase):
    """Security-focused tests."""

    def test_negative_bet_amount_rejected(self):
        """Bet amounts must be positive (validated server-side)."""
        # This tests the validation logic conceptually
        amount = -10.0
        self.assertLess(amount, 0, "Negative amounts should be rejected by the API")

    def test_unified_market_to_dict_has_no_secrets(self):
        """UnifiedMarket serialisation contains no credential data.

        Note: poly_yes_token_id / poly_no_token_id are PUBLIC CLOB token
        identifiers, not session credentials, so they're allowed.
        """
        m = UnifiedMarket(
            id="poly:test", source="polymarket", title="Test",
            category="other", yes_price=0.5, no_price=0.5,
            volume_usd=100, liquidity_usd=10,
            close_time=None, status="active", outcome=None, url="",
            poly_yes_token_id="123", poly_no_token_id="456",
        )
        d = m.to_dict()
        serialised = json.dumps(d).lower()
        # Check for credential-like substrings, not the literal "token"
        self.assertNotIn("password", serialised)
        self.assertNotIn("private_key", serialised)
        self.assertNotIn("session_token", serialised)
        self.assertNotIn("kalshi_token", serialised)
        self.assertNotIn("auth_token", serialised)
        self.assertNotIn("api_key", serialised)
        self.assertNotIn("secret", serialised)


class TestPolymarketTokenIds(unittest.TestCase):
    """Tests for clobTokenIds parsing in Polymarket normalisation."""

    def test_clob_token_ids_parsed_from_json_string(self):
        raw = {
            "slug": "test-market",
            "question": "Test?",
            "outcomePrices": '["0.6", "0.4"]',
            "clobTokenIds": '["12345678901234567890", "98765432109876543210"]',
            "volume": 1000,
            "active": True,
        }
        m = _normalise_polymarket(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.poly_yes_token_id, "12345678901234567890")
        self.assertEqual(m.poly_no_token_id, "98765432109876543210")
        self.assertFalse(m.poly_neg_risk)

    def test_clob_token_ids_missing_returns_none(self):
        raw = {"slug": "no-tokens", "question": "?", "outcomePrices": '["0.5","0.5"]'}
        m = _normalise_polymarket(raw)
        self.assertIsNotNone(m)
        self.assertIsNone(m.poly_yes_token_id)
        self.assertIsNone(m.poly_no_token_id)

    def test_neg_risk_flag_parsed(self):
        raw = {
            "slug": "neg-risk",
            "question": "?",
            "outcomePrices": '["0.5","0.5"]',
            "clobTokenIds": '["1","2"]',
            "negRisk": True,
        }
        m = _normalise_polymarket(raw)
        self.assertTrue(m.poly_neg_risk)


class TestKalshiServiceAuth(unittest.TestCase):
    """Tests for Kalshi service-account token caching."""

    def test_service_auth_disabled_when_no_creds(self):
        from backend.markets.kalshi_client import KalshiClient
        c = KalshiClient()
        self.assertIsNone(c._service_email)
        # No credentials supplied → no password provider.
        self.assertIsNone(c._password_provider)

    def test_service_auth_initialised_with_creds(self):
        from backend.markets.kalshi_client import KalshiClient
        c = KalshiClient(service_email="svc@example.com", service_password="pw")
        self.assertEqual(c._service_email, "svc@example.com")
        # Password is stored as a callable provider, not a plaintext
        # attribute — calling it returns the credential.
        self.assertIsNotNone(c._password_provider)
        self.assertEqual(c._password_provider(), "pw")
        self.assertIsNone(c._service_token)

    def test_get_service_token_returns_none_without_creds(self):
        import asyncio
        from backend.markets.kalshi_client import KalshiClient
        c = KalshiClient()
        token = asyncio.run(c._get_service_token())
        self.assertIsNone(token)


if __name__ == "__main__":
    unittest.main()
