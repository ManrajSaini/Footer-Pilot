"""Renderer tests.

Golden files in tests/signatures/golden/ pin the exact HTML of each template. After an
intentional template change, regenerate them and review the diff:

    UPDATE_GOLDEN=1 uv run pytest tests/signatures/test_render.py
"""

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from app.signatures.render import (
    MAX_SIGNATURE_CHARS,
    BannerData,
    EmployeeData,
    OrgContext,
    SignatureTooLongError,
    preview_document,
    render_signature,
)
from app.signatures.schema import TEMPLATES, SignatureDesign

GOLDEN_DIR = Path(__file__).parent / "golden"
ICONS = "https://assets.example.com/static/social"

FULL_EMPLOYEE = EmployeeData(
    full_name="Alex Morgan",
    job_title="Marketing Manager",
    department="Growth",
    email="alex@acme.com",
    phone_work="+1 (555) 010-2030",
    phone_mobile="+1 555 010 9999",
    photo_url="https://images.example.com/alex.jpg",
)
FULL_CONTEXT = OrgContext(
    social_icon_base_url=ICONS,
    logo_url="https://images.example.com/logo.png",
    banner=BannerData(
        image_url="https://images.example.com/banner.png",
        target_url="https://acme.com/webinar",
        alt_text="Join our webinar on Friday",
    ),
    powered_by_url="https://footerpilot.example.com/",
)
FULL_DESIGN = {
    "company": {
        "name": "Acme Inc",
        "tagline": "Tools for busy teams",
        "address": "1 Main Street, Springfield",
        "phone": "+1 555 000 0000",
        "website_url": "https://www.acme.com",
    },
    "socials": [
        {"network": "linkedin", "url": "https://www.linkedin.com/company/acme"},
        {"network": "x", "url": "https://x.com/acme"},
        {"network": "youtube", "url": "https://www.youtube.com/@acme"},
    ],
}

CASES = {
    "full": (FULL_DESIGN, FULL_EMPLOYEE, FULL_CONTEXT),
    "minimal": ({}, EmployeeData(full_name="Sam Lee", email="sam@acme.com"), OrgContext(ICONS)),
    "long_name": (
        FULL_DESIGN,
        replace(
            FULL_EMPLOYEE,
            full_name="Dr. Maximiliana Alexandrina Wolfeschlegelsteinhausen-Bergerdorff",
            job_title="Senior Vice President of Global Strategic Partnerships and Alliances",
            department="Corporate Development and International Operations",
        ),
        FULL_CONTEXT,
    ),
}


