"""Shareable source-card SVG endpoint tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

with patch("app.scheduler.start_scheduler"), patch("app.scheduler.run_pipeline", new_callable=AsyncMock, return_value={}):
    from app.main import _hash_password, app
from app.models import Source, User


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
        session.add(User(username="admin", email="t@t.com", password_hash=_hash_password("changeme"),
                         created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        # A known rated source.
        s = Source(
            handle="alice", platform="twitter",
            global_credibility=0.75, accuracy_global=0.7,
            qualifying_predictions=20, correct_qualifying=14,
            accuracy_unlocked=True, brier_score=0.18, brier_n=15,
        )
        s.categories_predicted_in = ["politics", "crypto"]
        session.add(s)
        await session.commit()

    import app.db as db_module
    import app.main as main_module
    db_module.engine = test_engine
    main_module.engine = test_engine

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await test_engine.dispose()


@pytest.mark.asyncio
async def test_share_card_returns_svg_for_existing_source(client):
    r = await client.get("/share/alice.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert "@alice" in r.text
    assert "RATED" in r.text


@pytest.mark.asyncio
async def test_share_card_returns_unknown_card_for_missing_source(client):
    # No 404 — embeds shouldn't break on missing handles.
    r = await client.get("/share/nobody.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert "not tracked yet" in r.text


@pytest.mark.asyncio
async def test_share_card_is_public_no_auth_redirect(client):
    """Embeds work from anywhere — auth middleware must NOT redirect /share/*."""
    r = await client.get("/share/alice.svg", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers.get("location") is None


@pytest.mark.asyncio
async def test_share_card_escapes_special_chars():
    """Handle is rendered verbatim into the SVG; XML special chars must escape."""
    from app.main import _svg_text_escape
    assert _svg_text_escape('<script>alert("x")</script>') == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"
    assert _svg_text_escape("a & b") == "a &amp; b"


@pytest.mark.asyncio
async def test_share_card_caps_handle_length(client):
    long = "x" * 200
    r = await client.get(f"/share/{long}.svg")
    assert r.status_code == 200
    # The SVG should still be valid (closes properly) even with a very long handle.
    assert r.text.strip().endswith("</svg>")
