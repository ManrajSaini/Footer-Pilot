"""Employees: one row per person in the customer's Google directory."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EmployeeStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"  # suspended or archived in Google
    REMOVED = "removed"  # no longer returned by the directory; kept, never deleted


class ApplyStatus(StrEnum):
    NEVER = "never"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    OVER_LIMIT = "over_limit"


def _one_of(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("org_id", "google_user_id", name="uq_employees_org_google_user"),
        CheckConstraint(_one_of("status", EmployeeStatus), name="status_valid"),
        CheckConstraint(_one_of("apply_status", ApplyStatus), name="apply_status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    google_user_id: Mapped[str] = mapped_column(String(64))

    # Copied from the directory on every sync.
    email: Mapped[str] = mapped_column(String(320))
    first_name: Mapped[str] = mapped_column(String(255), default="")
    last_name: Mapped[str] = mapped_column(String(255), default="")
    full_name: Mapped[str] = mapped_column(String(255), default="")
    job_title: Mapped[str] = mapped_column(String(255), default="")
    department: Mapped[str] = mapped_column(String(255), default="")
    phone_work: Mapped[str] = mapped_column(String(64), default="")
    phone_mobile: Mapped[str] = mapped_column(String(64), default="")
    has_mailbox: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    status: Mapped[str] = mapped_column(String(20), default=EmployeeStatus.ACTIVE)

    # Our hosted copy of the directory photo, and the Google etag it was made from.
    photo_url: Mapped[str | None] = mapped_column(Text)
    photo_etag: Mapped[str | None] = mapped_column(String(255))

    # Set by the admin: leave this person out of signature deployment.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Short public identifier, used in click-tracking links.
    short_id: Mapped[str] = mapped_column(String(8), unique=True)

    # Signature deployment state (filled in from Phase 5).
    applied_hash: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_status: Mapped[str] = mapped_column(String(20), default=ApplyStatus.NEVER)
    apply_error: Mapped[str | None] = mapped_column(Text)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.status == EmployeeStatus.ACTIVE
