"""The signature design model (schema version 1), stored as JSONB.

Everything a design contains is validated here, so the renderer can trust it:
colours are `#rrggbb`, URLs are http(s), fonts come from an allowlist and all text is
length-limited. Unknown keys are rejected.
"""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

MAX_URL_LENGTH = 2000
MAX_SOCIALS = 6
LOGO_MIN_WIDTH = 60
LOGO_MAX_WIDTH = 200

Template = Literal["classic", "side-logo", "photo", "minimal"]
TEMPLATES: dict[str, str] = {
    "classic": "Classic",
    "side-logo": "Side logo",
    "photo": "Photo",
    "minimal": "Minimal",
}

# Email-safe fonts only: web fonts do not survive in mail clients.
FontFamily = Literal[
    "Arial", "Helvetica", "Georgia", "Verdana", "Tahoma", "Trebuchet MS", "Times New Roman"
]
# Unquoted on purpose, so the stack can sit inside a style="..." attribute.
FONT_STACKS: dict[str, str] = {
    "Arial": "Arial, Helvetica, sans-serif",
    "Helvetica": "Helvetica, Arial, sans-serif",
    "Georgia": "Georgia, Times New Roman, serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Tahoma": "Tahoma, Verdana, sans-serif",
    "Trebuchet MS": "Trebuchet MS, Helvetica, sans-serif",
    "Times New Roman": "Times New Roman, Times, serif",
}

FontSize = Literal["small", "medium", "large"]
FONT_SIZES_PX: dict[str, int] = {"small": 12, "medium": 13, "large": 14}

SocialNetwork = Literal["linkedin", "x", "facebook", "instagram", "youtube", "github"]
SOCIAL_NETWORKS: dict[str, str] = {
    "linkedin": "LinkedIn",
    "x": "X",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "github": "GitHub",
}

_COLOR = re.compile(r"#[0-9a-f]{6}")
_HAS_HTTP_SCHEME = re.compile(r"https?://", re.IGNORECASE)
# "mailto:x" or "javascript:x" is a scheme; "example.com:8080" is a host and port.
_OTHER_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def clean_text(value: object) -> object:
    """Collapse whitespace (including newlines) and drop control characters."""
    if not isinstance(value, str):
        return value
    return " ".join(_CONTROL_CHARS.sub(" ", value).split())


def _validate_color(value: object) -> object:
    if isinstance(value, str):
        value = value.strip().lower()
        if _COLOR.fullmatch(value):
            return value
    raise ValueError("Enter a colour as a 6-digit hex code, for example #1d4ed8")


def normalise_url(value: object) -> object:
    """Accept http(s) URLs. A bare "example.com" becomes "https://example.com"."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Enter a web address")
    value = value.strip()
    if not value:
        return None
    if not _HAS_HTTP_SCHEME.match(value):
        if "://" in value or _OTHER_SCHEME.match(value):
            raise ValueError("Web addresses must start with http:// or https://")
        value = f"https://{value}"
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f"Web addresses can be at most {MAX_URL_LENGTH} characters")
    if re.search(r"\s", value) or _CONTROL_CHARS.search(value):
        raise ValueError("Web addresses cannot contain spaces")
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        hostname = None
    if not hostname:
        raise ValueError("Enter a valid web address, for example https://example.com")
    return value


def _require_url(value: str | None) -> str:
    if value is None:
        raise ValueError("Enter a web address")
    return value


def _validate_email(value: str) -> str:
    if value and not _EMAIL.fullmatch(value):
        raise ValueError("Enter a valid email address")
    return value


def text(max_length: int) -> type[str]:
    """A cleaned, length-limited string type."""
    return Annotated[str, BeforeValidator(clean_text), Field(max_length=max_length)]  # type: ignore[return-value]


Color = Annotated[str, BeforeValidator(_validate_color)]
OptionalHttpUrl = Annotated[str | None, BeforeValidator(normalise_url)]
HttpUrl = Annotated[str, BeforeValidator(normalise_url), AfterValidator(_require_url)]
EmailText = Annotated[
    str, BeforeValidator(clean_text), Field(max_length=254), AfterValidator(_validate_email)
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Brand(_Model):
    primary_color: Color = "#1d4ed8"  # names, labels, divider
    text_color: Color = "#1f2937"
    font_family: FontFamily = "Arial"
    font_size: FontSize = "medium"


class Logo(_Model):
    asset_id: UUID | None = None
    width_px: int = Field(default=120, ge=LOGO_MIN_WIDTH, le=LOGO_MAX_WIDTH)
    link_url: OptionalHttpUrl = None


class FieldToggles(_Model):
    """Which per-employee details are shown."""

    show_photo: bool = True
    show_job_title: bool = True
    show_department: bool = True
    show_phone_work: bool = True
    show_phone_mobile: bool = True
    show_email: bool = True


class Company(_Model):
    name: text(80) = ""
    tagline: text(120) = ""
    address: text(160) = ""
    phone: text(40) = ""
    website_url: OptionalHttpUrl = None


class Social(_Model):
    network: SocialNetwork
    url: HttpUrl


class SignatureDesign(_Model):
    schema_version: Literal[1] = 1
    template: Template = "classic"
    brand: Brand = Field(default_factory=Brand)
    logo: Logo = Field(default_factory=Logo)
    fields: FieldToggles = Field(default_factory=FieldToggles)
    company: Company = Field(default_factory=Company)
    socials: list[Social] = Field(default_factory=list, max_length=MAX_SOCIALS)
