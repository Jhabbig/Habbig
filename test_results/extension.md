# Extension test run

Command: `python3 -m pytest gateway/tests/test_extension*.py -q -p no:logging 2>&1 | tail -30`

Result: no test files matched the pattern `gateway/tests/test_extension*.py`.

Pytest output (tail -30):

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_extension*.py


no tests ran in 0.04s
```

Counts: passed=0, failed=0, errors=0, collected=0 (no files matched glob).
