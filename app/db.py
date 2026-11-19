"""Database engine and session handling."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(
            url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
        )
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session. Nothing is committed automatically.

        The code that performs a write calls `await session.commit()` itself, so the
        data is durable before the response (often a redirect) reaches the browser.
        Anything left uncommitted is rolled back when the session closes.
        """
        async with self._sessionmaker() as session:
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with request.app.state.db.session() as session:
        yield session
