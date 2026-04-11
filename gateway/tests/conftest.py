"""Pytest conftest — scoped patch for tests that opt into the shared DB.

Test files in this directory fall into two camps:

1. **New feature tests** (e.g. `test_email_system`, `test_feature_routes`)
   import `from tests import _testdb` to pick up an in-memory sqlite3
   connection with all migrations applied.

2. **Legacy tests from the previous session** (`test_user_features`,
   `test_sentry`, `test_http_auth`, etc.) use their own `contextlib`
   fakes that patch `db.conn` directly at import time.

If both camps load in the same pytest collection phase, whichever
monkey-patches last wins, silently breaking the other. This conftest
re-installs the `_testdb._fake_conn` patch **only** for test modules
that explicitly declare `USES_TESTDB = True` or import `_testdb`. Other
tests are left alone, so the orphan fixtures keep working.
"""

from __future__ import annotations

import sys

import pytest

from tests import _testdb  # noqa: F401 — always sets up the shared conn
import db


_SHARED_CONN_CM = _testdb._fake_conn
_TESTDB_MODULE_NAMES = {"tests._testdb", "_testdb"}


def _module_uses_testdb(test_file_module: str) -> bool:
    mod = sys.modules.get(test_file_module)
    if mod is None:
        return False
    # Explicit marker — a test file can set `USES_TESTDB = True`.
    if getattr(mod, "USES_TESTDB", False):
        return True
    # Inferred — if the module imported _testdb, it implicitly opts in.
    for name in _TESTDB_MODULE_NAMES:
        if name in getattr(mod, "__dict__", {}):
            return True
    return False


@pytest.fixture(autouse=True)
def _maybe_force_shared_testdb(request):
    """Re-apply the shared-conn patch for tests that use _testdb, and
    leave other tests alone so their own db.conn monkey-patches stick."""
    module_name = request.node.module.__name__ if hasattr(request.node, "module") else ""
    if _module_uses_testdb(module_name):
        db.conn = _SHARED_CONN_CM
    yield
