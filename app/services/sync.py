"""Directory sync: copy the customer's Google directory into the employees table.

Runs in the worker. People are matched on Google's user id, so renames and address
changes update the existing row. Nobody is ever deleted: people who disappear from
the directory are marked `removed`.
"""

import asyncio
import logging
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import Database
from app.google.base import (
    DirectoryUser,
    GoogleError,
    GoogleGateways,
    NotAdminError,
    NotAuthorizedError,
)
from app.models import Employee, EmployeeStatus, Organization, OrgStatus, SyncStatus
from app.services import accounts
from app.services.images import InvalidImageError, normalise_photo
from app.storage import Storage

logger = logging.getLogger(__name__)

PHOTO_CONCURRENCY = 5
STALE_AFTER = timedelta(minutes=10)
_SHORT_ID_ALPHABET = string.ascii_letters + string.digits

# Directory fields copied onto the employee, with the column width they are cut to.
_COPIED_FIELDS = {
    "email": 320,
    "first_name": 255,
    "last_name": 255,
    "full_name": 255,
    "job_title": 255,
    "department": 255,
    "phone_work": 64,
    "phone_mobile": 64,
}

# Stored in organizations.last_sync_error; the web layer turns these into sentences.
ERROR_NOT_AUTHORIZED = "not_authorized"
ERROR_NOT_ADMIN = "not_admin"
ERROR_UNAVAILABLE = "unavailable"
ERROR_NOT_CONNECTED = "not_connected"


@dataclass
class SyncResult:
    ok: bool
    error: str | None = None
    total: int = 0  # people the directory returned
    created: int = 0
    updated: int = 0
    removed: int = 0
    # Employees whose signature-relevant data changed (deployment uses this later).
    changed_ids: set[uuid.UUID] = field(default_factory=set)


def new_short_id() -> str:
    return "".join(secrets.choice(_SHORT_ID_ALPHABET) for _ in range(8))


def sync_in_progress(organization: Organization, now: datetime | None = None) -> bool:
    """True while a requested sync is queued or running and has not gone stale."""
    if organization.last_sync_status not in (SyncStatus.QUEUED, SyncStatus.RUNNING):
        return False
    requested = organization.sync_requested_at
    return requested is not None and (now or datetime.now(UTC)) - requested < STALE_AFTER


def sync_is_stale(organization: Organization, now: datetime | None = None) -> bool:
    """A sync was requested but never finished, for example because no worker is running."""
    in_flight = organization.last_sync_status in (SyncStatus.QUEUED, SyncStatus.RUNNING)
    return in_flight and not sync_in_progress(organization, now)


async def _fail(db: Database, org_id: uuid.UUID, error: GoogleError) -> SyncResult:
    if isinstance(error, NotAuthorizedError):
        code = ERROR_NOT_AUTHORIZED
    elif isinstance(error, NotAdminError):
        code = ERROR_NOT_ADMIN
    else:
        code = ERROR_UNAVAILABLE
    async with db.session() as session:
        organization = await session.get(Organization, org_id)
        organization.last_sync_status = SyncStatus.FAILED
        organization.last_sync_error = code
        if code == ERROR_NOT_AUTHORIZED:
            # Delegation was removed in the Admin console. The owner must reconnect.
            organization.status = OrgStatus.DISCONNECTED
        await session.commit()
    logger.warning("sync failed org_id=%s error=%s detail=%s", org_id, code, error)
    return SyncResult(ok=False, error=code)


