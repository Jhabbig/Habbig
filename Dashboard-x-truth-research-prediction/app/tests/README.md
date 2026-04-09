# app/tests/ — Pytest suite

Async pytest suite covering the FastAPI app, the credibility engine, the
scrapers, the extraction pipeline, the ranker, the resolver, and the desktop
entry point. Run from the package root:

```bash
pytest app/tests/ -v
```

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `conftest.py` | Shared fixtures: in-memory async SQLite engine, schema setup, sample `Source`/`Prediction`/`MarketSnapshot` rows, fixed "now" timestamp. |
| `test_api.py` | End-to-end FastAPI tests using `httpx.AsyncClient`. Auth flow, dashboard rendering, refresh endpoint. |
| `test_credibility.py` | Unit tests for `credibility/engine.py`, `category_scores`, `decay`, `diversity`. |
| `test_extractor.py` | Unit tests for `processing/extractor.py` — regex extraction, category classification. |
| `test_ranker.py` | Unit tests for `processing/ranker.py` — EV computation, risk flag thresholds. |
| `test_resolver.py` | Unit tests for `processing/resolver.py` — mock Polymarket client, mark predictions correct/incorrect, recompute credibility. |
| `test_desktop.py` | Smoke tests for the desktop entry point — frozen vs dev mode path resolution, port allocation. |
