"""Tests for the pipeline → Stage-3 ingestion path (app/engine/ingest.py)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

from app.engine.config import set_engine_config_override
from app.engine.ingest import fuse_ranked_predictions, job_from_prediction
from app.engine.service import get_engine, reset_engine_for_tests
from app.models import FusionAudit, Prediction, Source


@pytest.fixture(autouse=True)
def _reset_engine_state():
    reset_engine_for_tests()
    yield
    set_engine_config_override(None)
    reset_engine_for_tests()


@pytest_asyncio.fixture
async def engine_db(async_engine):
    import app.db as db_module
    original = db_module.engine
    db_module.engine = async_engine
    yield async_engine
    db_module.engine = original


def _prediction(**overrides) -> Prediction:
    base = dict(
        id=101,
        raw_post_id="twitter:555",
        market_slug="btc-150k",
        market_question="Will BTC hit 150k?",
        category="crypto",
        predicted_outcome="Yes",
        predicted_probability=0.7,
        market_implied_probability=0.55,
        hours_remaining_at_prediction=48.0,
    )
    base.update(overrides)
    return Prediction(**base)


def test_job_from_prediction_maps_all_three_streams(sample_source_rated):
    job = job_from_prediction(_prediction(), sample_source_rated)
    assert job.job_id == "pipeline-101"
    assert job.job_class == "pipeline"
    # Component 1: category credibility preferred over global
    assert len(job.credibility) == 1
    assert job.credibility[0].score == sample_source_rated.category_credibility["crypto"]
    assert job.credibility[0].post_id == "twitter:555"
    # Component 2: extracted probability + timing feature
    assert job.metrics.predicted["predicted_probability"] == 0.7
    assert job.metrics.extracted["hours_remaining"] == 48.0
    # Context: the matched market
    assert job.context["market_slug"] == "btc-150k"
    assert job.context["market_implied_probability"] == 0.55


def test_job_from_prediction_without_source_or_market():
    pred = _prediction(market_slug=None, market_implied_probability=None, predicted_probability=None)
    job = job_from_prediction(pred, None)
    assert job.credibility == []
    assert "predicted_probability" not in job.metrics.predicted
    assert "market_slug" not in job.context


def test_job_falls_back_to_global_credibility(sample_source_rated):
    job = job_from_prediction(_prediction(category="geopolitics"), sample_source_rated)
    # source has no geopolitics category credibility → global score
    assert job.credibility[0].score == sample_source_rated.global_credibility


@pytest.mark.asyncio
async def test_fuse_ranked_predictions_writes_audits(engine_db, session, sample_source_rated):
    pairs = [
        (_prediction(id=1), sample_source_rated),
        (_prediction(id=2, market_slug="fed-cut", predicted_probability=0.3), sample_source_rated),
        (_prediction(id=3, market_slug=None, market_implied_probability=None), None),  # degraded path
    ]
    fused = await fuse_ranked_predictions(pairs)
    assert fused == 3
    engine = get_engine()
    await engine.audit.stop()

    rows = (await session.exec(select(FusionAudit).where(FusionAudit.job_class == "pipeline"))).all()
    assert len(rows) == 3
    by_job = {r.job_id: r for r in rows}
    assert by_job["pipeline-1"].market_slug == "btc-150k"
    assert by_job["pipeline-3"].degraded is True  # no source, no market
    assert engine.metrics.snapshot()["jobs"] == 3
