"""Promotional banners: one active per organisation, earlier ones kept for their counts."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AssetKind, Banner, ClickEvent, LinkKind, TrackedLink
from app.services.images import normalise_banner
from app.signatures.render import BannerData
from app.storage import Storage

BANNER_MAX_DISPLAY_WIDTH = 600  # px in the signature; images are stored at up to twice that


@dataclass(frozen=True)
class BannerView:
    """A banner with its image and its click count, for pages."""

    banner: Banner
    asset: Asset
    clicks: int


async def get_banner_asset(
    session: AsyncSession, org_id: uuid.UUID, asset_id: uuid.UUID | None
) -> Asset | None:
    """This organisation's banner image with that id, or None."""
    if asset_id is None:
        return None
    result = await session.execute(
        select(Asset).where(
            Asset.id == asset_id, Asset.org_id == org_id, Asset.kind == AssetKind.BANNER
        )
    )
    return result.scalar_one_or_none()


async def create_banner_asset(
    session: AsyncSession, storage: Storage, org_id: uuid.UUID, data: bytes
) -> Asset:
    """Validate, re-encode and store an uploaded banner image. Raises InvalidImageError."""
    image = normalise_banner(data)
    key = f"orgs/{org_id}/banners/{image.digest}.{image.extension}"
    url = await storage.put(key, image.data, image.content_type)
    asset = Asset(
        org_id=org_id,
        kind=AssetKind.BANNER,
        storage_key=key,
        url=url,
        width=image.width,
        height=image.height,
        content_type=image.content_type,
        bytes=len(image.data),
    )
    session.add(asset)
    await session.commit()
    return asset


def display_width(asset: Asset) -> int:
    return min(BANNER_MAX_DISPLAY_WIDTH, asset.width)


def banner_data(banner: Banner, asset: Asset) -> BannerData:
    """What the renderer needs to show this banner."""
    return BannerData(
        image_url=asset.url,
        target_url=banner.target_url,
        alt_text=banner.alt_text,
        width_px=display_width(asset),
        id=str(banner.id),
    )


async def active_banner(session: AsyncSession, org_id: uuid.UUID) -> tuple[Banner, Asset] | None:
    row = (
        await session.execute(
            select(Banner, Asset)
            .join(Asset, Asset.id == Banner.asset_id)
            .where(Banner.org_id == org_id, Banner.is_active.is_(True))
        )
    ).first()
    return (row[0], row[1]) if row else None


async def _archive_active(session: AsyncSession, org_id: uuid.UUID) -> bool:
    current = await active_banner(session, org_id)
    if current is None:
        return False
    current[0].is_active = False
    current[0].archived_at = datetime.now(UTC)
    # Flushed now so the one-active-banner index is free before a new one is inserted.
    await session.flush()
    return True


async def activate_banner(
    session: AsyncSession, org_id: uuid.UUID, asset: Asset, target_url: str, alt_text: str
) -> Banner:
    """Make a new banner the active one, archiving whichever was active."""
    await _archive_active(session, org_id)
    banner = Banner(
        id=uuid.uuid4(),
        org_id=org_id,
        asset_id=asset.id,
        target_url=target_url,
        alt_text=alt_text,
        is_active=True,
    )
    session.add(banner)
    await session.commit()
    return banner


async def remove_banner(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Archive the active banner. False if there was none."""
    removed = await _archive_active(session, org_id)
    await session.commit()
    return removed


async def list_banners(session: AsyncSession, org_id: uuid.UUID) -> list[BannerView]:
    """Every banner, newest first, with its human clicks so far."""
    clicks = (
        select(TrackedLink.ref.label("ref"), func.count(ClickEvent.id).label("clicks"))
        .join(ClickEvent, ClickEvent.link_id == TrackedLink.id)
        .where(
            TrackedLink.org_id == org_id,
            TrackedLink.kind == LinkKind.BANNER,
            ClickEvent.is_bot.is_(False),
        )
        .group_by(TrackedLink.ref)
        .subquery()
    )
    rows = await session.execute(
        select(Banner, Asset, func.coalesce(clicks.c.clicks, 0))
        .join(Asset, Asset.id == Banner.asset_id)
        .outerjoin(clicks, clicks.c.ref == func.cast(Banner.id, TrackedLink.ref.type))
        .where(Banner.org_id == org_id)
        .order_by(Banner.created_at.desc())
    )
    return [BannerView(banner, asset, count) for banner, asset, count in rows.all()]
