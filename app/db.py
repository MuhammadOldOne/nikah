"""Database engine and sessions.

Transaction convention: services never commit. The owner of the unit of work does:
- web: the ``get_session`` dependency commits after the endpoint returns (rolls back on error);
- bot / workers: ``session_scope()`` commits when the block exits without an exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().DATABASE_URL,
            # Error messages must not carry bound values: they are personal data (profiles, chats).
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections (call on shutdown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work for non-web code: commit on success, roll back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Use it through ``app.web.deps.SessionDep`` (scope="function"),
    so the commit happens before the response is sent."""
    async with session_scope() as session:
        yield session


def affected_rows(result: Result[Any]) -> int:
    """Row count of an UPDATE/DELETE result (typed as a plain Result by SQLAlchemy)."""
    return cast(CursorResult[Any], result).rowcount or 0
