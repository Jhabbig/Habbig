"""Tests for the Polymarket fills tape (T2.2) and signed webhooks (T4.5)."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
import time

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_alert_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE NOT NULL,
            enabled INTEGER DEFAULT 0,
            telegram_chat_id TEXT DEFAULT '',
            telegram_bot_token TEXT DEFAULT '',
            webhook_url TEXT DEFAULT '',
            webhook_signing_key TEXT DEFAULT '',
            min_edge REAL DEFAULT 5.0,
            sports TEXT DEFAULT '[]',
            last_alert_at TEXT DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()


def _client():
    return TestClient(sd.app)


# ── _event_name (regression for the .strip(" vs") bug) ─────────────────────

def test_event_name_basic():
    assert sd._event_name("Lakers", "Warriors") == "Lakers vs Warriors"


def test_event_name_does_not_strip_trailing_letters():
    """Regression — `.strip(' vs')` strips a CHARACTER SET, so trailing
    s/v/' ' got chewed off, producing 'Lakers vs Warrior'."""
    assert sd._event_name("Lakers", "Warriors") == "Lakers vs Warriors"
    assert sd._event_name("Wizards", "Sixers") == "Wizards vs Sixers"
    assert sd._event_name("Bears", "Vikings") == "Bears vs Vikings"


def test_event_name_handles_missing_side():
    assert sd._event_name("Lakers", "") == "Lakers"
    assert sd._event_name("", "Warriors") == "Warriors"
    assert sd._event_name(None, None) == ""


def test_event_name_strips_whitespace():
    assert sd._event_name("  Lakers ", " Warriors ") == "Lakers vs Warriors"


# ── Polymarket fills tape ───────────────────────────────────────────────────

def test_fills_buffer_starts_empty(tmp_path, monkeypatch):
    """The ring buffer is module-level; reset for isolation."""
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [])
    r = _client().get("/api/poly-fills")
    assert r.status_code == 200
    body = r.json()
    assert body["fills"] == []
    assert body["n_buffer"] == 0


def test_handle_ws_captures_large_fill(monkeypatch):
    """A WS price_change event with size * price >= PM_FILL_MIN_USD should
    populate _LIVE_POLY_FILLS."""
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [])
    monkeypatch.setattr(sd, "_LIVE_POLY_PRICES", {})
    monkeypatch.setattr(sd, "PM_FILL_MIN_USD", 1000.0)
    # asset price 0.4, size 5000 -> $2000 fill
    raw = json.dumps({
        "event_type": "price_change",
        "asset_id": "tok-big",
        "price": "0.4",
        "size": "5000",
        "side": "BUY",
        "market": "cond-1",
    })
    asyncio.run(sd._handle_pm_ws_message(raw))
    assert len(sd._LIVE_POLY_FILLS) == 1
    f = sd._LIVE_POLY_FILLS[0]
    assert f["asset_id"] == "tok-big"
    assert f["usd"] == 2000.0
    assert f["side"] == "BUY"


def test_handle_ws_ignores_small_fills(monkeypatch):
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [])
    monkeypatch.setattr(sd, "_LIVE_POLY_PRICES", {})
    monkeypatch.setattr(sd, "PM_FILL_MIN_USD", 1000.0)
    raw = json.dumps({
        "event_type": "price_change",
        "asset_id": "tok",
        "price": "0.5",
        "size": "100",  # $50 — below threshold
        "side": "BUY",
    })
    asyncio.run(sd._handle_pm_ws_message(raw))
    assert sd._LIVE_POLY_FILLS == []


def test_handle_ws_ignores_missing_side(monkeypatch):
    """Frames without a BUY/SELL side aren't trade fills (could be book
    updates) — don't pollute the tape."""
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [])
    monkeypatch.setattr(sd, "_LIVE_POLY_PRICES", {})
    monkeypatch.setattr(sd, "PM_FILL_MIN_USD", 1000.0)
    raw = json.dumps({
        "event_type": "price_change",
        "asset_id": "tok",
        "price": "0.5",
        "size": "5000",
        # no "side"
    })
    asyncio.run(sd._handle_pm_ws_message(raw))
    assert sd._LIVE_POLY_FILLS == []


def test_buffer_is_capped(monkeypatch):
    """Buffer must not grow unboundedly."""
    monkeypatch.setattr(sd, "_LIVE_POLY_PRICES", {})
    monkeypatch.setattr(sd, "PM_FILL_MIN_USD", 100.0)
    monkeypatch.setattr(sd, "PM_FILL_BUFFER_MAX", 50)
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [])
    for i in range(80):
        raw = json.dumps({
            "event_type": "price_change",
            "asset_id": f"tok-{i}",
            "price": "0.5",
            "size": "1000",
            "side": "BUY",
        })
        asyncio.run(sd._handle_pm_ws_message(raw))
    assert len(sd._LIVE_POLY_FILLS) == 50
    # Oldest dropped — first entry should be one of the recent assets
    assert sd._LIVE_POLY_FILLS[0]["asset_id"] != "tok-0"


def test_poly_fills_endpoint_filters_and_sorts(monkeypatch):
    """Endpoint returns newest-first, applies min_usd + side filter."""
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [
        {"ts": 1, "asset_id": "a", "price": 0.5, "size": 200,
         "usd": 100, "side": "BUY", "market": ""},
        {"ts": 2, "asset_id": "b", "price": 0.5, "size": 4000,
         "usd": 2000, "side": "BUY", "market": ""},
        {"ts": 3, "asset_id": "c", "price": 0.5, "size": 10000,
         "usd": 5000, "side": "SELL", "market": ""},
    ])
    r = _client().get("/api/poly-fills?min_usd=1000")
    rows = r.json()["fills"]
    assert len(rows) == 2
    assert rows[0]["asset_id"] == "c"  # newest first
    assert rows[1]["asset_id"] == "b"

    r2 = _client().get("/api/poly-fills?min_usd=1000&side=BUY")
    rows2 = r2.json()["fills"]
    assert len(rows2) == 1
    assert rows2[0]["asset_id"] == "b"


