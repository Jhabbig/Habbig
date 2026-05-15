# Changelog + Docs Page Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_changelog*.py gateway/tests/test_docs*.py --tb=line -q -p no:logging
```

> Note: No `gateway/tests/test_docs*.py` files exist. The closest match is
> `gateway/tests/test_api_docs.py` (OpenAPI / Swagger docs page tests), which
> was included in this run.

## Result

**60 passed, 0 failed** (2.47s)

## Per-file breakdown

| File | Passed | Failed | Time |
|---|---|---|---|
| `gateway/tests/test_changelog.py` | 23 | 0 | 0.64s |
| `gateway/tests/test_changelog_widget.py` | 20 | 0 | 2.12s |
| `gateway/tests/test_api_docs.py` | 17 | 0 | 0.81s |
| **Total** | **60** | **0** | **2.47s** |

## Notes

- All tests green on first run.
- Numerous `Duplicate Operation ID` `UserWarning`s emitted by FastAPI's
  OpenAPI generator during `test_api_docs.py` (duplicate route IDs across
  admin/security modules). Non-fatal — schema still builds — but worth
  cleaning up. Suppressed in the verification re-runs above via
  `-W ignore::UserWarning`.
- One `PytestDeprecationWarning` from pytest-asyncio about
  `asyncio_default_fixture_loop_scope` being unset. Cosmetic.
