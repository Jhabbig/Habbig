"""External signals API + scorecard: token auth, validation, ledger flow, verdicts."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import db
import server


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def client(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(server, "_DEV_MODE", True)
    monkeypatch.setenv("FORECAST_SIGNALS_TOKEN", "sekrit")
    return TestClient(server.app)


def _auth(token="sekrit"):
    return {"Authorization": f"Bearer {token}"}


def _seed_market(conn):
    uid = db.upsert_market(
        conn,
        {
            "venue": "polymarket",
            "venue_id": "0xsig",
            "question": "Will the index close green this week?",
            "category": "finance",
            "url": "https://example.com",
            "end_date": _iso(datetime.now(timezone.utc) + timedelta(days=3)),
        },
    )
    db.insert_snapshot(conn, {"market_uid": uid, "yes_bid": 0.5, "yes_ask": 0.54, "last_price": 0.52, "baseline_prob": 0.52, "spread": 0.04, "liquidity": 900.0, "volume_24h": 10.0})
    return uid


def test_disabled_without_token_env(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(server, "_DEV_MODE", True)
    monkeypatch.delenv("FORECAST_SIGNALS_TOKEN", raising=False)
    c = TestClient(server.app)
    assert c.post("/api/signals", json={}).status_code == 503


def test_wrong_token_rejected(client):
    assert client.post("/api/signals", json={"source": "abc", "probability": 0.5}, headers=_auth("nope")).status_code == 401
    assert client.post("/api/signals", json={"source": "abc", "probability": 0.5}).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"source": "x", "probability": 0.5},  # source too short
        {"source": "market_baseline", "probability": 0.5},  # reserved
        {"source": "llm", "probability": 0.5},  # reserved
        {"source": "Bad Caps", "probability": 0.5},  # pattern
        {"source": "julian_stock_model", "probability": 1.0},  # bounds
        {"source": "julian_stock_model", "probability": "nan?"},  # type
        {"source": "julian_stock_model", "probability": 0.5, "question": "hi"},  # question too short
        {"source": "julian_stock_model", "probability": 0.5, "market_uid": "polymarket:missing"},  # unknown uid
    ],
)
def test_validation_rejections(client, body):
    resp = client.post("/api/signals", json=body, headers=_auth())
    assert resp.status_code in (400, 404)


def test_signal_on_board_market_flows_into_ledger(client, conn):
    uid = _seed_market(conn)
    resp = client.post(
        "/api/signals",
        json={"source": "julian_stock_model", "probability": 0.61, "market_uid": uid, "meta": {"features": 12}},
        headers=_auth(),
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "logged" and out["uid"] == uid and out["scored_against_market"] is True

    mp = conn.execute("SELECT source, model_prob, prob_method FROM model_probs WHERE market_uid = ?", (uid,)).fetchone()
    assert (mp["source"], mp["model_prob"], mp["prob_method"]) == ("julian_stock_model", 0.61, "external")
    cal = conn.execute("SELECT model_prob, market_prob, source FROM calibration WHERE market_uid = ? AND source = 'julian_stock_model'", (uid,)).fetchone()
    assert cal["model_prob"] == 0.61
    assert cal["market_prob"] == 0.52


def test_signal_on_free_form_question_creates_custom_market(client, conn):
    end = _iso(datetime.now(timezone.utc) + timedelta(days=10))
    resp = client.post(
        "/api/signals",
        json={"source": "friends_social_tool", "probability": 0.3, "question": "Will $ACME trend on social platforms this month?", "end_date": end},
        headers=_auth(),
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["uid"].startswith("custom:")
    assert out["scored_against_market"] is False
    m = db.get_market(conn, out["uid"])
    assert m["end_date"] == end and m["venue"] == "custom"


def _seed_scorecard_rows(conn, source, n, model_p, market_p, hit_rate):
    hits = round(n * hit_rate)
    for i in range(n):
        outcome = 1 if i < hits else 0
        db.log_calibration_row(
            conn,
            {
                "market_uid": f"polymarket:sc{source}{i}",
                "platform": "polymarket",
                "category": "finance",
                "target_date": "2026-08-01T00:00:00Z",
                "source": source,
                "model_prob": model_p,
                "market_prob": market_p,
                "outcome": outcome,
                "displayed_at": "2026-07-01",
                "prob_method": "external",
            },
        )


def test_scorecard_math_and_verdicts(client, conn):
    # Model says 0.9, market says 0.6, events hit 90% -> model clearly better.
    _seed_scorecard_rows(conn, "sharp_model", 100, 0.9, 0.6, 0.9)
    # Model says 0.9, market says 0.6, events hit 55% -> model clearly worse.
    _seed_scorecard_rows(conn, "noise_model", 100, 0.9, 0.6, 0.55)
    # Too little evidence either way.
    _seed_scorecard_rows(conn, "tiny_model", 5, 0.7, 0.6, 1.0)

    out = client.get("/api/scorecard").json()
    by = {r["source"]: r for r in out["sources"]}
    assert by["sharp_model"]["verdict"] == "beats_market"
    assert by["sharp_model"]["edge"] > 0
    assert by["noise_model"]["verdict"] == "below_market"
    assert by["noise_model"]["edge"] < 0
    assert by["tiny_model"]["verdict"] == "insufficient_data"
    # paired-Brier arithmetic spot check for sharp_model:
    # model brier = 0.9*(0.1^2)+0.1*(0.9^2) = 0.09; market = 0.9*0.16+0.1*0.36 = 0.18
    assert by["sharp_model"]["brier_model"] == pytest.approx(0.09, abs=1e-3)
    assert by["sharp_model"]["brier_market"] == pytest.approx(0.18, abs=1e-3)


def test_non_ascii_auth_header_is_401_not_500(client):
    # Bytes value: httpx refuses to send non-ASCII str headers, but a real
    # client can put these bytes on the wire (starlette decodes latin-1) —
    # this used to crash hmac.compare_digest into a 500.
    resp = client.post("/api/signals", json={"source": "abc_def", "probability": 0.5}, headers={"Authorization": "Bearer tökén".encode("latin-1")})
    assert resp.status_code == 401


def test_bearer_scheme_is_strict(client, conn):
    uid = _seed_market(conn)
    body = {"source": "strict_check", "probability": 0.5, "market_uid": uid}
    assert client.post("/api/signals", json=body, headers={"Authorization": "sekrit"}).status_code == 401
    assert client.post("/api/signals", json=body, headers={"Authorization": "Bearer  sekrit"}).status_code == 401
    assert client.post("/api/signals", json=body, headers={"Authorization": "Bearer sekrit "}).status_code == 401
    assert client.post("/api/signals", json=body, headers=_auth()).status_code == 200


def test_oversized_meta_rejected(client, conn):
    uid = _seed_market(conn)
    resp = client.post(
        "/api/signals",
        json={"source": "big_meta_model", "probability": 0.5, "market_uid": uid, "meta": {"blob": "x" * 10000}},
        headers=_auth(),
    )
    assert resp.status_code == 413
    assert conn.execute("SELECT COUNT(*) FROM model_probs WHERE source = 'big_meta_model'").fetchone()[0] == 0


def test_scored_against_market_honest_when_baseline_missing(client, conn):
    uid = db.upsert_market(
        conn,
        {
            "venue": "polymarket",
            "venue_id": "0xnosnap",
            "question": "Will the snapshotless market resolve?",
            "category": "politics",
            "url": "https://example.com",
            "end_date": _iso(datetime.now(timezone.utc) + timedelta(days=2)),
        },
    )
    resp = client.post("/api/signals", json={"source": "abc_model", "probability": 0.4, "market_uid": uid}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["scored_against_market"] is False
