"""The promotional banner: upload, preview inside the signature, activate and remove.

The banner is not part of the signature design. One banner is active at a time;
activating or removing one updates every mailbox, like "Apply to everyone".
"""

import uuid
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from app.config import get_settings
from app.db import get_session
from app.models import Asset, Deployment, DeploymentTrigger, Organization, Signature, User
from app.services import banners as banner_service
from app.services import deploy as deploy_service
from app.services import signatures as signature_service
from app.services.deploy import DeploymentInProgress
from app.services.images import MAX_UPLOAD_BYTES, InvalidImageError
from app.services.signatures import SAMPLE_LONGEST, UnknownLogoError
from app.signatures.render import (
    MAX_SIGNATURE_CHARS,
    BannerData,
    EmployeeData,
    SignatureTooLongError,
    preview_document,
    render_signature,
)
from app.signatures.schema import HttpUrl, SignatureDesign, text
from app.storage import Storage, get_storage
from app.tasks.deploy import start_deployment
from app.web.deps import current_org, require_member
from app.web.designer import DESIGNER_PATH, LENGTH_WARNING_FROM
from app.web.forms import error_text
from app.web.templating import templates

router = APIRouter()

BANNER_PATH = "/app/banner"
ERROR_FIELDS = ["banner", "target_url", "alt_text", "general"]

_NEEDS_IMAGE = "Upload a banner image first."
# Without one, a banner whose image is blocked or fails to load shows nothing at all.
_NEEDS_DESCRIPTION = "Add a short description. It is shown when the image does not load."
_TOO_LONG = (
    "With this banner the signature is longer than Gmail allows. Shorten the web "
    "address, or remove some text or links in the signature designer."
)
_BUSY = "Signatures are being applied right now. Wait for that to finish, then try again."


class _Details(BaseModel):
    """Where the banner leads, and the words shown when its image does not load."""

    model_config = ConfigDict(extra="forbid")

    target_url: HttpUrl
    alt_text: text(160) = ""  # type: ignore[valid-type]