def render(template: str, design: dict, employee: EmployeeData, ctx: OrgContext) -> str:
    return render_signature(
        SignatureDesign.model_validate({**design, "template": template}), employee, ctx
    )


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("template", TEMPLATES)
def test_matches_golden_file(template: str, case: str):
    html = render(template, *CASES[case])
    path = GOLDEN_DIR / f"{template}__{case}.html"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(html + "\n", encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden file {path.name}; run with UPDATE_GOLDEN=1"
    assert html == path.read_text(encoding="utf-8").rstrip("\n")


# --- Empty fields ---------------------------------------------------------------------

EMPTY_TABLE = '<table cellpadding="0" cellspacing="0" border="0"></table>'
EMPTY_ELEMENT = re.compile(r"<(div|td|tr|span|a|table)\b[^>]*>\s*</\1>")
SEPARATOR = r"<span[^>]*>&nbsp;\|&nbsp;</span>"
DANGLING_SEPARATOR = re.compile(rf"<div[^>]*>{SEPARATOR}|{SEPARATOR}</div>|{SEPARATOR}{SEPARATOR}")

SPARSE_CASES = {
    "no title": (FULL_DESIGN, replace(FULL_EMPLOYEE, job_title=""), FULL_CONTEXT),
    "no title or department": (
        FULL_DESIGN,
        replace(FULL_EMPLOYEE, job_title="", department=""),
        FULL_CONTEXT,
    ),
    "no phones": (
        FULL_DESIGN,
        replace(FULL_EMPLOYEE, phone_work="", phone_mobile=""),
        FULL_CONTEXT,
    ),
    "no work phone": (FULL_DESIGN, replace(FULL_EMPLOYEE, phone_work=""), FULL_CONTEXT),
    "no email": (FULL_DESIGN, replace(FULL_EMPLOYEE, email=""), FULL_CONTEXT),
    "no logo": (FULL_DESIGN, FULL_EMPLOYEE, replace(FULL_CONTEXT, logo_url=None)),
    "no photo": (FULL_DESIGN, replace(FULL_EMPLOYEE, photo_url=None), FULL_CONTEXT),
    "no name": (FULL_DESIGN, replace(FULL_EMPLOYEE, full_name=""), FULL_CONTEXT),
    "no company details": ({}, FULL_EMPLOYEE, OrgContext(ICONS)),
    "only a name": ({}, EmployeeData(full_name="Sam Lee"), OrgContext(ICONS)),
    "only contacts": (
        {},
        EmployeeData(email="sam@acme.com", phone_work="555 0100"),
        OrgContext(ICONS),
    ),
    "only a logo": ({}, EmployeeData(), OrgContext(ICONS, logo_url="https://i.example.com/l.png")),
}


@pytest.mark.parametrize("case", SPARSE_CASES)
@pytest.mark.parametrize("template", TEMPLATES)
def test_empty_fields_leave_no_gaps(template: str, case: str):
    html = render(template, *SPARSE_CASES[case])
    if html == EMPTY_TABLE:  # the text-only layout, when there is only a logo to show
        assert (template, case) == ("minimal", "only a logo")
        return
    assert not EMPTY_ELEMENT.search(html), EMPTY_ELEMENT.search(html)
    assert not DANGLING_SEPARATOR.search(html), DANGLING_SEPARATOR.search(html)


@pytest.mark.parametrize(
    "employee",
    [EmployeeData(), EmployeeData(full_name="  ", job_title=" ", email=" ", phone_work="\t")],
    ids=["empty", "whitespace only"],
)
@pytest.mark.parametrize("template", TEMPLATES)
def test_nothing_to_show_renders_an_empty_table(template: str, employee: EmployeeData):
    assert render(template, {}, employee, OrgContext(ICONS)) == EMPTY_TABLE


def test_field_toggles_hide_employee_details():
    hidden = {
        **FULL_DESIGN,
        "fields": {
            "show_photo": False,
            "show_job_title": False,
            "show_department": False,
            "show_phone_work": False,
            "show_phone_mobile": False,
            "show_email": False,
        },
    }
    html = render("photo", hidden, FULL_EMPLOYEE, FULL_CONTEXT)
    for value in (
        "Marketing Manager",
        "Growth",
        "010-2030",
        "010 9999",
        "alex@acme.com",
        "alex.jpg",
    ):
        assert value not in html
    assert "Alex Morgan" in html


# --- Safety ---------------------------------------------------------------------------

PAYLOAD = """<script>alert("x")</script>'&"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_html_in_any_field_is_escaped(template: str):
    employee = EmployeeData(
        full_name=PAYLOAD,
        job_title=PAYLOAD,
        department=PAYLOAD,
        email=PAYLOAD,
        phone_work=PAYLOAD,
        phone_mobile=PAYLOAD,
        photo_url=f'https://images.example.com/a.jpg"{PAYLOAD}',
    )
    design = {
        "company": {"name": PAYLOAD, "tagline": PAYLOAD, "address": PAYLOAD, "phone": PAYLOAD},
        "socials": [{"network": "x", "url": 'https://x.com/a"onmouseover="alert(1)'}],
    }
    ctx = OrgContext(
        ICONS,
        logo_url=f'https://images.example.com/l.png"{PAYLOAD}',
        banner=BannerData(f'https://i.example.com/b.png"{PAYLOAD}', "https://acme.com", PAYLOAD),
    )
    html = render(template, design, employee, ctx)

    assert "<script" not in html
    assert '"onmouseover=' not in html
    assert "&lt;script&gt;" in html
    # No attribute was broken out of: the only tags are the ones the templates emit.
    assert set(re.findall(r"<([a-z]+)", html)) <= {"table", "tr", "td", "div", "span", "a", "img"}


def test_output_uses_only_email_safe_markup():
    html = render("classic", *CASES["full"])
    assert "class=" not in html
    assert "<style" not in html
    assert "<script" not in html
    for image in re.findall(r"<img[^>]*>", html):
        assert re.search(r'src="https://', image), image
        assert 'width="' in image, image
        assert 'alt="' in image, image


def test_phone_links_keep_only_digits():
    html = render("classic", {}, EmployeeData(phone_work="+1 (555) 010-2030"), OrgContext(ICONS))
    assert 'href="tel:+15550102030"' in html
    assert "+1 (555) 010-2030</a>" in html


def test_phone_without_digits_is_not_linked():
    html = render("classic", {}, EmployeeData(phone_work="ext. only"), OrgContext(ICONS))
    assert "tel:" not in html
    assert "ext. only" in html


def test_logo_links_to_website_unless_overridden():
    ctx = OrgContext(ICONS, logo_url="https://i.example.com/l.png")
    company = {"company": {"website_url": "https://acme.com"}}
    assert '<a href="https://acme.com"' in render("classic", company, EmployeeData(), ctx)
    custom = {**company, "logo": {"link_url": "https://acme.com/about"}}
    assert '<a href="https://acme.com/about"' in render("classic", custom, EmployeeData(), ctx)
    assert "<a " not in render("classic", {}, EmployeeData(), ctx)


# --- Size limit -----------------------------------------------------------------------


def test_typical_signature_is_well_under_the_limit():
    for template in TEMPLATES:
        assert len(render(template, *CASES["full"])) < MAX_SIGNATURE_CHARS / 2


def test_oversized_signature_is_rejected():
    networks = ["linkedin", "x", "facebook", "instagram", "youtube", "github"]
    design = {
        "socials": [
            {"network": network, "url": "https://example.com/" + "a" * 1900} for network in networks
        ]
    }
    with pytest.raises(SignatureTooLongError) as error:
        render("classic", design, FULL_EMPLOYEE, FULL_CONTEXT)
    assert error.value.length > MAX_SIGNATURE_CHARS
    assert "10,000" in str(error.value)


def test_preview_document_wraps_the_signature():
    document = preview_document("<table></table>")
    assert document.startswith("<!doctype html>")
    assert "<body><table></table></body>" in document
    assert '<base target="_blank">' in document
