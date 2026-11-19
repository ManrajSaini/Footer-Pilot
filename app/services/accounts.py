"""Sign-in: turning a verified Google identity into a user and an organisation."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, OrgStatus, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoogleIdentity:
    """Claims from a verified Google ID token."""

    sub: str
    email: str
    domain: str  # the `hd` claim: the Workspace primary domain
    name: str = ""
    picture_url: str | None = None


async def _get_or_create_organization(session: AsyncSession, domain: str) -> Organization:
    # ON CONFLICT DO NOTHING makes two simultaneous first sign-ins from one domain safe.
    await session.execute(
        insert(Organization)
        .values(domain=domain, name=domain, status=OrgStatus.PENDING)
        .on_conflict_do_nothing(index_elements=[Organization.domain])
    )
    result = await session.execute(select(Organization).where(Organization.domain == domain))
    return result.scalar_one()


async def sign_in(session: AsyncSession, identity: GoogleIdentity) -> User:
    """Find or create the user (and their organisation), and record the login."""
    email = identity.email.lower()
    domain = identity.domain.lower()

    user = (
        await session.execute(select(User).where(User.google_sub == identity.sub))
    ).scalar_one_or_none()
    if user is None:
        # Same address under a new Google account id (for example after dev login, or
        # an account that was recreated). The Workspace admin controls who holds it.
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is not None:
            logger.info("rebinding google account user_id=%s", user.id)
            user.google_sub = identity.sub

    if user is None:
        organization = await _get_or_create_organization(session, domain)
        user = User(
            org_id=organization.id, organization=organization, google_sub=identity.sub, email=email
        )
        session.add(user)
        logger.info("new user org_id=%s domain=%s", organization.id, domain)

    user.email = email
    user.name = identity.name
    user.picture_url = identity.picture_url
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user


async def owner_email(session: AsyncSession, organization: Organization) -> str | None:
    if organization.owner_user_id is None:
        return None
    result = await session.execute(
        select(User.email).where(
            User.id == organization.owner_user_id, User.org_id == organization.id
        )
    )
    return result.scalar_one_or_none()
