"""The employees page: the synced directory, search, include/exclude and Sync now."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, SyncStatus, User
from app.services import deploy as deploy_service
from app.services import employees as employee_service
from app.services import sync as sync_service
from app.tasks.deploy import change_exclusion
from app.tasks.sync import request_sync
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()

# Sentences for the codes the sync service stores in organizations.last_sync_error.
SYNC_ERRORS = {
    sync_service.ERROR_NOT_ADMIN: (
        "The account that connected FooterPilot is no longer a Workspace super admin."
    ),
    sync_service.ERROR_UNAVAILABLE: "Google could not be reached. Please try again in a minute.",
    sync_service.ERROR_NOT_AUTHORIZED: (
        "FooterPilot no longer has access to your directory. Reconnect to continue."
    ),
}

# Where the status fragment is shown: the employees page, or step 4 of the wizard.
SYNC_VIEWS = ("employees", "setup")


def _sync_state(organization: Organization) -> str:
    if sync_service.sync_in_progress(organization):
        return "running"
    if sync_service.sync_is_stale(organization):
        return "stale"
    if organization.last_sync_status == SyncStatus.FAILED:
        return "failed"
    if organization.last_sync_status == SyncStatus.OK:
        return "ok"
    return "never"


async def sync_context(
    session: AsyncSession, organization: Organization, view: str
) -> dict[str, Any]:
    state = _sync_state(organization)
    # A sync that changed someone is followed by writing their signature. Keep the
    # page updating until that is done, so "Not applied yet" does not linger.
    applying = None
    if state != "running":
        applying = await deploy_service.deployment_in_progress(session, organization.id)
        if applying is not None:
            state = "applying"
    return {
        "organization": organization,
        "view": view,
        "state": state,
        "applying": applying,
        "sync_error": SYNC_ERRORS.get(organization.last_sync_error or "", "The sync failed."),
        "counts": await employee_service.count_employees(session, organization.id),
    }


def _view(value: str) -> str:
    if value not in SYNC_VIEWS:
        raise HTTPException(status_code=400, detail="Unknown view")
    return value


@router.get("/app/employees", response_class=HTMLResponse)
async def employees_page(
    request: Request,
    q: str = "",
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    context = await sync_context(session, organization, "employees")
    context.update(
        user=user,
        search=q,
        employees=await employee_service.list_employees(session, organization.id, q),
    )
    return templates.TemplateResponse(request, "app/employees.html", context)


@router.get("/app/employees/rows", response_class=HTMLResponse)
async def employee_rows(
    request: Request,
    q: str = "",
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """The table body only: used by search and refreshed when a sync finishes."""
    employees = await employee_service.list_employees(session, organization.id, q)
    return templates.TemplateResponse(
        request, "app/_employee_rows.html", {"employees": employees, "search": q}
    )


@router.get("/app/employees/counts", response_class=HTMLResponse)
async def employee_counts(
    request: Request,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    counts = await employee_service.count_employees(session, organization.id)
    return templates.TemplateResponse(request, "app/_employee_counts.html", {"counts": counts})


@router.post("/app/employees/sync", response_class=HTMLResponse)
async def sync_now(
    request: Request,
    view: str = Form("employees"),
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    await request_sync(session, organization)
    context = await sync_context(session, organization, _view(view))
    return templates.TemplateResponse(request, "app/_sync_status.html", context)


@router.get("/app/employees/sync-status", response_class=HTMLResponse)
async def sync_status(
    request: Request,
    view: str = "employees",
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Polled while a sync runs. Once it has finished, tells the page to reload its rows."""
    context = await sync_context(session, organization, _view(view))
    busy = context["state"] in ("running", "applying")
    headers = {} if busy else {"HX-Trigger": "employees-changed"}
    return templates.TemplateResponse(request, "app/_sync_status.html", context, headers=headers)


@router.post("/app/employees/{employee_id}/excluded", response_class=HTMLResponse)
async def set_excluded(
    request: Request,
    employee_id: uuid.UUID,
    excluded: bool = Form(),
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    employee = await change_exclusion(session, organization, employee_id, excluded)
    if employee is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "app/_employee_row.html",
        {"employee": employee},
        # The counts line and the status line reload themselves on this event.
        headers={"HX-Trigger": "employee-toggled"},
    )
