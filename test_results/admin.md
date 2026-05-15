# Admin Endpoint Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_admin_routes.py gateway/tests/test_admin_jobs.py gateway/tests/test_admin_newsletter.py gateway/tests/test_admin_email_templates.py --tb=line -q -p no:logging
```

## Missing test files

Two of the four requested test files do not exist in the repo:

- `gateway/tests/test_admin_routes.py` — NOT FOUND
- `gateway/tests/test_admin_email_templates.py` — NOT FOUND

Running the full command as-given fails immediately with:
```
ERROR: file or directory not found: gateway/tests/test_admin_routes.py
```

## Results for existing files

Ran the two files that do exist:

```bash
python3 -m pytest gateway/tests/test_admin_jobs.py gateway/tests/test_admin_newsletter.py --tb=line -p no:logging
```

**Summary:** `23 passed, 28 warnings in 3.32s`

- `gateway/tests/test_admin_jobs.py` — passed
- `gateway/tests/test_admin_newsletter.py` — passed

## Counts

- **Passed:** 23
- **Failed:** 0
- **Missing files:** 2 (`test_admin_routes.py`, `test_admin_email_templates.py`)
