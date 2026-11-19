"""The public, no-sign-up signature generator.

Stateless: nothing is stored, no images are uploaded or fetched (image URLs are only
embedded) and no tracking links are added.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings
from app.signatures.render import (
    EmployeeData,
    OrgContext,
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
    Color,
    EmailText,
    FontFamily,
    FontSize,
    OptionalHttpUrl,
    SignatureDesign,
    Template,
    text,
)
from app.web import ratelimit
from app.web.templating import templates

router = APIRouter()

PREVIEW_PATH = "/generator/preview"
PREVIEW_RATE_LIMIT = 120  # requests per client IP ...
PREVIEW_RATE_WINDOW = 60  # ... per this many seconds


class GeneratorInput(BaseModel):
    """The generator form. Field names match the form's input names."""

    model_config = ConfigDict(extra="ignore")

    full_name: text(80) = ""
    job_title: text(80) = ""
    company_name: text(80) = ""
    phone: text(40) = ""
    email: EmailText = ""
    website_url: OptionalHttpUrl = None
    logo_url: OptionalHttpUrl = None
    logo_width: int = Field(default=120, ge=LOGO_MIN_WIDTH, le=LOGO_MAX_WIDTH)
    photo_url: OptionalHttpUrl = None

    template: Template = "classic"
    primary_color: Color = "#1d4ed8"
    text_color: Color = "#1f2937"
    font_family: FontFamily = "Arial"
    font_size: FontSize = "medium"

    social_linkedin: OptionalHttpUrl = None
    social_x: OptionalHttpUrl = None
    social_facebook: OptionalHttpUrl = None
    social_instagram: OptionalHttpUrl = None
    social_youtube: OptionalHttpUrl = None
    social_github: OptionalHttpUrl = None

    def render(self) -> str:
        socials = [
            {"network": network, "url": url}
            for network in SOCIAL_NETWORKS
            if (url := getattr(self, f"social_{network}"))
        ]
        design = SignatureDesign.model_validate(
            {
                "template": self.template,
                "brand": {
                    "primary_color": self.primary_color,
                    "text_color": self.text_color,
                    "font_family": self.font_family,
                    "font_size": self.font_size,
                },
                "logo": {"width_px": self.logo_width},
                "company": {"name": self.company_name, "website_url": self.website_url},
                "socials": socials,
            }
        )
        employee = EmployeeData(
            full_name=self.full_name,
            job_title=self.job_title,
            email=self.email,
            phone_work=self.phone,
            photo_url=self.photo_url,
        )
        org_ctx = OrgContext(
            social_icon_base_url=f"{get_settings().asset_base_url}/static/social",
            logo_url=self.logo_url,
        )
        return render_signature(design, employee, org_ctx)


# What a first-time visitor sees, so the preview is never empty.
SAMPLE = GeneratorInput(
    full_name="Alex Morgan",
    job_title="Marketing Manager",
    company_name="Acme Inc",
    phone="+1 555 010 2030",
    email="alex@acme.com",
    website_url="https://www.acme.com",
    social_linkedin="https://www.linkedin.com/in/alexmorgan",
)

FIELD_LABELS = {
    "full_name": "Full name",
    "job_title": "Job title",
    "company_name": "Company",
    "phone": "Phone",
    "email": "Email",
    "website_url": "Website",
    "logo_url": "Logo image URL",
    "logo_width": "Logo width",
    "photo_url": "Photo image URL",
    "template": "Layout",
    "primary_color": "Accent colour",
    "text_color": "Text colour",
    "font_family": "Font",
    "font_size": "Text size",
    **{f"social_{key}": label for key, label in SOCIAL_NETWORKS.items()},
}


def _error_messages(error: ValidationError) -> list[str]:
    messages = []
    for detail in error.errors():
        field = str(detail["loc"][0]) if detail["loc"] else ""
        if detail["type"] == "value_error":
            message = detail["msg"].removeprefix("Value error, ")
        elif detail["type"] in ("string_too_long", "too_long"):
            message = f"Use at most {detail['ctx']['max_length']} characters"
        else:
            message = "This value is not valid"
        messages.append(f"{FIELD_LABELS.get(field, field)}: {message}")
    return messages


def _preview_context(signature_html: str) -> dict[str, Any]:
    return {
        "signature_html": signature_html,
        "preview_document": preview_document(signature_html),
    }


def _page_context() -> dict[str, Any]:
    return {
        "form": SAMPLE,
        "templates_choices": TEMPLATES,
        "fonts": list(FONT_STACKS),
        "font_sizes": {"small": "Small", "medium": "Medium", "large": "Large"},
        "socials": SOCIAL_NETWORKS,
        "logo_min": LOGO_MIN_WIDTH,
        "logo_max": LOGO_MAX_WIDTH,
        "preview_path": PREVIEW_PATH,
    }


@router.get("/email-signature-generator", response_class=HTMLResponse)
async def generator_page(request: Request) -> HTMLResponse:
    context = {**_page_context(), **_preview_context(SAMPLE.render())}
    return templates.TemplateResponse(request, "public/generator.html", context)


@router.post(PREVIEW_PATH, response_class=HTMLResponse)
async def generator_preview(request: Request) -> HTMLResponse:
    ratelimit.enforce(request.app.state.generator_limiter, request)

    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        signature_html = GeneratorInput.model_validate(values).render()
    except ValidationError as error:
        errors = _error_messages(error)
    except SignatureTooLongError as error:
        errors = [str(error)]
    else:
        return templates.TemplateResponse(
            request,
            "public/_generator_response.html",
            {**_preview_context(signature_html), "errors": []},
        )

    # Keep the last good preview on screen and only update the error list.
    return templates.TemplateResponse(
        request,
        "public/_generator_errors.html",
        {"errors": errors},
        headers={"HX-Reswap": "none"},
    )
