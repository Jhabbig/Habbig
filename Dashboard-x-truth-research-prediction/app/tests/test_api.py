from __future__ import annotations
from unittest.mock import AsyncMock, patch
import pytest, pytest_asyncio
from httpx import ASGITransport, AsyncClient

with patch("app.scheduler.start_scheduler"), patch("app.scheduler.run_pipeline", new_callable=AsyncMock, return_value={}):
    from app.main import app, _hash_password, _active_sessions, _make_session_token
from app.models import User


@pytest_asyncio.fixture
async def client():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel
    from app.db import AsyncSession
    from datetime import datetime, timezone

    test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(User(username="admin", email="test@test.com", password_hash=_hash_password("changeme"), created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        await session.commit()

    import app.db as db_module
    original_engine = db_module.engine
    db_module.engine = test_engine

    async def test_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[db_module.get_session] = test_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    db_module.engine = original_engine
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await test_engine.dispose()


def _cookies(username="admin"):
    """Create a valid session token and register it."""
    token = _make_session_token()
    _active_sessions[token] = username
    return {"session": token}


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

@pytest.mark.asyncio
async def test_login_page(client):
    r = await client.get("/login")
    assert r.status_code == 200 and "Sign in" in r.text

@pytest.mark.asyncio
async def test_login_wrong(client):
    r = await client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
    assert r.status_code == 200 and "Invalid" in r.text

@pytest.mark.asyncio
async def test_login_correct(client):
    r = await client.post("/login", data={"username": "admin", "password": "changeme"}, follow_redirects=False)
    assert r.status_code == 302

@pytest.mark.asyncio
async def test_register_page(client):
    r = await client.get("/register")
    assert r.status_code == 200 and "Create" in r.text

@pytest.mark.asyncio
async def test_register_weak_password(client):
    r = await client.post("/register", data={"username": "newuser", "email": "", "password": "weak", "password2": "weak"}, follow_redirects=False)
    assert "12 characters" in r.text

@pytest.mark.asyncio
async def test_register_duplicate(client):
    r = await client.post("/register", data={"username": "admin", "email": "", "password": "TestPass123!xx", "password2": "TestPass123!xx"}, follow_redirects=False)
    assert "already taken" in r.text

@pytest.mark.asyncio
async def test_register_username_too_long(client):
    r = await client.post("/register", data={"username": "a" * 16, "email": "", "password": "TestPass123!xx", "password2": "TestPass123!xx"}, follow_redirects=False)
    assert "3\u201315" in r.text or "15" in r.text

@pytest.mark.asyncio
async def test_dashboard_redirect(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 302

@pytest.mark.asyncio
async def test_dashboard_auth(client):
    r = await client.get("/", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_feed(client):
    r = await client.get("/feed", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_feed_filters(client):
    r = await client.get("/feed?category=crypto&sort=ev", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_best_bets(client):
    r = await client.get("/best-bets", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_sources(client):
    r = await client.get("/sources", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_leaderboard(client):
    r = await client.get("/leaderboard", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_markets(client):
    r = await client.get("/markets", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_markets_filters(client):
    r = await client.get("/markets?category=crypto&sort=price_high&per_page=20&platform=kalshi", cookies=_cookies())
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_profile(client):
    r = await client.get("/profile", cookies=_cookies())
    assert r.status_code == 200 and "Profile" in r.text

@pytest.mark.asyncio
async def test_refresh(client):
    with patch("app.scheduler.run_pipeline", new_callable=AsyncMock, return_value={"run_at": "now", "posts_fetched": 0, "predictions_extracted": 0, "markets_synced": 0, "errors": []}):
        r = await client.get("/refresh", cookies=_cookies())
        assert r.status_code == 200

@pytest.mark.asyncio
async def test_health_fields(client):
    d = (await client.get("/health")).json()
    assert all(k in d for k in ["predictions_total", "sources_total", "twitter_quota_remaining"])

@pytest.mark.asyncio
async def test_param_clamping(client):
    """Negative page or huge per_page should not crash."""
    r = await client.get("/feed?page=-1&per_page=99999", cookies=_cookies())
    assert r.status_code == 200
    r2 = await client.get("/markets?page=0&per_page=-5", cookies=_cookies())
    assert r2.status_code == 200
