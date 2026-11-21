"""The banner page: upload, preview, activate, replace and remove."""

import re

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.google.base import GoogleGateways
from app.models import (
    Asset,
    Banner,
    ClickEvent,
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    Employee,
    TrackedLink,
)
from app.services import banners as banner_service
from app.services.images import MAX_UPLOAD_BYTES, InvalidImageError, normalise_banner
from app.services.sync import sync_org
from tests.fakes import ScriptedGmail, image_bytes
from tests.helpers import HTMX, form, preview_of, sign_in

APPLY = "/app/designer/apply"
IMAGE = "/app/banner/image"
PREVIEW = "/app/banner/preview"
ACTIVATE = "/app/banner/activate"
REMOVE = "/app/banner/remove"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0 Safari/537.36"}
ELIGIBLE, WITH_MAILBOX = 11, 10  # see tests/test_deploy.py
TARGET = "https://acme.test/spring-sale"


@pytest.fixture
def mail(app: FastAPI) -> ScriptedGmail:
    gmail = ScriptedGmail()
    app.state.google = GoogleGateways(directory=app.state.google.directory, gmail=gmail)
    return gmail


@pytest.fixture
async def owner(app: FastAPI, db: Database, make_owner, mail):
    user = await make_owner("admin@acme.test")
    assert (await sync_org(db, app.state.google, app.state.storage, user.org_id)).ok
    return user


@pytest.fixture
async def designing(client: AsyncClient, owner):
    """Signed in, with the Classic template chosen but nothing applied."""
    headers = sign_in(client, owner)
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    return {**headers, **HTMX}


@pytest.fixture
async def live(client: AsyncClient, designing, run_jobs, mail):
    """The default design applied to every mailbox."""
    response = await client.post(APPLY, data=form(), headers=designing)
    assert response.status_code == 200, response.text
    await run_jobs()
    return designing


def upload(data: bytes, name: str = "banner.png", content_type: str = "image/png") -> dict:
    return {"file": (name, data, content_type)}


async def upload_banner(client: AsyncClient, headers, colour: str = "navy") -> str:
    """Upload a banner image and return its id, as the form would hold it."""
    response = await client.post(
        IMAGE, files=upload(image_bytes(colour, (1200, 300))), headers=headers
    )
    assert response.status_code == 200, response.text
    return re.search(r'name="banner_asset_id" value="([^"]+)"', response.text).group(1)


async def activate(client: AsyncClient, headers, asset_id: str, target: str = TARGET, alt="Sale"):
    return await client.post(
        ACTIVATE,
        data={"banner_asset_id": asset_id, "target_url": target, "alt_text": alt},
        headers=headers,
    )


async def rows(db: Database, model, *order) -> list:
    async with db.session() as session:
        return list((await session.execute(select(model).order_by(*order))).scalars())


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


def banner_link(html: str) -> tuple[str, str] | None:
    """(href, image address) of the banner in a signature, or None."""
    match = re.search(r'<a href="([^"]+)"[^>]*><img src="([^"]*/banners/[^"]+)"', html)
    return (match.group(1), match.group(2)) if match else None


# --- Images ---------------------------------------------------------------------------


def test_banner_images_are_shrunk_to_1200_pixels_and_re_encoded():
    image = normalise_banner(image_bytes("red", (2400, 600)))
    assert (image.width, image.height) == (1200, 300)
    assert image.content_type == "image/png"
    small = normalise_banner(image_bytes("red", (500, 100), "JPEG"))
    assert (small.width, small.height, small.extension) == (500, 100, "jpg")
    with pytest.raises(InvalidImageError):
        normalise_banner(b"<svg xmlns='http://www.w3.org/2000/svg'/>")


async def test_uploading_a_banner_stores_it_and_puts_it_in_the_form(client, db, owner, designing):
    response = await client.post(
        IMAGE, files=upload(image_bytes("navy", (2400, 600))), headers=designing
    )
    assert response.status_code == 200
    # Tells the form to refresh the preview with the new image.
    assert response.headers["HX-Trigger-After-Settle"] == "banner-changed"

    (asset,) = await rows(db, Asset)
    assert (asset.org_id, asset.kind) == (owner.org_id, "banner")
    assert (asset.width, asset.height) == (1200, 300)
    assert f"orgs/{owner.org_id}/banners/" in asset.storage_key
    assert f'name="banner_asset_id" value="{asset.id}"' in response.text
    assert f'src="{asset.url}"' in response.text and 'width="600"' in response.text
    # Uploading activates nothing.
    assert await rows(db, Banner) == []


