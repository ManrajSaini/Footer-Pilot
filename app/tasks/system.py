"""Operational tasks."""

import logging

from app.tasks import procrastinate_app

logger = logging.getLogger(__name__)


@procrastinate_app.task(name="system.ping", queue="default")
async def ping(message: str = "pong") -> None:
    """Smoke test for the queue: `python -m app.cli ping` defers it, the worker logs it."""
    logger.info("ping task executed message=%s", message)
