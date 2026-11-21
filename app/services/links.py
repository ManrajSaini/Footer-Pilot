"""Click tracking: which links are counted, and recording a click.

A signature shows `{TRACKING_BASE_URL}/c/<token>?e=<employee short id>` in place of a
counted link. Rows are created when a design goes live or the banner changes
(`ensure_links`); rendering only looks them up (`load_link_map`), so previewing a
draft never writes to the database.
"""

import re
import secrets
import string
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Banner, ClickEvent, Employee, LinkKind, Organization, TrackedLink
from app.signatures.render import LinkKey
from app.signatures.schema import SignatureDesign

TOKEN_LENGTH = 10
_TOKEN_ALPHABET = string.ascii_letters + string.digits

# Programs that follow links without a person clicking: mail security scanners, link
# previewers and crawlers. Their clicks are stored but left out of the counts.
_BOT_AGENT = re.compile(
    r"bot|crawl|spider|slurp|preview|scan|monitor|fetch|"
    r"proofpoint|mimecast|barracuda|symantec|forcepoint|trendmicro|safelinks|"
    r"googleimageproxy|facebookexternalhit|whatsapp|telegram|skypeuripreview|"
    r"curl|wget|python-|go-http-client|java/|okhttp|libwww|httpclient|headless",
    re.IGNORECASE,
)


def new_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def is_bot(user_agent: str | None) -> bool:
    """True for a missing user agent or one that names an automated client."""
    return not user_agent or bool(_BOT_AGENT.search(user_agent))


def link_keys(design: SignatureDesign, banner: Banner | None) -> list[LinkKey]:
    """Every link in this signature that is counted: website, socials and banner."""
    keys: list[LinkKey] = []
    if design.company.website_url:
        keys.append((LinkKind.WEBSITE, "", design.company.website_url))
    keys.extend((LinkKind.SOCIAL, social.network, social.url) for social in design.socials)
    if banner is not None:
        keys.append((LinkKind.BANNER, str(banner.id), banner.target_url))
    return keys


def tracking_address(token: str) -> str:
    return f"{get_settings().tracking_base_url}/c/{token}"


async def ensure_links(
    session: AsyncSession,
    organization: Organization,
    design: SignatureDesign,
    banner: Banner | None,
) -> None:
    """Create the tracked links this signature needs. Existing ones are kept.

    Does nothing when the organisation has switched tracking off.
    """
    if not organization.tracking_enabled:
        return
    for kind, ref, target_url in link_keys(design, banner):
        # ON CONFLICT DO NOTHING: safe if two requests publish at the same moment.
        await session.execute(
            insert(TrackedLink)
            .values(
                id=uuid.uuid4(),
                org_id=organization.id,
                token=new_token(),
                kind=kind,
                ref=ref,
                target_url=target_url,
            )
            .on_conflict_do_nothing(constraint="uq_tracked_links_slot")
        )
    await session.commit()


async def load_link_map(
    session: AsyncSession,
    organization: Organization,
    design: SignatureDesign,
    banner: Banner | None,
) -> dict[LinkKey, str]:
    """The tracking address of each counted link that already has one.

    Empty when tracking is off. A link with no row yet (a draft's new address, before
    it goes live) is simply absent, and the renderer leaves it untouched.
    """
    if not organization.tracking_enabled:
        return {}
    wanted = set(link_keys(design, banner))
    if not wanted:
        return {}
    rows = await session.execute(select(TrackedLink).where(TrackedLink.org_id == organization.id))
    return {
        (link.kind, link.ref, link.target_url): tracking_address(link.token)
        for link in rows.scalars()
        if (link.kind, link.ref, link.target_url) in wanted
    }


async def record_click(
    session: AsyncSession, token: str, employee_short_id: str | None, user_agent: str | None
) -> str | None:
    """Store a click and return where to send the visitor, or None for an unknown token.

    The destination comes only from the stored link.
    """
    link = (
        await session.execute(select(TrackedLink).where(TrackedLink.token == token))
    ).scalar_one_or_none()
    if link is None:
        return None

    employee_id = None
    if employee_short_id:
        # Only an employee of the link's own organisation can be credited.
        employee_id = (
            await session.execute(
                select(Employee.id).where(
                    Employee.short_id == employee_short_id, Employee.org_id == link.org_id
                )
            )
        ).scalar_one_or_none()

    session.add(
        ClickEvent(
            org_id=link.org_id,
            link_id=link.id,
            employee_id=employee_id,
            is_bot=is_bot(user_agent),
        )
    )
    await session.commit()
    return link.target_url


async def destination(session: AsyncSession, token: str) -> str | None:
    """Where a token leads, without recording anything (for HEAD requests)."""
    result = await session.execute(select(TrackedLink.target_url).where(TrackedLink.token == token))
    return result.scalar_one_or_none()
