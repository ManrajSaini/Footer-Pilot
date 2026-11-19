"""The setup wizard: authorise FooterPilot in the Admin console, then verify."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.google import get_google
from app.google.base import ADMIN_CONSOLE_DELEGATION_URL, SCOPES, GoogleGateways
from app.models import Organization, User
from app.services import accounts
from app.services.connection import VerifyResult, verify_connection
from app.tasks.sync import request_sync
from app.web.deps import APP_HOME_PATH, AccessDenied, current_org, require_member
from app.web.employees import sync_context
from app.web.templating import templates

router = APIRouter()

SETUP_SYNC_PATH = "/app/setup/sync"

# What the admin sees when verification fails: (title, explanation).
FAILURE_MESSAGES: dict[VerifyResult, tuple[str, str]] = {
    VerifyResult.NOT_AUTHORIZED: (
        "FooterPilot is not authorised yet",
        "Google has no matching entry for this Client ID with both scopes. Check that the "
        "Client ID was pasted exactly and that both scopes are in the list, separated by a "
        "comma. If you added it just now, wait a few minutes and try again: Google can take "
        "a little while to apply the change.",
    ),
    VerifyResult.NOT_ADMIN: (
        "This account is not a Workspace super admin",
        "Only a super admin can connect a domain. Sign out and sign in with a super admin "
        "account, or ask your Google Workspace admin to complete this step.",
    ),
    VerifyResult.UNAVAILABLE: (
        "The check with Google did not complete",
        "This is usually temporary. Please try again in a minute. If it keeps happening, "
        "contact us and we will look into it.",
    ),
}


@router.get("/app/setup", response_class=HTMLResponse)
async def setup(request: Request, user: User = Depends(require_member)) -> Response:
    if user.organization.is_connected:
        return RedirectResponse(APP_HOME_PATH, status_code=303)
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "app/setup.html",
        {
            "user": user,
            "organization": user.organization,
            "client_id": settings.google_dwd_client_id,
            "scopes": ",".join(SCOPES),
            "admin_console_url": ADMIN_CONSOLE_DELEGATION_URL,
            "setup_call_url": settings.setup_call_url,
        },
    )


@router.post("/app/setup/verify", response_class=HTMLResponse)
async def verify(
    request: Request,
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
    google: GoogleGateways = Depends(get_google),
) -> Response:
    result = await verify_connection(session, google, user)

    if result is VerifyResult.CONNECTED:
        # Start reading the directory straight away; step 4 shows the progress.
        await request_sync(session, user.organization)
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": SETUP_SYNC_PATH})
        return RedirectResponse(SETUP_SYNC_PATH, status_code=303)

    if result is VerifyResult.ALREADY_OWNED:
        raise AccessDenied(user, await accounts.owner_email(session, user.organization))

    title, explanation = FAILURE_MESSAGES[result]
    return templates.TemplateResponse(
        request,
        "app/_verify_result.html",
        {"title": title, "explanation": explanation, "user": user},
    )


@router.get(SETUP_SYNC_PATH, response_class=HTMLResponse)
async def setup_sync(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Step 4: the first directory sync."""
    context = await sync_context(session, organization, "setup")
    context["user"] = user
    return templates.TemplateResponse(request, "app/setup_sync.html", context)