class _Rejected(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors


@dataclass(frozen=True)
class _Candidate:
    """The banner described by the form, not yet active."""

    asset: Asset
    target_url: str | None  # None while the address is still empty (preview only)
    alt_text: str

    def data(self) -> BannerData:
        return BannerData(
            image_url=self.asset.url,
            # "#" keeps the image clickable-looking in the preview without leading anywhere.
            target_url=self.target_url or "#",
            alt_text=self.alt_text,
            width_px=banner_service.display_width(self.asset),
        )


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


async def _form_asset(
    session: AsyncSession, organization: Organization, form: FormData
) -> Asset | None:
    """The uploaded image the form refers to, if it is one of this organisation's."""
    try:
        asset_id = uuid.UUID(_text(form, "banner_asset_id"))
    except ValueError:
        return None
    return await banner_service.get_banner_asset(session, organization.id, asset_id)


async def _candidate(
    session: AsyncSession, organization: Organization, form: FormData, *, complete: bool
) -> _Candidate:
    """Read the banner form. Raises _Rejected.

    `complete` is what activating needs: an image and a web address. The preview is
    more lenient and accepts an address that has not been typed yet.
    """
    errors: dict[str, str] = {}
    asset = await _form_asset(session, organization, form)
    if asset is None:
        errors["banner"] = _NEEDS_IMAGE

    target_url, alt_text = _text(form, "target_url"), _text(form, "alt_text")
    details: _Details | None = None
    if target_url.strip() or complete:
        try:
            details = _Details(target_url=target_url, alt_text=alt_text)
        except ValidationError as error:
            for detail in error.errors():
                field = str(detail["loc"][0]) if detail["loc"] else "general"
                errors.setdefault(field if field in ERROR_FIELDS else "general", error_text(detail))
    else:
        try:
            alt_text = _Details(target_url="https://example.com", alt_text=alt_text).alt_text
        except ValidationError as error:
            errors["alt_text"] = error_text(error.errors()[0])

    if complete and details is not None and not details.alt_text:
        errors["alt_text"] = _NEEDS_DESCRIPTION

    if errors or asset is None:
        raise _Rejected(errors)
    if details is not None:
        return _Candidate(asset, details.target_url, details.alt_text)
    return _Candidate(asset, None, alt_text)


def _design(signature: Signature) -> SignatureDesign:
    """What the banner is shown in: the live design, or the draft before anything is live."""
    return SignatureDesign.model_validate(signature.published_design or signature.draft_design)


async def _subject(
    session: AsyncSession, organization: Organization, user: User, *, longest: bool = False
) -> EmployeeData:
    employees = await signature_service.list_preview_employees(session, organization.id)
    choice = SAMPLE_LONGEST if longest else signature_service.default_preview(employees, user.email)
    return signature_service.preview_subject(choice, employees, organization)


_KEEP = object()


async def _render(
    session: AsyncSession,
    organization: Organization,
    design: SignatureDesign,
    subject: EmployeeData,
    banner: Any = _KEEP,
) -> str:
    """The signature with the given banner (or none) in place of the active one.

    Raises UnknownLogoError or SignatureTooLongError.
    """
    org_ctx = await signature_service.build_org_context(session, organization, design)
    if banner is not _KEEP:
        org_ctx = replace(org_ctx, banner=banner)
    return render_signature(design, subject, org_ctx)


def _preview_context(signature_html: str) -> dict[str, Any]:
    length = len(signature_html)
    return {
        "preview_document": preview_document(signature_html),
        "length": length,
        "length_level": "warning" if length >= LENGTH_WARNING_FROM else "ok",
        "max_length": MAX_SIGNATURE_CHARS,
    }


def _errors_response(request: Request, errors: dict[str, str]) -> HTMLResponse:
    """Only the messages: the last good preview stays on screen."""
    return templates.TemplateResponse(
        request,
        "app/_designer_errors.html",
        {"errors": errors, "error_fields": ERROR_FIELDS},
        headers={"HX-Reswap": "none"},
    )


def _result(request: Request, message: str, link: tuple[str, str] | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "app/_apply_result.html", {"kind": "error", "message": message, "link": link}
    )


def _go(path: str) -> Response:
    return Response(status_code=200, headers={"HX-Redirect": path})


# --- Page -----------------------------------------------------------------------------


@router.get(BANNER_PATH, response_class=HTMLResponse)
async def banner_page(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    signature = await signature_service.get_signature(session, organization.id)
    base = {"user": user, "organization": organization, "designer_path": DESIGNER_PATH}
    if signature is None:
        return templates.TemplateResponse(request, "app/banner.html", {**base, "signature": None})

    errors: dict[str, str] = {}
    try:
        html = await _render(
            session, organization, _design(signature), await _subject(session, organization, user)
        )
    except (UnknownLogoError, SignatureTooLongError) as error:
        html = ""
        errors["general"] = str(error)

    banners = await banner_service.list_banners(session, organization.id)
    settings = get_settings()
    context = {
        **base,
        **_preview_context(html),
        "signature": signature,
        "is_published": signature.published_design is not None,
        "current": next((view for view in banners if view.banner.is_active), None),
        "past": [view for view in banners if not view.banner.is_active],
        "asset": None,
        "banner_error": None,
        "errors": errors,
        "error_fields": ERROR_FIELDS,
        "eligible_count": await deploy_service.count_eligible(session, organization.id),
        "images_are_public": settings.images_are_public,
        "tracking_is_public": settings.tracking_is_public,
    }
    return templates.TemplateResponse(request, "app/banner.html", context)


# --- Image ----------------------------------------------------------------------------


def _image_section(
    request: Request, asset: Asset | None, *, error: str | None = None, changed: bool = False
) -> HTMLResponse:
    # After a change, the form asks for a new preview with the new image id.
    headers = {"HX-Trigger-After-Settle": "banner-changed"} if changed else {}
    return templates.TemplateResponse(
        request, "app/_banner_image.html", {"asset": asset, "banner_error": error}, headers=headers
    )


@router.post("/app/banner/image", response_class=HTMLResponse)
async def upload_image(
    request: Request,
    organization: Organization = Depends(current_org),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
) -> HTMLResponse:
    """Store an uploaded banner image. Nothing is activated here."""
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _image_section(
            request,
            await _form_asset(session, organization, form),
            error="Choose an image to upload.",
        )

    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    try:
        asset = await banner_service.create_banner_asset(session, storage, organization.id, data)
    except InvalidImageError as error:
        # A failed upload leaves the image the form already had in place.
        return _image_section(
            request,
            await _form_asset(session, organization, form),
            error=f"That file could not be used: {error}.",
        )
    return _image_section(request, asset, changed=True)


# --- Preview --------------------------------------------------------------------------


@router.post("/app/banner/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """The signature with the banner from the form. Nothing is saved."""
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None:
        return _go(DESIGNER_PATH)  # type: ignore[return-value]
    try:
        candidate = await _candidate(session, organization, await request.form(), complete=False)
        html = await _render(
            session,
            organization,
            _design(signature),
            await _subject(session, organization, user),
            candidate.data(),
        )
    except _Rejected as rejected:
        # No image yet is the normal starting state, not something to complain about.
        errors = {k: v for k, v in rejected.errors.items() if v != _NEEDS_IMAGE}
        return _errors_response(request, errors)
    except SignatureTooLongError:
        return _errors_response(request, {"general": _TOO_LONG})
    except UnknownLogoError as error:
        return _errors_response(request, {"general": str(error)})

    context = {**_preview_context(html), "errors": {}, "error_fields": ERROR_FIELDS}
    return templates.TemplateResponse(request, "app/_banner_preview.html", context)


# --- Activate and remove --------------------------------------------------------------


async def _update_mailboxes(
    session: AsyncSession, organization: Organization, user: User, signature: Signature
) -> Deployment | None:
    """Write the changed signature to every mailbox, if a design is live.

    Raises DeploymentInProgress.
    """
    if signature.published_design is None:
        # Nothing is in any mailbox yet. The banner goes out with the first apply.
        return None
    await deploy_service.ensure_tracked_links(session, organization, _design(signature))
    return await start_deployment(
        session, organization, DeploymentTrigger.MANUAL, created_by=user.id
    )


async def _busy(
    request: Request, session: AsyncSession, organization: Organization
) -> HTMLResponse | None:
    running = await deploy_service.manual_deployment_in_progress(session, organization.id)
    if running is None:
        return None
    return _result(request, _BUSY, (f"/app/deployments/{running.id}", "See its progress"))


async def _after_change(
    request: Request,
    session: AsyncSession,
    organization: Organization,
    user: User,
    signature: Signature,
) -> Response:
    try:
        deployment = await _update_mailboxes(session, organization, user, signature)
    except DeploymentInProgress:
        # Started by someone else in the last moment. The banner change is saved.
        return _result(
            request,
            "The banner was saved, but signatures are being applied right now. When that "
            "finishes, use Apply to everyone in the signature designer.",
            (DESIGNER_PATH, "Go to the designer"),
        )
    return _go(f"/app/deployments/{deployment.id}" if deployment else BANNER_PATH)


@router.post("/app/banner/activate", response_class=HTMLResponse)
async def activate(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Make the banner in the form the active one and update every mailbox."""
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None:
        return _go(DESIGNER_PATH)
    try:
        candidate = await _candidate(session, organization, await request.form(), complete=True)
        # Checked for the longest name and title, so nobody's signature is refused later.
        await _render(
            session,
            organization,
            _design(signature),
            await _subject(session, organization, user, longest=True),
            candidate.data(),
        )
    except _Rejected as rejected:
        return _errors_response(
            request, {"general": "Fix the highlighted fields first.", **rejected.errors}
        )
    except SignatureTooLongError:
        return _errors_response(request, {"general": _TOO_LONG})
    except UnknownLogoError as error:
        return _errors_response(request, {"general": str(error)})

    if (busy := await _busy(request, session, organization)) is not None:
        return busy

    active = await banner_service.active_banner(session, organization.id)
    if active is not None:
        current = active[0]
        same = (current.asset_id, current.target_url, current.alt_text) == (
            candidate.asset.id,
            candidate.target_url,
            candidate.alt_text,
        )
        if same:
            return _result(request, "This banner is already the active one.")

    if candidate.target_url is None:  # cannot happen with complete=True; never guess one
        return _errors_response(request, {"target_url": "Enter a web address"})
    await banner_service.activate_banner(
        session, organization.id, candidate.asset, candidate.target_url, candidate.alt_text
    )
    return await _after_change(request, session, organization, user, signature)


@router.post("/app/banner/remove", response_class=HTMLResponse)
async def remove(
    request: Request,
    organization: Organization = Depends(current_org),
    user: User = Depends(require_member),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Take the banner out of every signature. It stays in the list of past banners."""
    signature = await signature_service.get_signature(session, organization.id)
    if signature is None:
        return _go(DESIGNER_PATH)
    if (busy := await _busy(request, session, organization)) is not None:
        return busy
    if not await banner_service.remove_banner(session, organization.id):
        return _go(BANNER_PATH)
    return await _after_change(request, session, organization, user, signature)
