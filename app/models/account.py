"""Organisations (one per Google Workspace domain) and the admins who sign in."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class OrgStatus(StrEnum):
    PENDING = "pending"  # created at first sign-in, not yet verified
    CONNECTED = "connected"  # domain-wide delegation verified
    DISCONNECTED = "disconnected"  # delegation was working and has stopped


class Plan(StrEnum):
    FREE = "free"
    PAID = "paid"


def _one_of(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(_one_of("status", OrgStatus), name="status_valid"),
        CheckConstraint(_one_of("plan", Plan), name="plan_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    google_customer_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default=OrgStatus.PENDING)
    # The admin who verified the connection. Null until verification succeeds; while
    # it is null, any signed-in user of the domain may attempt setup.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", use_alter=True, ondelete="SET NULL")
    )
    plan: Mapped[str] = mapped_column(String(20), default=Plan.FREE)
    tracking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    daily_enforce: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def is_connected(self) -> bool:
        return self.status == OrgStatus.CONNECTED


class User(Base):
    """A person who signs in to FooterPilot (not an employee record)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    picture_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Always loaded with the user: every request needs the organisation.
    organization: Mapped[Organization] = relationship(foreign_keys=[org_id], lazy="joined")

    def owns(self, organization: Organization) -> bool:
        return organization.owner_user_id == self.id
