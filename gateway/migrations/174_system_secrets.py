"""System-wide encrypted secret storage.

Tiny key/value table for global secrets that admins can rotate from
the UI — currently used by Signal Search to hold the X (Twitter)
bearer token, but generic enough for any future "one global secret per
key" use case (e.g. an outbound webhook signing key, an LLM provider
override token).

Why a dedicated table:
  * Existing Kalshi/Polymarket credentials live per-user in
    ``user_credentials`` — wrong shape for global app secrets.
  * App-config env vars are immutable at process start; storing here
    lets a super-admin rotate without an SSH + restart.
  * Values are encrypted at rest with the same Fernet key
    (``CREDENTIALS_ENCRYPTION_KEY``) used everywhere else, so the DB
    file leaking doesn't leak the token.

Schema is intentionally minimal:
  * ``key``        — short identifier, e.g. ``"signal_search.x_bearer"``
  * ``value_enc``  — Fernet ciphertext (never plaintext)
  * ``updated_at`` — unix seconds; surfaces "set 14 days ago" in UI
  * ``updated_by`` — admin user_id (nullable for migrations / cron sets)

Look-ups are by exact key — no LIKE / range scans — so a plain PK
index is enough.
"""

from __future__ import annotations


revision = "174"
down_revision = "173"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS system_secrets (
            key        TEXT PRIMARY KEY,
            value_enc  TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            updated_by INTEGER,
            FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )


def downgrade(cur) -> None:
    cur.execute("DROP TABLE IF EXISTS system_secrets")
