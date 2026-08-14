"""End-to-end tests for the Prediction Engine service: fusion orchestration,
dedup cache, graceful degradation, audit trail, replay/grading, and the
/api/v1/engine/* HTTP surface."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlmodel import select

from app.engine.config import set_engine_config_override
from app.engine.schemas import EngineJob
from app.engine.service import PredictionEngine, compute_prediction, reset_engine_for_tests
from app.models import FusionAudit, ResolvedMarket


@pytest.fixture(autouse=True)
def _reset_engine_state():
    reset_engine_for_tests()
    yield
    set_engine_config_override(None)
    reset_engine_for_tests()


@pytest_asyncio.fixture
async def engine_db(async_engine):
    """Point the audit writer's dynamic app.db.engine lookup at the test DB."""
    import app.db as db_module
    original = db_module.engine
    db_module.engine = async_engine
    yield async_engine
    db_module.engine = original


def full_job(job_id: str = "job-1", **overrides) -> EngineJob:
    base = {
        "job_id": job_id,
        "user_id": "u-1",
        "job_class": "interactive",
        "credibility": [
            {"post_id": "x:1", "source": "x", "score": 0.8, "features": {"followers": 10000}},
            {"post_id": "r:2", "source": "reddit", "score": 0.6, "features": {}},
        ],
        "metrics": {
            "predicted": {"predicted_probability": 0.72},
            "extracted": {"engagement_velocity": 400},
            "model_version": "claude-haiku-4-5",
            "usage": {"input_tokens": 1500, "output_tokens": 120, "cache_read_input_tokens": 1000},
        },
        "context": {"market_slug": "btc-150k", "market_implied_probability": 0.55, "category": "crypto"},
    }
    base.update(overrides)
    return EngineJob(**base)


# ---------------------------------------------------------------------------
# Core service behaviour
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_predict_returns_full_contract(engine_db):
    engine = PredictionEngine()
    out = await engine.predict(full_job())
    assert out.job_id == "job-1"
    assert 0.0 <= out.prediction.p_yes <= 1.0
    assert out.prediction.side in ("YES", "NO")
    assert 0.0 <= out.confidence <= 1.0
    assert not out.degraded
    assert out.cache_hit is False
    assert out.latency_ms > 0
    assert set(out.model_versions) == {"credibility", "metric", "fusion"}
    assert out.model_versions["metric"] == "claude-haiku-4-5"
    weights = [s.weight for s in out.contributing_signals]
    assert sum(weights) == pytest.approx(1.0, abs=0.01)
    await engine.audit.stop()


@pytest.mark.asyncio
async def test_duplicate_content_hits_cache_across_users(engine_db):
    engine = PredictionEngine()
    first = await engine.predict(full_job("job-a", user_id="alice"))
    second = await engine.predict(full_job("job-b", user_id="bob"))
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.job_id == "job-b"
    assert second.prediction.p_yes == first.prediction.p_yes
    assert engine.cache.hit_rate() == pytest.approx(0.5)
    await engine.audit.stop()


@pytest.mark.asyncio
async def test_missing_credibility_degrades_not_fails(engine_db):
    engine = PredictionEngine()
    healthy = await engine.predict(full_job("h1"))
    degraded = await engine.predict(full_job("d1", credibility=[]))
    assert degraded.degraded is True
    assert "credibility_unavailable" in degraded.degraded_reasons
    assert degraded.confidence < healthy.confidence
    assert 0.0 <= degraded.prediction.p_yes <= 1.0
    await engine.audit.stop()


@pytest.mark.asyncio
async def test_all_components_missing_returns_prior_at_floor(engine_db):
    set_engine_config_override({"degradation": {"confidence_floor": 0.05}})
    engine = PredictionEngine()
    out = await engine.predict(full_job(
        "empty", credibility=[], metrics={"predicted": {}, "extracted": {}}, context={},
    ))
    assert out.degraded is True
    assert set(out.degraded_reasons) == {"credibility_unavailable", "metrics_unavailable"}
    assert out.prediction.p_yes == 0.5
    assert out.confidence == pytest.approx(0.05)
    await engine.audit.stop()


@pytest.mark.asyncio
async def test_audit_row_stores_everything_needed_to_reproduce(engine_db, session):
    engine = PredictionEngine()
    job = full_job("audit-job")
    out = await engine.predict(job)
    await engine.audit.stop()

    row = (await session.exec(select(FusionAudit).where(FusionAudit.job_id == "audit-job"))).first()
    assert row is not None
    assert row.input_hash == job.content_hash()
    assert row.p_yes == out.prediction.p_yes
    assert row.model_tier == out.model_tier
    assert row.prompt_hash  # extraction system prompt fingerprint
    assert row.tokens_in == 1500 and row.tokens_out == 120 and row.cached_tokens_in == 1000
    assert row.cost_usd > 0
    assert row.market_slug == "btc-150k"
    inputs = json.loads(row.inputs_json)
    assert inputs["context"]["market_implied_probability"] == 0.55
    assert len(inputs["credibility"]) == 2
    # the stored inputs replay to the identical output — determinism
    replayed = compute_prediction(EngineJob(
        job_id="replay", credibility=inputs["credibility"],
        metrics=inputs["metrics"], context=inputs["context"],
    ))
    assert replayed.prediction.p_yes == out.prediction.p_yes


