"""Scheduled jobs. The worker queues these itself, on the cron lines below (UTC)."""

import logging

from app.db import Database
from app.models import Organization
from app.services.deploy import organizations_to_sync
from app.tasks import procrastinate_app
from app.tasks.context import get_context
from app.tasks.deploy import enforce_all
from app.tasks.sync import request_sync

logger = logging.getLogger(__name__)


async def sync_all(db: Database) -> int:
    """Queue a directory sync for every connected organisation."""
    async with db.session() as session:
        org_ids = await organizations_to_sync(session)
        for org_id in org_ids:
            await request_sync(session, await session.get(Organization, org_id))
    logger.info("hourly sync queued organisations=%s", len(org_ids))
    return len(org_ids)


@procrastinate_app.periodic(cron="0 * * * *")
@procrastinate_app.task(name="periodic.hourly_sync", queue="default")
async def hourly_sync(timestamp: int) -> None:
    """Every hour: pick up new hires and changed details. Each sync then updates the
    signatures of the people who changed."""
    await sync_all(get_context().db)


@procrastinate_app.periodic(cron="30 3 * * *")
@procrastinate_app.task(name="periodic.daily_enforce", queue="default")
async def daily_enforce(timestamp: int) -> None:
    """Once a day: overwrite signatures that employees changed themselves."""
    await enforce_all(get_context().db)
