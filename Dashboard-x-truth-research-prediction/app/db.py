from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from app.config import settings

engine = create_async_engine(
    settings["DATABASE_URL"],
    echo=False,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


class AsyncSession(_AsyncSession):
    async def exec(self, statement, **kwargs):
        result = await self.execute(statement, **kwargs)
        return _ScalarResult(result)


class _ScalarResult:
    def __init__(self, result):
        self._result = result

    def first(self):
        row = self._result.first()
        if row is None:
            return None
        return row[0] if len(row) == 1 else row

    def all(self):
        rows = self._result.all()
        return [row[0] if len(row) == 1 else row for row in rows]

    def one(self):
        row = self._result.one()
        return row[0] if len(row) == 1 else row

    def __iter__(self):
        return iter(self.all())


async def init_db() -> None:
    import app.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
