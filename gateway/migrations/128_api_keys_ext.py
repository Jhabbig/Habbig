"""Extend the existing api_keys table for the public developer API.

Adds:
  - api_keys.scopes — comma-separated scope list ('read' default; 'read,write'
    unlocks POST /api/public/v1/predictions). Kept as TEXT + split-on-comma
    instead of a proper JSON column because sqlite's json1 is optional on
    some distros we've seen in staging and the scope surface is tiny.
  - api_usage_hourly — (api_key_id, hour_bucket) → request_count rollup.
    The public API middleware UPSERTs this row on every validated request
    and rejects when request_count exceeds api_keys.rate_limit_hour.
    hour_bucket is a UNIX timestamp truncated to the hour (``now - now % 3600``)
    so range queries stay integer-only.

Additive only — every ALTER is guarded so this migration is safe to re-run.
Migration 014 created the base api_keys table; 128 just annotates it.
"""

revision = "128"
down_revision = "127"


def _columns(c, table):
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    cols = _columns(c, "api_keys")
    if "scopes" not in cols:
        c.execute(
            "ALTER TABLE api_keys ADD COLUMN scopes TEXT NOT NULL DEFAULT 'read'"
        )

    c.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_hourly (
            api_key_id    INTEGER NOT NULL,
            hour_bucket   INTEGER NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (api_key_id, hour_bucket)
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_usage_bucket "
        "ON api_usage_hourly(hour_bucket)"
    )


def downgrade(c):
    # Additive-only. Matching convention from migrations 003/006/022 —
    # we never drop a column because another deploy might still be reading it.
    pass
