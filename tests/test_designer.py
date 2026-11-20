"""The signature designer: template picker, draft saving, preview and logo upload."""

import html as html_module
import io
import re
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from PIL import Image
from sqlalchemy import func, select

from app.db import Database
from app.models import Asset, Employee, Organization, Plan, Signature
from app.services import signatures as signature_service
from app.services.images import MAX_UPLOAD_BYTES, InvalidImageError, normalise_logo
from app.services.sync import sync_org
from app.signatures.schema import SignatureDesign
from app.web.designer import LENGTH_WARNING_FROM, _length_level, design_from_form
from tests.fakes import image_bytes
from tests.helpers import HTMX, form, preview_of, sign_in

DRAFT = "/app/designer/draft"


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


async def signature_of(db: Database, org_id) -> Signature | None:
    async with db.session() as session:
        return await signature_service.get_signature(session, org_id)


async def draft(db: Database, org_id) -> SignatureDesign:
    return signature_service.draft_of(await signature_of(db, org_id))


@pytest.fixture
async def owner(app: FastAPI, db: Database, make_owner):
    """A connected organisation with the fake directory synced, but no signature yet."""
    user = await make_owner("admin@acme.test")
    assert (await sync_org(db, app.state.google, app.state.storage, user.org_id)).ok
    return user


@pytest.fixture
async def designing(client: AsyncClient, db: Database, owner):
    """Signed in as the owner, with the Classic template chosen. Returns request headers."""
    headers = sign_in(client, owner)
    response = await client.post(
        "/app/designer/start", data={"template": "classic"}, headers=headers
    )
    assert response.status_code == 303
    return {**headers, **HTMX}


# --- Access ---------------------------------------------------------------------------


async def test_designer_requires_sign_in(client: AsyncClient, db):
    response = await client.get("/app/designer")
    assert (response.status_code, response.headers["location"]) == (303, "/signup")


async def test_designer_requires_a_connected_organisation(client: AsyncClient, make_user):
    sign_in(client, await make_user("admin@acme.test"))
    response = await client.get("/app/designer")
    assert (response.status_code, response.headers["location"]) == (303, "/app/setup")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/app/designer/start"),
        ("POST", DRAFT),
        ("POST", "/app/designer/discard"),
        ("POST", "/app/designer/logo"),
        ("DELETE", "/app/designer/logo"),
    ],
)
async def test_changes_require_csrf_token(client: AsyncClient, db, owner, method, path):
    sign_in(client, owner)
    response = await client.request(method, path, data={"template": "classic"})
    assert response.status_code == 403
    assert await signature_of(db, owner.org_id) is None


# --- Choosing a template --------------------------------------------------------------


async def test_picker_shows_four_layouts_with_the_admins_own_details(client: AsyncClient, owner):
    sign_in(client, owner)
    response = await client.get("/app/designer")
    assert response.status_code == 200
    page = response.text
    assert "Choose a layout to start from" in page
    for label in ("Classic", "Side logo", "Photo", "Minimal"):
        assert f"Use {label}" in page
    documents = [html_module.unescape(d) for d in re.findall(r'srcdoc="([^"]*)"', page)]
    assert len(documents) == 4
    # Rendered for the signed-in admin, with defaults taken from the organisation.
    assert all("admin@acme.test" in d and "IT Administrator" in d for d in documents)
    assert all("https://acme.test" in d and ">Acme<" in d for d in documents)
    # Stand-in images make the layouts tell apart before a logo is uploaded.
    logo, photo = "/static/img/placeholder-logo.png", "/static/img/placeholder-photo.png"
    classic, side_logo, with_photo, minimal = documents
    assert logo in classic and logo in side_logo and logo in with_photo
    assert photo in with_photo and photo not in classic
    assert logo not in minimal  # the text-only layout
    for image in (logo, photo):
        assert (await client.get(image)).headers["content-type"] == "image/png"


async def test_choosing_a_template_creates_the_draft(client: AsyncClient, db, owner):
    headers = sign_in(client, owner)
    response = await client.post(
        "/app/designer/start", data={"template": "side-logo"}, headers=headers
    )
    assert (response.status_code, response.headers["location"]) == (303, "/app/designer")

    signature = await signature_of(db, owner.org_id)
    design = signature_service.draft_of(signature)
    assert design.template == "side-logo"
    assert design.company.name == "Acme"
    assert design.company.website_url == "https://acme.test"
    assert signature.published_design is None

    page = (await client.get("/app/designer")).text
    assert "Signature designer" in page
    assert "Draft · not applied to everyone yet" in page


