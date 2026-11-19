"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Database
from app.log import configure_logging
from app.tasks import procrastinate_app
from app.web import health, public
from app.web.csrf import csrf_protect
from app.web.errors import register_error_handlers
from app.web.templating import STATIC_DIR

SESSION_COOKIE = "fp_session"
SESSION_MAX_AGE = 14 * 24 * 60 * 60  # seconds


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(settings.sqlalchemy_url)
        try:
            # Opened here so request handlers can defer background jobs.
            async with procrastinate_app.open_async():
                yield
        finally:
            await app.state.db.dispose()

    app = FastAPI(
        title="FooterPilot",
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.is_production,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(public.router)
    return app
