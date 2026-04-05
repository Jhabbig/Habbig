from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from app.db import AsyncSession
from app.models import MarketSnapshot, Prediction, RawPost, Source, SourcePredictionRecord

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(async_engine, expire_on_commit=False) as sess:
        yield sess


@pytest.fixture
def sample_raw_post():
    post = RawPost(id="twitter:12345", platform="twitter", author_handle="testuser", author_display_name="Test User", follower_count=10000, verified=True, content="I predict Bitcoin will win the crypto championship with 75% chance of reaching 100k by December", posted_at=NOW - timedelta(hours=24), fetched_at=NOW, engagement_json="{}")
    post.engagement = {"likes": 150, "retweets": 30, "replies": 10}
    return post


@pytest.fixture
def sample_source_new():
    return Source(handle="newuser", platform="twitter", follower_count=500, verified=False, engagement_ratio=0.03, total_predictions=0, qualifying_predictions=0, correct_qualifying=0, accuracy_unlocked=False, created_at=NOW, last_seen=NOW)


@pytest.fixture
def sample_source_rated():
    s = Source(handle="rateduser", platform="twitter", follower_count=50000, verified=True, engagement_ratio=0.05, total_predictions=15, qualifying_predictions=12, correct_qualifying=8, accuracy_unlocked=True, accuracy_global=0.67, decay_weighted_accuracy=0.65, global_credibility=0.55, created_at=NOW - timedelta(days=120), last_seen=NOW)
    s.categories_predicted_in = ["politics", "crypto", "sports"]
    s.category_credibility = {"politics": 0.6, "crypto": 0.7, "sports": 0.4}
    return s
