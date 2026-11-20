"""Writing the signature into mailboxes.

A deployment is one run over a set of employees. Each mailbox is handled on its own
(`apply_signature`), so one failure never blocks the rest, and the deployment row
keeps running totals that the progress page reads.

Rendering goes through the same functions as the designer preview
(`build_org_context`, `employee_data`), so what was previewed is what is written.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.google.base import (
    GoogleGateways,
    NotAuthorizedError,
    PermanentError,
    RetryableError,
    SkipError,
)
from app.models import (
    ApplyStatus,
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    Employee,
    EmployeeStatus,
    Organization,
    OrgStatus,
    Signature,
    User,
)
from app.models.deployment import IN_PROGRESS
from app.services import signatures as signature_service
from app.signatures.render import SignatureTooLongError, render_signature
from app.signatures.schema import SignatureDesign

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # tries per mailbox before a temporary error is recorded as a failure
# A deployment still unfinished after this long is given up on (the worker was
# probably stopped), so that it cannot block new ones for ever.
STALE_AFTER = timedelta(minutes=30)

SKIP_NO_MAILBOX = "This person has no Gmail mailbox"
SKIP_NOT_ELIGIBLE = "No longer active, or excluded"
FAIL_TOO_LONG = "The signature is too long for this person"
FAIL_TEMPORARY = "Google was temporarily unavailable. Use Retry failed to try again"


class DeploymentInProgress(Exception):
    """A manual deployment is already running for this organisation."""


class NothingPublished(Exception):
    """There is no applied design to deploy."""


def signature_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


# --- Who gets the signature -----------------------------------------------------------


def _eligible():
    """Active and not excluded by the admin. (Plan limits are added with billing.)"""
    return (Employee.status == EmployeeStatus.ACTIVE, Employee.excluded.is_(False))


async def eligible_employee_ids(
    session: AsyncSession, org_id: uuid.UUID, only: set[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    query = select(Employee.id).where(Employee.org_id == org_id, *_eligible())
    if only is not None:
        query = query.where(Employee.id.in_(only))
    return list((await session.execute(query.order_by(Employee.email))).scalars())


async def count_eligible(session: AsyncSession, org_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Employee).where(Employee.org_id == org_id, *_eligible())
    )
    return result.scalar_one()


async def count_failed(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Eligible employees whose latest attempt failed (skips are not counted)."""
    result = await session.execute(
        select(func.count())
        .select_from(Employee)
        .where(Employee.org_id == org_id, *_eligible(), Employee.apply_status == ApplyStatus.FAILED)
    )
    return result.scalar_one()


async def employees_needing_attention(session: AsyncSession, org_id: uuid.UUID) -> list[Employee]:
    """Eligible employees whose latest attempt failed or was skipped, with the reason."""
    result = await session.execute(
        select(Employee)
        .where(
            Employee.org_id == org_id,
            *_eligible(),
            Employee.apply_status.in_([ApplyStatus.FAILED, ApplyStatus.SKIPPED]),
        )
        .order_by(Employee.apply_status, func.lower(Employee.full_name))
    )
    return list(result.scalars())


# --- Deployments ----------------------------------------------------------------------


async def expire_stale_deployments(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        update(Deployment)
        .where(
            Deployment.org_id == org_id,
            Deployment.status.in_(IN_PROGRESS),
            Deployment.created_at < datetime.now(UTC) - STALE_AFTER,
        )
        .values(status=DeploymentStatus.FAILED, finished_at=datetime.now(UTC))
    )
    await session.commit()