async def test_unknown_template_is_rejected(client: AsyncClient, db, owner):
    headers = sign_in(client, owner)
    response = await client.post("/app/designer/start", data={"template": "fancy"}, headers=headers)
    assert response.status_code == 400
    assert await signature_of(db, owner.org_id) is None


async def test_starting_again_does_not_overwrite_the_draft(client: AsyncClient, db, designing):
    await client.post(DRAFT, data=form(company_name="Kept"), headers=designing)
    await client.post("/app/designer/start", data={"template": "minimal"}, headers=designing)
    signatures = await count(db, Signature)
    assert signatures == 1


async def count(db: Database, model) -> int:
    async with db.session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.parametrize(
    ("name", "domain", "expected"),
    [
        ("acme.test", "acme.test", "Acme"),
        ("big-river-co.example", "big-river-co.example", "Big River Co"),
        ("Acme Holdings Ltd", "acme.test", "Acme Holdings Ltd"),
    ],
)
def test_default_company_name(name, domain, expected):
    organization = Organization(name=name, domain=domain)
    assert signature_service.default_company_name(organization) == expected


# --- The designer page ----------------------------------------------------------------


async def test_page_shows_the_saved_draft_and_a_preview(client: AsyncClient, db, owner, designing):
    await client.post(
        DRAFT,
        data=form(
            template="photo",
            primary_color="#0f766e",
            font_family="Georgia",
            company_tagline="Tools for busy teams",
            social_linkedin="linkedin.com/company/acme",
            show_department=None,
        ),
        headers=designing,
    )
    page = (await client.get("/app/designer")).text

    assert 'value="photo" class="sr-only" checked' in page
    assert 'name="primary_color" value="#0f766e"' in page
    assert '<option value="Georgia" selected>' in page
    assert 'name="company_tagline" value="Tools for busy teams"' in page
    assert 'name="social_linkedin" value="https://linkedin.com/company/acme"' in page
    assert re.search(r'name="show_photo"[^>]*checked', page)
    assert not re.search(r'name="show_department"[^>]*checked', page)

    # Previewed as the signed-in admin by default.
    admin = await employee(db, "admin@acme.test")
    assert f'<option value="{admin.id}" selected>' in page
    signature = preview_of(page)
    assert "admin@acme.test" in signature
    assert "Tools for busy teams" in signature
    assert "color:#0f766e" in signature
    assert "characters" in page
    assert 'content="noindex"' in page


async def test_preview_choices_list_employees_then_test_cases(client: AsyncClient, designing):
    page = (await client.get("/app/designer")).text
    options = re.findall(r"<option value=\"([^\"]+)\"[^>]*>([^<]+)</option>", page)
    labels = [
        html_module.unescape(label)
        for value, label in options
        if "sample:" in value or "-" in value
    ]
    assert labels[-2:] == ["Longest name and title", "Missing optional fields"]
    assert "Priya Raman" in labels and "Liam O'Connor" in labels
    assert "Olivia Brown" in labels  # suspended people can still be previewed


async def test_designer_shows_free_plan_note(client: AsyncClient, db, owner, designing):
    assert "Powered by FooterPilot" in (await client.get("/app/designer")).text
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        organization.plan = Plan.PAID
        await session.commit()
    page = (await client.get("/app/designer")).text
    assert "free plan" not in page
    assert "Powered by" not in preview_of(page)


# --- Saving a draft -------------------------------------------------------------------


