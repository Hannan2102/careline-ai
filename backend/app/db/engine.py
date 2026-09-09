"""Database engine and session lifecycle.

Async SQLAlchemy. PostgreSQL in Docker, SQLite for a laptop without it -- the
same two-tier arrangement as the EHR layer, and for the same reason: the
project has to be runnable without Docker.

Schema is created with ``create_all``. There are no migrations yet because
there is nothing to migrate: every environment builds this schema from scratch.
Alembic belongs here the moment a deployed database needs to survive a schema
change, and that is recorded as a gap rather than pre-built ceremony.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.db.base import Base
from app.observability.logging import get_logger

logger = get_logger(__name__)


def _is_memory_sqlite(url: str) -> bool:
    return url.startswith("sqlite") and (":memory:" in url or "mode=memory" in url)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        options: dict[str, object] = {"echo": echo, "future": True}

        if _is_memory_sqlite(url):
            # An in-memory SQLite database lives inside one connection. With
            # the default pool, create_schema() and the first query can land on
            # different connections, and the tables appear to vanish. StaticPool
            # keeps everyone on the same one.
            options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False}

        self._engine: AsyncEngine = create_async_engine(url, **options)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(url=settings.database_url, echo=False)

    async def create_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("database_schema_ready", url=self._safe_url())

    async def drop_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A unit of work. Commits on success, rolls back on failure."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()

    def _safe_url(self) -> str:
        """The URL without credentials, for logging."""
        if "@" not in self.url:
            return self.url
        scheme, _, rest = self.url.partition("://")
        return f"{scheme}://***@{rest.rpartition('@')[2]}"
