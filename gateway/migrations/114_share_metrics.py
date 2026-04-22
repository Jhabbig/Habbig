"""Analytics rollup for the public share surface.

Each view of /s/m/, /s/s/, or /s/p/ writes a row here. The admin page
(/admin/sharing) aggregates these into: shares-per-type, top-shared
items, signup conversion rate, top sharers.

Fields:
  * ``share_id`` is the integer PK of the matching row in
    shared_market_cards / shared_source_cards / shared_predictions.
    We don't FK because share_type varies — sqlite would need three
    nullable FK columns to enforce it. Enforce at insert time in the
    route handler instead.
  * ``referrer`` is the Host portion of the HTTP Referer header,
    bucketed coarsely ('twitter' / 'linkedin' / 'slack' / 'direct').
    The full URL isn't stored — that's a privacy leak with no analytic
    value.
  * ``viewer_country`` pulls from the ``CF-IPCountry`` header
    (Cloudflare injects it automatically on the edge). NULL when the
    request didn't go through Cloudflare, which is fine.
  * ``signed_up`` + ``signed_up_user_id`` are filled in asynchronously
    when a viewer's signup flow completes. The wiring lives in
    ``db_sharing.link_share_to_signup(share_metric_id, user_id)``.
"""

from __future__ import annotations

import sqlite3


revision = "114"
down_revision = "113"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS share_metrics (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            share_type           TEXT NOT NULL,
            share_id             INTEGER NOT NULL,
            referrer             TEXT,
            viewer_country       TEXT,
            viewed_at            INTEGER NOT NULL,
            signed_up            INTEGER NOT NULL DEFAULT 0,
            signed_up_user_id    INTEGER
                                 REFERENCES users(id) ON DELETE SET NULL,
            signed_up_at         INTEGER
        )
        """
    )
    # Time-range sweeps: last-7-days / last-30-days / today, always
    # sorted by viewed_at DESC.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_share_metrics_type_time "
        "ON share_metrics(share_type, viewed_at DESC)"
    )
    # Conversion-rate lookups group by share_type + signed_up — the
    # partial index keeps the b-tree scoped to converted rows so the
    # admin dashboard's "how many became subs?" tile is cheap.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_share_metrics_converted "
        "ON share_metrics(share_type, viewed_at DESC) WHERE signed_up = 1"
    )
    # Top-sharers reporting needs a join back into the share-type table
    # via (share_type, share_id); keep it cheap with this composite.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_share_metrics_share "
        "ON share_metrics(share_type, share_id)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS share_metrics")
