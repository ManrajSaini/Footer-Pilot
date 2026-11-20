"""Deployments: one run of writing the signature into mailboxes."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DeploymentTrigger(StrEnum):
    MANUAL = "manual"  # the admin clicked Apply to everyone (or Retry failed)
    TEST = "test"  # Apply to me only
    SYNC = "sync"  # new or changed employees after a directory sync
    ENFORCE = "enforce"  # daily re-apply that undoes employees' own edits
    PLAN_CHANGE = "plan_change"  # the plan changed, so the credit link must change


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"  # stopped early, for example because access was lost


IN_PROGRESS = (DeploymentStatus.QUEUED, DeploymentStatus.RUNNING)


def _one_of(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(_one_of("trigger", DeploymentTrigger), name="trigger_valid"),
        CheckConstraint(_one_of("status", DeploymentStatus), name="status_valid"),
        # The database itself guarantees one manual deployment at a time per organisation.
        Index(
            "uq_deployments_one_manual_in_progress",
            "org_id",
            unique=True,
            postgresql_where="trigger = 'manual' AND status IN ('queued', 'running')",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default=DeploymentStatus.QUEUED)
    # Counters. A mailbox that already had the right signature counts as succeeded.
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def processed(self) -> int:
        return self.succeeded + self.failed + self.skipped

    @property
    def in_progress(self) -> bool:
        return self.status in IN_PROGRESS