@pytest.mark.parametrize(
    ("data", "name", "content_type", "message"),
    [
        (b"not an image", "banner.png", "image/png", "not a readable image"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "b.svg", "image/svg+xml", "not a readable"),
        (b"x" * (MAX_UPLOAD_BYTES + 1), "big.png", "image/png", "larger than 2 MB"),
        (image_bytes(fmt="BMP"), "banner.bmp", "image/bmp", "PNG, JPEG or GIF"),
    ],
)
async def test_unusable_banner_files_are_refused(
    client, db, designing, data, name, content_type, message
):
    response = await client.post(IMAGE, files=upload(data, name, content_type), headers=designing)
    assert response.status_code == 200
    assert message in response.text and 'role="alert"' in response.text
    assert "HX-Trigger-After-Settle" not in response.headers
    assert await rows(db, Asset) == []


async def test_failed_upload_keeps_the_image_already_chosen(client, designing):
    asset_id = await upload_banner(client, designing)
    response = await client.post(
        IMAGE, data={"banner_asset_id": asset_id}, files=upload(b"junk"), headers=designing
    )
    assert "could not be used" in response.text
    assert f'name="banner_asset_id" value="{asset_id}"' in response.text

    empty = await client.post(IMAGE, data={"x": "1"}, headers=designing)
    assert "Choose an image" in empty.text


# --- The page -------------------------------------------------------------------------


async def test_banner_page_asks_for_a_signature_first(client, owner):
    sign_in(client, owner)
    page = (await client.get("/app/banner")).text
    assert "Design your signature first" in page
    assert 'href="/app/designer"' in page and "banner-form" not in page


async def test_banner_page_before_any_banner(client, designing):
    page = (await client.get("/app/banner")).text
    assert "No banner is active" in page and "New banner" in page
    assert 'hx-post="/app/banner/preview"' in page and 'hx-post="/app/banner/activate"' in page
    assert "None yet" in page
    # The preview is the signature as it stands: no banner in it.
    assert banner_link(preview_of(page)) is None
    assert "Admin" in preview_of(page)
    # Nothing is live, so the dialog does not promise to change mailboxes.
    assert "Nobody's mailbox changes" in page


async def test_banner_page_states_how_many_mailboxes_change(client, live):
    page = (await client.get("/app/banner")).text
    assert re.search(rf"<strong[^>]*>{ELIGIBLE} employees</strong>", page)
    assert f"Update {ELIGIBLE} employees" in page
    # The test server keeps images locally, like a developer's machine.
    assert re.search(r"banner image will not load in\s+real email", page)


# --- Preview --------------------------------------------------------------------------


async def test_preview_shows_the_banner_under_the_signature(client, db, designing):
    asset_id = await upload_banner(client, designing)
    (asset,) = await rows(db, Asset)
    response = await client.post(
        PREVIEW,
        data={
            "banner_asset_id": asset_id,
            "target_url": "acme.test/sale",
            "alt_text": " Big  sale ",
        },
        headers=designing,
    )
    assert response.status_code == 200
    html = preview_of(response.text)
    # A bare address is completed, and the image is shown at up to 600 pixels.
    assert banner_link(html) == ("https://acme.test/sale", asset.url)
    assert 'width="600"' in html and 'alt="Big sale"' in html
    # Previewing saves nothing.
    assert await rows(db, Banner) == [] and await rows(db, TrackedLink) == []


async def test_preview_works_before_the_address_is_typed(client, designing):
    asset_id = await upload_banner(client, designing)
    response = await client.post(
        PREVIEW,
        data={"banner_asset_id": asset_id, "target_url": "", "alt_text": ""},
        headers=designing,
    )
    href, _ = banner_link(preview_of(response.text))
    assert href == "#"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("javascript:alert(1)", "must start with http"),
        ("ftp://acme.test/file", "must start with http"),
        ("https://", "valid web address"),
        ("https://acme.test/a b", "cannot contain spaces"),
        ("duvewfoh", "valid web address"),  # one word is a typo, not a site
        ("https://aaaaaaa", "valid web address"),
    ],
)
async def test_preview_refuses_an_unsafe_address(client, designing, target, message):
    asset_id = await upload_banner(client, designing)
    response = await client.post(
        PREVIEW, data={"banner_asset_id": asset_id, "target_url": target}, headers=designing
    )
    assert response.headers["HX-Reswap"] == "none"  # the last good preview stays
    assert message in response.text
    assert "srcdoc" not in response.text


