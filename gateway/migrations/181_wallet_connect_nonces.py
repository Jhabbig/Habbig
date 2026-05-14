"""SIWE (Sign-In With Ethereum, EIP-4361) nonce store for Polymarket wallet
connect.

Before this migration, ``POST /api/markets/connect/polymarket`` accepted
any 0x-prefixed 40-hex address — no cryptographic proof that the caller
controlled the private key. An attacker who guessed (or scraped) a
victim's wallet could attach it to their own narve.ai account and watch
the victim's positions in real time, harvest signal-following behaviour
through the portfolio feed, or seed a fake wallet to muddy /signals.

Fix: require the client to sign a short, audit-readable SIWE message
that pins (a) our domain, (b) Polymarket chain id, (c) a server-issued
nonce. The server recovers the signer from the signature and refuses
the connect if it doesn't match the posted address.

This table is the nonce ledger:

  * ``nonce``       — opaque 128-bit hex string handed out by the
                      ``/connect/polymarket/nonce`` endpoint. Primary
                      key so a second presentation of the same nonce
                      is a UNIQUE-constraint reject at insert time —
                      defence-in-depth on top of the application-level
                      ``used_at`` check.
  * ``user_id``     — who requested it. Binds the nonce to a session
                      so a leaked nonce can't be replayed against a
                      different account. CASCADE on user delete is fine
                      — pending nonces become meaningless if the user
                      is gone.
  * ``created_at``  — issuance time (unix seconds). Stale-nonce check
                      uses ``now - created_at > 300``.
  * ``used_at``     — nullable; set when the nonce is consumed by a
                      successful verify. NULL means "still valid".

Lookups are always (a) by PK on connect, (b) range scan on
``created_at`` for the cleanup job. The PK gives (a) free; we add an
index on ``created_at`` for (b) so the nightly DELETE doesn't seq-scan
the whole table once it has a few weeks of history.
"""

from __future__ import annotations


revision = "181"
down_revision = "180"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_connect_nonces (
            nonce      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            used_at    INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_nonces_created "
        "ON wallet_connect_nonces(created_at)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_wallet_nonces_created")
    cur.execute("DROP TABLE IF EXISTS wallet_connect_nonces")
