"""Turn a design plus one employee into email-safe signature HTML.

`render_signature` is a pure function: no database, no network, no clock. The public
generator, the designer preview and deployment all call it, so what an admin previews
is byte-for-byte what lands in a mailbox.

Output rules (Gmail sanitises signature HTML): table layout, inline styles only,
allowlisted fonts, absolute image URLs with explicit width and alt text, and at most
MAX_SIGNATURE_CHARS characters.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.signatures.schema import (
    FONT_SIZES_PX,
    FONT_STACKS,
    SOCIAL_NETWORKS,
    SignatureDesign,
)

MAX_SIGNATURE_CHARS = 10_000  # Gmail's limit for a signature
SOCIAL_ICON_PX = 22
PHOTO_PX = 72
MUTED_COLOR = "#888888"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class SignatureTooLongError(ValueError):
    def __init__(self, length: int) -> None:
        self.length = length
        super().__init__(
            f"The signature is {length:,} characters; Gmail allows {MAX_SIGNATURE_CHARS:,}. "
            "Shorten the text or remove some links."
        )


@dataclass(frozen=True)
class EmployeeData:
    """Per-person details. Not validated here: the template escapes every value."""

    full_name: str = ""
    job_title: str = ""
    department: str = ""
    email: str = ""
    phone_work: str = ""
    phone_mobile: str = ""
    photo_url: str | None = None
    # Added to tracked links, so a click can be credited to this person's signature.
    short_id: str | None = None


@dataclass(frozen=True)
class BannerData:
    image_url: str
    target_url: str
    alt_text: str = ""
    width_px: int = 480
    id: str = ""  # identifies the banner's tracked link


# (kind, ref, destination) of a link whose clicks are counted. Kinds and refs:
# ("website", "", url), ("social", network, url), ("banner", banner id, url).
LinkKey = tuple[str, str, str]


@dataclass(frozen=True)
class OrgContext:
    """Everything the renderer needs that is neither design nor employee."""

    social_icon_base_url: str  # absolute URL of the folder holding <network>.png
    logo_url: str | None = None
    banner: BannerData | None = None
    powered_by_url: str | None = None  # set on the free plan
    # Tracking address for each counted link. A link missing from the map keeps its
    # real destination. `mailto:` and `tel:` links are never in it.
    link_map: Mapping[LinkKey, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Item:
    text: str
    href: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class _View:
    """Display-ready values. Templates only lay these out; decisions are made here."""

    name: str
    role: str
    company: str
    tagline: str
    contact_lines: list[list[_Item]]
    logo: dict[str, Any] | None
    photo_url: str | None
    socials: list[dict[str, str]]
    banner: BannerData | None
    powered_by_url: str | None
    styles: dict[str, Any] = field(default_factory=dict)

    @property
    def has_identity(self) -> bool:
        return bool(self.name or self.role or self.company or self.tagline)

    @property
    def contact_items(self) -> list[_Item]:
        return [item for line in self.contact_lines for item in line]


def _tel_href(phone: str) -> str | None:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 3:
        return None
    return f"tel:{'+' if phone.lstrip().startswith('+') else ''}{digits}"


def _display_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}".rstrip("/") or url


def _build_view(design: SignatureDesign, employee: EmployeeData, ctx: OrgContext) -> _View:
    show = design.fields
    company = design.company

    def tracked(kind: str, ref: str, url: str) -> str:
        """The counting address for a link, or the link itself if it is not tracked."""
        address = ctx.link_map.get((kind, ref, url))
        if address is None:
            return url
        return f"{address}?e={employee.short_id}" if employee.short_id else address

    website_href = tracked("website", "", company.website_url) if company.website_url else None

    role_parts = [
        employee.job_title.strip() if show.show_job_title else "",
        employee.department.strip() if show.show_department else "",
    ]

    phones = [
        _Item(employee.phone_work.strip(), _tel_href(employee.phone_work), "T")
        if show.show_phone_work
        else None,
        _Item(employee.phone_mobile.strip(), _tel_href(employee.phone_mobile), "M")
        if show.show_phone_mobile
        else None,
    ]
    email = employee.email.strip() if show.show_email else ""
    online = [
        _Item(email, f"mailto:{email}") if email else None,
        _Item(_display_url(company.website_url), website_href) if company.website_url else None,
    ]
    office = [
        _Item(company.phone, _tel_href(company.phone), "O") if company.phone else None,
        _Item(company.address) if company.address else None,
    ]
    contact_lines = [
        [item for item in line if item is not None and item.text]
        for line in (phones, online, office)
    ]

    logo = None
    if ctx.logo_url:
        logo = {
            "url": ctx.logo_url,
            "width": design.logo.width_px,
            "href": design.logo.link_url or website_href,
            "alt": company.name or "Logo",
        }

    banner = ctx.banner
    if banner is not None:
        banner = replace(banner, target_url=tracked("banner", banner.id, banner.target_url))

    icon_base = ctx.social_icon_base_url.rstrip("/")
    size_px = FONT_SIZES_PX[design.brand.font_size]
    return _View(
        name=employee.full_name.strip(),
        role=" · ".join(part for part in role_parts if part),
        company=company.name,
        tagline=company.tagline,
        contact_lines=[line for line in contact_lines if line],
        logo=logo,
        photo_url=employee.photo_url if show.show_photo else None,
        socials=[
            {
                "label": SOCIAL_NETWORKS[social.network],
                "url": tracked("social", social.network, social.url),
                "icon_url": f"{icon_base}/{social.network}.png",
            }
            for social in design.socials
        ],
        banner=banner,
        powered_by_url=ctx.powered_by_url,
        styles={
            "font": FONT_STACKS[design.brand.font_family],
            "size": size_px,
            "name_size": size_px + 3,
            "small_size": size_px - 2,
            "primary": design.brand.primary_color,
            "text": design.brand.text_color,
            "muted": MUTED_COLOR,
            "icon": SOCIAL_ICON_PX,
            "photo": PHOTO_PX,
        },
    )


_environment = ImmutableSandboxedEnvironment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)

_BETWEEN_TAGS = re.compile(r">\s+<")
_WHITESPACE_RUN = re.compile(r"\s{2,}")


def _minify(html: str) -> str:
    """Drop layout whitespace. Every character counts against Gmail's limit."""
    html = _BETWEEN_TAGS.sub("><", html.strip())
    return _WHITESPACE_RUN.sub(" ", html)


def render_signature(design: SignatureDesign, employee: EmployeeData, org_ctx: OrgContext) -> str:
    view = _build_view(design, employee, org_ctx)
    template = _environment.get_template(f"{design.template}.html.j2")
    html = _minify(template.render(v=view, s=view.styles))
    if len(html) > MAX_SIGNATURE_CHARS:
        raise SignatureTooLongError(len(html))
    return html


_PREVIEW_DOCUMENT = (
    '<!doctype html><html><head><meta charset="utf-8"><base target="_blank">'
    "<style>body{{margin:0;padding:16px;background:#ffffff}}</style></head>"
    "<body>{signature}</body></html>"
)


def preview_document(signature_html: str) -> str:
    """Wrap a rendered signature in a bare page for an <iframe srcdoc> preview.

    The iframe keeps the site's CSS away from the signature, so the preview shows
    what a mail client will show.
    """
    return _PREVIEW_DOCUMENT.format(signature=signature_html)
