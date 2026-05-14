"""Extend api_keys with origin allowlist + usage_count for the embed API.

The base api_keys table was created in migration 014 (id, key_hash,
key_prefix, user_id, name, tier, rate_limit_hour, created_at,
last_used_at, revoked_at). Migration 128 added `scopes`. This migration
adds the two remaining columns the embed-API key management UI relies
on:

  - allowed_origins TEXT NULL — comma-separated host list. NULL/empty
    means "no origin restriction" (open key). When set, an incoming
    request whose Origin or Referer host doesn't match one of the
    listed bare-hostnames is rejected with 403 by
    queries.api_keys.validate_api_key.

  - usage_count INTEGER NOT NULL DEFAULT 0 — lifetime counter, bumped
    on every successful validate_api_key() call. Distinct from the
    per-hour bucket in api_usage_hourly (migration 128) which is used
    for rate limiting; usage_count surfaces "total calls made by this
    key" on the settings page without a sum over buckets.

Numbered 181 because revisions 179 and 180 were claimed by parallel
work (wallet-connect nonces and webhook hardening) — this migration
sequences after them.

Additive only — every ALTER is guarded, so this migration is safe to
re-run. We deliberately do NOT drop or recreate the existing table —
production has live key hashes that other deploys still validate.
"""

from __future__ import annotations


revision = "180"
down_revision = "179"


def _columns(c, table):
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c) -> None:
    cols = _columns(c, "api_keys")
    if "allowed_origins" not in cols:
        c.execute(
            "ALTER TABLE api_keys ADD COLUMN allowed_origins TEXT"
        )
    if "usage_count" not in cols:
        c.execute(
            "ALTER TABLE api_keys ADD COLUMN usage_count INTEGER NOT NULL DEFAULT 0"
        )


def downgrade(c) -> None:
    # Additive-only. Matching convention from migrations 003/006/022/128 —
    # we never drop a column because another deploy might still be
    # reading it.
    pass