def test_poly_fills_endpoint_attaches_event_context(monkeypatch):
    """When a comparison has poly_token_id == fill's asset_id, the fill
    row should be enriched with event/outcome/trade_poly_url."""
    monkeypatch.setattr(sd, "_LIVE_POLY_FILLS", [
        {"ts": 1, "asset_id": "tok-1", "price": 0.4, "size": 5000,
         "usd": 2000, "side": "BUY", "market": ""},
    ])
    # Patch dashboard_data directly. The endpoint reads it under
    # _data_lock, but the lock is asyncio-scoped and the TestClient
    # runs the request in its own event loop — the dict mutation is
    # safe because there's no concurrent writer in this test.
    original = list(sd.dashboard_data.get("comparisons") or [])
    sd.dashboard_data["comparisons"] = [{
        "home_team": "Lakers", "away_team": "Warriors",
        "trade_poly_url": "https://polymarket.com/x",
        "condition_id": "c1",
        "outcomes": [{"outcome_name": "Lakers", "poly_token_id": "tok-1"}],
    }]
    try:
        r = _client().get("/api/poly-fills")
        row = r.json()["fills"][0]
        assert row["event"] == "Lakers vs Warriors"
        assert row["outcome"] == "Lakers"
        assert row["trade_poly_url"] == "https://polymarket.com/x"
    finally:
        sd.dashboard_data["comparisons"] = original


# ── Signed webhooks ─────────────────────────────────────────────────────────

def test_signed_post_omits_signature_when_no_key(tmp_path, monkeypatch):
    """No signing key -> no signature header. We can't test the network
    call against a real URL here, so we mock requests.post and inspect
    the headers it was called with."""
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers

        class _Resp:
            status_code = 200
        return _Resp()

    monkeypatch.setattr(sd.requests, "post", fake_post)
    # _is_safe_webhook_url will reject anything non-HTTPS by default — patch it
    monkeypatch.setattr(sd, "_is_safe_webhook_url", lambda u: True)

    sd._signed_webhook_post("https://example.test/hook", {"hello": "world"}, signing_key=None)
    assert "X-Sharpe-Signature" not in captured["headers"]
    assert "X-Sharpe-Timestamp" not in captured["headers"]
    # Body is deterministic JSON
    assert json.loads(captured["data"]) == {"hello": "world"}


def test_signed_post_adds_signature_when_key_set(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["data"] = data
        captured["headers"] = headers

        class _Resp:
            status_code = 200
        return _Resp()

    monkeypatch.setattr(sd.requests, "post", fake_post)
    monkeypatch.setattr(sd, "_is_safe_webhook_url", lambda u: True)

    key = "whsec_test_key_123"
    sd._signed_webhook_post("https://example.test/hook", {"a": 1}, signing_key=key)

    # Signature header present and well-formed
    sig_hdr = captured["headers"]["X-Sharpe-Signature"]
    ts_hdr = captured["headers"]["X-Sharpe-Timestamp"]
    assert sig_hdr.startswith("sha256=")
    # Verify the signature ourselves
    expected = hmac.new(
        key.encode(),
        ts_hdr.encode() + b"." + captured["data"],
        hashlib.sha256,
    ).hexdigest()
    assert sig_hdr == f"sha256={expected}"


def test_signed_post_rejects_unsafe_url(monkeypatch):
    """Pre-flight URL check should reject and not call requests.post."""
    called = {"ran": False}

    def fake_post(*a, **kw):
        called["ran"] = True

    monkeypatch.setattr(sd.requests, "post", fake_post)
    monkeypatch.setattr(sd, "_is_safe_webhook_url", lambda u: False)
    ok = sd._signed_webhook_post("http://internal.bad/hook", {"x": 1}, "key")
    assert ok is False
    assert called["ran"] is False


def test_signing_key_rotate_and_revoke(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    r = c.post("/api/webhooks/signing-key")
    assert r.status_code == 200
    body = r.json()
    assert body["signing_key"].startswith("whsec_")
    # Encrypted at rest — look it up via the helper
    plaintext = sd._get_webhook_signing_key("dev-user")
    assert plaintext == body["signing_key"]

    # Revoke
    r2 = c.request("DELETE", "/api/webhooks/signing-key")
    assert r2.status_code == 200
    assert sd._get_webhook_signing_key("dev-user") is None


def test_test_webhook_returns_400_without_url(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().post("/api/webhooks/test")
    assert r.status_code == 400


def test_signing_key_endpoints_blocked_for_bearer(tmp_path, monkeypatch):
    """Bearer tokens shouldn't be able to manage HMAC keys (same lockout
    as token CRUD — prevents privilege chaining)."""
    _setup_isolated_db(tmp_path, monkeypatch)
    # Create a profile + token row directly
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.executescript("""
        CREATE TABLE profiles (id TEXT PRIMARY KEY, email TEXT, username TEXT, is_admin INTEGER DEFAULT 0);
        CREATE TABLE sports_api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT DEFAULT '',
            scopes TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT,
            revoked_at TEXT
        );
    """)
    conn.execute("INSERT INTO profiles (id) VALUES ('u1')")
    plaintext = "shrp_bearertokenforhmactests"
    conn.execute(
        "INSERT INTO sports_api_tokens (user_id, token_hash) VALUES (?, ?)",
        ("u1", sd._hash_api_token(plaintext)),
    )
    conn.commit()
    conn.close()
    r = _client().post("/api/webhooks/signing-key",
                         headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 403
