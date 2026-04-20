"""Integration test for the spike email notifier.

Uses ``EMAIL_DRY_RUN=1`` so the notifier exercises its full code path
(including the gateway auth.db read and per-user rate-limit check) but
doesn't actually attempt SMTP. The gateway DB is a pristine tmp-sqlite
built with the same users/subscriptions shape the real gateway uses.

Covers:
  * Fires one email per Pro subscriber and records a row per send.
  * Re-fire on the same spike is a no-op (dedup via email_notifications).
  * Free-tier and unsubscribed users are excluded from the recipient list.
"""

from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

import db
import notifications


pytestmark = pytest.mark.integration


def _build_fake_gateway_db(path: Path) -> None:
    """Create a minimal users + subscriptions table with representative rows.

    The notifier's query only reads 6 columns (id, email, email_marketing,
    suspended, is_deleted, intelligence_addon_active) plus a subquery over
    subscriptions (user_id, plan, status, expires_at). Everything else can
    be omitted.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            email_marketing INTEGER DEFAULT 1,
            suspended INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            intelligence_addon_active INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            plan TEXT,
            status TEXT,
            expires_at INTEGER
        )
    """)
    # Two Pro subscribers (one via subscriptions.plan='pro', one via
    # intelligence_addon_active=1), one free user, one unsubscribed,
    # one suspended. Only the first two should receive emails.
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'pro1@example.test')")
    conn.execute("INSERT INTO users (id, email, intelligence_addon_active) VALUES (2, 'pro2@example.test', 1)")
    conn.execute("INSERT INTO users (id, email) VALUES (3, 'free@example.test')")
    conn.execute("INSERT INTO users (id, email, email_marketing) VALUES (4, 'unsubbed@example.test', 0)")
    conn.execute("INSERT INTO users (id, email, suspended) VALUES (5, 'suspended@example.test', 1)")
    conn.execute(
        "INSERT INTO subscriptions (user_id, plan, status, expires_at) VALUES (1, 'pro', 'active', ?)",
        (int(_time.time()) + 86400 * 30,),
    )
    # Free user has no subscription row at all; notifier should not pick them up.
    conn.commit()
    conn.close()


@pytest.fixture
def gateway_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway_auth.db"
    _build_fake_gateway_db(path)
    monkeypatch.setenv("GATEWAY_AUTH_DB", str(path))
    monkeypatch.setenv("EMAIL_DRY_RUN", "1")
    # Flip the master kill switch on for these tests. In prod the flag
    # stays false until launch day (see config.py comment).
    import config
    monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ALLOWLIST", [])
    return path


