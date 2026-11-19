"""The company signature design, and the images uploaded for it."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AssetKind(StrEnum):
    LOGO = "logo"
    BANNER = "banner"


class Signature(Base):
    """One per organisation in V1.

    The designer edits `draft_design`. Only applying the signature copies it to
    `published_design`, which is what deployment uses, so half-finished edits never
    reach a mailbox. Both hold a SignatureDesign (app/signatures/schema.py) as JSON.
    """

    __tablename__ = "signatures"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    draft_design: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_design: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Asset(Base):
    """An uploaded image (already re-encoded) and where it is hosted."""

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ({})".format(", ".join(f"'{kind.value}'" for kind in AssetKind)),
            name="kind_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    storage_key: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(50))
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
