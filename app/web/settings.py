"""Organisation settings."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Deployment, DeploymentTrigger, Organization, User
from app.services import deploy as deploy_service
from app.services import signatures as signature_service
from app.signatures.schema import SignatureDesign
from app.tasks.deploy import start_deployment
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()


def _page(
    request: Request,
    user: User,
    organization: Organization,
    *,
    saved: bool = False,
    deployment: Deployment | None = None,
) -> HTMLResponse:
    context = {
        "user": user,
        "organization": organization,
        "saved": saved,
        "deployment": deployment,
        "tracking_is_public": get_settings().tracking_is_public,
    }
    return templates.TemplateResponse(request, "app/settings.html", context)


@router.get("/app/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
) -> HTMLResponse:
    return _page(request, user, organization)


async def _apply_tracking_change(
    session: AsyncSession, organization: Organization
) -> Deployment | None:
    """Rewrite every mailbox after click counting was switched on or off.

    The links in a signature differ between the two, so each mailbox needs the other
    version. Nothing happens while no signature is live.
    """
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None or signature.published_design is None:
        return None
    design = SignatureDesign.model_validate(signature.published_design)
    await deploy_service.ensure_tracked_links(session, organization, design)
    # An automatic update, so it can run beside an "Apply to everyone" already under
    # way: each mailbox is still only written by one job at a time.
    return await start_deployment(session, organization, DeploymentTrigger.SYNC)


@router.post("/app/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    form = await request.form()
    # An unticked checkbox is absent from the form.
    organization.daily_enforce = "daily_enforce" in form
    tracking = "tracking_enabled" in form
    tracking_changed = tracking != organization.tracking_enabled
    organization.tracking_enabled = tracking
    await session.commit()

    deployment = await _apply_tracking_change(session, organization) if tracking_changed else None
    return _page(request, user, organization, saved=True, deployment=deployment)
