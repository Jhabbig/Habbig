# Status Endpoint Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_status*.py gateway/tests/test_incident*.py -q -p no:logging 2>&1 | tail -30
```

## Missing test files

No files match `gateway/tests/test_incident*.py` — pattern returns zero files.

The command as-given fails in zsh with `no matches found: gateway/tests/test_incident*.py` because zsh aborts on unmatched globs. Re-ran with `setopt NULL_GLOB` so the unmatched pattern is dropped and the rest of the command runs against `test_status*.py`.

## Results for existing files

Files matched by `gateway/tests/test_status*.py`:

- `gateway/tests/test_status_admin.py`
- `gateway/tests/test_status_monitoring.py`
- `gateway/tests/test_status_page.py`

```
......................................                                   [100%]
=============================== warnings summary ===============================
tests/test_status_page.py::TestPublicStatusPage::test_status_page_has_subscribe_form
  <string>:5: RuntimeWarning: coroutine 'send_email_job' was never awaited
tests/test_status_monitoring.py::TestCheckServiceHealth::test_recovery_auto_resolves_incident
  RuntimeWarning: coroutine 'send_email_job' was never awaited

38 passed, 2 warnings in 6.12s
```

## Counts

- **Passed:** 38
- **Failed:** 0
- **Missing files:** 1 pattern (`test_incident*.py` — no matches)
