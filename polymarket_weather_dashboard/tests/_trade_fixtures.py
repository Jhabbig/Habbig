"""Shared fixtures for the Phase 2 trade-engine tests.

The engine modules accept a `conn_factory` callable so tests can pass an
in-memory SQLite connection without booting Flask. We replicate the
Phase 2 portion of `_SCHEMA` from server.py here — keeping the two in
sync is part of the test promise.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


PHASE2_SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_credentials (
    user_id      TEXT PRIMARY KEY,
    key_id       TEXT NOT NULL,
    ciphertext   TEXT NOT NULL,
    is_demo      INTEGER DEFAULT 0,
    label        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    disabled_at  TEXT
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    action                TEXT NOT NULL,
    qty                   INTEGER NOT NULL,
    type                  TEXT NOT NULL,
    limit_price_cents     INTEGER,
    status                TEXT NOT NULL DEFAULT 'accepted',
    filled_qty            INTEGER NOT NULL DEFAULT 0,
    avg_fill_price_cents  INTEGER,
    client_order_id       TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    order_id            INTEGER NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,
    action              TEXT NOT NULL,
    qty                 INTEGER NOT NULL,
    price_cents         INTEGER NOT NULL,
    realized_pnl_cents  INTEGER NOT NULL DEFAULT 0,
    filled_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    user_id           TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               INTEGER NOT NULL,
    avg_price_cents   INTEGER NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (user_id, ticker, side)
);

CREATE TABLE IF NOT EXISTS live_order_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    kalshi_order_id   TEXT,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    action            TEXT NOT NULL,
    qty               INTEGER NOT NULL,
    type              TEXT NOT NULL,
    limit_price_cents INTEGER,
    status            TEXT NOT NULL,
    http_status       INTEGER,
    client_order_id   TEXT,
    response_json     TEXT,
    ts                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS trade_user_limits (
    user_id              TEXT PRIMARY KEY,
    max_order_usd        REAL,
    max_daily_usd        REAL,
    max_open_positions   INTEGER,
    max_position_usd     REAL,
    daily_loss_limit_usd REAL,
    killed               INTEGER NOT NULL DEFAULT 0,
    kill_reason          TEXT,
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS trade_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT,
    ip_addr   TEXT,
    ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def make_in_memory_conn_factory():
    """Return ``(conn_factory, raw_conn)`` — a single in-memory SQLite
    connection wrapped to mimic the (readonly=False) protocol used by
    the production code. Single connection so all tables persist
    between calls within one test."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PHASE2_SCHEMA)
    lock = threading.Lock()

    @contextlib.contextmanager
    def factory(readonly=False):
        with lock:
            try:
                yield conn
                if not readonly:
                    conn.commit()
            except Exception:
                if not readonly:
                    conn.rollback()
                raise

    return factory, conn


def make_test_rsa_key() -> tuple[bytes, rsa.RSAPrivateKey]:
    """Generate a 2048-bit RSA private key + its PEM bytes for tests."""
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, pk
