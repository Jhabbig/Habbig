"""HTTP surface for the Prediction Engine, mounted under /api/v1/engine/*.

Auth is injected at include_router time in app/main.py (the same X-API-Key
dependency the rest of /api/v1 uses), which keeps this module free of any
import back into main.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.engine.config import get_engine_config
from app.engine.schemas import EngineJob, EnginePrediction
from app.engine.service import get_engine

router = APIRouter(prefix="/api/v1/engine", tags=["engine"])


@router.post("/predict", response_model=EnginePrediction)
async def predict(job: EngineJob) -> EnginePrediction:
    """Fuse Component 1 + Component 2 signals into a calibrated prediction."""
    return await get_engine().predict(job)


@router.get("/metrics")
async def metrics() -> JSONResponse:
    engine = get_engine()
    snapshot = engine.metrics.snapshot()
    snapshot["dedup_cache"] = engine.cache.stats()
    snapshot["audit"] = {"written": engine.audit.written, "dropped": engine.audit.dropped}
    return JSONResponse(snapshot)


@router.get("/config")
async def config() -> JSONResponse:
    """The live (hot-reloaded) engine config — what the next request will use."""
    return JSONResponse(get_engine_config())


@router.post("/replay")
async def run_replay(limit: int = Query(default=1000, ge=1, le=10000),
                     grade: bool = Query(default=True)) -> JSONResponse:
    """Grade any newly-resolved markets, then re-score history with the current fusion."""
    from app.engine import replay as replay_mod
    import app.db as db
    async with db.AsyncSession(db.engine, expire_on_commit=False) as session:
        graded = await replay_mod.grade_pending(session) if grade else 0
        report = await replay_mod.replay(session, limit=limit)
    report["newly_graded"] = graded
    return JSONResponse(report)


@router.post("/calibration/fit")
async def calibration_fit(method: str = Query(default="platt", pattern="^(platt|isotonic)$"),
                          limit: int = Query(default=5000, ge=10, le=100000)) -> JSONResponse:
    """Fit calibration params from logged outcomes (never platform content)."""
    from app.engine import replay as replay_mod
    import app.db as db
    async with db.AsyncSession(db.engine, expire_on_commit=False) as session:
        result = await replay_mod.fit_calibration(session, method=method, limit=limit)
    return JSONResponse(result)


@router.get("/cost")
async def cost() -> JSONResponse:
    """Measured cost readout over the audit history: tokens/job, hit rate, $/1k."""
    from app.engine import replay as replay_mod
    import app.db as db
    async with db.AsyncSession(db.engine, expire_on_commit=False) as session:
        readout = await replay_mod.cost_readout(session)
    return JSONResponse(readout)
