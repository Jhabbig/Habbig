import sys
import tempfile
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import db as gateway_db  # noqa: E402

# Repoint before server.py is imported — its module-level init_db() must never
# touch the developer's real gateway/auth.db.
gateway_db.DB_PATH = Path(tempfile.mkdtemp(prefix="gw-test-")) / "auth.db"


def _reset_thread_conn():
    c = getattr(gateway_db._local, "conn", None)
    if c is not None:
        c.close()
        gateway_db._local.conn = None


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_db, "DB_PATH", tmp_path / "auth.db")
    _reset_thread_conn()
    gateway_db.init_db()
    yield gateway_db
    _reset_thread_conn()
