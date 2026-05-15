# Cache Tests

**Command:**
```bash
python3 -m pytest gateway/tests/test_cache*.py gateway/tests/test_ttl*.py --tb=line -q -p no:logging
```

**Note:** No `test_ttl*.py` files exist in `gateway/tests/`. Ran the three matching `test_cache*.py` files explicitly.

**Files run:**
- `gateway/tests/test_cache.py`
- `gateway/tests/test_cache_invalidation.py`
- `gateway/tests/test_cache_service.py`

## Result

**53 passed, 0 failed**

## Output

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
.....................................................                    [100%]
=============================== warnings summary ===============================
tests/test_cache.py::TestAdminCachePage::test_admin_sees_page
  /Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_client.py:812: DeprecationWarning: Setting per-request cookies=<...> is being deprecated, because the expected behaviour on cookie persistence is ambiguous. Set cookies directly on the client instance instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```
