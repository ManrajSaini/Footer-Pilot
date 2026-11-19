"""The employees page, Sync now, include/exclude and step 4 of the wizard."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from sqlalchemy import select

from app.db import Database
from app.models import Employee, Organization, OrgStatus, SyncStatus
from app.services.sync import STALE_AFTER, sync_org
from tests.helpers import HTMX, sign_in


async def synced_owner(app: FastAPI, db: Database, make_owner, email: str = "admin@acme.test"):
    """An owner whose organisation has already been synced from the fake directory."""
    owner = await make_owner(email)
    result = await sync_org(db, app.state.google, app.state.storage, owner.org_id)
    assert result.ok
    return owner


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


async def organization(db: Database, org_id) -> Organization:
    async with db.session() as session:
        return await session.get(Organization, org_id)


# --- Access ---------------------------------------------------------------------------

PAGES = ["/app/employees", "/app/employees/rows", "/app/employees/counts", "/app/setup/sync"]


@pytest.mark.parametrize("path", [*PAGES, "/app/employees/sync-status"])
async def test_pages_redirect_anonymous_visitors(client: AsyncClient, db, path):
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/signup"


@pytest.mark.parametrize("path", PAGES)
async def test_pages_need_a_connected_organisation(client: AsyncClient, make_user, path):
    sign_in(client, await make_user("admin@acme.test"))
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/setup"


async def test_colleague_of_a_connected_organisation_sees_nothing(
    app, client: AsyncClient, db, make_owner, make_user
):
    await synced_owner(app, db, make_owner)
    sign_in(client, await make_user("colleague@acme.test"))
    response = await client.get("/app/employees")
    assert response.status_code == 403
    assert "priya.raman" not in response.text


# --- Listing --------------------------------------------------------------------------


async def test_page_lists_the_synced_directory(app, client: AsyncClient, db, make_owner):
    sign_in(client, await synced_owner(app, db, make_owner))
    response = await client.get("/app/employees")
    assert response.status_code == 200
    page = response.text

    # The admin plus the eleven people in the fixture directory.
    assert "12 in your directory" in page
    assert "11 will get the signature" in page  # everyone except the suspended one
    assert "1 suspended" in page
    for text in (
        "Priya Raman",
        "Head of Operations",
        "priya.raman@acme.test",
        "+1 415 555 0101",
        "Sofía Martínez",
        "Liam O&#39;Connor",
        "Wolfeschlegelsteinhausen-Bergerdorff",
        "Suspended",
        "No mailbox",
        "Not applied yet",
    ):
        assert text in page, text
    assert page.count("Included") >= 11
    assert "Last synced just now" in page


async def test_photos_are_shown_and_served(app, client: AsyncClient, db, make_owner):
    sign_in(client, await synced_owner(app, db, make_owner))
    priya = await employee(db, "priya.raman@acme.test")
    assert priya.photo_url.startswith("http://testserver/media/orgs/")
    assert priya.photo_url in (await client.get("/app/employees")).text

    photo = await client.get(priya.photo_url)
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"

    chen = await employee(db, "chen.wei@acme.test")
    assert chen.photo_url is None  # no photo in the directory: initials are shown instead


async def test_empty_directory_explains_what_to_do(client: AsyncClient, make_owner):
    sign_in(client, await make_owner("admin@acme.test"))
    page = (await client.get("/app/employees")).text
    assert "No employees yet" in page
    assert "has not been synced yet" in page
    assert "0 in your directory" in page


async def test_dashboard_shows_the_employee_count(app, client: AsyncClient, db, make_owner):
    sign_in(client, await synced_owner(app, db, make_owner))
    page = (await client.get("/app")).text
    assert 'href="/app/employees"' in page
    assert "11 will get the signature" in page


# --- Search ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected", "absent"),
    [
        ("priya", "Priya Raman", "James Okafor"),
        ("OKAFOR", "James Okafor", "Priya Raman"),  # case-insensitive
        ("chen.wei@", "Chen Wei", "Priya Raman"),  # by email
        ("  aisha  ", "Aisha Khan", "Priya Raman"),  # surrounding spaces ignored
        ("o'connor", "Liam O&#39;Connor", "Priya Raman"),
    ],
)
async def test_search_by_name_or_email(
    app, client: AsyncClient, db, make_owner, query, expected, absent
):
    sign_in(client, await synced_owner(app, db, make_owner))
    for path in ("/app/employees/rows", "/app/employees"):
        text = (await client.get(path, params={"q": query})).text
        assert expected in text, path
        assert absent not in text, path


@pytest.mark.parametrize("query", ["%", "_", "zzzz", "%%%"])
async def test_search_treats_wildcards_as_plain_text(
    app, client: AsyncClient, db, make_owner, query
):
    sign_in(client, await synced_owner(app, db, make_owner))
    text = (await client.get("/app/employees/rows", params={"q": query})).text
    assert "Nobody matches" in text
    assert "Priya Raman" not in text


async def test_search_text_cannot_inject_markup(app, client: AsyncClient, db, make_owner):
    sign_in(client, await synced_owner(app, db, make_owner))
    response = await client.get("/app/employees", params={"q": '"><script>alert(1)</script>'})
    assert response.status_code == 200
    assert "<script>alert(1)" not in response.text


# --- Include and exclude --------------------------------------------------------------


async def test_excluding_and_including_an_employee(app, client: AsyncClient, db, make_owner):
    headers = {**sign_in(client, await synced_owner(app, db, make_owner)), **HTMX}
    james = await employee(db, "james.okafor@acme.test")
    url = f"/app/employees/{james.id}/excluded"

    response = await client.post(url, data={"excluded": "true"}, headers=headers)
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<tr")  # just the one row
    assert "Excluded" in response.text
    assert response.headers["HX-Trigger"] == "employee-toggled"
    assert (await employee(db, james.email)).excluded is True
    assert (
        "10 will get the signature · 1 excluded" in (await client.get("/app/employees/counts")).text
    )

    response = await client.post(url, data={"excluded": "false"}, headers=headers)
    assert "Included" in response.text
    assert (await employee(db, james.email)).excluded is False


async def test_exclude_requires_csrf_token(app, client: AsyncClient, db, make_owner):
    sign_in(client, await synced_owner(app, db, make_owner))
    james = await employee(db, "james.okafor@acme.test")
    response = await client.post(f"/app/employees/{james.id}/excluded", data={"excluded": "true"})
    assert response.status_code == 403
    assert (await employee(db, james.email)).excluded is False


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
async def test_exclude_unknown_employee(app, client: AsyncClient, db, make_owner, bad_id):
    headers = sign_in(client, await synced_owner(app, db, make_owner))
    response = await client.post(
        f"/app/employees/{bad_id}/excluded", data={"excluded": "true"}, headers=headers
    )
    assert response.status_code in (404, 422)


# --- Tenant isolation -----------------------------------------------------------------


async def test_one_organisation_cannot_see_or_change_another(
    app, client: AsyncClient, db, make_owner
):
    await synced_owner(app, db, make_owner, "admin@acme.test")
    globex = await synced_owner(app, db, make_owner, "admin@globex.test")
    acme_james = await employee(db, "james.okafor@acme.test")

    headers = sign_in(client, globex)
    for path in ("/app/employees", "/app/employees/rows"):
        text = (await client.get(path)).text
        assert "james.okafor@globex.test" in text
        assert "acme.test" not in text
    # Searching for the other company's address finds nobody.
    found = (await client.get("/app/employees/rows", params={"q": "acme.test"})).text
    assert "Nobody matches" in found

    # Globex's admin tries to exclude an Acme employee by id.
    response = await client.post(
        f"/app/employees/{acme_james.id}/excluded", data={"excluded": "true"}, headers=headers
    )
    assert response.status_code == 404
    assert (await employee(db, "james.okafor@acme.test")).excluded is False


# --- Sync now -------------------------------------------------------------------------


async def test_sync_now_queues_runs_and_refreshes(
    client: AsyncClient, db, make_owner, job_queue, run_jobs
):
    owner = await make_owner("admin@acme.test")
    headers = {**sign_in(client, owner), **HTMX}

    started = await client.post("/app/employees/sync", headers=headers)
    assert started.status_code == 200
    assert "Syncing with Google" in started.text
    assert 'hx-trigger="every 2s"' in started.text  # the fragment keeps polling
    assert len(job_queue.jobs) == 1
    assert (await organization(db, owner.org_id)).last_sync_status == SyncStatus.QUEUED

    waiting = await client.get("/app/employees/sync-status", headers=HTMX)
    assert "Syncing with Google" in waiting.text
    assert "HX-Trigger" not in waiting.headers

    await run_jobs()

    done = await client.get("/app/employees/sync-status", headers=HTMX)
    assert "Last synced just now" in done.text
    assert "every 2s" not in done.text  # polling stops
    assert done.headers["HX-Trigger"] == "employees-changed"  # the table reloads itself
    assert "Priya Raman" in (await client.get("/app/employees/rows")).text
    assert "12 in your directory" in (await client.get("/app/employees/counts")).text


async def test_sync_now_requires_csrf_token(client: AsyncClient, make_owner, job_queue):
    sign_in(client, await make_owner("admin@acme.test"))
    assert (await client.post("/app/employees/sync")).status_code == 403
    assert job_queue.jobs == {}


async def test_sync_that_never_finished_is_reported(client: AsyncClient, db, make_owner):
    owner = await make_owner("admin@acme.test")
    async with db.session() as session:
        org = await session.get(Organization, owner.org_id)
        org.last_sync_status = SyncStatus.QUEUED
        org.sync_requested_at = datetime.now(UTC) - STALE_AFTER - timedelta(seconds=5)
        await session.commit()

    sign_in(client, owner)
    text = (await client.get("/app/employees/sync-status")).text
    assert "did not finish" in text
    assert "every 2s" not in text  # no endless spinner


async def test_failed_sync_shows_the_reason(client: AsyncClient, db, make_owner, run_jobs):
    owner = await make_owner("outage@acme.test")  # the fake directory is unreachable for this admin
    headers = sign_in(client, owner)
    await client.post("/app/employees/sync", headers=headers)
    await run_jobs()

    text = (await client.get("/app/employees/sync-status")).text
    assert "Google could not be reached" in text
    assert 'role="alert"' in text
    assert (await organization(db, owner.org_id)).status == OrgStatus.CONNECTED


async def test_lost_access_sends_the_owner_back_to_the_wizard(
    client: AsyncClient, db, make_owner, run_jobs
):
    owner = await make_owner("nodelegation@acme.test")
    headers = sign_in(client, owner)
    await client.post("/app/employees/sync", headers=headers)
    await run_jobs()

    assert (await organization(db, owner.org_id)).status == OrgStatus.DISCONNECTED
    response = await client.get("/app/employees")
    assert response.headers["location"] == "/app/setup"
    assert "lost access" in (await client.get("/app/setup")).text


@pytest.mark.parametrize("path", ["/app/employees/sync-status?view=nope"])
async def test_unknown_view_is_rejected(client: AsyncClient, make_owner, path):
    sign_in(client, await make_owner("admin@acme.test"))
    assert (await client.get(path)).status_code == 400


# --- Wizard step 4 --------------------------------------------------------------------


async def test_verification_starts_the_first_sync(
    client: AsyncClient, db, make_user, job_queue, run_jobs
):
    user = await make_user("admin@acme.test")
    headers = {**sign_in(client, user), **HTMX}

    verified = await client.post("/app/setup/verify", headers=headers)
    assert verified.headers["HX-Redirect"] == "/app/setup/sync"
    assert [job["task_name"] for job in job_queue.jobs.values()] == ["sync.sync_org"]

    page = await client.get("/app/setup/sync")
    assert page.status_code == 200
    assert "Reading your directory from Google" in page.text
    assert "view=setup" in page.text and 'hx-trigger="every 2s"' in page.text

    await run_jobs()

    done = await client.get("/app/employees/sync-status", params={"view": "setup"})
    assert "12 employees found" in done.text
    assert "11 active, 1 suspended" in done.text
    assert 'href="/app/employees"' in done.text
    assert "every 2s" not in done.text


async def test_step_four_offers_a_retry_when_the_sync_failed(
    client: AsyncClient, make_owner, run_jobs
):
    owner = await make_owner("outage@acme.test")
    headers = {**sign_in(client, owner), **HTMX}
    await client.post("/app/employees/sync", data={"view": "setup"}, headers=headers)
    await run_jobs()

    text = (await client.get("/app/setup/sync")).text
    assert "Google could not be reached" in text
    assert "Try again" in text


async def test_step_four_singular_wording(app: FastAPI, client: AsyncClient, db, make_owner):
    from tests.fakes import ScriptedDirectory, gateways, person

    owner = await make_owner("admin@solo.test")
    solo = gateways(ScriptedDirectory([person("admin", "solo.test")]))
    await sync_org(db, solo, app.state.storage, owner.org_id)
    sign_in(client, owner)
    text = (await client.get("/app/employees/sync-status", params={"view": "setup"})).text
    assert "1 employee found" in text
