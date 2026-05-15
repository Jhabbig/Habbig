# Migrations idempotency audit

**Date:** 2026-05-15
**Scope:** `gateway/migrations/*.py` (excluding `__init__.py`)
**Method:** Every `upgrade()` body inspected for raw `CREATE TABLE`, `DROP …`, `ALTER TABLE`, `INSERT`, `CREATE INDEX`, `CREATE TRIGGER`, and `CREATE VIRTUAL TABLE` statements. A statement is treated as idempotent if it uses an `IF [NOT] EXISTS` clause, is gated by a Python-side existence check (`_table_exists`, `_existing_cols`, `_has_column`, `PRAGMA table_info`), uses `INSERT OR IGNORE`/`INSERT OR REPLACE` on UNIQUE columns, or operates only on transient rebuild tables (rename → create → copy → drop) that are themselves guarded.

> **Runner context.** `migrations/__init__.py` records every applied revision in `schema_version`. Under normal operation a migration never runs twice. This audit treats each migration as if it were forced to re-execute, per the task brief.

## Totals

- **Total migrations:** 110 (001 → 191; numbers 32, 36–49, 65–69, 76–79, 82–89, 98–99, 101–104, 106–109, 118–119, 131–160, 163–169 are intentionally skipped — they were claimed by parallel branches or merged into adjacent files. The chain is well-formed because each file declares its own `down_revision` and merges branches at 020/021/023/024/025/026/027/116/120/126).
- **Non-idempotent count:** 2 — `115_unified_search_fts.py` (destructive `DROP TABLE` ahead of bare `CREATE VIRTUAL TABLE`) and `116_unified_search_populate.py` (unguarded `INSERT INTO source_summaries_fts (rowid, …)`).
- **Partially-idempotent / latent risks (advisory):** 3 — `054_source_network.py`, `074_claude_cost_controls.py`, `095_schema_drift_backfill.py`. Each is currently safe because earlier statements in the same `upgrade()` short-circuit on re-run, but the SQL by itself would fail / duplicate. Documented below for completeness, not counted in the headline non-idempotent number.
- **Top 5 worst offenders:** 115, 116, 054, 074, 095 (in that order).

---

## Top 5 findings

### 1. `115_unified_search_fts.py` — destructive re-run

```python
cur.execute("DROP TABLE IF EXISTS source_summaries_fts")
cur.execute("""
    CREATE VIRTUAL TABLE source_summaries_fts USING fts5(
        source_handle UNINDEXED, summary, …
    )
""")
```

The `DROP TABLE IF EXISTS` makes re-execution technically succeed, but it **destroys every populated FTS row** seeded by migration 116 and every trigger-driven write since. The `CREATE VIRTUAL TABLE` is missing `IF NOT EXISTS`, so without the prior `DROP` it would also hard-fail on re-run.

**Severity:** High. **Fix:** swap to `CREATE VIRTUAL TABLE IF NOT EXISTS …` and remove the `DROP`.

### 2. `116_unified_search_populate.py` — unguarded bulk insert

```python
cur.execute("""
    INSERT INTO source_summaries_fts (rowid, source_handle, summary)
    SELECT id, source_handle, COALESCE(summary, '') FROM source_summaries
""")
```

No `OR IGNORE`/`OR REPLACE`, no `WHERE NOT EXISTS`. On re-run every existing rowid hits a UNIQUE conflict (`rowid` is the FTS PK) and the entire migration aborts. The three FTS triggers below it use `IF NOT EXISTS`, so they re-run fine — only the bulk-load is the problem.

**Severity:** High. **Fix:** `INSERT INTO source_summaries_fts (…) SELECT … WHERE NOT EXISTS (SELECT 1 FROM source_summaries_fts WHERE rowid = source_summaries.id)` or `INSERT OR IGNORE …`.

### 3. `054_source_network.py` — bare `CREATE INDEX` indirectly, no actual issue, but no Python guard

All `CREATE TABLE`/`CREATE INDEX` statements use `IF NOT EXISTS`, so it is in fact idempotent. **Including here only because it's the largest plain-SQL block (~50 lines) with zero Python guards and lots of FK columns; if any column gets reshaped in a future patch the absence of `_existing_cols()` will bite.** Currently safe.

