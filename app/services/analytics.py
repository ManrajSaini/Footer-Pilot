"""Click counts for the analytics page.

Clicks by automated clients (`is_bot`) are never counted. Days are UTC days.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Banner, ClickEvent, LinkKind, TrackedLink
from app.signatures.render import LinkKey
from app.signatures.schema import SOCIAL_NETWORKS

RANGES = (7, 30, 90)
DEFAULT_RANGE = 30
_X_LABELS = 6  # about this many dates are written under the chart


@dataclass(frozen=True)
class DayCount:
    day: date
    clicks: int
    percent: float  # height of the bar, as a share of the top of the axis
    show_label: bool  # whether the date is written under this bar


@dataclass(frozen=True)
class LinkCount:
    label: str
    detail: str  # extra words after the label, for example "current" or "archived"
    target_url: str
    clicks: int


@dataclass(frozen=True)
class Analytics:
    days: int
    total: int
    per_day: list[DayCount]
    axis_ticks: list[int]  # from 0 up to the top of the axis
    links: list[LinkCount]

    @property
    def busiest(self) -> DayCount | None:
        best = max(self.per_day, key=lambda item: item.clicks, default=None)
        return best if best and best.clicks else None


def axis_ticks(peak: int) -> list[int]:
    """Round tick values from 0 to at or above `peak`, at most five of them."""
    if peak <= 4:
        return list(range(5))
    rough = peak / 4
    magnitude = 10 ** math.floor(math.log10(rough))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= rough)
    top = math.ceil(peak / step) * step
    return list(range(0, top + 1, step))


def _link_label(link: TrackedLink, banners: dict[str, Banner]) -> tuple[str, str]:
    if link.kind == LinkKind.WEBSITE:
        return "Website", ""
    if link.kind == LinkKind.SOCIAL:
        return SOCIAL_NETWORKS.get(link.ref, link.ref.title()), ""
    banner = banners.get(link.ref)
    if banner is None:
        return "Banner", "removed"
    name = f"Banner: {banner.alt_text}" if banner.alt_text else "Banner"
    return name, "current" if banner.is_active else f"until {banner.archived_at:%d %b %Y}"


async def load(
    session: AsyncSession,
    org_id: uuid.UUID,
    days: int,
    *,
    in_use: frozenset[LinkKey] = frozenset(),
    today: date | None = None,
) -> Analytics:
    """Clicks for the last `days` days, today included.

    `in_use` names the links in the signature right now; they are listed even with no
    clicks. Other links (an earlier banner, a changed address) appear once clicked.
    """
    today = today or datetime.now(UTC).date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, time.min, tzinfo=UTC)
    counted = and_(ClickEvent.is_bot.is_(False), ClickEvent.clicked_at >= since)

    day = cast(func.timezone("UTC", ClickEvent.clicked_at), Date)
    rows = await session.execute(
        select(day, func.count()).where(ClickEvent.org_id == org_id, counted).group_by(day)
    )
    by_day = dict(rows.all())

    ticks = axis_ticks(max(by_day.values(), default=0))
    label_every = math.ceil(days / _X_LABELS) if days > 7 else 1
    per_day = [
        DayCount(
            day=first + timedelta(days=index),
            clicks=by_day.get(first + timedelta(days=index), 0),
            percent=round(100 * by_day.get(first + timedelta(days=index), 0) / ticks[-1], 2),
            # Counted back from today, so the most recent day always carries its date.
            show_label=(days - 1 - index) % label_every == 0,
        )
        for index in range(days)
    ]

    clicks = (
        select(ClickEvent.link_id.label("link_id"), func.count().label("clicks"))
        .where(ClickEvent.org_id == org_id, counted)
        .group_by(ClickEvent.link_id)
        .subquery()
    )
    link_rows = await session.execute(
        select(TrackedLink, func.coalesce(clicks.c.clicks, 0).label("clicks"))
        .outerjoin(clicks, clicks.c.link_id == TrackedLink.id)
        .where(TrackedLink.org_id == org_id)
        .order_by(func.coalesce(clicks.c.clicks, 0).desc(), TrackedLink.created_at)
    )
    banner_rows = await session.execute(select(Banner).where(Banner.org_id == org_id))
    banners = {str(banner.id): banner for banner in banner_rows.scalars()}

    links = [
        LinkCount(*_link_label(link, banners), target_url=link.target_url, clicks=count)
        for link, count in link_rows.all()
        if count or (link.kind, link.ref, link.target_url) in in_use
    ]
    return Analytics(
        days=days, total=sum(by_day.values()), per_day=per_day, axis_ticks=ticks, links=links
    )
