"""The signature designer: template picker, form, live preview and logo upload.

Only the draft is edited here. Applying it to mailboxes (and with that, publishing it)
is a separate step.
"""

import uuid
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from app.config import get_settings
from app.db import get_session
from app.google import get_google
from app.google.base import GoogleGateways
from app.models import ApplyStatus, Asset, DeploymentTrigger, Organization, Signature, User
from app.services import deploy as deploy_service
from app.services import signatures as signature_service
from app.services.deploy import FAIL_TEMPORARY, DeploymentInProgress, NotInDirectory
from app.services.images import MAX_UPLOAD_BYTES, InvalidImageError
from app.services.signatures import UnknownLogoError, UnknownPreviewError
from app.signatures.render import (
    MAX_SIGNATURE_CHARS,
    SignatureTooLongError,
    preview_document,
    render_signature,
)
from app.signatures.schema import (
    FONT_STACKS,
    LOGO_MAX_WIDTH,
    LOGO_MIN_WIDTH,
    SOCIAL_NETWORKS,
    TEMPLATES,
    SignatureDesign,
)
from app.storage import Storage, get_storage
from app.tasks.deploy import start_deployment
from app.web.deps import APP_HOME_PATH, current_org, require_member
from app.web.forms import error_text
from app.web.templating import templates

router = APIRouter()

DESIGNER_PATH = "/app/designer"
LENGTH_WARNING_FROM = 9_000  # characters; Gmail refuses a signature above 10,000

TOGGLES = {
    "show_photo": "Photo",
    "show_job_title": "Job title",
    "show_department": "Department",
    "show_phone_work": "Work phone",
    "show_phone_mobile": "Mobile phone",
    "show_email": "Email address",
}

# Where a validation error on the design model is shown in the form.
_ERROR_FIELDS = {
    ("template",): "template",
    ("brand", "primary_color"): "primary_color",
    ("brand", "text_color"): "text_color",
    ("brand", "font_family"): "font_family",
    ("brand", "font_size"): "font_size",
    ("logo", "asset_id"): "logo",
    ("logo", "width_px"): "logo_width",
    ("logo", "link_url"): "logo_link_url",
    ("company", "name"): "company_name",
    ("company", "tagline"): "company_tagline",
    ("company", "address"): "company_address",
    ("company", "phone"): "company_phone",
    ("company", "website_url"): "company_website_url",
}
# Every place an error can appear; each is re-sent (usually empty) on every save.
ERROR_FIELDS = [*_ERROR_FIELDS.values(), *(f"social_{key}" for key in SOCIAL_NETWORKS), "general"]


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def design_from_form(form: FormData) -> SignatureDesign:
    """Build the design from the designer form. Raises FormErrors."""
    socials = [
        {"network": key, "url": url}
        for key in SOCIAL_NETWORKS
        if (url := _text(form, f"social_{key}").strip())
    ]
    data = {
        "template": _text(form, "template"),
        "brand": {
            "primary_color": _text(form, "primary_color"),
            "text_color": _text(form, "text_color"),
            "font_family": _text(form, "font_family"),
            "font_size": _text(form, "font_size"),
        },
        "logo": {
            "asset_id": _text(form, "logo_asset_id") or None,
            "width_px": _text(form, "logo_width"),
            "link_url": _text(form, "logo_link_url"),
        },
        # An unticked checkbox is simply absent from the form.
        "fields": {name: name in form for name in TOGGLES},
        "company": {
            "name": _text(form, "company_name"),
            "tagline": _text(form, "company_tagline"),
            "address": _text(form, "company_address"),
            "phone": _text(form, "company_phone"),
            "website_url": _text(form, "company_website_url"),
        },
        "socials": socials,
    }
    try:
        return SignatureDesign.model_validate(data)
    except ValidationError as error:
        errors: dict[str, str] = {}
        for detail in error.errors():
            location = tuple(detail["loc"])
            if location[:1] == ("socials",) and len(location) > 1:
                field = f"social_{socials[location[1]]['network']}"
            else:
                field = _ERROR_FIELDS.get(location[:2], "general")
            errors.setdefault(field, error_text(detail))
        raise FormErrors(errors) from error


