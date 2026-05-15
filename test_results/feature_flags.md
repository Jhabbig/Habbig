# Feature Flag Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_feature*.py gateway/tests/test_flag*.py --tb=line -q -p no:logging
```

## Glob expansion

The shell glob `gateway/tests/test_flag*.py` matches no files in the repo
(no test files start with `test_flag`). zsh `nomatch` aborts the original
command before pytest runs. The two files that match `test_feature*.py`
are:

- `gateway/tests/test_feature_flags.py`
- `gateway/tests/test_feature_routes.py`

There are also `gateway/tests/test_claude_ai_features.py` and
`gateway/tests/test_user_features.py`, but those do not match the
`test_feature*` prefix glob and were excluded by the command as written.

## Results

Ran the two existing matched files explicitly:

```bash
python3 -m pytest gateway/tests/test_feature_flags.py gateway/tests/test_feature_routes.py --tb=line -p no:logging
```

**Summary:** `36 passed, 1 warning in 2.66s`

- `gateway/tests/test_feature_flags.py` — passed
- `gateway/tests/test_feature_routes.py` — passed

## Counts

- **Passed:** 36
- **Failed:** 0
- **Missing/unmatched globs:** `test_flag*.py` (no files match)
