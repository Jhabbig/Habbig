import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.get_conn(tmp_path / "test.sqlite")
    yield c
    c.close()
