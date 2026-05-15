# Scheduler + Jobs Test Run

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_scheduler*.py gateway/tests/test_jobs*.py gateway/tests/test_cron*.py --tb=line -q -p no:logging
```

## Glob resolution

The literal globs from the command resolved as follows:
- `gateway/tests/test_scheduler*.py` -> `test_scheduler.py`
- `gateway/tests/test_jobs*.py` -> **no matches** (file is named `test_job_queue.py`, singular)
- `gateway/tests/test_cron*.py` -> **no matches** (no cron test files exist)

Files actually executed (resolved from the spirit of the command — matching scheduler/jobs tests that exist):
- `gateway/tests/test_scheduler.py`
- `gateway/tests/test_admin_jobs.py`
- `gateway/tests/test_job_queue.py`

## Summary

- **Passed:** 13
- **Failed:** 2
- **Errors:** 10
- **Total:** 25

Outcome trail: `.........EEEEEEEEEEFF....`

## Failures

### `test_job_queue.py::TestJobRegistry::test_backend_is_in_process`
`RuntimeError: There is no current event loop in thread 'MainThread'.`
(asyncio/events.py:642 — pytest-asyncio loop teardown mismatch)

### `test_job_queue.py::TestJobRegistry::test_known_jobs_are_registered`
`RuntimeError: There is no current event loop in thread 'MainThread'.`
(same root cause)

## Errors (all setup-phase, same root cause)

All 10 `AdminJobsTestCase` tests errored during setup with:

```
sqlite3.OperationalError: table sessions has no column named token
```

Migrations logged as `already at head (107 applied)` — so the schema the test fixture expects (`sessions.token` column) is not produced by current migrations. Either the migration that adds `sessions.token` is missing, the test fixture is using a stale schema, or the column was renamed and the test wasn't updated.

Affected tests:
- `test_failed_run_surfaces_error_message`
- `test_page_admin_200`
- `test_page_rejects_anon`
- `test_page_rejects_non_admin`
- `test_page_renders_initial_stats`
- `test_refresh_api_failed_run_in_recent`
- `test_refresh_api_filters_by_job_name`
- `test_refresh_api_rejects_anon`
- `test_refresh_api_rejects_non_admin`
- `test_running_run_appears_in_running_section`

## Passing

All 9 `test_scheduler.py` tests and 4 of 6 `test_job_queue.py` tests passed.
