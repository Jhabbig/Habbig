# Takes & Community Tests

**Command:** `python3 -m pytest gateway/tests/test_takes*.py gateway/tests/test_community*.py -q -p no:logging`

**Date:** 2026-05-15
**Branch:** feature/platform-build

## Result

- **Passed:** 0
- **Failed:** 0
- **Tests collected:** 0

No test files matched the patterns `gateway/tests/test_takes*.py` or `gateway/tests/test_community*.py`. Pytest reported "no tests ran in 0.01s" with `ERROR: file or directory not found: gateway/tests/test_takes*.py`.

## Notes

- No file named `test_takes*.py` exists in `gateway/tests/`. The closest match is `test_market_takes.py`.
- No file named `test_community*.py` exists in `gateway/tests/`.
- The glob expansion produced no matching files, so pytest received the literal pattern strings as arguments and failed to locate them.

## Output (tail -30)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_takes*.py


no tests ran in 0.01s
```
