"""Connecting an organisation to its Google Workspace domain.

Ownership rule: an organisation has no owner until someone proves, through this check,
that the customer's super admin authorised FooterPilot and that they are that admin.
That person becomes the owner. Nobody else from the domain gets access afterwards.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google.base import (
    GoogleGateways,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
)
from app.models import Organization, OrgStatus, User

logger = logging.getLogger(__name__)


class VerifyResult(StrEnum):
    CONNECTED = "connected"
    NOT_AUTHORIZED = "not_authorized"  # delegation missing or scopes do not match
    NOT_ADMIN = "not_admin"
    UNAVAILABLE = "unavailable"
    ALREADY_OWNED = "already_owned"  # someone else connected this domain first


async def verify_connection(
    session: AsyncSession, google: GoogleGateways, user: User
) -> VerifyResult:
    org_id = user.org_id
    if user.organization.owner_user_id not in (None, user.id):
        return VerifyResult.ALREADY_OWNED

    try:
        admin = await google.directory.verify_admin(user.email)
        await google.gmail.verify_access(user.email)
    except NotAuthorizedError:
        logger.info("verification failed: not authorised org_id=%s", org_id)
        return VerifyResult.NOT_AUTHORIZED
    except NotAdminError:
        logger.info("verification failed: not an admin org_id=%s user_id=%s", org_id, user.id)
        return VerifyResult.NOT_ADMIN
    except GoogleUnavailableError as error:
        logger.warning("verification failed: google unavailable org_id=%s error=%s", org_id, error)
        return VerifyResult.UNAVAILABLE

    # Lock the row and re-check: two admins may be verifying at the same moment.
    organization = (
        await session.execute(
            select(Organization)
            .where(Organization.id == org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if organization.owner_user_id not in (None, user.id):
        await session.rollback()
        return VerifyResult.ALREADY_OWNED

    organization.owner_user_id = user.id
    organization.google_customer_id = admin.customer_id
    organization.status = OrgStatus.CONNECTED
    if organization.connected_at is None:
        organization.connected_at = datetime.now(UTC)
    await session.commit()
    logger.info("organisation connected org_id=%s owner_user_id=%s", org_id, user.id)
    return VerifyResult.CONNECTED
