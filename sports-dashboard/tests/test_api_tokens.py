"""Tests for Bearer API tokens (T5.1)."""
import sqlite3

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE profiles (
            id TEXT PRIMARY KEY,
            email TEXT, username TEXT, is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
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
    conn.commit()
    conn.close()


def _client():
    return TestClient(sd.app)


# ── _hash_api_token ─────────────────────────────────────────────────────────

def test_hash_is_deterministic():
    assert sd._hash_api_token("shrp_abc") == sd._hash_api_token("shrp_abc")
    assert sd._hash_api_token("a") != sd._hash_api_token("b")


def test_hash_is_64_hex_chars():
    h = sd._hash_api_token("test")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ── _resolve_bearer_token ───────────────────────────────────────────────────

def test_resolve_rejects_short_token(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._resolve_bearer_token("short") is None


def test_resolve_rejects_unknown_token(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._resolve_bearer_token("shrp_aaaaaaaaaaaaaaaa") is None


def test_resolve_returns_user_for_valid_token(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute("INSERT INTO profiles (id, email, username, is_admin) VALUES (?,?,?,?)",
                  ("u1", "u1@x", "u1", 0))
    plaintext = "shrp_validtoken1234567890"
    conn.execute(
        "INSERT INTO sports_api_tokens (user_id, name, token_hash, token_prefix) "
        "VALUES (?, ?, ?, ?)",
        ("u1", "test", sd._hash_api_token(plaintext), plaintext[:8]),
    )
    conn.commit()
    conn.close()
    user = sd._resolve_bearer_token(plaintext)
    assert user is not None
    assert user["id"] == "u1"
    assert user["email"] == "u1@x"
    assert user["_bearer_token_id"]


def test_resolve_rejects_revoked_token(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute("INSERT INTO profiles (id) VALUES ('u1')")
    plaintext = "shrp_validtoken1234567890"
    conn.execute(
        "INSERT INTO sports_api_tokens (user_id, token_hash, revoked_at) "
        "VALUES (?, ?, datetime('now'))",
        ("u1", sd._hash_api_token(plaintext)),
    )
    conn.commit()
    conn.close()
    assert sd._resolve_bearer_token(plaintext) is None


def test_resolve_bumps_last_used_at(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute("INSERT INTO profiles (id) VALUES ('u1')")
    plaintext = "shrp_validtoken1234567890"
    conn.execute(
        "INSERT INTO sports_api_tokens (user_id, token_hash) VALUES (?, ?)",
        ("u1", sd._hash_api_token(plaintext)),
    )
    conn.commit()
    conn.close()
    # Before use
    with sqlite3.connect(str(sd._DB_PATH)) as c:
        before = c.execute("SELECT last_used_at FROM sports_api_tokens").fetchone()[0]
    assert before is None
    sd._resolve_bearer_token(plaintext)
    # After use
    with sqlite3.connect(str(sd._DB_PATH)) as c:
        after = c.execute("SELECT last_used_at FROM sports_api_tokens").fetchone()[0]
    assert after is not None


# ── /api/auth/tokens endpoints ──────────────────────────────────────────────

def test_create_returns_plaintext_once(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().post("/api/auth/tokens", json={"name": "automation"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("shrp_")
    assert len(body["token"]) > 20
    assert body["token_prefix"] == body["token"][:8]
    # Listing tokens never includes plaintext
    r2 = _client().get("/api/auth/tokens")
    items = r2.json()["tokens"]
    assert len(items) == 1
    assert "token" not in items[0]  # plaintext gone
    assert items[0]["token_prefix"] == body["token_prefix"]


def test_create_rejects_invalid_scopes(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().post("/api/auth/tokens", json={"scopes": "not-a-list"})
    assert r.status_code == 400


def test_create_via_bearer_token_forbidden(tmp_path, monkeypatch):
    """A Bearer token must not be able to mint or list other tokens —
    that's a session-only operation. Otherwise a leaked token could
    self-perpetuate."""
    _setup_isolated_db(tmp_path, monkeypatch)
    # Create via session first
    r = _client().post("/api/auth/tokens", json={"name": "initial"})
    plaintext = r.json()["token"]
    # Now try to use it to create another
    r2 = _client().post(
        "/api/auth/tokens",
        json={"name": "evil"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # Bearer was accepted (user resolved) but the endpoint refuses
    assert r2.status_code == 403


def test_revoke_then_use_fails(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    r = c.post("/api/auth/tokens", json={"name": "test"})
    plaintext = r.json()["token"]
    token_id = r.json()["id"]
    # Revoke
    r2 = c.request("DELETE", f"/api/auth/tokens/{token_id}")
    assert r2.status_code == 200
    # Trying to use it now should not resolve to a user
    assert sd._resolve_bearer_token(plaintext) is None


def test_revoke_nonexistent_returns_404(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().request("DELETE", "/api/auth/tokens/9999")
    assert r.status_code == 404


def test_listed_tokens_include_revoked(tmp_path, monkeypatch):
    """Listing should show revoked tokens too (with revoked_at set) so
    users can see what they've cycled."""
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    r = c.post("/api/auth/tokens", json={"name": "alpha"})
    c.request("DELETE", f"/api/auth/tokens/{r.json()['id']}")
    items = c.get("/api/auth/tokens").json()["tokens"]
    assert len(items) == 1
    assert items[0]["revoked_at"] is not None
