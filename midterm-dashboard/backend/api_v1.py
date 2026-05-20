"""Public v1 API.

Stable, documented JSON endpoints intended for external consumers
(journalists, researchers, partners). Distinct from ``/data/*`` which is the
internal API the SPA uses — internal endpoints can change shape without
warning; ``/v1/*`` won't.

Versioning policy: additive changes only within v1. Schema-breaking changes
ship as ``/v2/*``. Every response includes an ``api_version`` field so
clients can sanity-check.

Authentication: none for v1. Endpoints are read-only and the data is
already public. Rate limiting is applied by the same Redis middleware that
governs the internal API.

This module exposes a function ``register(app, *, get_state)`` that the main
FastAPI app calls to attach the routes; that indirection lets us keep
``main.py`` from growing further.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import HTTPException, Query

logger = logging.getLogger(__name__)

API_VERSION = "v1"


def register(app, *, get_state: Callable):
    """Attach v1 routes to ``app``. ``get_state()`` returns the AppState."""

    @app.get("/v1/forecasts", tags=["v1"])
    async def forecasts(
        race_type: Optional[str] = Query(None, description="Filter by race_type (senate/house/governor/...)"),
        min_confidence: float = Query(0.0, ge=0.0, le=1.0),
        limit: int = Query(200, ge=1, le=1000),
    ):
        """House forecasts across every active race.

        Each row carries the Brier-weighted ensemble probability (``forecast_d``,
        the P(Democrat wins)), the ensemble confidence, the sources used and
        their weights, and an inlined ``smart_money`` block when top-quality
        wallet positioning data is available.
        """
        from main import data_forecasts  # local import to avoid cycle
        out = await data_forecasts(race_type=race_type, min_confidence=min_confidence, limit=limit)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/forecast/{race_key}", tags=["v1"])
    async def forecast_one(race_key: str):
        """House forecast for a single race plus the inlined smart-money summary."""
        from main import data_forecast
        out = await data_forecast(race_key)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/forecast/conditional", tags=["v1"])
    async def forecast_conditional(given: str = Query(..., description="Format: <race_key>=<D|R>")):
        """Re-score every race conditional on one race resolving D or R.

        Powers joint-distribution questions like "if D wins PA Senate, what's
        the implied MI forecast?" Returns a per-race ``delta_pp`` and the
        pairwise ``correlation`` used in the propagation.
        """
        from main import data_forecast_conditional
        out = await data_forecast_conditional(given=given)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/forecast/wave", tags=["v1"])
    async def forecast_wave(swing_pp: float = Query(0.0, ge=-15.0, le=15.0)):
        """Apply a fixed national-environment swing (in pp) to every race.

        Positive favours D, negative favours R. Returns updated forecasts and
        chamber-level expected seat counts.
        """
        from main import data_forecast_wave
        out = await data_forecast_wave(swing_pp=swing_pp)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/election-night", tags=["v1"])
    async def election_night():
        """Race-night master view: synthetic narve.ai calls per race plus
        chamber-control totals (called / lean / tossup; floor / ceiling)."""
        from main import data_election_night
        out = await data_election_night()
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/smart-money/{race_key}", tags=["v1"])
    async def smart_money(race_key: str):
        """Top-quality-wallet positioning for the polymarket markets in
        a race. Returns total $ positioned, distinct wallet count, and the
        lean direction (D / R) with strength."""
        from main import data_smart_money
        out = await data_smart_money(race_key)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/news/race/{race_key}", tags=["v1"])
    async def news_for_race(race_key: str, limit: int = Query(20, ge=1, le=100)):
        """Recent political news tagged to this race, joined with measured
        market reactions when available."""
        from main import data_news_for_race
        out = await data_news_for_race(race_key, limit=limit)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/lag-curve", tags=["v1"])
    async def lag_curve(min_delta_pp: float = Query(1.0, ge=0.1, le=10.0)):
        """Per-source median time between a tagged news event and the first
        material price move. The signature "how fast does each market reprice"
        metric."""
        from main import data_news_lag_curve
        out = await data_news_lag_curve(min_delta_pp=min_delta_pp)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1/backtest", tags=["v1"])
    async def backtest(since_days: int = Query(30, ge=1, le=365)):
        """Per-source Brier score on resolved races. Lower = better calibrated."""
        from main import data_backtest
        out = await data_backtest(since_days=since_days)
        out["api_version"] = API_VERSION
        return out

    @app.get("/v1", tags=["v1"])
    async def root():
        """Index of available v1 endpoints."""
        return {
            "api_version": API_VERSION,
            "endpoints": [
                {"path": "/v1/forecasts",                "method": "GET", "desc": "All race forecasts"},
                {"path": "/v1/forecast/{race_key}",      "method": "GET", "desc": "One race forecast"},
                {"path": "/v1/forecast/conditional",     "method": "GET", "desc": "Conditional re-scoring"},
                {"path": "/v1/forecast/wave",            "method": "GET", "desc": "Apply a national swing"},
                {"path": "/v1/election-night",           "method": "GET", "desc": "Race-night master view"},
                {"path": "/v1/smart-money/{race_key}",   "method": "GET", "desc": "Top-wallet positioning"},
                {"path": "/v1/news/race/{race_key}",     "method": "GET", "desc": "Race news + reactions"},
                {"path": "/v1/lag-curve",                "method": "GET", "desc": "News→market median lag"},
                {"path": "/v1/backtest",                 "method": "GET", "desc": "Per-source Brier scores"},
            ],
            "methodology": "/methodology",
            "license": "Data is public and free to reuse with attribution to narve.ai.",
        }
