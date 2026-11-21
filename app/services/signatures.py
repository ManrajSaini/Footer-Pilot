"""The company signature: its draft, its assets, and rendering it for a person.

`build_org_context` and `employee_data` are the single route from database rows to the
renderer's inputs. The designer preview uses them now and deployment will use the same
two functions, so a preview is exactly what a mailbox receives.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    Asset,
    AssetKind,
    Employee,
    EmployeeStatus,
    Organization,
    Plan,
    Signature,
)
from app.services import banners as banner_service
from app.services import links as link_service
from app.services.images import normalise_logo
from app.signatures.render import EmployeeData, OrgContext, render_signature
from app.signatures.schema import SignatureDesign, Template
from app.storage import Storage

# Preview choices that are not real employees.
SAMPLE_LONGEST = "sample:longest"
SAMPLE_MISSING = "sample:missing"
SAMPLES = {
    SAMPLE_LONGEST: "Longest name and title",
    SAMPLE_MISSING: "Missing optional fields",
}


class UnknownLogoError(ValueError):
    """The design refers to a logo that is not one of this organisation's uploads."""


class UnknownPreviewError(LookupError):
    """The requested preview subject is not an employee of this organisation."""


# --- The signature row ----------------------------------------------------------------


async def get_signature(session: AsyncSession, org_id: uuid.UUID) -> Signature | None:
    result = await session.execute(select(Signature).where(Signature.org_id == org_id))
    return result.scalar_one_or_none()


def default_company_name(organization: Organization) -> str:
    """The organisation's name, or a readable guess from its domain."""
    if organization.name and organization.name != organization.domain:
        return organization.name[:80]
    return organization.domain.split(".")[0].replace("-", " ").title()[:80]


def default_design(organization: Organization, template: Template) -> SignatureDesign:
    return SignatureDesign.model_validate(
        {
            "template": template,
            "company": {
                "name": default_company_name(organization),
                "website_url": f"https://{organization.domain}",
            },
        }
    )


async def create_signature(
    session: AsyncSession, organization: Organization, template: Template
) -> Signature:
    """Create the organisation's signature from a template, if it has none yet."""
    existing = await get_signature(session, organization.id)
    if existing is not None:
        return existing
    signature = Signature(
        org_id=organization.id,
        draft_design=default_design(organization, template).model_dump(mode="json"),
    )
    session.add(signature)
    await session.commit()
    return signature


def draft_of(signature: Signature) -> SignatureDesign:
    return SignatureDesign.model_validate(signature.draft_design)


def has_unpublished_changes(signature: Signature) -> bool:
    """True when the draft differs from what is live (or nothing is live yet)."""
    return signature.published_design != signature.draft_design


async def save_draft(session: AsyncSession, signature: Signature, design: SignatureDesign) -> None:
    data = design.model_dump(mode="json")
    if data != signature.draft_design:
        signature.draft_design = data
        signature.updated_at = datetime.now(UTC)
    await session.commit()


async def discard_draft(session: AsyncSession, signature: Signature) -> bool:
    """Reset the draft to the published design. False if nothing is published."""
    if signature.published_design is None:
        return False
    signature.draft_design = dict(signature.published_design)
    await session.commit()
    return True


# --- Logos ----------------------------------------------------------------------------


async def get_logo(
    session: AsyncSession, org_id: uuid.UUID, asset_id: uuid.UUID | None
) -> Asset | None:
    """This organisation's logo asset with that id, or None."""
    if asset_id is None:
        return None
    result = await session.execute(
        select(Asset).where(
            Asset.id == asset_id, Asset.org_id == org_id, Asset.kind == AssetKind.LOGO
        )
    )
    return result.scalar_one_or_none()


async def create_logo(
    session: AsyncSession, storage: Storage, org_id: uuid.UUID, data: bytes
) -> Asset:
    """Validate, re-encode and store an uploaded logo. Raises InvalidImageError."""
    image = normalise_logo(data)
    key = f"orgs/{org_id}/logos/{image.digest}.{image.extension}"
    url = await storage.put(key, image.data, image.content_type)
    asset = Asset(
        org_id=org_id,
        kind=AssetKind.LOGO,
        storage_key=key,
        url=url,
        width=image.width,
        height=image.height,
        content_type=image.content_type,
        bytes=len(image.data),
    )
    session.add(asset)
    await session.commit()
    return asset


# --- Rendering ------------------------------------------------------------------------


