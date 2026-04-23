# DB health audit — 2026-04-23

Scope: `gateway/auth.db` on the local working-tree (WAL mode, 2.4 MB,
283 users, 150 predictions, 12 scored sources). Production has strictly
more data than dev but the schema is identical.

## Part 1 — integrity checks

```
$ sqlite3 auth.db "PRAGMA integrity_check"   → ok
$ sqlite3 auth.db "PRAGMA foreign_key_check" → (empty, no violations)
$ sqlite3 auth.db "PRAGMA quick_check"       → ok
```

Clean. No action required.

## Part 2 — foreign key audit

80 tables total; 43 non-FTS tables; **36 foreign keys across 35 tables**
(dumped via `PRAGMA foreign_key_list`, see `scripts/db_fk_audit.sh`).

### Policy compliance

Rule: per-user data → `ON DELETE CASCADE`;
audit/security/payments → `ON DELETE SET NULL`;
reference data → `ON DELETE RESTRICT`.

| Class | Count | Expected | Result |
|---|---|---|---|
| Per-user (sessions, subscriptions, user_*, saved_*, followed_*, push_*, api_keys, …) | 27 | CASCADE | ✅ all 27 CASCADE |
| Admin-link columns (`approved_by_admin_id`, `commission_paid_by_admin_id`, `gifted_by_admin_id`, `revoked_by_admin_id`) | 4 | SET NULL | ✅ all 4 SET NULL |
| Audit-link columns (`analytics_events.user_id`, `feedback_submissions.user_id`, `affiliate_conversions.referred_user_id`) | 3 | SET NULL | ✅ all 3 SET NULL |
| Derived (`predictions_reextracted.original_prediction_id`) | 1 | SET NULL | ✅ SET NULL |
| `insider_market_correlations.signal_id → insider_signals.id` | 1 | CASCADE | ✅ CASCADE (correlation dies with signal) |

### Soft gaps (`NO ACTION`)

Two FK edges have `ON DELETE NO ACTION`, which in SQLite means "FAIL if
still referenced" — same as RESTRICT but without the explicit label.

1. `invite_tokens.claimed_by_user_id → users.id`
2. `users.invite_token_id → invite_tokens.id`

**Impact:** deleting a user who claimed an invite would fail the
transaction rather than orphaning the token. Similarly, deleting an
invite_tokens row would fail if any user points at it. Given both
edges are nullable, `SET NULL` would be a softer fit — delete the
user, keep the token row for audit with a tombstoned `claimed_by`.

**Decision:** document, don't migrate. Fixing this in SQLite requires
the full table-rebuild dance (new table + INSERT SELECT + DROP +
RENAME, rebuild indexes + FK triggers), which is high blast-radius
for a soft-issue and we rarely hard-delete users (`suspended`/
`deleted_at` soft flags are the norm). Candidate for migration 160
if the policy becomes urgent.

## Part 3 — orphan sweep

Queries run via `sqlite3` against every cross-table reference:

```
predictions missing source_credibility row      136
saved_predictions without prediction              0
saved_predictions without user                    0
user_predictions without user                     0
followed_sources without user                     0
subscriptions without user                        0
sessions without user                             0
intelligence_messages without conversation        0
```

### The 136 "orphan" predictions

All 136 come from **test fixtures** — handles like `test_mixed_src`,
`test_unlocked_src`, `test_old_src`, etc. — that live in the dev DB
because the test suite occasionally seeds against the local file
(typically via `_testdb` but some older tests INSERT directly).

This is not a schema bug. `predictions.source_handle` intentionally
has **no foreign key** to `source_credibility` because sources appear
in predictions before the nightly credibility scorer has computed a
row for them. The "orphan" is the normal pre-scoring state.

**Action:** none for prod. The dev fixtures could be cleaned up but
doing it here would fight whatever test is currently seeding them.

## Part 4 — duplicates

Six uniqueness checks, all clean:

```
dup users.email                              0
dup users.username                           0
dup source_credibility.source_handle         0
dup sessions.token                           0
dup invite_tokens.token                      0
dup subscriptions(user_id,dashboard_key)     0
```

`users.email`, `users.username`, and `invite_tokens.token` already
have explicit `UNIQUE` constraints in the schema; the remaining three
rely on application-layer UPSERT discipline which is working.

## Part 5 — WAL + checkpoint

```
auth.db        2.4 MB
auth.db-shm      32 KB
auth.db-wal       0 B
```

WAL is empty — prior checkpoint landed cleanly. Nothing to truncate
now. Periodic policy added via:

* `scripts/db_checkpoint.py` — `PRAGMA wal_checkpoint(TRUNCATE)`,
  wired to the APScheduler nightly cron (added in this batch).
* `scripts/db_optimize.py` — `PRAGMA optimize`, monthly cron.

## Part 6 — backup strategy

3-2-1 adapted for single-server. Scripts written to `scripts/` and
documented below; actual cron installation is an ops step for the
production host (`scripts/install_backup_cron.sh` does the wiring).

* **Hourly local snapshot** → `scripts/backup_hourly.sh`, retains 24.
* **Daily compressed + 30d retention** → `scripts/backup_daily.sh`.
* **Weekly encrypted offsite** → `scripts/backup_offsite.sh`, requires
  `BACKUP_GPG_RECIPIENT` + `BACKUP_OFFSITE_RSYNC_TARGET` env vars (documented
  in `.env.example`).
* **Verification** → `scripts/backup_verify.sh` picks latest daily,
  `gunzip` to `/tmp`, `PRAGMA integrity_check`; alerts on non-ok.

Backup directory layout on the server (not committed — created by the
install script):

```
/var/backups/narve/
├── auth.db.YYYYMMDD_HHMM      ← hourly, 24 files
└── daily/
    └── auth.db.YYYYMMDD.gz    ← daily, 30 files
```

## Part 7 — restore runbook

Added to `RUNBOOK.md` under "Restore from backup" — hourly, daily,
offsite procedures each with their data-loss tolerance window.

## Part 8 — /admin/backups

New admin page (via `admin_routes.py → backups_page`) surfaces:

* Latest hourly/daily/offsite file timestamp + size.
* Last verification run result.
* Age-based alerts — hourly > 2h stale or daily > 26h stale renders
  a warning card (aligned with the CSS token system, no dependencies).

## Part 9 — quarterly recovery drill

Migration **161** adds `drill_runs (id, started_at, completed_at,
integrity_ok, users_live, users_restore, notes)`. A new cron job
`recovery_drill` (registered every 90 days):

1. `.backup /tmp/drill_<ts>.db` from the live DB.
2. `PRAGMA integrity_check` + `PRAGMA foreign_key_check` on the copy.
3. `SELECT COUNT(*) FROM users` on both live and restore; compares.
4. Writes `drill_runs` row.
5. Removes the drill copy.

Divergence > 1% = alert via the notification pipeline.

## Summary

| Check | Status |
|---|---|
| `integrity_check` | ok |
| `foreign_key_check` | ok |
| FK `ON DELETE` policy | 34/36 compliant, 2 soft gaps (tracked) |
| Orphan sweep | 0 real (136 test fixtures) |
| Uniqueness | 0 duplicates across 6 constraints |
| WAL size | 0 B (healthy) |
| Hourly backup | script ready, cron pending install |
| Daily backup | script ready, cron pending install |
| Offsite weekly | script ready, env vars documented |
| Verification | script ready |
| Restore runbook | merged into RUNBOOK.md |
| `/admin/backups` | live behind admin guard |
| Quarterly drill | migration 161 + scheduled job |
