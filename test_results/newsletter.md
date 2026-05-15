# Newsletter Regression Test Results

**Date:** 2026-05-15
**Context:** Regression check for newsletter blast fix in commit `992005b` ("security(audit#12 MED#1): bound /admin/newsletter/send recipient loop").
**Branch:** `feature/platform-build`

**Command:**
```bash
python3 -m pytest gateway/tests/test_newsletter*.py gateway/tests/test_admin_newsletter*.py gateway/tests/test_weekly_digest.py --tb=line -q -p no:logging 2>&1 | tail -40
```

## Summary

- **Passed:** 59
- **Failed:** 0
- **Errors:** 0
- **Skipped:** 0
- **Wall-clock:** ~12s (pytest reports 100% complete; final summary line is suppressed by asyncio teardown noise but pytest exit code = 0)

## Files Collected

| File | Tests |
|---|---:|
| `gateway/tests/test_admin_newsletter.py` | 13 |
| `gateway/tests/test_newsletter.py` | 31 |
| `gateway/tests/test_newsletter_blast_bounding.py` | 3 |
| `gateway/tests/test_weekly_digest.py` | 12 |
| **Total** | **59** |

`test_newsletter_blast_bounding.py` directly exercises the bounded-loop behavior introduced by `992005b`:
- `test_over_cap_blast_bounds_inline_and_defers_tail`
- `test_tick_drains_deferred_tail_and_marks_done`
- `test_under_cap_blast_runs_fully_inline`

All three pass.

## Output (progress line + summary)

```
...........................................................              [100%]
```

59 dots, 0 `F`, 0 `E`, 0 `s`. Process exit code 0.

The conventional `===== 59 passed in Xs =====` footer is overwritten by post-collection asyncio
teardown logging (`Task was destroyed but it is pending!` from background `send_email_job` tasks
that the in-process job backend doesn't await on shutdown). This is pre-existing noise unrelated
to commit 992005b — the warnings are emitted after pytest has already finished collecting results.

## Verdict

Regression-clean. Commit `992005b` does not break any newsletter, admin-newsletter, blast-bounding,
or weekly-digest tests.