async def build_org_context(
    session: AsyncSession, organization: Organization, design: SignatureDesign
) -> OrgContext:
    """Everything the renderer needs besides the design and the employee.

    Raises UnknownLogoError if the design points at a logo this organisation does not own.
    """
    settings = get_settings()
    logo = await get_logo(session, organization.id, design.logo.asset_id)
    if design.logo.asset_id is not None and logo is None:
        raise UnknownLogoError("The logo could not be found. Please upload it again.")
    active = await banner_service.active_banner(session, organization.id)
    banner = active[0] if active else None
    return OrgContext(
        social_icon_base_url=f"{settings.asset_base_url}/static/social",
        logo_url=logo.url if logo else None,
        banner=banner_service.banner_data(*active) if active else None,
        link_map=await link_service.load_link_map(session, organization, design, banner),
        # The free plan carries a small credit link (plan rules arrive with billing).
        powered_by_url=(
            f"{settings.marketing_base_url}/?ref=signature"
            if organization.plan == Plan.FREE
            else None
        ),
    )


def employee_data(employee: Employee) -> EmployeeData:
    return EmployeeData(
        full_name=employee.full_name,
        job_title=employee.job_title,
        department=employee.department,
        email=employee.email,
        phone_work=employee.phone_work,
        phone_mobile=employee.phone_mobile,
        photo_url=employee.photo_url,
        short_id=employee.short_id,
    )


# --- Preview subjects -----------------------------------------------------------------


@dataclass(frozen=True)
class PreviewChoice:
    value: str  # an employee id, or one of the SAMPLES keys
    label: str


async def list_preview_employees(session: AsyncSession, org_id: uuid.UUID) -> list[Employee]:
    """People a signature can be previewed for: everyone still in the directory."""
    result = await session.execute(
        select(Employee)
        .where(Employee.org_id == org_id, Employee.status != EmployeeStatus.REMOVED)
        .order_by(
            case((Employee.status == EmployeeStatus.ACTIVE, 0), else_=1),
            func.lower(Employee.full_name),
            Employee.email,
        )
        .limit(1000)
    )
    return list(result.scalars())


def preview_choices(employees: list[Employee]) -> list[PreviewChoice]:
    people = [PreviewChoice(str(e.id), e.full_name or e.email) for e in employees]
    return [*people, *(PreviewChoice(value, label) for value, label in SAMPLES.items())]


def default_preview(employees: list[Employee], admin_email: str) -> str:
    """Preview as the signed-in admin if they are in the directory, else the first person."""
    for employee in employees:
        if employee.email == admin_email:
            return str(employee.id)
    return str(employees[0].id) if employees else SAMPLE_LONGEST


def _longest(values: list[str], fallback: str) -> str:
    return max(values, key=len, default="") or fallback


def sample_employee(key: str, employees: list[Employee], domain: str) -> EmployeeData:
    """A made-up person that stresses the layout, built from this company's real data."""
    if key == SAMPLE_MISSING:
        # Only what every directory entry has: a name and an address.
        return EmployeeData(full_name="Sam Lee", email=f"sam.lee@{domain}")
    return EmployeeData(
        full_name=_longest(
            [e.full_name for e in employees],
            "Maximiliana Alexandrina Wolfeschlegelsteinhausen-Bergerdorff",
        ),
        job_title=_longest(
            [e.job_title for e in employees], "Senior Vice President of Strategic Partnerships"
        ),
        department=_longest([e.department for e in employees], "Corporate Development"),
        email=_longest([e.email for e in employees], f"maximiliana.wolfeschlegel@{domain}"),
        phone_work=_longest([e.phone_work for e in employees], "+1 (415) 555-0100 ext. 1234"),
        phone_mobile=_longest([e.phone_mobile for e in employees], "+1 (415) 555-0199"),
        photo_url=next((e.photo_url for e in employees if e.photo_url), None),
    )


def preview_subject(
    preview_as: str, employees: list[Employee], organization: Organization
) -> EmployeeData:
    """Resolve a preview choice. Raises UnknownPreviewError for anything else.

    `employees` is already limited to this organisation, so an id from another
    organisation is simply not found.
    """
    if preview_as in SAMPLES:
        return sample_employee(preview_as, employees, organization.domain)
    for employee in employees:
        if str(employee.id) == preview_as:
            return employee_data(employee)
    raise UnknownPreviewError(preview_as)


async def render_preview(
    session: AsyncSession,
    organization: Organization,
    design: SignatureDesign,
    subject: EmployeeData,
) -> str:
    """Raises UnknownLogoError or SignatureTooLongError."""
    return render_signature(design, subject, await build_org_context(session, organization, design))
