"""Shared test setup.

Tests run against a dedicated Postgres database (created by docker-compose) and never
call a real external service. Environment variables are fixed here, before any app
module is imported, so a developer's .env (which may hold real Google credentials)
cannot leak into the test run.
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
        "MARKETING_BASE_URL": "",
        "TRACKING_BASE_URL": "",
        "ASSET_BASE_URL": "",
        "GOOGLE_MODE": "fake",
        "DEV_LOGIN": "false",
        "GOOGLE_OAUTH_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "",
        "GOOGLE_DWD_CLIENT_ID": "123456789012345678901",
        "STORAGE_BACKEND": "local",
        "SETUP_CALL_URL": "https://example.com/book-a-call",
    }
)

import shutil  # noqa: E402
import tempfile  # noqa: E402

# Uploaded files go to a throwaway folder, never to the developer's var/media.
MEDIA_ROOT = tempfile.mkdtemp(prefix="footerpilot-test-media-")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx2 import ASGITransport, AsyncClient  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base, Organization, OrgStatus, User  # noqa: E402
from app.runtime import new_event_loop  # noqa: E402
from app.services import accounts  # noqa: E402
from app.services.accounts import GoogleIdentity  # noqa: E402
from app.tasks import procrastinate_app  # noqa: E402
from app.tasks.context import WorkerContext, set_context  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _remove_test_media():
    yield
    shutil.rmtree(MEDIA_ROOT, ignore_errors=True)


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


@pytest.fixture(scope="session")
def schema(require_db) -> None:
    """Rebuild the test database from the Alembic migrations, once per test run.

    Using the migrations (not `create_all`) means the tests run against the same
    schema production gets.
    """
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


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


@pytest.fixture
async def db(app: FastAPI, schema) -> AsyncIterator[Database]:
    """The app's database, emptied after the test."""
    database: Database = app.state.db
    yield database
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with database.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


MakeUser = Callable[..., Awaitable[User]]


@pytest.fixture
def make_user(db: Database) -> MakeUser:
    """Create a user the way a real sign-in does: `await make_user("a@acme.test")`."""

    async def _make(email: str, name: str = "") -> User:
        async with db.session() as session:
            return await accounts.sign_in(
                session,
                GoogleIdentity(
                    sub=f"sub-{email}", email=email, domain=email.partition("@")[2], name=name
                ),
            )

    return _make


@pytest.fixture
def make_owner(db: Database, make_user: MakeUser) -> MakeUser:
    """A user whose organisation is already connected, with them as its owner."""

    async def _make(email: str, name: str = "") -> User:
        user = await make_user(email, name)
        async with db.session() as session:
            organization = await session.get(Organization, user.org_id)
            organization.owner_user_id = user.id
            organization.status = OrgStatus.CONNECTED
            organization.google_customer_id = "C-test"
            organization.connected_at = datetime.now(UTC)
            await session.commit()
        async with db.session() as session:
            return await session.get(User, user.id)

    return _make


@pytest.fixture
def run_jobs(app: FastAPI, job_queue: InMemoryConnector):
    """Run every queued background job now, in-process: `await run_jobs()`.

    Jobs use the app's own database, Google gateways and storage, read at call time,
    so a test can swap `app.state.google` first.
    """

    async def _run() -> None:
        set_context(
            WorkerContext(db=app.state.db, google=app.state.google, storage=app.state.storage)
        )
        await procrastinate_app.run_worker_async(wait=False)

    yield _run
    set_context(None)
