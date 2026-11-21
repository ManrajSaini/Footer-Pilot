"""Click counts for the links in the company signature."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, User
from app.services import analytics as analytics_service
from app.services import banners as banner_service
from app.services import links as link_service
from app.services import signatures as signature_service
from app.services.analytics import DEFAULT_RANGE, RANGES
from app.signatures.schema import SignatureDesign
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()


@router.get("/app/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    days: str = "",
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    chosen = int(days) if days.isdecimal() and int(days) in RANGES else DEFAULT_RANGE

    # The links in the live signature are listed even before anyone clicks them.
    in_use: frozenset = frozenset()
    signature = await signature_service.get_signature(session, organization.id)
    if signature is not None and signature.published_design and organization.tracking_enabled:
        active = await banner_service.active_banner(session, organization.id)
        design = SignatureDesign.model_validate(signature.published_design)
        in_use = frozenset(link_service.link_keys(design, active[0] if active else None))

    context = {
        "user": user,
        "organization": organization,
        "ranges": RANGES,
        "stats": await analytics_service.load(session, organization.id, chosen, in_use=in_use),
    }
    return templates.TemplateResponse(request, "app/analytics.html", context)
