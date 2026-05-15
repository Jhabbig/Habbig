# Collections Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_collections*.py --tb=line -q -p no:logging
```

## Glob expansion

The shell glob `gateway/tests/test_collections*.py` matches exactly one
file in the repo:

- `gateway/tests/test_collections.py`

## Results

```
.................................                                        [100%]
33 passed in 12.87s
```

- `gateway/tests/test_collections.py` — passed

## Counts

- **Passed:** 33
- **Failed:** 0
</content>
</invoke>