async def get_deployment(
    session: AsyncSession, org_id: uuid.UUID, deployment_id: uuid.UUID
) -> Deployment | None:
    result = await session.execute(
        select(Deployment).where(Deployment.id == deployment_id, Deployment.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def manual_deployment_in_progress(
    session: AsyncSession, org_id: uuid.UUID
) -> Deployment | None:
    await expire_stale_deployments(session, org_id)
    result = await session.execute(
        select(Deployment).where(
            Deployment.org_id == org_id,
            Deployment.trigger == DeploymentTrigger.MANUAL,
            Deployment.status.in_(IN_PROGRESS),
        )
    )
    return result.scalar_one_or_none()


async def latest_deployment(session: AsyncSession, org_id: uuid.UUID) -> Deployment | None:
    """The most recent company-wide deployment (not a personal test)."""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.org_id == org_id, Deployment.trigger != DeploymentTrigger.TEST)
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_test(session: AsyncSession, org_id: uuid.UUID) -> Deployment | None:
    """The most recent successful "Apply to me only"."""
    result = await session.execute(
        select(Deployment)
        .where(
            Deployment.org_id == org_id,
            Deployment.trigger == DeploymentTrigger.TEST,
            Deployment.succeeded > 0,
        )
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def publish_draft(session: AsyncSession, signature: Signature) -> None:
    """Make the current draft the live design."""
    signature.published_design = dict(signature.draft_design)
    signature.published_at = datetime.now(UTC)
    await session.commit()


async def create_deployment(
    session: AsyncSession,
    organization: Organization,
    trigger: DeploymentTrigger,
    *,
    created_by: uuid.UUID | None = None,
    employee_ids: set[uuid.UUID] | None = None,
) -> tuple[Deployment, list[uuid.UUID]]:
    """Record a deployment and decide who it covers. The caller queues the work.

    Raises NothingPublished, or DeploymentInProgress for a second manual deployment.
    """
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None or signature.published_design is None:
        raise NothingPublished
    await expire_stale_deployments(session, organization.id)

    targets = await eligible_employee_ids(session, organization.id, employee_ids)
    deployment = Deployment(
        id=uuid.uuid4(),
        org_id=organization.id,
        trigger=trigger,
        created_by=created_by,
        total=len(targets),
        # Nothing to do: finished before it started.
        status=DeploymentStatus.RUNNING if targets else DeploymentStatus.COMPLETED,
        finished_at=None if targets else datetime.now(UTC),
    )
    session.add(deployment)
    try:
        await session.commit()
    except IntegrityError as error:
        # The partial unique index allows one manual deployment in progress.
        await session.rollback()
        raise DeploymentInProgress from error
    logger.info(
        "deployment created org_id=%s deployment_id=%s trigger=%s total=%s",
        organization.id,
        deployment.id,
        trigger,
        len(targets),
    )
    return deployment, targets


async def _record(
    session: AsyncSession,
    employee: Employee,
    deployment_id: uuid.UUID | None,
    outcome: ApplyStatus,
    *,
    error: str | None = None,
    digest: str | None = None,
) -> None:
    """Store one mailbox's result and, in the same transaction, the deployment totals."""
    employee.apply_status = outcome
    employee.apply_error = error
    if outcome is ApplyStatus.APPLIED:
        employee.applied_hash = digest
        employee.applied_at = datetime.now(UTC)

    if deployment_id is not None:
        column = {
            ApplyStatus.APPLIED: Deployment.succeeded,
            ApplyStatus.FAILED: Deployment.failed,
            ApplyStatus.SKIPPED: Deployment.skipped,
        }[outcome]
        processed = Deployment.succeeded + Deployment.failed + Deployment.skipped + 1
        # Only failures make a run "completed with errors". A skipped mailbox (for
        # example someone with no Gmail) is expected and would otherwise taint every run.
        failures = Deployment.failed + (1 if outcome is ApplyStatus.FAILED else 0)
        finished = processed >= Deployment.total
        # One statement, so concurrent mailboxes cannot lose each other's increments
        # and exactly one of them sees the last count and closes the deployment.
        await session.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id, Deployment.status.in_(IN_PROGRESS))
            .values(
                {
                    column: column + 1,
                    Deployment.status: case(
                        (
                            finished,
                            case(
                                (failures > 0, DeploymentStatus.COMPLETED_WITH_ERRORS),
                                else_=DeploymentStatus.COMPLETED,
                            ),
                        ),
                        else_=Deployment.status,
                    ),
                    Deployment.finished_at: case((finished, func.now()), else_=None),
                }
            )
        )
    await session.commit()


async def _connection_lost(
    session: AsyncSession, organization: Organization, deployment_id: uuid.UUID | None
) -> None:
    """Delegation is gone: stop this deployment and send the owner back to setup."""
    organization.status = OrgStatus.DISCONNECTED
    if deployment_id is not None:
        await session.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id, Deployment.status.in_(IN_PROGRESS))
            .values(status=DeploymentStatus.FAILED, finished_at=func.now())
        )
    await session.commit()
    logger.warning("deployment stopped: access lost org_id=%s", organization.id)


@dataclass(frozen=True)
class ApplyOutcome:
    status: ApplyStatus | None  # None: nothing was done (deployment over, or access lost)
    error: str | None = None
    wrote: bool = False  # True when Gmail was actually changed