async def test_saving_updates_the_draft_and_returns_the_preview(
    client: AsyncClient, db, owner, designing
):
    priya = await employee(db, "priya.raman@acme.test")
    response = await client.post(
        DRAFT,
        data=form(
            template="minimal",
            primary_color="#BE123C",
            text_color="#111827",
            font_family="Verdana",
            font_size="large",
            company_name="Acme Corporation",
            company_phone="+1 415 555 0000",
            company_address="1 Main Street",
            company_website_url="acme.example/team",
            social_x="https://x.com/acme",
            preview_as=str(priya.id),
        ),
        headers=designing,
    )
    assert response.status_code == 200
    assert "HX-Reswap" not in response.headers

    signature = preview_of(response.text)
    assert "Priya Raman" in signature and "Head of Operations" in signature
    assert "Acme Corporation" in signature
    assert 'href="https://acme.example/team"' in signature  # scheme added
    assert "color:#be123c" in signature and "font-family:Verdana" in signature
    assert "http://testserver/static/social/x.png" in signature
    assert "Powered by" in signature  # free plan
    assert "Draft saved" in response.text
    # Every error slot is sent back, and all of them are empty.
    slots = dict(re.findall(r'<p id="error-([a-z_]+)"[^>]*>([^<]*)</p>', response.text))
    assert "company_name" in slots and "social_x" in slots
    assert not any(slots.values())

    saved = await draft(db, owner.org_id)
    assert saved.template == "minimal"
    assert saved.brand.primary_color == "#be123c"
    assert (saved.brand.font_family, saved.brand.font_size) == ("Verdana", "large")
    assert saved.company.name == "Acme Corporation"
    assert saved.company.website_url == "https://acme.example/team"
    assert [(s.network, s.url) for s in saved.socials] == [("x", "https://x.com/acme")]


async def test_draft_round_trips_through_the_page(client: AsyncClient, db, owner, designing):
    """Posting exactly what the page shows changes nothing."""
    await client.post(
        DRAFT,
        data=form(company_tagline="Tools & things <b>", company_address='1 "Main" St'),
        headers=designing,
    )
    before = (await signature_of(db, owner.org_id)).draft_design

    page = (await client.get("/app/designer")).text
    assert 'value="Tools &amp; things &lt;b&gt;"' in page  # escaped in the form
    await client.post(
        DRAFT,
        data=form(company_tagline="Tools & things <b>", company_address='1 "Main" St'),
        headers=designing,
    )
    assert (await signature_of(db, owner.org_id)).draft_design == before


async def test_unticked_details_are_hidden_for_everyone(client: AsyncClient, db, owner, designing):
    priya = await employee(db, "priya.raman@acme.test")
    response = await client.post(
        DRAFT,
        data=form(
            show_job_title=None,
            show_department=None,
            show_phone_work=None,
            show_phone_mobile=None,
            show_email=None,
            show_photo=None,
            template="photo",
            preview_as=str(priya.id),
        ),
        headers=designing,
    )
    signature = preview_of(response.text)
    assert "Priya Raman" in signature
    for hidden in (
        "Head of Operations",
        "Operations",
        "555 0101",
        "555 0191",
        "priya.raman@",
        "/media/",
    ):
        assert hidden not in signature
    assert (await draft(db, owner.org_id)).fields.show_email is False


@pytest.mark.parametrize(
    ("changes", "slot", "message"),
    [
        ({"primary_color": "blue"}, "primary_color", "Enter a colour"),
        ({"text_color": "#12"}, "text_color", "Enter a colour"),
        ({"template": "fancy"}, "template", "This value is not valid"),
        ({"font_family": "Comic Sans MS"}, "font_family", "This value is not valid"),
        ({"font_size": "huge"}, "font_size", "This value is not valid"),
        ({"logo_width": "999"}, "logo_width", "This value is not valid"),
        ({"logo_width": ""}, "logo_width", "This value is not valid"),
        ({"logo_link_url": "javascript:alert(1)"}, "logo_link_url", "must start with http"),
        ({"company_website_url": "ftp://acme.test"}, "company_website_url", "must start with http"),
        ({"company_name": "x" * 81}, "company_name", "Use at most 80 characters"),
        ({"company_tagline": "x" * 121}, "company_tagline", "Use at most 120 characters"),
        ({"social_youtube": "data:text/html,x"}, "social_youtube", "must start with http"),
        (
            {"social_x": "javascript:alert(1)", "social_linkedin": "https://linkedin.com/in/ok"},
            "social_x",
            "must start with http",
        ),
        ({"logo_asset_id": "not-a-uuid"}, "logo", "This value is not valid"),
    ],
)
async def test_invalid_input_is_reported_next_to_its_field_and_not_saved(
    client: AsyncClient, db, owner, designing, changes, slot, message
):
    before = (await signature_of(db, owner.org_id)).draft_design
    response = await client.post(DRAFT, data=form(**changes), headers=designing)

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "none"  # the last good preview stays on screen
    assert "srcdoc=" not in response.text
    slots = dict(re.findall(r'<p id="error-([a-z_]+)"[^>]*>([^<]*)</p>', response.text))
    assert message in html_module.unescape(slots[slot])
    assert [name for name, text in slots.items() if text] == [slot]  # no other field is marked
    assert (await signature_of(db, owner.org_id)).draft_design == before