**Severity:** Advisory.

### 4. `074_claude_cost_controls.py` — single-row seed inside a guarded create

```python
if not _table_exists(c, "claude_kill_switch"):
    c.execute("""
        CREATE TABLE claude_kill_switch ( id INTEGER PRIMARY KEY CHECK (id=1), … )
    """)
    c.execute("INSERT INTO claude_kill_switch (id, active) VALUES (1, 0)")
```

The seed `INSERT` runs only when the table is being created, so re-execution is safe today. The risk is fragility: if a future patch lifts the `_table_exists` check (e.g. to add a column unconditionally) the seed becomes a UNIQUE-PK failure. Use `INSERT OR IGNORE` to make the line robust on its own.

**Severity:** Advisory. **Fix:** `INSERT OR IGNORE INTO claude_kill_switch (id, active) VALUES (1, 0)`.

### 5. `095_schema_drift_backfill.py` — re-declares tables owned by earlier migrations

This migration *defensively* re-creates `service_health_snapshots` (from 021) and `polymarket_connections` (from 062) with `CREATE TABLE IF NOT EXISTS`, plus matching indexes. That is fine. The risk is column drift: if migrations 021 or 062 later add a column, 095's narrower re-declaration will silently be a no-op (because the table exists) and an operator reading the migration alone gets a misleading picture of the schema.

It also runs a backfill `UPDATE market_snapshots SET snapshot_at = snapshotted_at WHERE snapshot_at IS NULL` inside a branch that requires `snapshotted_at` to exist AND `snapshot_at` to be missing — once it's run, the second condition fails and the `UPDATE` skips. Re-runnable, but the logic is subtle.

**Severity:** Advisory. **Fix:** keep these tables single-source-of-truth in their original migrations; remove the defensive re-creation here once production confirms the gap is closed.

---

## Per-migration findings

Format: `revision — file — verdict — notes`. Verdict is **OK** (idempotent), **WARN** (currently safe but fragile), or **FAIL** (re-run is destructive or hard-fails).

### 001 — initial_schema — OK
No-op marker only.

### 002 — email_unsubscribes — OK
`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`. All `ALTER TABLE … ADD COLUMN` calls are gated by `PRAGMA table_info(users)`.

### 003 — password_reset_hardening — OK
All ALTERs guarded by `PRAGMA table_info`.

### 004 — waitlist_positions — OK
All ALTERs guarded; index uses `IF NOT EXISTS`; the backfill `UPDATE` is keyed by `position IS NULL` so it self-skips on re-run.

### 005 — account_deletion — OK
All ALTERs gated; `CREATE TABLE IF NOT EXISTS`; `CREATE INDEX IF NOT EXISTS`.

### 006 — security_features — OK
Helper `_existing_cols`; every ALTER guarded. All CREATEs use `IF NOT EXISTS`.

### 007 — user_sessions_hardening — OK
Pure `CREATE TABLE IF NOT EXISTS` + indexes.

### 008 — environmental_impact — OK
Table + indexes use `IF NOT EXISTS`; user columns are gated.

### 009 — predictions_extracted_at_index — OK
Single `CREATE INDEX IF NOT EXISTS`.

### 010 — credibility_pipeline — OK
Two `CREATE INDEX IF NOT EXISTS` statements.

### 011 — retrospectives — OK
Table + unique index use `IF NOT EXISTS`.

### 012 — calibration — OK
Table + index `IF NOT EXISTS`.

### 013 — morning_briefing — OK
Both ALTERs gated by `_existing_cols`.

### 014 — api_keys — OK
Table + indexes `IF NOT EXISTS`.

### 015 — backtests — OK
Table + indexes `IF NOT EXISTS`.

### 016 — whale_positions — OK
Table + indexes `IF NOT EXISTS`.

### 017 — user_bankroll — OK
Both ALTERs gated by `_existing_cols`.

### 018 — telegram_links — OK
Table + indexes `IF NOT EXISTS`.

