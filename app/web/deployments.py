"""Progress and results of applying the signature."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ApplyStatus, Deployment, DeploymentTrigger, Organization, User
from app.services import deploy as deploy_service
from app.services.deploy import DeploymentInProgress, NothingPublished
from app.tasks.deploy import start_deployment
from app.web.deps import current_org, require_member
from app.web.templating import templates

router = APIRouter()

TRIGGER_LABELS = {
    DeploymentTrigger.MANUAL: "Apply to everyone",
    DeploymentTrigger.TEST: "Apply to me only",
    DeploymentTrigger.SYNC: "Automatic update after a directory sync",
    DeploymentTrigger.ENFORCE: "Daily re-apply",
    DeploymentTrigger.PLAN_CHANGE: "Plan change",
}


async def _deployment(
    session: AsyncSession, organization: Organization, deployment_id: uuid.UUID
) -> Deployment:
    await deploy_service.expire_stale_deployments(session, organization.id)
    deployment = await deploy_service.get_deployment(session, organization.id, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404)
    return deployment


async def _status_context(
    session: AsyncSession, organization: Organization, deployment: Deployment
) -> dict[str, Any]:
    attention = (
        []
        if deployment.in_progress
        else await deploy_service.employees_needing_attention(session, organization.id)
    )
    return {
        "deployment": deployment,
        "trigger_label": TRIGGER_LABELS.get(deployment.trigger, deployment.trigger),
        "percent": round(100 * deployment.processed / deployment.total)
        if deployment.total
        else 100,
        "attention": attention,
        "can_retry": any(employee.apply_status == ApplyStatus.FAILED for employee in attention),
    }


@router.get("/app/deployments/{deployment_id}", response_class=HTMLResponse)
async def deployment_page(
    request: Request,
    deployment_id: uuid.UUID,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    deployment = await _deployment(session, organization, deployment_id)
    context = await _status_context(session, organization, deployment)
    context.update(user=user, organization=organization)
    return templates.TemplateResponse(request, "app/deployment.html", context)


@router.get("/app/deployments/{deployment_id}/status", response_class=HTMLResponse)
async def deployment_status(
    request: Request,
    deployment_id: uuid.UUID,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Polled every two seconds while the deployment runs."""
    deployment = await _deployment(session, organization, deployment_id)
    context = await _status_context(session, organization, deployment)
    return templates.TemplateResponse(request, "app/_deployment_status.html", context)


@router.post("/app/deployments/retry-failed")
async def retry_failed(
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Try again for the employees whose latest attempt failed."""
    attention = await deploy_service.employees_needing_attention(session, organization.id)
    failed = {e.id for e in attention if e.apply_status == ApplyStatus.FAILED}
    if not failed:
        raise HTTPException(status_code=409, detail="Nothing to retry")
    try:
        deployment = await start_deployment(
            session,
            organization,
            DeploymentTrigger.MANUAL,
            created_by=user.id,
            employee_ids=failed,
            force=True,
        )
    except (DeploymentInProgress, NothingPublished) as error:
        raise HTTPException(status_code=409, detail="Cannot retry right now") from error
    return Response(status_code=200, headers={"HX-Redirect": f"/app/deployments/{deployment.id}"})