async def test_fixing_the_input_clears_the_error(client: AsyncClient, designing):
    await client.post(DRAFT, data=form(primary_color="blue"), headers=designing)
    response = await client.post(DRAFT, data=form(primary_color="#2563eb"), headers=designing)
    assert '<p id="error-primary_color" hx-swap-oob="true"' in response.text
    assert 'role="alert">Enter' not in response.text
    assert "srcdoc=" in response.text


async def test_signature_over_gmails_limit_is_not_saved(client: AsyncClient, db, owner, designing):
    before = (await signature_of(db, owner.org_id)).draft_design
    long_url = "https://example.com/" + "a" * 1900
    socials = {
        f"social_{n}": long_url
        for n in ("linkedin", "x", "facebook", "instagram", "youtube", "github")
    }
    response = await client.post(DRAFT, data=form(**socials), headers=designing)

    assert response.headers["HX-Reswap"] == "none"
    assert "Gmail allows 10,000" in response.text
    assert (await signature_of(db, owner.org_id)).draft_design == before


async def test_html_in_fields_cannot_reach_the_page(client: AsyncClient, designing):
    attack = '"><script>alert(1)</script>'
    response = await client.post(
        DRAFT, data=form(company_name=attack, company_tagline=attack), headers=designing
    )
    assert "<script>alert(1)" not in response.text
    assert "<script" not in preview_of(response.text)
    page = (await client.get("/app/designer")).text
    assert "<script>alert(1)" not in page


async def test_saving_without_a_signature_sends_the_admin_to_the_picker(client: AsyncClient, owner):
    headers = {**sign_in(client, owner), **HTMX}
    response = await client.post(DRAFT, data=form(), headers=headers)
    assert response.status_code == 409
    assert response.headers["HX-Redirect"] == "/app/designer"


# --- Preview subjects -----------------------------------------------------------------


async def test_preview_as_another_employee(client: AsyncClient, db, designing):
    james = await employee(db, "james.okafor@acme.test")
    response = await client.post(DRAFT, data=form(preview_as=str(james.id)), headers=designing)
    signature = preview_of(response.text)
    assert "James Okafor" in signature and "Account Executive" in signature
    assert "admin@acme.test" not in signature

    page = (await client.get("/app/designer", params={"preview_as": str(james.id)})).text
    assert f'<option value="{james.id}" selected>' in page
    assert "James Okafor" in preview_of(page)


async def test_preview_longest_name_and_title(client: AsyncClient, designing):
    response = await client.post(DRAFT, data=form(preview_as="sample:longest"), headers=designing)
    signature = preview_of(response.text)
    # The worst case across the whole company, combined into one made-up person.
    assert "Maximiliana Alexandrina Wolfeschlegelsteinhausen-Bergerdorff" in signature
    assert "Senior Vice President of Global Strategic Partnerships and Alliances" in signature
    assert "Corporate Development and International Operations" in signature


async def test_preview_missing_optional_fields(client: AsyncClient, designing):
    response = await client.post(DRAFT, data=form(preview_as="sample:missing"), headers=designing)
    signature = preview_of(response.text)
    assert "Sam Lee" in signature and "sam.lee@acme.test" in signature
    assert "tel:" not in signature
    assert not re.search(r"<(div|td|span)[^>]*>\s*</\1>", signature)  # no empty rows


async def test_preview_of_another_organisations_employee_is_refused(
    app, client: AsyncClient, db, make_owner, owner, designing
):
    globex = await make_owner("admin@globex.test")
    await sync_org(db, app.state.google, app.state.storage, globex.org_id)
    outsider = await employee(db, "priya.raman@globex.test")
    before = (await signature_of(db, owner.org_id)).draft_design

    response = await client.post(
        DRAFT, data=form(company_name="Changed", preview_as=str(outsider.id)), headers=designing
    )
    assert response.status_code == 404
    assert "globex" not in response.text
    assert (await signature_of(db, owner.org_id)).draft_design == before  # nothing saved either

    # On a page load an unknown id falls back to the default instead of failing.
    page = await client.get("/app/designer", params={"preview_as": str(outsider.id)})
    assert page.status_code == 200
    assert "globex" not in page.text