async def test_preview_without_an_image_is_quiet(client, designing):
    response = await client.post(
        PREVIEW, data={"banner_asset_id": "", "target_url": TARGET}, headers=designing
    )
    assert response.headers["HX-Reswap"] == "none"
    assert "Upload a banner image" not in response.text


async def test_long_description_is_refused(client, designing):
    asset_id = await upload_banner(client, designing)
    response = await activate(client, designing, asset_id, alt="x" * 161)
    assert "Use at most 160 characters" in response.text
    assert response.headers["HX-Reswap"] == "none"


# --- Activating -----------------------------------------------------------------------


async def test_activating_puts_the_banner_in_every_mailbox(client, db, owner, mail, live, run_jobs):
    asset_id = await upload_banner(client, live)
    (asset,) = await rows(db, Asset)
    writes = len(mail.writes)

    response = await activate(client, live, asset_id)
    assert response.status_code == 200, response.text
    (banner,) = await rows(db, Banner)
    assert (banner.org_id, str(banner.asset_id)) == (owner.org_id, asset_id)
    assert (banner.target_url, banner.alt_text, banner.is_active) == (TARGET, "Sale", True)

    latest = (await rows(db, Deployment, Deployment.created_at))[-1]
    assert response.headers["HX-Redirect"] == f"/app/deployments/{latest.id}"
    assert (latest.trigger, latest.created_by, latest.total) == (
        DeploymentTrigger.MANUAL,
        owner.id,
        ELIGIBLE,
    )

    await run_jobs()
    assert len(mail.writes) == writes + WITH_MAILBOX
    links = {link.kind: link for link in await rows(db, TrackedLink)}
    assert (links["banner"].ref, links["banner"].target_url) == (str(banner.id), TARGET)
    for email in ("admin@acme.test", "chen.wei@acme.test"):
        short_id = (await employee(db, email)).short_id
        assert banner_link(mail.signature(email)) == (
            f"http://testserver/c/{links['banner'].token}?e={short_id}",
            asset.url,
        )
    assert 'alt="Sale"' in mail.signature("admin@acme.test")

    # The page now shows it as the active banner, with its clicks.
    await client.get(f"/c/{links['banner'].token}", headers=BROWSER)
    page = (await client.get("/app/banner")).text
    assert "Replace it with a new banner" in page and TARGET in page
    assert re.search(r"Clicks</dt>\s*<dd[^>]*>1</dd>", page)
    assert banner_link(preview_of(page)) is not None


async def test_clicking_the_banner_leads_to_its_target_and_is_counted(
    client, db, mail, live, run_jobs
):
    await activate(client, live, await upload_banner(client, live))
    await run_jobs()
    href, _ = banner_link(mail.signature("chen.wei@acme.test"))
    client.cookies.clear()  # whoever received the email

    response = await client.get(href.removeprefix("http://testserver"), headers=BROWSER)
    assert (response.status_code, response.headers["location"]) == (302, TARGET)
    (event,) = await rows(db, ClickEvent)
    assert event.employee_id == (await employee(db, "chen.wei@acme.test")).id


async def test_click_appears_on_the_analytics_page(client, db, mail, live, run_jobs):
    await activate(client, live, await upload_banner(client, live), alt="Spring sale")
    await run_jobs()
    href, _ = banner_link(mail.signature("admin@acme.test"))
    await client.get(href.removeprefix("http://testserver"), headers=BROWSER)

    page = (await client.get("/app/analytics")).text
    assert re.search(r"text-5xl[^>]*>1</p>", page)
    assert "Banner: Spring sale" in page and "(current)" in page and TARGET in page