class TestSpikeEmailNotification:
    @pytest.mark.asyncio
    async def test_sends_to_pro_users_only(self, fresh_db, gateway_db):
        spike_id = db.insert_spike(
            entity="TestCorp", detected_hour=db.current_hour_iso(),
            z_score=4.0, multiple_of_baseline=3.5, avg_annoyance=78.0,
            count=12, sample_post_ids=["t:1"],
            summary="Test spike",
            sample_excerpts=["example excerpt"],
            confidence_score=72.0,
            sources_breakdown=[{"source": "reddit", "count": 12}],
        )
        assert spike_id is not None

        result = await notifications.send_spike_email(
            spike_id=spike_id,
            entity="TestCorp",
            summary="Test spike",
            confidence=72.0,
            entity_url="https://annoyance.narve.ai/entity/TestCorp",
        )
        # Exactly the two Pro users (pro1 via subscriptions.plan='pro',
        # pro2 via intelligence_addon_active=1) — free/unsubbed/suspended
        # don't count.
        assert result["recipients"] == 2
        assert result["sent"] == 2
        assert result["failed"] == 0

        # Each recipient gets a row in email_notifications.
        with db.cursor() as cur:
            rows = cur.execute(
                "SELECT user_email, status FROM email_notifications WHERE spike_id = ?",
                (spike_id,),
            ).fetchall()
        emails = {r["user_email"]: r["status"] for r in rows}
        assert emails == {
            "pro1@example.test": "sent",
            "pro2@example.test": "sent",
        }

    @pytest.mark.asyncio
    async def test_refire_same_spike_no_duplicate(self, fresh_db, gateway_db):
        spike_id = db.insert_spike(
            entity="DupCorp", detected_hour=db.current_hour_iso(),
            z_score=4.0, multiple_of_baseline=3.5, avg_annoyance=78.0,
            count=12, sample_post_ids=["t:1"],
        )

        r1 = await notifications.send_spike_email(
            spike_id=spike_id, entity="DupCorp", summary="first",
            confidence=70.0, entity_url="https://x.test/e/DupCorp",
        )
        r2 = await notifications.send_spike_email(
            spike_id=spike_id, entity="DupCorp", summary="second",
            confidence=70.0, entity_url="https://x.test/e/DupCorp",
        )

        # Second call finds the prior email_notifications rows and skips.
        assert r1["sent"] == 2
        assert r2["sent"] == 0
        assert r2["skipped"] == 2

        # Only two ledger rows exist, not four.
        with db.cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) FROM email_notifications WHERE spike_id = ?",
                (spike_id,),
            ).fetchone()
        assert row[0] == 2

    @pytest.mark.asyncio
    async def test_no_gateway_db_returns_zero_recipients(self, fresh_db, monkeypatch):
        """If GATEWAY_AUTH_DB is not set (or file is missing), notifier
        degrades gracefully — zero recipients, no exception."""
        import config
        monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ALLOWLIST", [])
        monkeypatch.delenv("GATEWAY_AUTH_DB", raising=False)
        monkeypatch.setenv("EMAIL_DRY_RUN", "1")

        spike_id = db.insert_spike(
            entity="NoGatewayCorp", detected_hour=db.current_hour_iso(),
            z_score=4.0, multiple_of_baseline=3.5, avg_annoyance=78.0,
            count=12, sample_post_ids=["t:1"],
        )
        result = await notifications.send_spike_email(
            spike_id=spike_id, entity="NoGatewayCorp", summary="x",
            confidence=60.0, entity_url="https://x.test/",
        )
        assert result == {"sent": 0, "skipped": 0, "failed": 0, "recipients": 0}

    @pytest.mark.asyncio
    async def test_flag_gate_disabled_skips_all(self, fresh_db, gateway_db, monkeypatch):
        """PRE-RELEASE SAFETY: when EMAIL_NOTIFICATIONS_ENABLED is false,
        the notifier exits immediately — no gateway DB read, no SMTP, no
        ledger rows. Ship-to-staging should default to this state."""
        import config
        monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ENABLED", False)

        spike_id = db.insert_spike(
            entity="DisabledCorp", detected_hour=db.current_hour_iso(),
            z_score=4.0, multiple_of_baseline=3.5, avg_annoyance=78.0,
            count=12, sample_post_ids=["t:1"],
        )
        result = await notifications.send_spike_email(
            spike_id=spike_id, entity="DisabledCorp", summary="x",
            confidence=60.0, entity_url="https://x.test/",
        )
        assert result == {"sent": 0, "skipped": 0, "failed": 0, "recipients": 0}
        # No ledger rows written
        with db.cursor() as cur:
            n = cur.execute(
                "SELECT COUNT(*) FROM email_notifications WHERE spike_id = ?",
                (spike_id,),
            ).fetchone()[0]
        assert n == 0

    @pytest.mark.asyncio
    async def test_allowlist_filters_recipients(self, fresh_db, gateway_db, monkeypatch):
        """Soak-test mode: allowlist filters the gateway-DB recipient set
        down to a named subset. Both Pro subscribers in the fixture
        (pro1 + pro2), but allowlist only includes pro2 → exactly one send."""
        import config
        monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(config, "EMAIL_NOTIFICATIONS_ALLOWLIST", ["pro2@example.test"])

        spike_id = db.insert_spike(
            entity="SoakCorp", detected_hour=db.current_hour_iso(),
            z_score=4.0, multiple_of_baseline=3.5, avg_annoyance=78.0,
            count=12, sample_post_ids=["t:1"],
        )
        result = await notifications.send_spike_email(
            spike_id=spike_id, entity="SoakCorp", summary="x",
            confidence=60.0, entity_url="https://x.test/",
        )
        assert result["recipients"] == 1
        assert result["sent"] == 1

        with db.cursor() as cur:
            rows = cur.execute(
                "SELECT user_email FROM email_notifications WHERE spike_id = ?",
                (spike_id,),
            ).fetchall()
        assert [r["user_email"] for r in rows] == ["pro2@example.test"]