@pytest.mark.parametrize("value", ["nonsense", "sample:unknown", str(uuid.uuid4())])
async def test_unknown_preview_subject_is_refused(client: AsyncClient, designing, value):
    response = await client.post(DRAFT, data=form(preview_as=value), headers=designing)
    assert response.status_code == 404


async def test_preview_without_any_employees_uses_the_sample(client: AsyncClient, make_owner):
    lonely = await make_owner("admin@empty.test")  # connected, but never synced
    headers = {**sign_in(client, lonely), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    page = await client.get("/app/designer")
    assert page.status_code == 200
    assert '<option value="sample:longest" selected>' in page.text
    assert "Maximiliana" in preview_of(page.text)


# --- Tenant isolation -----------------------------------------------------------------


async def test_each_organisation_has_its_own_design(
    app, client: AsyncClient, db, make_owner, owner, designing
):
    await client.post(DRAFT, data=form(company_tagline="Acme only"), headers=designing)

    globex = await make_owner("admin@globex.test")
    await sync_org(db, app.state.google, app.state.storage, globex.org_id)
    headers = {**sign_in(client, globex), **HTMX}
    # Globex has not chosen a template: it sees the picker, not Acme's design.
    picker = (await client.get("/app/designer")).text
    assert "Choose a layout" in picker and "Acme only" not in picker

    await client.post("/app/designer/start", data={"template": "minimal"}, headers=headers)
    await client.post(DRAFT, data=form(company_name="Globex", template="minimal"), headers=headers)
    assert "Acme only" not in (await client.get("/app/designer")).text

    assert (await draft(db, owner.org_id)).company.tagline == "Acme only"
    assert (await draft(db, owner.org_id)).template == "classic"
    assert (await draft(db, globex.org_id)).company.name == "Globex"


# --- Draft and published --------------------------------------------------------------


async def publish(db: Database, org_id) -> None:
    """Stand-in for applying the signature: make the current draft the live design."""
    async with db.session() as session:
        signature = await signature_service.get_signature(session, org_id)
        signature.published_design = dict(signature.draft_design)
        signature.published_at = datetime.now(UTC)
        await session.commit()


async def test_status_badge_follows_draft_and_published(client: AsyncClient, db, owner, designing):
    assert "Draft · not applied to everyone yet" in (await client.get("/app/designer")).text

    await publish(db, owner.org_id)
    page = (await client.get("/app/designer")).text
    assert "Live · no changes" in page and "Discard changes" not in page

    saved = await client.post(DRAFT, data=form(company_tagline="New line"), headers=designing)
    assert "Unpublished changes" in saved.text and "Discard changes" in saved.text
    assert "Unpublished changes" in (await client.get("/app/designer")).text

    # Changing it back makes the draft equal to what is live again.
    back = await client.post(DRAFT, data=form(), headers=designing)
    assert "Live · no changes" in back.text


async def test_discard_resets_the_draft_to_the_live_design(
    client: AsyncClient, db, owner, designing
):
    await client.post(DRAFT, data=form(company_tagline="Live line"), headers=designing)
    await publish(db, owner.org_id)
    await client.post(DRAFT, data=form(company_tagline="Abandoned idea"), headers=designing)

    response = await client.post("/app/designer/discard", headers=designing)
    assert response.headers["HX-Redirect"] == "/app/designer"
    assert (await draft(db, owner.org_id)).company.tagline == "Live line"
    page = (await client.get("/app/designer")).text
    assert "Live line" in page and "Abandoned idea" not in page


async def test_discard_is_refused_when_nothing_is_live(client: AsyncClient, db, owner, designing):
    await client.post(DRAFT, data=form(company_tagline="Only a draft"), headers=designing)
    response = await client.post("/app/designer/discard", headers=designing)
    assert response.status_code == 409
    assert (await draft(db, owner.org_id)).company.tagline == "Only a draft"


def test_length_indicator_levels():
    assert _length_level(0) == "ok"
    assert _length_level(LENGTH_WARNING_FROM - 1) == "ok"
    assert _length_level(LENGTH_WARNING_FROM) == "warning"


async def test_length_is_shown_with_the_preview(client: AsyncClient, designing):
    response = await client.post(DRAFT, data=form(), headers=designing)
    length = int(re.search(r'data-length="(\d+)"', response.text).group(1))
    assert length == len(preview_of(response.text))
    assert f"{length:,} of 10,000 characters" in response.text
    assert 'data-length-level="ok"' in response.text


async def test_dashboard_shows_the_signature_state(client: AsyncClient, db, owner):
    sign_in(client, owner)
    assert "Not designed yet" in (await client.get("/app")).text
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        await signature_service.create_signature(session, organization, "classic")
    assert "Not applied to everyone yet" in (await client.get("/app")).text


# --- Logo -----------------------------------------------------------------------------


def upload(data: bytes, name: str = "logo.png", content_type: str = "image/png") -> dict:
    return {"file": (name, data, content_type)}


async def test_uploading_a_logo_stores_it_and_puts_it_in_the_form(
    client: AsyncClient, db, owner, designing
):
    response = await client.post(
        "/app/designer/logo", files=upload(image_bytes("navy", (800, 200))), headers=designing
    )
    assert response.status_code == 200
    # Tells the form to save itself with the new logo and refresh the preview.
    assert response.headers["HX-Trigger-After-Settle"] == "designer-changed"

    async with db.session() as session:
        asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.org_id == owner.org_id and asset.kind == "logo"
    assert (asset.width, asset.height) == (400, 100)  # shrunk to 400 px wide
    assert asset.content_type == "image/png"
    assert asset.url.startswith(f"http://testserver/media/orgs/{owner.org_id}/logos/")
    assert f'name="logo_asset_id" value="{asset.id}"' in response.text
    assert asset.url in response.text and "Replace logo" in response.text

    # Uploading alone does not change the draft; the form's next save does.
    assert (await draft(db, owner.org_id)).logo.asset_id is None
    saved = await client.post(
        DRAFT, data=form(logo_asset_id=str(asset.id), logo_width="160"), headers=designing
    )
    signature = preview_of(saved.text)
    assert f'<img src="{asset.url}" width="160"' in signature
    assert (await draft(db, owner.org_id)).logo.asset_id == asset.id

    served = await client.get(asset.url)
    assert (served.status_code, served.headers["content-type"]) == (200, "image/png")
    assert asset.url in (await client.get("/app/designer")).text


async def test_removing_the_logo_clears_it_from_the_form(client: AsyncClient, db, owner, designing):
    uploaded = await client.post(
        "/app/designer/logo", files=upload(image_bytes()), headers=designing
    )
    asset_id = re.search(r'name="logo_asset_id" value="([^"]+)"', uploaded.text).group(1)
    await client.post(DRAFT, data=form(logo_asset_id=asset_id), headers=designing)

    response = await client.request("DELETE", "/app/designer/logo", headers=designing)
    assert response.headers["HX-Trigger-After-Settle"] == "designer-changed"
    assert 'name="logo_asset_id" value=""' in response.text
    assert "Upload logo" in response.text and "No logo" in response.text

    saved = await client.post(DRAFT, data=form(logo_asset_id=""), headers=designing)
    assert "<img" not in preview_of(saved.text)
    assert (await draft(db, owner.org_id)).logo.asset_id is None


@pytest.mark.parametrize(
    ("data", "name", "content_type", "message"),
    [
        (b"x" * (MAX_UPLOAD_BYTES + 1), "big.png", "image/png", "larger than 2 MB"),
        (b"this is not an image", "notes.png", "image/png", "not a readable image"),
        (
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "logo.svg",
            "image/svg+xml",
            "not a readable image",
        ),
        (image_bytes(fmt="BMP"), "logo.bmp", "image/bmp", "PNG, JPEG or GIF"),
        (
            image_bytes(fmt="WEBP"),
            "logo.png",
            "image/png",
            "PNG, JPEG or GIF",
        ),  # wrong type, right name
        (b"", "empty.png", "image/png", "not a readable image"),
    ],
    ids=["too large", "not an image", "svg", "bmp", "webp disguised as png", "empty"],
)
async def test_unusable_uploads_are_rejected(
    client: AsyncClient, db, designing, data, name, content_type, message
):
    response = await client.post(
        "/app/designer/logo", files=upload(data, name, content_type), headers=designing
    )
    assert response.status_code == 200
    assert message in response.text
    assert 'role="alert"' in response.text
    assert "HX-Trigger-After-Settle" not in response.headers  # the form is not told to save
    assert await count(db, Asset) == 0


async def test_failed_upload_keeps_the_existing_logo(client: AsyncClient, db, designing):
    first = await client.post("/app/designer/logo", files=upload(image_bytes()), headers=designing)
    asset_id = re.search(r'name="logo_asset_id" value="([^"]+)"', first.text).group(1)
    response = await client.post(
        "/app/designer/logo",
        data={"logo_asset_id": asset_id},
        files=upload(b"junk"),
        headers=designing,
    )
    assert "could not be used" in response.text
    assert f'name="logo_asset_id" value="{asset_id}"' in response.text


async def test_upload_without_a_file(client: AsyncClient, designing):
    response = await client.post("/app/designer/logo", data={"x": "1"}, headers=designing)
    assert "Choose an image" in response.text


async def test_another_organisations_logo_cannot_be_used(
    client: AsyncClient, db, make_owner, owner, designing
):
    globex = await make_owner("admin@globex.test")
    globex_headers = {**sign_in(client, globex), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=globex_headers)
    uploaded = await client.post(
        "/app/designer/logo", files=upload(image_bytes("green")), headers=globex_headers
    )
    globex_logo = re.search(r'name="logo_asset_id" value="([^"]+)"', uploaded.text).group(1)

    # Acme's admin submits Globex's logo id.
    headers = {**sign_in(client, owner), **HTMX}
    before = (await signature_of(db, owner.org_id)).draft_design
    response = await client.post(DRAFT, data=form(logo_asset_id=globex_logo), headers=headers)
    assert response.headers["HX-Reswap"] == "none"
    assert "could not be found" in response.text
    assert "globex" not in response.text
    assert (await signature_of(db, owner.org_id)).draft_design == before


# --- Logo normalisation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "size", "expected_type", "expected_size"),
    [
        ("PNG", (800, 200), "image/png", (400, 100)),
        ("JPEG", (1000, 500), "image/jpeg", (400, 200)),
        ("GIF", (300, 900), "image/png", (133, 400)),  # tall: limited by height
        ("PNG", (120, 40), "image/png", (120, 40)),  # small logos are not enlarged
    ],
)
def test_logo_is_shrunk_and_re_encoded(fmt, size, expected_type, expected_size):
    result = normalise_logo(image_bytes("teal", size, fmt))
    assert result.content_type == expected_type
    assert (result.width, result.height) == expected_size
    assert Image.open(io.BytesIO(result.data)).size == expected_size


