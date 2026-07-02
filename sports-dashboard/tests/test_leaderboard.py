"""Tests for the public CLV leaderboard (T4.7)."""
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_clv_leaderboard_optin (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            joined_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE sports_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            market_name TEXT, outcome TEXT,
            entry_price REAL, amount REAL,
            exit_price REAL, pnl REAL,
            status TEXT DEFAULT 'open',
            resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            sport TEXT, book TEXT, market_type TEXT,
            line REAL, commence_time TEXT, source TEXT,
            closing_book_prob REAL, clv_pp REAL,
            notes TEXT, home_team TEXT, away_team TEXT
        );
    """)
    conn.commit()
    conn.close()


def _client():
    return TestClient(sd.app)


def _add_trade(user_id, pnl, clv_pp, status="closed",
                resolved_at_offset_days=1):
    """Insert a closed trade resolved N days ago."""
    resolved = (datetime.now(timezone.utc) - timedelta(days=resolved_at_offset_days)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_trades "
        "(user_id, market_name, entry_price, amount, exit_price, pnl, "
        " status, resolved_at, clv_pp) "
        "VALUES (?, 'm', 50, 100, 60, ?, ?, ?, ?)",
        (user_id, pnl, status, resolved, clv_pp),
    )
    conn.commit()
    conn.close()


def _add_optin(user_id, display_name):
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_clv_leaderboard_optin (user_id, display_name) "
        "VALUES (?, ?)",
        (user_id, display_name),
    )
    conn.commit()
    conn.close()


# ── _compute_clv_leaderboard ────────────────────────────────────────────────

def test_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._compute_clv_leaderboard() == []


def test_excludes_users_below_min_trades(tmp_path, monkeypatch):
    """LEADERBOARD_MIN_TRADES gate prevents 1-bet flukes from topping
    the board."""
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 5)
    _add_optin("u1", "alice")
    # Only 2 trades — should not appear
    for _ in range(2):
        _add_trade("u1", pnl=50, clv_pp=3.0)
    assert sd._compute_clv_leaderboard() == []


def test_includes_users_above_min_trades(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("u1", "alice")
    for _ in range(5):
        _add_trade("u1", pnl=50, clv_pp=3.0)
    rows = sd._compute_clv_leaderboard()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "alice"
    assert rows[0]["n_trades"] == 5


def test_excludes_non_optin_users(tmp_path, monkeypatch):
    """Even a user with great CLV doesn't appear unless opted in."""
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    # Note: no _add_optin for u1
    for _ in range(5):
        _add_trade("u1", pnl=100, clv_pp=10.0)
    assert sd._compute_clv_leaderboard() == []


def test_excludes_trades_without_clv(tmp_path, monkeypatch):
    """Trades where clv_pp is NULL shouldn't count."""
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("u1", "alice")
    # 5 trades but only 2 have clv_pp
    _add_trade("u1", pnl=50, clv_pp=3.0)
    _add_trade("u1", pnl=50, clv_pp=4.0)
    for _ in range(3):
        _add_trade("u1", pnl=50, clv_pp=None)
    # n_trades for the leaderboard counts only clv-having rows (2 < min 3)
    assert sd._compute_clv_leaderboard() == []


