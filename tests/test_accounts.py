"""Sign-in service and the database schema."""

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select

from app.db import Database
from app.models import Base, Organization, OrgStatus, User
from app.services import accounts
from app.services.accounts import GoogleIdentity


def identity(email: str, sub: str | None = None, **extra) -> GoogleIdentity:
    return GoogleIdentity(
        sub=sub or f"sub-{email}", email=email, domain=email.partition("@")[2], **extra
    )


async def test_first_sign_in_creates_a_pending_organisation(db: Database):
    async with db.session() as session:
        user = await accounts.sign_in(session, identity("Dana@ACME.test", name="Dana"))
    assert user.email == "dana@acme.test"
    assert user.organization.domain == "acme.test"
    assert user.organization.name == "acme.test"
    assert user.organization.status == OrgStatus.PENDING
    assert user.organization.plan == "free"
    assert user.organization.tracking_enabled is True


async def test_same_address_under_a_new_google_id_keeps_the_user(db: Database):
    async with db.session() as session:
        first = await accounts.sign_in(session, identity("dana@acme.test", sub="dev:dana"))
    async with db.session() as session:
        again = await accounts.sign_in(session, identity("dana@acme.test", sub="google-123"))
        total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    assert again.id == first.id
    assert again.google_sub == "google-123"
    assert total == 1


async def test_simultaneous_first_sign_ins_share_one_organisation(db: Database):
    async def sign_in(email: str) -> User:
        async with db.session() as session:
            return await accounts.sign_in(session, identity(email))

    users = await asyncio.gather(*(sign_in(f"person{n}@acme.test") for n in range(5)))
    assert len({user.org_id for user in users}) == 1
    async with db.session() as session:
        total = (await session.execute(select(func.count()).select_from(Organization))).scalar_one()
    assert total == 1


async def test_owner_email(db: Database):
    async with db.session() as session:
        user = await accounts.sign_in(session, identity("dana@acme.test"))
        assert await accounts.owner_email(session, user.organization) is None
        user.organization.owner_user_id = user.id
        await session.commit()
        assert await accounts.owner_email(session, user.organization) == "dana@acme.test"


async def test_deleting_an_organisation_removes_its_users(db: Database):
    async with db.session() as session:
        user = await accounts.sign_in(session, identity("dana@acme.test"))
        user.organization.owner_user_id = user.id
        await session.commit()
        await session.delete(user.organization)
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == 0


async def test_models_match_the_migrated_schema(db: Database):
    """Fails when a model changes without a migration (or the other way round)."""

    def differences(connection):
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    async with db.engine.connect() as connection:
        assert await connection.run_sync(differences) == []