def test_compute_prediction_is_deterministic():
    job = full_job("det")
    a = compute_prediction(job)
    b = compute_prediction(job)
    assert a.prediction.p_yes == b.prediction.p_yes
    assert a.confidence == b.confidence


@pytest.mark.asyncio
async def test_metrics_track_cost_and_tiers(engine_db):
    engine = PredictionEngine()
    await engine.predict(full_job("m1"))
    await engine.predict(full_job("m2", job_class="batch"))
    snap = engine.metrics.snapshot()
    assert snap["jobs"] == 2
    assert snap["tokens_in"] > 0
    assert snap["cost_usd"] > 0
    assert "claude-opus-4-8" in snap["tier_mix"] or "claude-haiku-4-5" in snap["tier_mix"]
    await engine.audit.stop()


# ---------------------------------------------------------------------------
# Replay / grading / cost readout
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def graded_history(engine_db, session):
    """Predictions on two markets, one resolved YES and one NO."""
    engine = PredictionEngine()
    for i, (slug, p_hint) in enumerate([("m-yes", 0.8), ("m-yes", 0.75), ("m-no", 0.2), ("m-no", 0.3)]):
        await engine.predict(full_job(
            f"g{i}",
            metrics={"predicted": {"predicted_probability": p_hint}, "extracted": {}, "model_version": "x",
                     "usage": {"input_tokens": 1000, "output_tokens": 100}},
            context={"market_slug": slug, "market_implied_probability": p_hint},
        ))
    await engine.audit.stop()
    session.add(ResolvedMarket(market_slug="m-yes", outcome="Yes", resolved_at=datetime.now(timezone.utc)))
    session.add(ResolvedMarket(market_slug="m-no", outcome="No", resolved_at=datetime.now(timezone.utc)))
    await session.commit()
    return engine


@pytest.mark.asyncio
async def test_grade_and_replay(graded_history, session):
    from app.engine.replay import grade_pending, replay

    graded = await grade_pending(session)
    assert graded == 4
    report = await replay(session, limit=100)
    assert report["n"] == 4
    assert report["deterministic"] is True
    assert report["stored"]["accuracy"] == 1.0  # 0.8/0.75 on YES market, 0.2/0.3 on NO market
    assert 0.0 <= report["stored"]["brier"] <= 0.25
    assert len(report["reliability"]) == 10


@pytest.mark.asyncio
async def test_fit_calibration_from_logged_outcomes_only(graded_history, session):
    from app.engine.replay import fit_calibration, grade_pending

    await grade_pending(session)
    platt = await fit_calibration(session, method="platt")
    assert platt["n"] == 4 and "platt" in platt
    iso = await fit_calibration(session, method="isotonic")
    ys = [pt[1] for pt in iso["isotonic_points"]]
    assert ys == sorted(ys)


@pytest.mark.asyncio
async def test_cost_readout_aggregates_audits(graded_history, session):
    from app.engine.replay import cost_readout

    readout = await cost_readout(session)
    assert readout["jobs"] == 4
    assert readout["total_cost_usd"] > 0
    assert readout["cost_per_1k_predictions_usd"] > 0
    assert isinstance(readout["cost_alert"], bool)


# ---------------------------------------------------------------------------
# HTTP surface (auth + contract)
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def api_client():
    from datetime import datetime, timezone
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    with patch("app.scheduler.start_scheduler"), patch("app.scheduler.run_pipeline", new_callable=AsyncMock, return_value={}):
        from app.main import _hash_api_key, _hash_password, app
    from app.db import AsyncSession
    from app.models import APIKey, User

    test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        u = User(username="admin", email="t@t.com", password_hash=_hash_password("changeme"),
                 created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        session.add(u)
        await session.commit()
        await session.refresh(u)
        key_plain = "narve_engine-test-key"
        session.add(APIKey(user_id=u.id, key_hash=_hash_api_key(key_plain), key_prefix=key_plain[:14]))
        await session.commit()

    import app.db as db_module
    import app.main as main_module
    original_engine = db_module.engine
    original_main_engine = main_module.engine
    db_module.engine = test_engine
    main_module.engine = test_engine

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, key_plain

    db_module.engine = original_engine
    main_module.engine = original_main_engine
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_api_predict_requires_key(api_client):
    ac, _ = api_client
    r = await ac.post("/api/v1/engine/predict", json=full_job("no-auth").model_dump())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_predict_contract(api_client):
    ac, key = api_client
    r = await ac.post("/api/v1/engine/predict", json=full_job("http-1").model_dump(),
                      headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "http-1"
    assert set(body) >= {"prediction", "confidence", "contributing_signals",
                         "model_versions", "degraded", "latency_ms"}
    assert 0.0 <= body["prediction"]["p_yes"] <= 1.0
    from app.engine.service import get_engine
    await get_engine().audit.stop()


@pytest.mark.asyncio
async def test_api_metrics_and_config(api_client):
    ac, key = api_client
    r = await ac.get("/api/v1/engine/metrics", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert "cache_hit_rate" in r.json() and "dedup_cache" in r.json()
    r = await ac.get("/api/v1/engine/config", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert "model_tiers" in r.json() and "fusion" in r.json()


@pytest.mark.asyncio
async def test_api_malformed_job_is_422_not_500(api_client):
    ac, key = api_client
    r = await ac.post("/api/v1/engine/predict", json={"credibility": "not-a-list"},
                      headers={"X-API-Key": key})
    assert r.status_code == 422