async def test_activating_before_anything_is_live_changes_no_mailbox(
    client, db, mail, designing, run_jobs
):
    response = await activate(client, designing, await upload_banner(client, designing))
    assert response.headers["HX-Redirect"] == "/app/banner"
    assert await rows(db, Deployment) == [] and mail.writes == []
    page = (await client.get("/app/banner")).text
    assert "Not in anyone's mailbox yet" in page

    # It goes out with the first Apply to everyone.
    await client.post(APPLY, data=form(), headers=designing)
    await run_jobs()
    href, _ = banner_link(mail.signature("admin@acme.test"))
    assert href.startswith("http://testserver/c/")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"banner_asset_id": "", "target_url": TARGET}, "Upload a banner image first"),
        ({"banner_asset_id": "not-a-uuid", "target_url": TARGET}, "Upload a banner image first"),
        ({"target_url": ""}, "Enter a web address"),
        ({"target_url": "javascript:alert(1)"}, "must start with http"),
        ({"target_url": "duvewfoh"}, "valid web address"),
        # With no description, a banner whose image is blocked would show nothing.
        ({"target_url": TARGET}, "Add a short description"),
        ({"target_url": TARGET, "alt_text": "   "}, "Add a short description"),
    ],
)
async def test_incomplete_banner_is_not_activated(client, db, mail, live, fields, message):
    data = {"banner_asset_id": await upload_banner(client, live), "alt_text": "", **fields}
    response = await client.post(ACTIVATE, data=data, headers=live)
    assert response.headers["HX-Reswap"] == "none"
    assert message in response.text and "Fix the highlighted fields" in response.text
    assert await rows(db, Banner) == []
    assert len(await rows(db, Deployment)) == 1


async def test_activating_the_same_banner_again_does_nothing(client, db, live, run_jobs):
    asset_id = await upload_banner(client, live)
    await activate(client, live, asset_id)
    await run_jobs()
    response = await activate(client, live, asset_id)
    assert "already the active one" in response.text
    assert len(await rows(db, Banner)) == 1 and len(await rows(db, Deployment)) == 2


async def test_banner_is_refused_while_signatures_are_being_applied(client, db, mail, live):
    asset_id = await upload_banner(client, live)
    await client.post(APPLY, data=form(company_tagline="New"), headers=live)  # jobs not run
    running = (await rows(db, Deployment, Deployment.created_at))[-1]
    assert running.status == DeploymentStatus.RUNNING

    response = await activate(client, live, asset_id)
    assert "being applied right now" in response.text
    assert f"/app/deployments/{running.id}" in response.text
    assert "HX-Redirect" not in response.headers
    assert await rows(db, Banner) == []
    assert "being applied right now" in (await client.post(REMOVE, headers=live)).text


# --- Replacing and removing -----------------------------------------------------------


async def test_replacing_a_banner_gives_a_new_link_and_keeps_the_old_counts(
    client, db, owner, mail, live, run_jobs
):
    await activate(client, live, await upload_banner(client, live, "navy"), alt="First")
    await run_jobs()
    old_href, old_image = banner_link(mail.signature("admin@acme.test"))
    for _ in range(3):
        await client.get(old_href.removeprefix("http://testserver"), headers=BROWSER)

    second = await activate(
        client, live, await upload_banner(client, live, "green"), "https://acme.test/jobs", "Second"
    )
    assert "HX-Redirect" in second.headers
    await run_jobs()

    first, latest = await rows(db, Banner, Banner.created_at)
    assert (first.is_active, latest.is_active) == (False, True)
    assert first.archived_at is not None and latest.archived_at is None
    new_href, new_image = banner_link(mail.signature("admin@acme.test"))
    assert new_href != old_href and new_image != old_image
    banner_links = [link for link in await rows(db, TrackedLink) if link.kind == "banner"]
    assert {link.ref for link in banner_links} == {str(first.id), str(latest.id)}

    await client.get(new_href.removeprefix("http://testserver"), headers=BROWSER)
    # An email sent last week still leads to the first banner's page, and still counts for it.
    old = await client.get(old_href.removeprefix("http://testserver"), headers=BROWSER)
    assert old.headers["location"] == TARGET

    async with db.session() as session:
        views = await banner_service.list_banners(session, owner.org_id)
    assert [(view.banner.alt_text, view.clicks) for view in views] == [("Second", 1), ("First", 4)]

    page = (await client.get("/app/banner")).text
    assert "Earlier banners" in page and TARGET in page
    assert re.search(r"tabular-nums[^>]*>4</td>", page)
    analytics = (await client.get("/app/analytics")).text
    assert "Banner: Second" in analytics and "Banner: First" in analytics
    assert "(current)" in analytics and "(until " in analytics


