"""Public /api/v1/* + API key management tests."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

with patch("app.scheduler.start_scheduler"), patch("app.scheduler.run_pipeline", new_callable=AsyncMock, return_value={}):
    from app.main import _active_sessions, _hash_api_key, _hash_password, _make_session_token, app
from app.models import APIKey, User


@pytest_asyncio.fixture
async def client():
    from datetime import datetime, timezone
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    from app.db import AsyncSession

    test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        u = User(username="admin", email="t@t.com", password_hash=_hash_password("changeme"),
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        session.add(u)
        await session.commit()
        await session.refresh(u)
        # Insert a known-good API key for that user.
        key_plain = "narve_test-key-12345"
        session.add(APIKey(user_id=u.id, key_hash=_hash_api_key(key_plain), key_prefix=key_plain[:14]))
        await session.commit()

    import app.db as db_module
    import app.main as main_module
    original_engine = db_module.engine
    original_main_engine = main_module.engine
    db_module.engine = test_engine
    main_module.engine = test_engine

    async def test_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s
    app.dependency_overrides[db_module.get_session] = test_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, key_plain

    app.dependency_overrides.clear()
    db_module.engine = original_engine
    main_module.engine = original_main_engine
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_api_requires_key(client):
    ac, _ = client
    r = await ac.get("/api/v1/signals")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_rejects_unknown_key(client):
    ac, _ = client
    r = await ac.get("/api/v1/signals", headers={"X-API-Key": "narve_not-a-real-key"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_signals_with_valid_key(client):
    ac, key = client
    r = await ac.get("/api/v1/signals", headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert "signals" in body and "count" in body


@pytest.mark.asyncio
async def test_api_sources_with_valid_key(client):
    ac, key = client
    r = await ac.get("/api/v1/sources", headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert "sources" in body


@pytest.mark.asyncio
async def test_api_source_detail_404_when_missing(client):
    ac, key = client
    r = await ac.get("/api/v1/sources/nonexistent", headers={"X-API-Key": key})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_backtest_runs(client):
    ac, key = client
    r = await ac.get("/api/v1/backtest?min_ev=0.1&min_credibility=0.5", headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert "n_signals" in body and "sharpe" in body and "daily_curve" in body


@pytest.mark.asyncio
async def test_api_v1_paths_bypass_session_middleware(client):
    """Without an API key the surface should 401, not redirect to /login."""
    ac, _ = client
    r = await ac.get("/api/v1/signals", follow_redirects=False)
    assert r.status_code == 401
    assert "location" not in {k.lower() for k in r.headers}