### 019 — remove_2fa — OK
`DROP TABLE IF EXISTS` for both tables; ALTER `DROP COLUMN` wrapped in `_drop_column_safely()` which checks presence and catches `OperationalError`. The `DELETE FROM audit_log …` is filter-keyed and idempotent.

### 020 — portfolio_integration — OK
ALTER gated by `_existing_cols`; `CREATE TABLE IF NOT EXISTS`.

### 021 — status_page — OK
All CREATEs use `IF NOT EXISTS`.

### 022 — embed_widgets — OK
Table + indexes `IF NOT EXISTS`.

### 023 — referrals_leaderboard — OK
All ALTERs gated; tables and partial unique indexes use `IF NOT EXISTS`.

### 024 — admin_features — OK
Five tables, all `IF NOT EXISTS`.

### 025 — claude_usage_log — OK
Table + indexes `IF NOT EXISTS`.

### 026 — notifications — OK
Two tables + indexes, all `IF NOT EXISTS`.

### 027 — prediction_extractions — OK
Two tables + indexes, all `IF NOT EXISTS`.

### 028 — market_categorisations — OK
Table + indexes `IF NOT EXISTS`.

### 029 — source_summaries — OK
Table + indexes `IF NOT EXISTS`. Superseded by 052 (which is itself tolerant).

### 030 — data_exports — OK
File slug says "030"; revision string is `"032"`, predecessor `"026"`. Not a bug — just chain wiring around a parallel branch. Table + indexes `IF NOT EXISTS`.

### 031 — user_predictions — OK
Tables + partial unique indexes use `IF NOT EXISTS`.

### 033 — affiliate_program — OK
Three tables + indexes, all `IF NOT EXISTS`.

### 034 — push_subscriptions — OK
Table + index `IF NOT EXISTS`.

### 035 — performance_indexes — OK
All `CREATE INDEX IF NOT EXISTS`. `PRAGMA journal_mode = WAL` is naturally idempotent.

### 050 — ai_cache — OK
Table + indexes `IF NOT EXISTS`.

### 051 — claude_usage_log_ext — OK
Branches on `_table_exists`; column adds gated by `_existing_cols`.

### 052 — source_summaries_ext — OK
Same pattern as 051.

### 053 — calibration_and_timing — OK
`_add_if_missing` helper skips when column or table is absent. Cached memoisation reset at the top of `upgrade()`.