async def _apply(
    session: AsyncSession,
    google: GoogleGateways,
    organization: Organization,
    employee: Employee,
    design: SignatureDesign,
    deployment_id: uuid.UUID | None,
    *,
    force: bool,
    final_attempt: bool,
) -> ApplyOutcome:
    """Render for one employee and write it to their mailbox if it changed."""
    if not employee.has_mailbox:
        await _record(session, employee, deployment_id, ApplyStatus.SKIPPED, error=SKIP_NO_MAILBOX)
        return ApplyOutcome(ApplyStatus.SKIPPED, SKIP_NO_MAILBOX)

    try:
        org_ctx = await signature_service.build_org_context(session, organization, design)
        html = render_signature(design, signature_service.employee_data(employee), org_ctx)
    except SignatureTooLongError:
        await _record(session, employee, deployment_id, ApplyStatus.FAILED, error=FAIL_TOO_LONG)
        return ApplyOutcome(ApplyStatus.FAILED, FAIL_TOO_LONG)

    digest = signature_hash(html)
    up_to_date = employee.apply_status == ApplyStatus.APPLIED and employee.applied_hash == digest
    if up_to_date and not force:
        # Already what the mailbox has: no call to Google.
        await _record(session, employee, deployment_id, ApplyStatus.APPLIED, digest=digest)
        return ApplyOutcome(ApplyStatus.APPLIED)

    try:
        await google.gmail.set_signature(employee.email, html)
    except NotAuthorizedError:
        await _connection_lost(session, organization, deployment_id)
        return ApplyOutcome(None)
    except SkipError as error:
        await _record(session, employee, deployment_id, ApplyStatus.SKIPPED, error=error.reason)
        return ApplyOutcome(ApplyStatus.SKIPPED, error.reason)
    except RetryableError:
        if not final_attempt:
            raise  # the job queue tries again later, with a growing delay
        await _record(session, employee, deployment_id, ApplyStatus.FAILED, error=FAIL_TEMPORARY)
        return ApplyOutcome(ApplyStatus.FAILED, FAIL_TEMPORARY)
    except PermanentError as error:
        await _record(session, employee, deployment_id, ApplyStatus.FAILED, error=str(error))
        return ApplyOutcome(ApplyStatus.FAILED, str(error))

    await _record(session, employee, deployment_id, ApplyStatus.APPLIED, digest=digest)
    return ApplyOutcome(ApplyStatus.APPLIED, wrote=True)


async def apply_signature(
    db: Database,
    google: GoogleGateways,
    employee_id: uuid.UUID,
    deployment_id: uuid.UUID,
    *,
    force: bool = False,
    final_attempt: bool = False,
) -> ApplyOutcome:
    """Apply the published signature to one employee, as part of a deployment.

    Raises RetryableError (unless `final_attempt`), which the job queue retries.
    """
    async with db.session() as session:
        deployment = await session.get(Deployment, deployment_id)
        if deployment is None or not deployment.in_progress:
            return ApplyOutcome(None)  # cancelled, or stopped because access was lost
        employee = await session.get(Employee, employee_id)
        if employee is None or employee.org_id != deployment.org_id:
            return ApplyOutcome(None)
        organization = await session.get(Organization, deployment.org_id)
        signature = await signature_service.get_signature(session, organization.id)

        if not employee.is_active or employee.excluded:
            await _record(
                session, employee, deployment_id, ApplyStatus.SKIPPED, error=SKIP_NOT_ELIGIBLE
            )
            return ApplyOutcome(ApplyStatus.SKIPPED, SKIP_NOT_ELIGIBLE)

        design = SignatureDesign.model_validate(signature.published_design)
        return await _apply(
            session,
            google,
            organization,
            employee,
            design,
            deployment_id,
            force=force,
            final_attempt=final_attempt,
        )


class NotInDirectory(Exception):
    """The signed-in admin has no employee record to apply a test signature to."""


async def apply_to_self(
    session: AsyncSession,
    google: GoogleGateways,
    organization: Organization,
    user: User,
    design: SignatureDesign,
) -> ApplyOutcome:
    """Apply to me only: write the given (draft) design to the admin's own mailbox.

    Runs at once, not through the queue, so the admin gets an immediate answer.
    Nothing is published and nobody else is touched.
    """
    employee = (
        await session.execute(
            select(Employee).where(Employee.org_id == organization.id, Employee.email == user.email)
        )
    ).scalar_one_or_none()
    if employee is None or employee.status == EmployeeStatus.REMOVED:
        raise NotInDirectory

    deployment = Deployment(
        id=uuid.uuid4(),
        org_id=organization.id,
        trigger=DeploymentTrigger.TEST,
        created_by=user.id,
        total=1,
        status=DeploymentStatus.RUNNING,
    )
    session.add(deployment)
    await session.commit()
    return await _apply(
        session,
        google,
        organization,
        employee,
        design,
        deployment.id,
        force=True,
        final_attempt=True,  # no retries here: report a temporary failure straight away
    )


# --- Automatic updates ----------------------------------------------------------------


async def organizations_to_sync(session: AsyncSession) -> list[uuid.UUID]:
    result = await session.execute(
        select(Organization.id).where(Organization.status == OrgStatus.CONNECTED)
    )
    return list(result.scalars())


async def organizations_to_enforce(session: AsyncSession) -> list[Organization]:
    """Connected organisations with a live signature and daily enforcement switched on."""
    result = await session.execute(
        select(Organization)
        .join(Signature, Signature.org_id == Organization.id)
        .where(
            Organization.status == OrgStatus.CONNECTED,
            Organization.daily_enforce.is_(True),
            Signature.published_design.is_not(None),
        )
    )
    return list(result.scalars())