def test_excludes_open_trades(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("u1", "alice")
    for _ in range(5):
        _add_trade("u1", pnl=50, clv_pp=3.0, status="open")
    assert sd._compute_clv_leaderboard() == []


def test_window_filter(tmp_path, monkeypatch):
    """resolved_at must be within `days` window."""
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("u1", "alice")
    # 5 trades from 200 days ago — outside 90-day default
    for _ in range(5):
        _add_trade("u1", pnl=50, clv_pp=3.0, resolved_at_offset_days=200)
    assert sd._compute_clv_leaderboard(days=90) == []
    # But within 365 days they appear
    rows = sd._compute_clv_leaderboard(days=365)
    assert len(rows) == 1


def test_ranks_by_mean_clv_desc(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("u1", "alice")
    _add_optin("u2", "bob")
    _add_optin("u3", "charlie")
    # alice: mean 5pp
    for _ in range(3):
        _add_trade("u1", pnl=50, clv_pp=5.0)
    # bob: mean 8pp (best)
    for _ in range(3):
        _add_trade("u2", pnl=50, clv_pp=8.0)
    # charlie: mean 2pp
    for _ in range(3):
        _add_trade("u3", pnl=50, clv_pp=2.0)
    rows = sd._compute_clv_leaderboard()
    assert [r["display_name"] for r in rows] == ["bob", "alice", "charlie"]
    assert rows[0]["rank"] == 1
    assert rows[0]["mean_clv_pp"] == 8.0


def test_negative_clv_ranks_correctly(tmp_path, monkeypatch):
    """Users with negative mean CLV still appear (transparent track record),
    just at the bottom."""
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 3)
    _add_optin("good", "alice")
    _add_optin("bad", "bob")
    for _ in range(3):
        _add_trade("good", pnl=50, clv_pp=3.0)
    for _ in range(3):
        _add_trade("bad", pnl=-50, clv_pp=-2.0)
    rows = sd._compute_clv_leaderboard()
    assert rows[0]["display_name"] == "alice"
    assert rows[1]["display_name"] == "bob"
    assert rows[1]["mean_clv_pp"] < 0


def test_limit_caps_results(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "LEADERBOARD_MIN_TRADES", 1)
    for i in range(10):
        _add_optin(f"u{i}", f"user_{i}")
        _add_trade(f"u{i}", pnl=50, clv_pp=i)  # different CLV per user
    rows = sd._compute_clv_leaderboard(limit=3)
    assert len(rows) == 3


# ── Endpoints ───────────────────────────────────────────────────────────────

def test_leaderboard_endpoint_anonymous_readable(tmp_path, monkeypatch):
    """No auth required to GET — public conversion surface."""
    _setup_isolated_db(tmp_path, monkeypatch)
    # In DEV_MODE the test client auth-resolves; the endpoint itself
    # doesn't check user, so this still validates the contract.
    r = _client().get("/api/leaderboard/clv")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body


def test_optin_get_returns_null_when_not_joined(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().get("/api/leaderboard/optin")
    assert r.status_code == 200
    assert r.json()["optin"] is None


def test_optin_put_join_and_get(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    r = c.put("/api/leaderboard/optin", json={"display_name": "alice"})
    assert r.status_code == 200
    body = c.get("/api/leaderboard/optin").json()
    assert body["optin"]["display_name"] == "alice"


def test_optin_put_rejects_invalid_name(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    # Server strips outer whitespace for UX, so " padded " is valid;
    # but invalid characters anywhere, names < 2 chars, or names > 30
    # chars must be rejected.
    bad_names = ["", "a", "x"*40, "name!with!bangs", "@admin", "💀ghost"]
    for n in bad_names:
        r = c.put("/api/leaderboard/optin", json={"display_name": n})
        assert r.status_code == 400, f"expected rejection for {n!r}"


def test_optin_put_rejects_duplicate_name(tmp_path, monkeypatch):
    """Case-insensitive uniqueness prevents impersonation."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_optin("other-user", "Alice")
    r = _client().put("/api/leaderboard/optin", json={"display_name": "alice"})
    assert r.status_code == 409


def test_optin_put_can_rename_own_entry(tmp_path, monkeypatch):
    """A user can update their own display_name in place."""
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    c.put("/api/leaderboard/optin", json={"display_name": "alice"})
    r = c.put("/api/leaderboard/optin", json={"display_name": "alice2"})
    assert r.status_code == 200
    assert c.get("/api/leaderboard/optin").json()["optin"]["display_name"] == "alice2"


def test_optin_delete_leaves_board(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    c.put("/api/leaderboard/optin", json={"display_name": "alice"})
    c.request("DELETE", "/api/leaderboard/optin")
    assert c.get("/api/leaderboard/optin").json()["optin"] is None