def test_logo_transparency_is_kept():
    source = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    output = io.BytesIO()
    source.save(output, format="PNG")
    image = Image.open(io.BytesIO(normalise_logo(output.getvalue()).data))
    assert image.mode == "RGBA" and image.getpixel((10, 10))[3] == 0


def test_logo_metadata_is_dropped():
    source = Image.new("RGB", (200, 100), "white")
    exif = Image.Exif()
    exif[0x010E] = "internal draft, do not share"
    output = io.BytesIO()
    source.save(output, format="JPEG", exif=exif)
    assert b"do not share" in output.getvalue()
    assert b"do not share" not in normalise_logo(output.getvalue()).data


def test_animated_gif_keeps_its_first_frame():
    frames = [Image.new("RGB", (100, 50), colour) for colour in ("red", "blue")]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    image = Image.open(io.BytesIO(normalise_logo(output.getvalue()).data)).convert("RGB")
    red, _, blue = image.getpixel((50, 25))
    assert red > 200 and blue < 60


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "WEBP", "ICO"])
def test_other_image_formats_are_refused(fmt):
    with pytest.raises(InvalidImageError, match="PNG, JPEG or GIF"):
        normalise_logo(image_bytes(size=(64, 64), fmt=fmt))


# --- Form parsing ---------------------------------------------------------------------


def test_form_becomes_a_design():
    from starlette.datastructures import FormData

    design = design_from_form(
        FormData(form(social_github="github.com/acme", show_photo=None, logo_width="180"))
    )
    assert design.fields.show_photo is False and design.fields.show_email is True
    assert design.logo.width_px == 180 and design.logo.asset_id is None
    assert [(s.network, s.url) for s in design.socials] == [("github", "https://github.com/acme")]
