"""Signature deployment as background jobs: one job per mailbox."""

import contextlib
import logging
import uuid

from procrastinate import JobContext, RetryStrategy
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.google.base import RetryableError
from app.models import Deployment, DeploymentTrigger, Employee, Organization
from app.services import deploy as deploy_service
from app.services import employees as employee_service
from app.services.deploy import MAX_ATTEMPTS, NothingPublished
from app.tasks import procrastinate_app
from app.tasks.context import get_context

logger = logging.getLogger(__name__)

# Procrastinate retries while the number of earlier attempts is below `max_attempts`,
# so MAX_ATTEMPTS tries in total means MAX_ATTEMPTS - 1 here. Waits: 3, 9, 27, 81 s.
_RETRY = RetryStrategy(
    max_attempts=MAX_ATTEMPTS - 1, exponential_wait=3, retry_exceptions={RetryableError}
)


@procrastinate_app.task(
    name="deploy.apply_signature", queue="default", pass_context=True, retry=_RETRY
)
async def apply_signature_task(
    context: JobContext, employee_id: str, deployment_id: str, force: bool = False
) -> None:
    services = get_context()
    await deploy_service.apply_signature(
        services.db,
        services.google,
        uuid.UUID(employee_id),
        uuid.UUID(deployment_id),
        force=force,
        # On the last try a temporary error is recorded as a failure instead of raised.
        final_attempt=context.job.attempts >= MAX_ATTEMPTS - 1,
    )


@procrastinate_app.task(
    name="deploy.clear_signature", queue="default", pass_context=True, retry=_RETRY
)
async def clear_signature_task(context: JobContext, employee_id: str) -> None:
    services = get_context()
    await deploy_service.clear_signature(
        services.db,
        services.google,
        uuid.UUID(employee_id),
        final_attempt=context.job.attempts >= MAX_ATTEMPTS - 1,
    )


async def change_exclusion(
    session: AsyncSession, organization: Organization, employee_id: uuid.UUID, excluded: bool
) -> Employee | None:
    """Include or exclude one employee, and make their mailbox match.

    Excluding removes the company signature from their Gmail (if we put one there).
    Including applies the live signature to them straight away.
    Returns None if the employee is not in this organisation.
    """
    employee = await employee_service.set_excluded(session, organization.id, employee_id, excluded)
    if employee is None:
        return None
    if excluded:
        if employee.applied_hash is not None:
            await clear_signature_task.configure(lock=f"employee:{employee.id}").defer_async(
                employee_id=str(employee.id)
            )
    elif employee.is_active:
        # With no live signature yet there is nothing to apply: they will get it when
        # the admin applies to everyone.
        with contextlib.suppress(NothingPublished):
            await start_deployment(
                session, organization, DeploymentTrigger.SYNC, employee_ids={employee.id}
            )
    return employee


async def start_deployment(
    session: AsyncSession,
    organization: Organization,
    trigger: DeploymentTrigger,
    *,
    created_by: uuid.UUID | None = None,
    employee_ids: set[uuid.UUID] | None = None,
    force: bool = False,
) -> Deployment:
    """Create a deployment and queue one job per eligible employee.

    `employee_ids` limits it to those people (still filtered for eligibility).
    `force` writes every mailbox even when its signature has not changed.
    Raises NothingPublished or DeploymentInProgress.
    """
    deployment, targets = await deploy_service.create_deployment(
        session, organization, trigger, created_by=created_by, employee_ids=employee_ids
    )
    for employee_id in targets:
        # The lock stops two jobs writing the same mailbox at once; different
        # mailboxes run in parallel.
        await apply_signature_task.configure(lock=f"employee:{employee_id}").defer_async(
            employee_id=str(employee_id), deployment_id=str(deployment.id), force=force
        )
    return deployment


async def deploy_changes_after_sync(
    db: Database, org_id: uuid.UUID, changed_ids: set[uuid.UUID]
) -> Deployment | None:
    """After a directory sync: update the people whose details changed, if a
    signature is live. New hires get theirs here."""
    if not changed_ids:
        return None
    async with db.session() as session:
        organization = await session.get(Organization, org_id)
        if organization is None or not organization.is_connected:
            return None
        try:
            return await start_deployment(
                session, organization, DeploymentTrigger.SYNC, employee_ids=changed_ids
            )
        except NothingPublished:
            return None


async def enforce_all(db: Database) -> int:
    """Daily: re-apply the live signature to everyone, undoing employees' own edits."""
    async with db.session() as session:
        organizations = await deploy_service.organizations_to_enforce(session)
        for organization in organizations:
            await start_deployment(session, organization, DeploymentTrigger.ENFORCE, force=True)
    logger.info("daily enforcement queued organisations=%s", len(organizations))
    return len(organizations)
