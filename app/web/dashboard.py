"""The signed-in home page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, User
from app.services.employees import count_employees
from app.services.signatures import get_signature
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()


@router.get("/app", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    counts = await count_employees(session, organization.id)
    signature = await get_signature(session, organization.id)
    return templates.TemplateResponse(
        request,
        "app/dashboard.html",
        {"user": user, "organization": organization, "counts": counts, "signature": signature},
    )