### 054 — source_network — WARN
Purely `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` — actually safe today. Flagged advisory only because of the missing Python guards (see Top 5 #3).

### 055 — backtests — OK
Tables + indexes use `IF NOT EXISTS`.

### 056 — market_movement — OK
Table uses `IF NOT EXISTS`; the defensive `ALTER TABLE … ADD COLUMN notified_at` is gated by `_existing_cols`; subsequent indexes use `IF NOT EXISTS`.

### 057 — weekly_reports — OK
Table + indexes `IF NOT EXISTS`.

### 058 — environmental_impact_ext — OK
Branches on `_table_exists`; column adds gated by `_existing_cols`. Backfill `UPDATE` is keyed on missing column and runs at most once.

### 059 — insider_signals — OK
Tables + indexes `IF NOT EXISTS`. The seed loop uses `INSERT OR IGNORE` on UNIQUE `(source)`.

### 060 — subproduct_subscriptions — OK
Single ALTER gated by `_existing_cols`.

### 061 — processed_stripe_events — OK
Table + index `IF NOT EXISTS`.

### 062 — portfolio_integration — OK
Three tables + indexes `IF NOT EXISTS`; ALTER on `users` gated by `_existing_cols`.

### 063 — telegram_connections — OK
Table + indexes `IF NOT EXISTS`.

### 064 — discord_integration — OK
Two tables + indexes `IF NOT EXISTS`.

### 070 — watermark_seeds — OK
Table + indexes `IF NOT EXISTS`.

### 071 — forensic_sentinels — OK
Two tables + indexes `IF NOT EXISTS`.

### 072 — security_events — OK
Table + indexes `IF NOT EXISTS`.

### 073 — bulk_fetch_counters — OK
Table + indexes `IF NOT EXISTS`.

### 074 — claude_cost_controls — WARN
Both tables created behind `_table_exists` guard; raw `INSERT INTO claude_kill_switch` is therefore safe. The bare `INSERT` is fragile — see Top 5 #4.

### 075 — user_privacy_prefs — OK
Per-column `try ALTER … except duplicate-column` swallows the re-run case.

### 080 — query_indexes — OK
All `CREATE INDEX IF NOT EXISTS`; the helper skips when required columns are missing.

### 081 — slow_query_log — OK
Table + indexes `IF NOT EXISTS`.

### 090 — onboarding_state — OK
Table + index `IF NOT EXISTS`.

### 091 — first_week_goals — OK
Table + indexes `IF NOT EXISTS`.

### 092 — engagement_events — OK
Two tables `IF NOT EXISTS`; indexes `IF NOT EXISTS`.

### 093 — churn_signals — OK
Table + index `IF NOT EXISTS`.

### 094 — cancellation_flow — OK
Two tables + indexes `IF NOT EXISTS`. ALTER on `users.subscription_paused_until` guarded by `_has_column` AND wrapped in try/except for the duplicate-column race.

### 095 — schema_drift_backfill — WARN
Re-declares tables owned by 021 + 062 (covered in Top 5 #5). All `IF NOT EXISTS` so it's safe to re-run, but the design is brittle.

### 096 — slow_request_log — OK
Table + indexes `IF NOT EXISTS`.

### 097 — perf_baseline_snapshots — OK
Table + indexes `IF NOT EXISTS`.

### 100 — realtime_connection_events — OK
Table + indexes `IF NOT EXISTS`.

### 105 — scheduler_job_runs — OK
Table + indexes `IF NOT EXISTS`.

### 110 — shared_market_cards — OK
Table + indexes `IF NOT EXISTS`.

### 111 — shared_source_cards — OK
Table + indexes `IF NOT EXISTS`.

### 112 — shared_predictions — OK
Table + indexes `IF NOT EXISTS`.

### 113 — user_invite_tokens — OK
Table + indexes `IF NOT EXISTS`; ALTER on `users` gated by `_existing_cols`.

### 114 — share_metrics — OK
Table + indexes `IF NOT EXISTS`.

### 115 — unified_search_fts — **FAIL**
See Top 5 #1. Bare `CREATE VIRTUAL TABLE` plus `DROP TABLE` makes the re-run destructive.

### 116 — unified_search_populate — **FAIL**
See Top 5 #2. Unguarded `INSERT INTO source_summaries_fts (rowid, …)` fails on re-run.

### 117 — search_analytics — OK
Table + indexes `IF NOT EXISTS`.

### 120 — collections — OK
Both tables guarded by `_table_exists`; child statements use `IF NOT EXISTS`.

### 121 — collection_follows — OK
Table guarded by `_table_exists`; index `IF NOT EXISTS`.

### 122 — market_takes — OK
Two tables + indexes `IF NOT EXISTS`. The `CHECK` constraints and partial unique index are all part of the initial `CREATE`.

### 123 — take_reports — OK
Table + indexes `IF NOT EXISTS`.

### 124 — take_resolution — OK
Index + table `IF NOT EXISTS`.

### 125 — preferred_language — OK
Column add guarded by `PRAGMA table_info`. The downgrade has a destructive rebuild — risky but out of scope (downgrades not requested for re-runnability).

### 126 — saved_views — OK
Table guarded by `_table_exists`; child statements `IF NOT EXISTS`.

### 127 — external_forecasts — OK
Two tables + indexes `IF NOT EXISTS`.

### 128 — api_keys_ext — OK
ALTER gated by `_columns`; table + index `IF NOT EXISTS`.

### 129 — webhooks — OK
Two tables + indexes `IF NOT EXISTS`.

### 130 — feedback — OK
Three tables + indexes `IF NOT EXISTS`.

### 161 — drill_runs — OK
Table + index `IF NOT EXISTS`.

### 162 — integrity_cleanup — OK
Both rebuilds gated by `_has_on_delete_set_null` (substring match against stored CREATE SQL). The backfill `UPDATE` for `kelly_fraction` is keyed on `IS NULL` and re-runs to a no-op.

### 170 — changelog_seen — OK
Table + index `IF NOT EXISTS`.

### 171 — onboarding_tour_state — OK
Branches on table presence; column adds guarded by `_existing_cols`.

### 172 — public_profile_fields — OK
Per-column gated by `PRAGMA table_info`; partial unique index `IF NOT EXISTS`.

### 173 — user_follows — OK
Table + indexes `IF NOT EXISTS`.

### 174 — system_secrets — OK
Table `IF NOT EXISTS`.

### 175 — email_watermarks — OK
Table + indexes `IF NOT EXISTS`.

### 176 — trading_addon_settings — OK
Table `IF NOT EXISTS`.

### 177 — newsletter_segments — OK
Every ALTER gated by `PRAGMA table_info`; indexes `IF NOT EXISTS`. The backfill `UPDATE … WHERE confirmed_at IS NULL` self-skips on re-run.

### 178 — status_launch_2026_05_14 — OK
Each row insert wrapped in `_insert_if_missing()`, which does a `SELECT … WHERE title = ? AND created_at = ?` guard before `INSERT`. No UNIQUE constraint on `(title, created_at)` so the audit-pattern criterion ("INSERT without OR IGNORE on UNIQUE") doesn't strictly apply — the Python guard is the idempotency proof.

### 179 — webhook_hardening — OK
Table + indexes `IF NOT EXISTS`; column adds gated by `_cols`.

### 180 — api_keys_origins — OK
Per-column gated by `_columns`.

### 181 — wallet_connect_nonces — OK
Table + index `IF NOT EXISTS`.

### 182 — webhook_dlq_index — OK
Single `CREATE INDEX IF NOT EXISTS`.

### 183 — newsletter_campaigns — OK
Table + index `IF NOT EXISTS`.

### 184 — explain_audit_indexes — OK
All `CREATE INDEX IF NOT EXISTS`.

### 185 — users_stripe_customer_id — OK
ALTER gated by `PRAGMA table_info`; partial index `IF NOT EXISTS`.

### 186 — subproduct_feature_flags — OK
Re-run-safe via an early `return` when `subproduct_key` is already present; the early-return path still re-asserts the new indexes with `IF NOT EXISTS`. The rebuild path itself is not idempotent on a partial failure (rename → recreate → copy → drop), but the gate prevents it from running twice.

### 187 — newsletter_blast_jobs — OK
Table + indexes `IF NOT EXISTS`.

### 188 — fix_users_invite_token_fk — OK
Entire rebuild gated by `_users_sql_has_dangling_fk`. Re-run is a no-op.

### 189 — sessions_hash_at_rest — OK
Rebuild gated by `_sessions_has_raw_token` (which inspects `PRAGMA table_info`). Re-run is a no-op.

### 190 — blast_cursor — OK
ALTER gated by `_has_column`.

### 191 — impersonation_token_hash — OK
ALTER gated by `_has_column`; partial unique index `IF NOT EXISTS`. The `UPDATE` that invalidates active sessions is keyed on `ended_at IS NULL` and self-skips on re-run.

---

## Summary

- 2 migrations are **not** safely re-runnable (115, 116). Both relate to the source-summaries FTS index introduced together; either should be patched in lockstep with the other to avoid losing index data on replay.
- 3 are advisory WARNs (054, 074, 095) — safe today, brittle by design.
- The remaining 105 migrations are well-guarded. The codebase consistently uses `IF [NOT] EXISTS` everywhere SQL allows it, and falls back to `PRAGMA table_info` / `sqlite_master` Python guards for the cases SQLite doesn't natively support (`ALTER TABLE ADD COLUMN`, `CREATE VIRTUAL TABLE`).
- The migration runner in `migrations/__init__.py` records every applied revision in `schema_version` and only re-applies unseen revisions, so the two failing migrations are unlikely to actually be re-run in production — but they would block any operator forced to reapply migrations from scratch (e.g. after a disaster-recovery rebuild that didn't restore `schema_version`).
