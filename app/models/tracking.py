"""Banners, the links whose clicks are counted, and the clicks themselves."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Banner(Base):
    """A promotional image shown under every signature, linking somewhere.

    At most one per organisation is active. Activating another archives the current
    one; archived banners are kept so their click counts stay visible.
    """

    __tablename__ = "banners"
    __table_args__ = (
        Index("uq_banners_one_active", "org_id", unique=True, postgresql_where="is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    target_url: Mapped[str] = mapped_column(Text)
    alt_text: Mapped[str] = mapped_column(String(160), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LinkKind(StrEnum):
    BANNER = "banner"
    SOCIAL = "social"
    WEBSITE = "website"


class TrackedLink(Base):
    """One destination whose clicks are counted.

    A signature links to /c/<token> instead of the destination; that address records
    the click and redirects to `target_url`. The destination only ever comes from this
    row, never from the request, so the redirect cannot be pointed elsewhere.

    `ref` says which link it is: the banner's id, the social network, or "" for the
    website. A changed destination gets a new row, so earlier counts stay with the
    address people were actually sent to.
    """

    __tablename__ = "tracked_links"
    __table_args__ = (
        UniqueConstraint("org_id", "kind", "ref", "target_url", name="uq_tracked_links_slot"),
        CheckConstraint(
            "kind IN ({})".format(", ".join(f"'{kind.value}'" for kind in LinkKind)),
            name="kind_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(10), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    ref: Mapped[str] = mapped_column(String(64), default="")
    target_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClickEvent(Base):
    """One click. No IP address and no user agent is stored."""

    __tablename__ = "click_events"
    __table_args__ = (
        Index("ix_click_events_org_clicked", "org_id", "clicked_at"),
        Index("ix_click_events_link_clicked", "link_id", "clicked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracked_links.id", ondelete="CASCADE"))
    # Whose signature was clicked, when the link carried their short id.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL")
    )
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Mail scanners and link previewers follow links too; these are kept but not counted.
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
