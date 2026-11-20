"""Organisation settings."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, User
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()


@router.get("/app/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "app/settings.html", {"user": user, "organization": organization, "saved": False}
    )


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
    await session.commit()
    return templates.TemplateResponse(
        request, "app/settings.html", {"user": user, "organization": organization, "saved": True}
    )
