"""Directory sync as a background job."""

import logging
import uuid
from datetime import UTC, datetime

from procrastinate import exceptions
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, SyncStatus
from app.services.sync import SyncResult, sync_org
from app.tasks import procrastinate_app
from app.tasks.context import get_context
from app.tasks.deploy import deploy_changes_after_sync

logger = logging.getLogger(__name__)


@procrastinate_app.task(name="sync.sync_org", queue="default")
async def sync_org_task(org_id: str) -> None:
    context = get_context()
    organization_id = uuid.UUID(org_id)

    async def update_signatures(result: SyncResult) -> None:
        # New hires and people whose details changed get the live signature now.
        await deploy_changes_after_sync(context.db, organization_id, result.changed_ids)

    await sync_org(context.db, context.google, context.storage, organization_id, update_signatures)


async def request_sync(session: AsyncSession, organization: Organization) -> None:
    """Queue a sync for this organisation and mark it as requested.

    Safe to call repeatedly: if one is already waiting, no second job is added.
    """
    organization.sync_requested_at = datetime.now(UTC)
    organization.last_sync_status = SyncStatus.QUEUED
    await session.commit()

    org_id = str(organization.id)
    try:
        await sync_org_task.configure(
            lock=f"org:{org_id}",  # one job per organisation runs at a time
            queueing_lock=f"sync:{org_id}",  # and at most one sync waits in the queue
        ).defer_async(org_id=org_id)
    except exceptions.AlreadyEnqueued:
        logger.info("sync already queued org_id=%s", org_id)
