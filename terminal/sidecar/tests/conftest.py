import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from narve_sidecar import db as db_mod  # noqa: E402
from narve_sidecar.server import create_app  # noqa: E402


@pytest.fixture()
def db_file(tmp_path):
    return str(tmp_path / "terminal.db")


@pytest.fixture()
def conn(db_file):
    c = db_mod.connect(db_file)
    yield c
    c.close()


@pytest.fixture()
def client(db_file):
    app = create_app(db_file)
    with TestClient(app) as tc:
        yield tc


def add_source(conn, sid, alpha=2.0, beta=2.0, name=""):
    conn.execute(
        "INSERT INTO sources(id, name, alpha, beta, is_sample, created_at)"
        " VALUES (?, ?, ?, ?, 0, ?)", (sid, name, alpha, beta, db_mod.utcnow()))
    conn.commit()


def add_question(conn, qid, title=None, status="live"):
    conn.execute(
        "INSERT INTO questions(id, title, status, is_sample, created_at)"
        " VALUES (?, ?, ?, 0, ?)", (qid, title or qid, status, db_mod.utcnow()))
    conn.commit()


def add_prediction(conn, sid, qid, p, stated_at="2026-08-01T12:00:00Z", note=None):
    conn.execute(
        "INSERT INTO predictions(source_id, question_id, p, stated_at, note)"
        " VALUES (?, ?, ?, ?, ?)", (sid, qid, p, stated_at, note))
    conn.commit()


def add_snapshot(conn, qid, yes_price, venue="polymarket", market_id=None,
                 captured_at="2026-08-01T12:00:00Z", liquidity=None):
    conn.execute(
        "INSERT INTO market_snapshots"
        "(venue, market_id, question_id, yes_price, liquidity, captured_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (venue, market_id or qid, qid, yes_price, liquidity, captured_at))
    conn.commit()
