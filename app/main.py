"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Database
from app.google import build_gateways
from app.log import configure_logging
from app.tasks import procrastinate_app
from app.web import auth, dashboard, dev, generator, health, onboarding, public, ratelimit
from app.web.csrf import csrf_protect
from app.web.deps import register_access_handlers
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
    register_access_handlers(app)

    app.state.generator_limiter = ratelimit.RateLimiter(
        generator.PREVIEW_RATE_LIMIT, generator.PREVIEW_RATE_WINDOW
    )

    app.state.google = build_gateways(settings)
    app.state.oauth_google = auth.build_google_oauth(settings)

    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(generator.router)
    app.include_router(auth.router)
    app.include_router(onboarding.router)
    app.include_router(dashboard.router)
    if settings.dev_login:  # refused by Settings when ENV=production
        app.include_router(dev.router)
    return app
