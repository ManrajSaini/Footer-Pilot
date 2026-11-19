"""Shared test setup.

Tests run against a dedicated Postgres database (created by docker-compose) and never
call a real external service. Environment variables are fixed here, before any app
module is imported, so a developer's .env cannot leak into the test run.
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://footerpilot:footerpilot@127.0.0.1:5432/footerpilot_test",
)
os.environ.update(
    {
        "ENV": "dev",
        "DATABASE_URL": TEST_DATABASE_URL,
        "SECRET_KEY": "test-secret-key",
        "APP_BASE_URL": "http://testserver",
        "GOOGLE_MODE": "fake",
        "DEV_LOGIN": "false",
        "STORAGE_BACKEND": "local",
    }
)

from collections.abc import AsyncIterator  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402

from app.main import create_app  # noqa: E402
from app.runtime import new_event_loop  # noqa: E402
from app.tasks import procrastinate_app  # noqa: E402


def pytest_asyncio_loop_factories(config, item):
    # psycopg's async mode needs the selector loop on Windows (see app/runtime.py).
    return {"default": new_event_loop}


@pytest.fixture(scope="session")
def require_db() -> None:
    """Fail with a clear message when the test database is not running."""
    try:
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=3).close()
    except psycopg.OperationalError as error:
        pytest.fail(
            f"Test database is not reachable ({error}). Start it with: docker compose up -d",
            pytrace=False,
        )


@pytest.fixture
def job_queue() -> InMemoryConnector:
    """Replace the Postgres job queue with an in-memory one for the test."""
    connector = InMemoryConnector()
    with procrastinate_app.replace_connector(connector):
        yield connector


@pytest.fixture
async def app(job_queue: InMemoryConnector) -> AsyncIterator[FastAPI]:
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: let the 500 handler answer, as a real server would.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