async def _fetch_new_photos(
    google: GoogleGateways,
    storage: Storage,
    org_id: uuid.UUID,
    admin_email: str,
    users: list[DirectoryUser],
    known_etags: dict[str, str | None],
) -> dict[str, str | None]:
    """Download, normalise and store photos whose etag changed.

    Returns {google user id: public URL, or None if the user turned out to have no
    photo}. A photo that fails is left out, so the next sync tries it again.
    """
    wanted = [u for u in users if u.photo_etag and u.photo_etag != known_etags.get(u.id)]
    limit = asyncio.Semaphore(PHOTO_CONCURRENCY)

    async def fetch(user: DirectoryUser) -> tuple[str, str | None] | None:
        async with limit:
            try:
                photo = await google.directory.get_photo(admin_email, user.id)
                if photo is None:
                    return user.id, None
                image = await asyncio.to_thread(normalise_photo, photo.data)
                key = f"orgs/{org_id}/photos/{image.digest}.{image.extension}"
                return user.id, await storage.put(key, image.data, image.content_type)
            except (GoogleError, InvalidImageError) as error:
                logger.warning("photo skipped org_id=%s reason=%s", org_id, type(error).__name__)
            except Exception:
                logger.exception("photo upload failed org_id=%s", org_id)
            return None

    results = await asyncio.gather(*(fetch(user) for user in wanted))
    return dict(result for result in results if result is not None)


async def sync_org(
    db: Database, google: GoogleGateways, storage: Storage, org_id: uuid.UUID
) -> SyncResult:
    async with db.session() as session:
        organization = await session.get(Organization, org_id)
        admin_email = (
            await accounts.owner_email(session, organization) if organization is not None else None
        )
        if organization is None or not organization.is_connected or admin_email is None:
            logger.info("sync skipped: organisation is not connected org_id=%s", org_id)
            return SyncResult(ok=False, error=ERROR_NOT_CONNECTED)
        organization.last_sync_status = SyncStatus.RUNNING
        await session.commit()
        rows = await session.execute(
            select(Employee.google_user_id, Employee.photo_etag).where(Employee.org_id == org_id)
        )
        known_etags = dict(rows.all())

    # Network work happens outside any database transaction.
    try:
        users = await google.directory.list_users(admin_email)
    except GoogleError as error:
        return await _fail(db, org_id, error)
    photos = await _fetch_new_photos(google, storage, org_id, admin_email, users, known_etags)

    result = SyncResult(ok=True, total=len(users))
    now = datetime.now(UTC)
    async with db.session() as session:
        rows = await session.execute(select(Employee).where(Employee.org_id == org_id))
        existing = {employee.google_user_id: employee for employee in rows.scalars()}
        seen: set[str] = set()

        for user in users:
            seen.add(user.id)
            values = {name: getattr(user, name)[:width] for name, width in _COPIED_FIELDS.items()}
            values["has_mailbox"] = user.has_mailbox
            values["status"] = (
                EmployeeStatus.SUSPENDED
                if user.suspended or user.archived
                else EmployeeStatus.ACTIVE
            )

            employee = existing.get(user.id)
            created = employee is None
            if employee is None:
                employee = Employee(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    google_user_id=user.id,
                    short_id=new_short_id(),
                    **values,
                )
                session.add(employee)
                changed = True
            else:
                changed = any(getattr(employee, name) != value for name, value in values.items())
                for name, value in values.items():
                    setattr(employee, name, value)

            if user.photo_etag is None:
                changed = changed or employee.photo_url is not None
                employee.photo_url = None
                employee.photo_etag = None
            elif user.id in photos:
                changed = changed or employee.photo_url != photos[user.id]
                employee.photo_url = photos[user.id]
                employee.photo_etag = user.photo_etag

            employee.synced_at = now
            if changed:
                result.changed_ids.add(employee.id)
                if created:
                    result.created += 1
                else:
                    result.updated += 1

        for google_user_id, employee in existing.items():
            if google_user_id not in seen and employee.status != EmployeeStatus.REMOVED:
                employee.status = EmployeeStatus.REMOVED
                result.removed += 1
                result.changed_ids.add(employee.id)

        organization = await session.get(Organization, org_id)
        organization.last_sync_at = now
        organization.last_sync_status = SyncStatus.OK
        organization.last_sync_error = None
        await session.commit()

    logger.info(
        "sync finished org_id=%s total=%s created=%s updated=%s removed=%s",
        org_id,
        result.total,
        result.created,
        result.updated,
        result.removed,
    )
    return result
