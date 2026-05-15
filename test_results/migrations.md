# Migration Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_migration_188.py gateway/tests/test_migrations*.py -k "migration" --tb=line -q -p no:logging
```

## Summary

- **Passed:** 13
- **Failed:** 0
- **Total:** 13
- **Duration:** 0.69s
- **Status:** All passing

## Files

- `gateway/tests/test_migration_188.py` — 6 tests
- `gateway/tests/test_migrations.py` — 7 tests

## Tests

### `test_migration_188.py`
- `test_dangling_fk_is_detected`
- `test_insert_fails_before_migration`
- `test_migration_fixes_fk_and_preserves_rows`
- `test_insert_works_after_migration`
- `test_migration_is_idempotent`
- `test_migration_skips_clean_db`

### `test_migrations.py`
- `TestMigrationDiscovery::test_discover_finds_all_migrations`
- `TestMigrationDiscovery::test_migrations_are_sorted`
- `TestUpgradeToHead::test_idempotent`
- `TestUpgradeToHead::test_migration_002_creates_unsubscribe_table`
- `TestUpgradeToHead::test_migration_004_adds_waitlist_columns`
- `TestUpgradeToHead::test_migration_005_adds_deletion_fields`
- `TestUpgradeToHead::test_schema_version_table_populated`

## Raw output

```
.............                                                            [100%]
13 passed in 0.69s
```
