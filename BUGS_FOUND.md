# Bugs Found — bug-hunt loop log

CHANGELOG-style append-only log from focused bug-hunt iterations. Newest entries on top.

---

## 2026-05-16 09:54 — iteration 1

- **HIGH [gateway/migrations/199_background_jobs_send_email_index.py:36]** Migration 199 runs `CREATE INDEX … ON background_jobs(…)` but the `background_jobs` table is created lazily by `jobs/backend.py::_ensure_jobs_table()` rather than by any earlier migration. On a fresh DB the migration fails with `sqlite3.OperationalError: no such table: main.background_jobs`. In production this is masked because the index was hot-applied out-of-band before deploy (per the migration's own docstring), and `server.py` wraps `upgrade_to_head()` in a try/except that swallows the error. But: (a) every test process blew up at conftest import, and (b) any fresh install / staging DB / disaster-recovery rebuild will silently skip the index and slow `/admin/email-addresses` 5-10x. **Fix (deferred — file is in the no-touch list):** either call `_ensure_jobs_table()` from inside migration 199's `upgrade()` before the `CREATE INDEX`, or move the `CREATE TABLE background_jobs` itself into a proper migration (cleaner long-term). Test-side workaround applied in `tests/_testdb.py` this iteration to unblock the suite.

- **HIGH [gateway/admin_routes.py:3723]** Two functions both named `_fmt_ts` in the same module. The second definition (line 3723, added in commit `08b1bf2`) takes only one positional arg and shadows the original `_fmt_ts(ts, fmt="…")` at line 113. Result: every caller that passes a format string — `/admin/users` (lines 2439, 2440), `/admin/jobs` CSV export (lines 239, 749, 2967, 2975, 2983) — raises `TypeError: _fmt_ts() takes 1 positional argument but 2 were given` and returns HTTP 500. Caught by `test_admin_users` and `test_admin_jobs` tests. **Fixed this iteration:** renamed the shadowing function to `_fmt_email_addresses_ts` and updated its 4 internal callers.
