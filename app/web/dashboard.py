"""The signed-in home page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, User
from app.services.deploy import count_failed, latest_deployment, latest_test
from app.services.employees import count_employees
from app.services.signatures import get_signature
from app.web.deps import require_member, require_set_up_org
from app.web.templating import templates

router = APIRouter()


@router.get("/app", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    organization: Organization = Depends(require_set_up_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    counts = await count_employees(session, organization.id)
    context = {
        "user": user,
        "organization": organization,
        "counts": counts,
        "signature": await get_signature(session, organization.id),
        "deployment": await latest_deployment(session, organization.id),
        "test": await latest_test(session, organization.id),
        "attention": await count_failed(session, organization.id),
    }
    return templates.TemplateResponse(request, "app/dashboard.html", context)
