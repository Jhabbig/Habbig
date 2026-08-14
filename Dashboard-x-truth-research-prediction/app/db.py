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


async def _migrate_add_missing_columns(conn) -> None:
    """Add any columns that exist on the SQLModel but not in the live DB.

    SQLModel's ``create_all`` only creates *new* tables; it doesn't add new
    columns to existing tables. Without this step, a deployment that predates
    the v2 MVP starts crashing immediately with "no such column: ..." on the
    first query that touches the new fields (Source.brier_score,
    Prediction.bet_side, MarketSnapshot.event_*, User.telegram_*, etc.).

    Each ALTER TABLE ADD COLUMN is idempotent (we check the live schema first),
    so re-running on a fresh DB is a no-op. Works for SQLite only — Postgres
    deployments should use Alembic.
    """
    import app.models  # noqa: F401  ensure tables are registered

    from sqlalchemy import text

    # Pull each table's live column set in one query so we don't N+1.
    existing: dict[str, set[str]] = {}
    table_names = [t.name for t in SQLModel.metadata.sorted_tables]
    for name in table_names:
        res = await conn.execute(text(f"PRAGMA table_info('{name}')"))
        existing[name] = {row[1] for row in res.fetchall()}

    for table in SQLModel.metadata.sorted_tables:
        live = existing.get(table.name, set())
        if not live:
            continue  # table didn't exist before create_all just made it — done
        for column in table.columns:
            if column.name in live:
                continue
            try:
                col_type = column.type.compile(dialect=conn.engine.dialect)
            except Exception:
                col_type = "TEXT"
            null_clause = "" if column.nullable else " NOT NULL"
            default_clause = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                # Best-effort SQL literal for simple scalar defaults.
                val = column.default.arg
                if isinstance(val, bool):
                    default_clause = f" DEFAULT {1 if val else 0}"
                elif isinstance(val, (int, float)):
                    default_clause = f" DEFAULT {val}"
                elif isinstance(val, str):
                    safe = val.replace("'", "''")
                    default_clause = f" DEFAULT '{safe}'"
            # SQLite ALTER TABLE doesn't accept NOT NULL without a default — if the
            # column is non-null and has no scalar default, fall back to nullable
            # so the migration succeeds. Application code provides the default at
            # insert time via the SQLModel field default_factory.
            if not column.nullable and not default_clause:
                null_clause = ""
            sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}{null_clause}{default_clause}'
            await conn.execute(text(sql))


async def init_db() -> None:
    import app.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_add_missing_columns(conn)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
