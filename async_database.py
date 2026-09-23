"""Async sessions for claim pre-validation; share the configured database URL."""

from collections.abc import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from database import url

# Psycopg 3 supports both sync and async engines. SQLite is only for tests.
async_url = (
    url.set(drivername="sqlite+aiosqlite")
    if url.get_backend_name() == "sqlite"
    else url.set(drivername="postgresql+psycopg")
)
async_engine = create_async_engine(
    async_url,
    pool_pre_ping=True,
    connect_args=(
        {} if async_url.get_backend_name() == "sqlite" else {"connect_timeout": 5}
    ),
)
AsyncSessionLocal = async_sessionmaker(
    async_engine,
    autoflush=False,
    expire_on_commit=False,
)


async def get_async_db() -> AsyncIterator[AsyncSession]:
    # Closing the session rolls back unfinished work, including on cancellation.
    async with AsyncSessionLocal() as session:
        yield session
