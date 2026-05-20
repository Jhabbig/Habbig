"""Tests for the Web Push subscription endpoints.

Actual push delivery is a network-bound operation against the user's
browser-vendor push endpoint, so these only test the subscribe/
unsubscribe/validate path. _send_web_push is exercised via a no-op
path since pywebpush either is installed (and would fail with an
invalid endpoint) or isn't (and the function returns 0).
"""
import json

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


# ── /api/push/vapid-public-key ──────────────────────────────────────────────

def test_vapid_key_returns_503_when_unset(monkeypatch):
    monkeypatch.setattr(sd, "VAPID_PUBLIC_KEY", "")
    r = _client().get("/api/push/vapid-public-key")
    assert r.status_code == 503


def test_vapid_key_returns_key_when_set(monkeypatch):
    monkeypatch.setattr(sd, "VAPID_PUBLIC_KEY", "BFakeKey")
    r = _client().get("/api/push/vapid-public-key")
    assert r.status_code == 200
    body = r.json()
    assert body["public_key"] == "BFakeKey"
    assert "push_available" in body


# ── /api/push/subscribe ─────────────────────────────────────────────────────

def test_subscribe_requires_endpoint():
    r = _client().post("/api/push/subscribe", json={})
    assert r.status_code == 400


def test_subscribe_rejects_non_https():
    r = _client().post("/api/push/subscribe", json={
        "endpoint": "http://example.com/push",
        "keys": {"p256dh": "abc", "auth": "def"},
    })
    assert r.status_code == 400


def test_subscribe_requires_keys():
    r = _client().post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/x",
    })
    assert r.status_code == 400


def test_subscribe_upsert_round_trip(tmp_path, monkeypatch):
    """A POST followed by another POST with the same endpoint is idempotent."""
    import sqlite3
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            last_pushed_at TEXT,
            UNIQUE(user_id, endpoint)
        );
    """)
    conn.commit()
    conn.close()

    c = _client()
    body = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/abcd1234",
        "keys": {"p256dh": "first-p256dh", "auth": "first-auth"},
    }
    r1 = c.post("/api/push/subscribe", json=body)
    assert r1.status_code == 200

    # Re-subscribe with rotated keys — should upsert, not duplicate
    body["keys"] = {"p256dh": "second-p256dh", "auth": "second-auth"}
    r2 = c.post("/api/push/subscribe", json=body)
    assert r2.status_code == 200

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT p256dh, auth FROM sports_push_subscriptions").fetchall()
    assert len(rows) == 1
    assert rows[0] == ("second-p256dh", "second-auth")
    conn.close()


def test_unsubscribe_removes_subscription(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            last_pushed_at TEXT,
            UNIQUE(user_id, endpoint)
        );
    """)
    conn.commit()
    conn.close()

    c = _client()
    c.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/xyz",
        "keys": {"p256dh": "k", "auth": "a"},
    })
    r = c.request("DELETE", "/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/xyz",
    })
    assert r.status_code == 200
    assert r.json()["deleted"] == 1


# ── _send_web_push ──────────────────────────────────────────────────────────

def test_send_web_push_returns_zero_when_not_available(monkeypatch):
    """No VAPID keys configured -> nothing delivered, no exception."""
    monkeypatch.setattr(sd, "_PUSH_AVAILABLE", False)
    assert sd._send_web_push("any-user", {"title": "x", "body": "y"}) == 0


def test_send_web_push_returns_zero_when_no_subscriptions(tmp_path, monkeypatch):
    """User has push enabled but no devices registered."""
    import sqlite3
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    monkeypatch.setattr(sd, "_PUSH_AVAILABLE", True)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            last_pushed_at TEXT,
            UNIQUE(user_id, endpoint)
        );
    """)
    conn.commit()
    conn.close()
    assert sd._send_web_push("nobody-home", {"title": "x"}) == 0