async def test_the_database_allows_one_active_banner_per_organisation(client, db, owner, live):
    asset_id = await upload_banner(client, live)
    await activate(client, live, asset_id)
    async with db.session() as session:
        session.add(Banner(org_id=owner.org_id, asset_id=asset_id, target_url=TARGET))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_removing_the_banner_takes_it_out_of_every_mailbox(client, db, mail, live, run_jobs):
    await activate(client, live, await upload_banner(client, live))
    await run_jobs()
    href, _ = banner_link(mail.signature("admin@acme.test"))
    await client.get(href.removeprefix("http://testserver"), headers=BROWSER)
    writes = len(mail.writes)

    response = await client.post(REMOVE, headers=live)
    latest = (await rows(db, Deployment, Deployment.created_at))[-1]
    assert response.headers["HX-Redirect"] == f"/app/deployments/{latest.id}"
    await run_jobs()

    assert len(mail.writes) == writes + WITH_MAILBOX
    assert banner_link(mail.signature("admin@acme.test")) is None
    (banner,) = await rows(db, Banner)
    assert banner.is_active is False and banner.archived_at is not None
    # Its link still works for emails already sent, and its count is kept.
    assert (
        await client.get(href.removeprefix("http://testserver"), headers=BROWSER)
    ).status_code == 302
    page = (await client.get("/app/banner")).text
    assert "No banner is active" in page
    assert re.search(r"tabular-nums[^>]*>2</td>", page)


async def test_removing_when_there_is_no_banner_changes_nothing(client, db, mail, live, run_jobs):
    writes = len(mail.writes)
    response = await client.post(REMOVE, headers=live)
    assert response.headers["HX-Redirect"] == "/app/banner"
    await run_jobs()
    assert len(mail.writes) == writes and len(await rows(db, Deployment)) == 1


async def test_every_mailbox_written_has_the_banner(client, mail, live, run_jobs):
    await activate(client, live, await upload_banner(client, live))
    await run_jobs()
    for email in mail.written_to:
        assert banner_link(mail.signature(email)) is not None


# --- Access ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [IMAGE, PREVIEW, ACTIVATE, REMOVE])
async def test_banner_changes_require_csrf_token(client, db, owner, mail, path):
    sign_in(client, owner)
    response = await client.post(path, data={"target_url": TARGET}, headers=HTMX)
    assert response.status_code == 403
    assert await rows(db, Banner) == [] and await rows(db, Asset) == []


@pytest.mark.parametrize("path", [IMAGE, PREVIEW, ACTIVATE, REMOVE])
async def test_banner_changes_require_sign_in(client, db, path):
    headers = {**sign_in(client, None), **HTMX}
    response = await client.post(path, data={"target_url": TARGET}, headers=headers)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/signup"


async def test_another_organisations_banner_image_cannot_be_used(
    app, client, db, make_owner, owner, mail, live
):
    globex = await make_owner("admin@globex.test")
    assert (await sync_org(db, app.state.google, app.state.storage, globex.org_id)).ok
    globex_headers = {**sign_in(client, globex), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=globex_headers)
    globex_image = await upload_banner(client, globex_headers, "green")

    # Acme's admin submits Globex's image id.
    headers = {**sign_in(client, owner), **HTMX}
    response = await activate(client, headers, globex_image)
    assert "Upload a banner image first" in response.text
    assert await rows(db, Banner) == []
    preview = await client.post(
        PREVIEW, data={"banner_asset_id": globex_image, "target_url": TARGET}, headers=headers
    )
    assert "srcdoc" not in preview.text and "globex" not in preview.text


async def test_banners_are_kept_apart_between_organisations(
    app, client, db, make_owner, owner, mail, live, run_jobs
):
    await activate(client, live, await upload_banner(client, live))
    await run_jobs()

    globex = await make_owner("admin@globex.test")
    assert (await sync_org(db, app.state.google, app.state.storage, globex.org_id)).ok
    globex_headers = {**sign_in(client, globex), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=globex_headers)
    page = (await client.get("/app/banner")).text
    assert "No banner is active" in page and TARGET not in page

    # Removing "the banner" as Globex leaves Acme's alone.
    assert (await client.post(REMOVE, headers=globex_headers)).headers[
        "HX-Redirect"
    ] == "/app/banner"
    (banner,) = await rows(db, Banner)
    assert (banner.org_id, banner.is_active) == (owner.org_id, True)
    await client.post(APPLY, data=form(company_name="Globex"), headers=globex_headers)
    await run_jobs()
    assert banner_link(mail.signature("admin@globex.test")) is None