class FormErrors(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors


def _length_level(length: int) -> str:
    return "warning" if length >= LENGTH_WARNING_FROM else "ok"


def _preview_context(signature_html: str) -> dict[str, Any]:
    return {
        "preview_document": preview_document(signature_html),
        "length": len(signature_html),
        "length_level": _length_level(len(signature_html)),
        "max_length": MAX_SIGNATURE_CHARS,
    }


def _status_context(signature: Signature, *, saved: bool = False) -> dict[str, Any]:
    return {
        "is_published": signature.published_design is not None,
        "has_changes": signature_service.has_unpublished_changes(signature),
        "saved": saved,
    }


async def _require_signature(session: AsyncSession, organization: Organization) -> Signature:
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None:
        # No template chosen yet: back to the picker.
        raise HTTPException(status_code=409, headers={"HX-Redirect": DESIGNER_PATH})
    return signature


# --- Pages ----------------------------------------------------------------------------


@router.get(DESIGNER_PATH, response_class=HTMLResponse)
async def designer(
    request: Request,
    preview_as: str = "",
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    signature = await signature_service.get_signature(session, organization.id)
    employees = await signature_service.list_preview_employees(session, organization.id)
    selected = preview_as or signature_service.default_preview(employees, user.email)
    try:
        subject = signature_service.preview_subject(selected, employees, organization)
    except UnknownPreviewError:
        selected = signature_service.default_preview(employees, user.email)
        subject = signature_service.preview_subject(selected, employees, organization)

    base = {"user": user, "organization": organization}

    if signature is None:
        # Step 5 of setup: choose a starting layout. No logo exists yet (and the person
        # may have no photo), so stand-ins are used: without them three of the four
        # layouts would look the same.
        images = f"{get_settings().asset_base_url}/static/img"
        subject = replace(subject, photo_url=subject.photo_url or f"{images}/placeholder-photo.png")
        cards = []
        for key, label in TEMPLATES.items():
            design = signature_service.default_design(organization, key)
            org_ctx = await signature_service.build_org_context(session, organization, design)
            org_ctx = replace(org_ctx, logo_url=f"{images}/placeholder-logo.png")
            html = render_signature(design, subject, org_ctx)
            cards.append({"key": key, "label": label, "document": preview_document(html)})
        return templates.TemplateResponse(
            request, "app/designer_start.html", {**base, "cards": cards}
        )

    design = signature_service.draft_of(signature)
    errors: dict[str, str] = {}
    try:
        html = await signature_service.render_preview(session, organization, design, subject)
    except (UnknownLogoError, SignatureTooLongError) as error:
        html = ""
        errors["logo" if isinstance(error, UnknownLogoError) else "general"] = str(error)

    logo = await signature_service.get_logo(session, organization.id, design.logo.asset_id)
    context = {
        **base,
        **_preview_context(html),
        **_status_context(signature),
        "design": design,
        "socials": {social.network: social.url for social in design.socials},
        "logo": logo,
        "logo_error": None,
        "errors": errors,
        "error_fields": ERROR_FIELDS,
        "preview_choices": signature_service.preview_choices(employees),
        "preview_as": selected,
        "layouts": TEMPLATES,
        "fonts": list(FONT_STACKS),
        "font_sizes": {"small": "Small", "medium": "Medium", "large": "Large"},
        "social_networks": SOCIAL_NETWORKS,
        "toggles": TOGGLES,
        "logo_min": LOGO_MIN_WIDTH,
        "logo_max": LOGO_MAX_WIDTH,
        "eligible_count": await deploy_service.count_eligible(session, organization.id),
        "images_are_public": get_settings().images_are_public,
        "tracking_is_public": get_settings().tracking_is_public,
    }
    return templates.TemplateResponse(request, "app/designer.html", context)


@router.post("/app/designer/start")
async def start(
    template: str = Form(),
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if template not in TEMPLATES:
        raise HTTPException(status_code=400, detail="Unknown layout")
    await signature_service.create_signature(session, organization, template)  # type: ignore[arg-type]
    return RedirectResponse(DESIGNER_PATH, status_code=303)


# --- Saving and previewing ------------------------------------------------------------


@dataclass
class _Saved:
    signature: Signature
    design: SignatureDesign
    html: str  # rendered for the person chosen under "Preview as"


class _Rejected(Exception):
    """The form did not validate. Nothing was saved."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors


async def _save_form(
    request: Request, organization: Organization, user: User, session: AsyncSession
) -> _Saved:
    """Validate the designer form and save it as the draft. Raises _Rejected.

    Every action that uses the draft (preview, apply) goes through here first, so it
    always works from exactly what is on screen, even within the autosave delay.
    """
    signature = await _require_signature(session, organization)
    form = await request.form()
    employees = await signature_service.list_preview_employees(session, organization.id)
    selected = _text(form, "preview_as") or signature_service.default_preview(employees, user.email)
    try:
        subject = signature_service.preview_subject(selected, employees, organization)
    except UnknownPreviewError:
        # Not one of this organisation's employees.
        raise HTTPException(status_code=404, detail="Unknown employee") from None

    try:
        design = design_from_form(form)
        html = await signature_service.render_preview(session, organization, design, subject)
    except FormErrors as error:
        raise _Rejected(error.errors) from error
    except UnknownLogoError as error:
        raise _Rejected({"logo": str(error)}) from error
    except SignatureTooLongError as error:
        raise _Rejected({"general": str(error)}) from error

    await signature_service.save_draft(session, signature, design)
    return _Saved(signature, design, html)


def _errors_response(request: Request, errors: dict[str, str]) -> HTMLResponse:
    """Only the messages: the last good preview stays on screen."""
    return templates.TemplateResponse(
        request,
        "app/_designer_errors.html",
        {"errors": errors, "error_fields": ERROR_FIELDS},
        headers={"HX-Reswap": "none"},
    )


@router.post("/app/designer/draft", response_class=HTMLResponse)
async def save_draft(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Validate the form, save it as the draft and return the new preview.

    If anything is wrong, nothing is saved: the response only carries the messages.
    """
    try:
        saved = await _save_form(request, organization, user, session)
    except _Rejected as rejected:
        return _errors_response(request, rejected.errors)
    context = {
        **_preview_context(saved.html),
        **_status_context(saved.signature, saved=True),
        "errors": {},
        "error_fields": ERROR_FIELDS,
    }
    return templates.TemplateResponse(request, "app/_designer_saved.html", context)


# --- Applying -------------------------------------------------------------------------

_FIX_FIRST = "Fix the highlighted fields before applying."


def _apply_result(request: Request, kind: str, message: str, link: tuple[str, str] | None = None):
    return templates.TemplateResponse(
        request, "app/_apply_result.html", {"kind": kind, "message": message, "link": link}
    )


@router.post("/app/designer/apply-test", response_class=HTMLResponse)
async def apply_to_me(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
    google: GoogleGateways = Depends(get_google),
) -> Response:
    """Write the current draft to the admin's own mailbox only. Nothing is published."""
    try:
        saved = await _save_form(request, organization, user, session)
    except _Rejected as rejected:
        return _errors_response(request, {"general": _FIX_FIRST, **rejected.errors})

    try:
        outcome = await deploy_service.apply_to_self(
            session, google, organization, user, saved.design
        )
    except NotInDirectory:
        return _apply_result(
            request,
            "error",
            f"{user.email} is not in the employee list yet. Sync your directory first.",
            ("/app/employees", "Go to Employees"),
        )

    if outcome.status is None:
        # Access to the domain was lost; the dashboard explains and offers Reconnect.
        return Response(status_code=200, headers={"HX-Redirect": APP_HOME_PATH})
    if outcome.status is ApplyStatus.APPLIED:
        return _apply_result(
            request,
            "success",
            f"Applied to {user.email} only, as a test. Start a new message in Gmail to "
            "see it (reload Gmail if it is already open). Nobody else was changed and the "
            "design is still a draft. When it looks right, use Apply to everyone.",
        )
    if outcome.error == FAIL_TEMPORARY:
        return _apply_result(
            request, "error", "Google was temporarily unavailable. Please try again in a minute."
        )
    return _apply_result(request, "error", f"Your signature was not changed: {outcome.error}.")


@router.post("/app/designer/apply", response_class=HTMLResponse)
async def apply_to_everyone(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Publish the draft and start writing it to every included mailbox."""
    try:
        saved = await _save_form(request, organization, user, session)
    except _Rejected as rejected:
        return _errors_response(request, {"general": _FIX_FIRST, **rejected.errors})

    running = await deploy_service.manual_deployment_in_progress(session, organization.id)
    if running is None:
        await deploy_service.publish_draft(session, saved.signature)
        try:
            deployment = await start_deployment(
                session,
                organization,
                DeploymentTrigger.MANUAL,
                created_by=user.id,
                # The admin asked for everyone: write each mailbox even if unchanged,
                # which also undoes any edits employees made themselves.
                force=True,
            )
        except DeploymentInProgress:
            running = await deploy_service.manual_deployment_in_progress(session, organization.id)
        else:
            return Response(
                status_code=200, headers={"HX-Redirect": f"/app/deployments/{deployment.id}"}
            )

    link = (f"/app/deployments/{running.id}", "See its progress") if running else None
    return _apply_result(
        request, "error", "The signature is already being applied. Wait for that to finish.", link
    )


@router.post("/app/designer/discard")
async def discard(
    request: Request,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Throw away draft changes and go back to the design that is live."""
    signature = await _require_signature(session, organization)
    if not await signature_service.discard_draft(session, signature):
        raise HTTPException(status_code=409, detail="Nothing has been applied yet")
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": DESIGNER_PATH})
    return RedirectResponse(DESIGNER_PATH, status_code=303)


# --- Logo -----------------------------------------------------------------------------


def _logo_section(
    request: Request, logo: Asset | None, *, error: str | None = None, changed: bool = False
) -> HTMLResponse:
    # After a change, the form re-saves itself with the new logo id and refreshes the preview.
    headers = {"HX-Trigger-After-Settle": "designer-changed"} if changed else {}
    return templates.TemplateResponse(
        request, "app/_designer_logo.html", {"logo": logo, "logo_error": error}, headers=headers
    )


@router.post("/app/designer/logo", response_class=HTMLResponse)
async def upload_logo(
    request: Request,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
) -> HTMLResponse:
    """Store an uploaded logo. The draft is not changed here: the form saves it next."""
    await _require_signature(session, organization)
    form = await request.form()

    async def current_logo() -> Asset | None:
        """The logo the form already had, so a failed upload leaves it in place."""
        try:
            asset_id = uuid.UUID(_text(form, "logo_asset_id"))
        except ValueError:
            return None
        return await signature_service.get_logo(session, organization.id, asset_id)

    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _logo_section(request, await current_logo(), error="Choose an image to upload.")

    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    try:
        asset = await signature_service.create_logo(session, storage, organization.id, data)
    except InvalidImageError as error:
        message = str(error)
        return _logo_section(
            request,
            await current_logo(),
            error=f"That file could not be used: {message}.",
        )
    return _logo_section(request, asset, changed=True)


@router.delete("/app/designer/logo", response_class=HTMLResponse)
async def remove_logo(
    request: Request,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Clear the logo from the form. The uploaded file itself is kept."""
    await _require_signature(session, organization)
    return _logo_section(request, None, changed=True)
