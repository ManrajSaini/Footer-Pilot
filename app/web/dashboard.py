"""The signed-in home page."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.models import Organization, User
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()


@router.get("/app", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "app/dashboard.html", {"user": user, "organization": organization}
    )
