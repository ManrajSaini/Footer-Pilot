"""Applying from the designer, the deployment page, the dashboard and settings."""

import re
import uuid

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.google.base import GoogleGateways, NotAuthorizedError, PermanentError, RetryableError
from app.models import (
    ApplyStatus,
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    Employee,
    Organization,
    OrgStatus,
)
from app.services import signatures as signature_service
from app.services.sync import sync_org
from tests.fakes import ScriptedGmail
from tests.helpers import HTMX, form, sign_in

APPLY_TEST = "/app/designer/apply-test"
APPLY = "/app/designer/apply"
ELIGIBLE, WITH_MAILBOX = 11, 10  # see tests/test_deploy.py


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
    """Signed in, with the Classic template chosen. Returns HTMX request headers."""
    headers = sign_in(client, owner)
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    return {**headers, **HTMX}


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


async def signature_row(db: Database, org_id):
    async with db.session() as session:
        return await signature_service.get_signature(session, org_id)


async def deployments(db: Database) -> list[Deployment]:
    async with db.session() as session:
        rows = await session.execute(select(Deployment).order_by(Deployment.created_at))
        return list(rows.scalars())


async def apply_everyone(client: AsyncClient, headers, **changes) -> str:
    """Apply to everyone and return the id of the deployment it started."""
    response = await client.post(APPLY, data=form(**changes), headers=headers)
    assert response.status_code == 200, response.text
    return response.headers["HX-Redirect"].rsplit("/", 1)[1]


# --- The Apply card -------------------------------------------------------------------


async def test_designer_offers_both_ways_to_apply(client: AsyncClient, designing):
    page = (await client.get("/app/designer")).text
    assert 'hx-post="/app/designer/apply-test"' in page
    assert 'hx-post="/app/designer/apply"' in page
    # The confirmation states how many mailboxes will change.
    assert f"Apply to {ELIGIBLE} employees" in page
    assert re.search(rf"<strong[^>]*>{ELIGIBLE} employees</strong>", page)
    assert "cannot be brought back" in page


async def test_designer_warns_when_images_cannot_reach_mailboxes(client: AsyncClient, designing):
    # The test server keeps images locally, like a developer's machine.
    assert "will not load in real email" in (await client.get("/app/designer")).text


@pytest.mark.parametrize(
    ("settings", "public"),
    [
        ({"storage_backend": "local", "asset_base_url": "https://app.example.com"}, False),
        ({"asset_base_url": "http://127.0.0.1:8000"}, False),
        ({"asset_base_url": "http://localhost:8000"}, False),
        ({"asset_base_url": "https://app.example.com"}, True),
    ],
)
def test_images_are_public_only_with_hosted_storage_and_a_real_address(settings, public):
    r2 = {
        "storage_backend": "r2",
        "r2_account_id": "a",
        "r2_access_key_id": "k",
        "r2_secret_access_key": "s",
        "r2_bucket": "b",
        "r2_public_base_url": "https://assets.example.com",
    }
    assert Settings(_env_file=None, **{**r2, **settings}).images_are_public is public


async def test_apply_buttons_are_disabled_without_anyone_to_apply_to(
    client: AsyncClient, make_owner
):
    lonely = await make_owner("admin@empty.test")
    headers = sign_in(client, lonely)
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    page = (await client.get("/app/designer")).text
    assert "no included, active employees" in page


# --- Apply to me only -----------------------------------------------------------------


async def test_apply_to_me_writes_only_my_mailbox(client: AsyncClient, db, owner, mail, designing):
    response = await client.post(
        APPLY_TEST, data=form(company_tagline="Straight from the form"), headers=designing
    )
    assert response.status_code == 200
    assert "Applied to admin@acme.test only, as a test" in response.text
    assert "Nobody else was changed" in response.text
    assert "still a draft" in response.text and "Apply to everyone" in response.text

    assert mail.written_to == ["admin@acme.test"]
    # What was on screen is what was written, even though it was never saved before.
    assert "Straight from the form" in mail.signature("admin@acme.test")
    signature = await signature_row(db, owner.org_id)
    assert signature.published_design is None  # nothing published
    assert signature.draft_design["company"]["tagline"] == "Straight from the form"
    (run,) = await deployments(db)
    assert (run.trigger, run.status, run.created_by) == ("test", "completed", owner.id)


async def test_apply_to_me_with_invalid_input_changes_nothing(
    client: AsyncClient, db, owner, mail, designing
):
    response = await client.post(APPLY_TEST, data=form(primary_color="blue"), headers=designing)
    assert response.headers["HX-Reswap"] == "none"
    assert "Fix the highlighted fields before applying" in response.text
    assert "Enter a colour" in response.text
    assert mail.writes == [] and await deployments(db) == []


async def test_apply_to_me_reports_a_mailbox_google_refuses(client: AsyncClient, mail, designing):
    mail.fail["admin@acme.test"] = PermanentError("Google refused the change (HTTP 403: nope)")
    response = await client.post(APPLY_TEST, data=form(), headers=designing)
    assert "Your signature was not changed" in response.text
    assert 'role="alert"' in response.text


async def test_apply_to_me_reports_a_temporary_problem(client: AsyncClient, mail, designing):
    mail.fail["admin@acme.test"] = RetryableError("HTTP 503")
    response = await client.post(APPLY_TEST, data=form(), headers=designing)
    assert "temporarily unavailable" in response.text
    assert "Retry failed" not in response.text  # that button does not exist here


async def test_apply_to_me_when_the_admin_is_not_in_the_directory(
    client: AsyncClient, make_owner, mail
):
    stranger = await make_owner("admin@empty.test")  # never synced
    headers = {**sign_in(client, stranger), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    response = await client.post(APPLY_TEST, data=form(), headers=headers)
    assert "is not in the employee list yet" in response.text
    assert 'href="/app/employees"' in response.text
    assert mail.writes == []


async def test_apply_to_me_when_access_was_lost(client: AsyncClient, db, owner, mail, designing):
    mail.fail["admin@acme.test"] = NotAuthorizedError("unauthorized_client")
    response = await client.post(APPLY_TEST, data=form(), headers=designing)
    assert response.headers["HX-Redirect"] == "/app"  # the dashboard explains

    dashboard = await client.get("/app")
    assert "lost access" in dashboard.text and "Reconnect" in dashboard.text
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).status == OrgStatus.DISCONNECTED


# --- Apply to everyone ----------------------------------------------------------------


async def test_apply_to_everyone_publishes_and_starts_a_deployment(
    client: AsyncClient, db, owner, mail, designing, job_queue, run_jobs
):
    deployment_id = await apply_everyone(client, designing, company_tagline="Now live")

    signature = await signature_row(db, owner.org_id)
    assert signature.published_design == signature.draft_design
    assert signature.published_design["company"]["tagline"] == "Now live"
    assert signature.published_at is not None
    assert len(job_queue.jobs) == ELIGIBLE
    (run,) = await deployments(db)
    assert str(run.id) == deployment_id
    assert (run.trigger, run.total, run.created_by) == ("manual", ELIGIBLE, owner.id)

    # While it runs, the page polls.
    page = await client.get(f"/app/deployments/{deployment_id}")
    assert page.status_code == 200
    # The step Google only lets each person do for themselves, ready to send on.
    assert "One-time step for each person in Gmail" in page.text
    assert "Insert signature before quoted text in replies" in page.text
    assert "Copy this note" in page.text
    assert f"0 of {ELIGIBLE} done" in page.text
    assert 'hx-trigger="every 2s"' in page.text

    await run_jobs()
    status = await client.get(f"/app/deployments/{deployment_id}/status")
    assert f"Done. {WITH_MAILBOX} mailboxes have the signature." in status.text
    assert "1 skipped" in status.text
    assert "every 2s" not in status.text  # polling has stopped
    assert "Tom Baker" in status.text and "no Gmail mailbox" in status.text
    assert "Retry failed" not in status.text  # a skip is not a failure

    assert len(mail.writes) == WITH_MAILBOX
    assert "Now live" in mail.signature("priya.raman@acme.test")
    assert "Live · no changes" in (await client.get("/app/designer")).text


async def test_applying_again_rewrites_every_mailbox(
    client: AsyncClient, mail, designing, run_jobs
):
    await apply_everyone(client, designing)
    await run_jobs()
    await apply_everyone(client, designing)  # no changes at all
    await run_jobs()
    # The admin asked for everyone, so every mailbox is written again. This is what
    # puts back a signature an employee edited themselves.
    assert len(mail.writes) == 2 * WITH_MAILBOX


async def test_apply_to_everyone_with_invalid_input_publishes_nothing(
    client: AsyncClient, db, owner, mail, designing, job_queue
):
    response = await client.post(
        APPLY, data=form(company_website_url="javascript:alert(1)"), headers=designing
    )
    assert response.headers["HX-Reswap"] == "none"
    assert "HX-Redirect" not in response.headers
    assert "Fix the highlighted fields before applying" in response.text
    assert (await signature_row(db, owner.org_id)).published_design is None
    assert job_queue.jobs == {} and await deployments(db) == []


async def test_apply_while_one_is_running_is_refused(
    client: AsyncClient, db, owner, mail, designing, job_queue
):
    first = await apply_everyone(client, designing, company_tagline="First")
    response = await client.post(APPLY, data=form(company_tagline="Second"), headers=designing)
    assert "HX-Redirect" not in response.headers
    assert "already being applied" in response.text
    assert f'href="/app/deployments/{first}"' in response.text
    assert len(await deployments(db)) == 1
    assert len(job_queue.jobs) == ELIGIBLE
    # The second design was saved as a draft, but did not replace what is being applied.
    signature = await signature_row(db, owner.org_id)
    assert signature.published_design["company"]["tagline"] == "First"
    assert signature.draft_design["company"]["tagline"] == "Second"


async def test_discard_returns_to_the_applied_design(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    await apply_everyone(client, designing, company_tagline="Applied line")
    await run_jobs()
    saved = await client.post(
        "/app/designer/draft", data=form(company_tagline="Abandoned"), headers=designing
    )
    assert "Unpublished changes" in saved.text
    await client.post("/app/designer/discard", headers=designing)
    assert (await signature_row(db, owner.org_id)).draft_design["company"]["tagline"] == (
        "Applied line"
    )


@pytest.mark.parametrize("path", [APPLY_TEST, APPLY, "/app/deployments/retry-failed"])
async def test_applying_requires_csrf_token(client: AsyncClient, db, owner, mail, path):
    headers = sign_in(client, owner)
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    response = await client.post(path, data=form())
    assert response.status_code == 403
    assert mail.writes == [] and await deployments(db) == []


@pytest.mark.parametrize("path", [APPLY_TEST, APPLY])
async def test_applying_requires_a_connected_organisation(
    client: AsyncClient, make_user, mail, path
):
    headers = {**sign_in(client, await make_user("admin@acme.test")), **HTMX}
    response = await client.post(path, data=form(), headers=headers)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/app/setup"
    assert mail.writes == []


# --- Failures and retry ---------------------------------------------------------------


async def test_failed_mailboxes_are_listed_and_can_be_retried(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    mail.fail["liam.oconnor@acme.test"] = PermanentError("Google refused the change (HTTP 403)")
    first = await apply_everyone(client, designing)
    await run_jobs()

    status = (await client.get(f"/app/deployments/{first}/status")).text
    assert "1 of 11 mailbox could not be updated" in " ".join(status.split())
    assert "Liam O&#39;Connor" in status and "Google refused the change" in status
    assert "Retry failed" in status
    assert "1 mailbox could not be updated" in (await client.get("/app")).text

    # The cause is fixed, and the admin retries.
    del mail.fail["liam.oconnor@acme.test"]
    before = len(mail.writes)
    response = await client.post("/app/deployments/retry-failed", headers=designing)
    second = response.headers["HX-Redirect"].rsplit("/", 1)[1]
    assert second != first
    await run_jobs()

    assert mail.written_to[before:] == ["liam.oconnor@acme.test"]  # only the failed one
    retry = (await deployments(db))[-1]
    assert (retry.trigger, retry.total, retry.status) == ("manual", 1, "completed")
    assert (await employee(db, "liam.oconnor@acme.test")).apply_status == ApplyStatus.APPLIED
    assert "could not be updated" not in (await client.get("/app")).text


async def test_retry_with_nothing_failed_is_refused(
    client: AsyncClient, db, mail, designing, run_jobs
):
    await apply_everyone(client, designing)
    await run_jobs()
    response = await client.post("/app/deployments/retry-failed", headers=designing)
    assert response.status_code == 409
    assert len(await deployments(db)) == 1


async def test_deployment_stopped_by_lost_access_is_explained(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    mail.fail["admin@acme.test"] = NotAuthorizedError("unauthorized_client")
    await apply_everyone(client, designing)
    await run_jobs()

    (run,) = await deployments(db)
    assert run.status == DeploymentStatus.FAILED
    # The organisation is disconnected now: its pages lead back to setup...
    assert (await client.get(f"/app/deployments/{run.id}")).headers["location"] == "/app/setup"
    # ...and the dashboard says why.
    dashboard = (await client.get("/app")).text
    assert "lost access" in dashboard and "Stopped early" in dashboard


async def test_employees_page_shows_each_persons_result(
    client: AsyncClient, mail, designing, run_jobs
):
    mail.fail["liam.oconnor@acme.test"] = PermanentError("refused")
    await apply_everyone(client, designing)
    await run_jobs()
    page = (await client.get("/app/employees")).text
    assert page.count(">Applied<") + page.count("Applied\n") >= 1
    assert "Failed" in page and "Skipped" in page
    assert "Not applied yet" not in page


# --- Deployment pages -----------------------------------------------------------------


async def test_deployment_of_another_organisation_is_not_found(
    app, client: AsyncClient, db, make_owner, owner, mail, designing
):
    deployment_id = await apply_everyone(client, designing)

    globex = await make_owner("admin@globex.test")
    sign_in(client, globex)
    for path in (f"/app/deployments/{deployment_id}", f"/app/deployments/{deployment_id}/status"):
        assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize("bad_id", ["nope", str(uuid.uuid4())])
async def test_unknown_deployment(client: AsyncClient, designing, bad_id):
    assert (await client.get(f"/app/deployments/{bad_id}")).status_code in (404, 422)


async def test_deployment_pages_require_sign_in(client: AsyncClient, db):
    response = await client.get(f"/app/deployments/{uuid.uuid4()}")
    assert (response.status_code, response.headers["location"]) == (303, "/signup")


# --- Dashboard ------------------------------------------------------------------------


async def test_dashboard_shows_the_last_deployment(client: AsyncClient, mail, designing, run_jobs):
    assert "Nothing has been sent to mailboxes" in (await client.get("/app")).text

    deployment_id = await apply_everyone(client, designing)
    running = (await client.get("/app")).text
    assert "In progress" in running and f"0 of {ELIGIBLE} mailboxes done" in running

    await run_jobs()
    done = (await client.get("/app")).text
    assert "Completed" in done and f"{WITH_MAILBOX} updated, 1 skipped" in done
    assert f'href="/app/deployments/{deployment_id}"' in done
    assert ">Live<" in done


async def test_a_personal_test_is_shown_as_a_test_not_as_applied_to_everyone(
    client: AsyncClient, mail, designing
):
    await client.post(APPLY_TEST, data=form(), headers=designing)
    dashboard = (await client.get("/app")).text
    assert "Not yet" in dashboard  # nothing has gone to everyone
    assert "Tested on your own mailbox just now" in dashboard
    assert "Nothing has been sent to mailboxes" not in dashboard  # one mailbox was written
    assert "Not applied to everyone yet" in dashboard
    assert "Draft · not applied to everyone yet" in (await client.get("/app/designer")).text


async def test_a_failed_test_is_not_reported_as_tested(client: AsyncClient, mail, designing):
    mail.fail["admin@acme.test"] = PermanentError("refused")
    await client.post(APPLY_TEST, data=form(), headers=designing)
    assert "Nothing has been sent to mailboxes" in (await client.get("/app")).text


async def test_dashboard_for_a_never_connected_organisation_goes_to_setup(
    client: AsyncClient, make_user
):
    sign_in(client, await make_user("admin@acme.test"))
    assert (await client.get("/app")).headers["location"] == "/app/setup"


# --- Settings -------------------------------------------------------------------------


async def test_daily_enforcement_can_be_switched_off_and_on(client: AsyncClient, db, owner):
    headers = sign_in(client, owner)
    page = (await client.get("/app/settings")).text
    assert re.search(r'name="daily_enforce"[^>]*checked', page)  # on by default
    assert "One-time step for each person in Gmail" in page  # also kept here for later

    off = await client.post("/app/settings", data={}, headers=headers)
    assert "Settings saved" in off.text
    assert not re.search(r'name="daily_enforce"[^>]*checked', off.text)
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).daily_enforce is False

    await client.post("/app/settings", data={"daily_enforce": "1"}, headers=headers)
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).daily_enforce is True


async def test_settings_require_csrf_token_and_a_connected_organisation(
    client: AsyncClient, db, owner, make_user
):
    sign_in(client, owner)
    assert (await client.post("/app/settings", data={})).status_code == 403
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).daily_enforce is True

    sign_in(client, await make_user("someone@pending.test"))
    assert (await client.get("/app/settings")).headers["location"] == "/app/setup"


async def test_settings_only_change_the_signed_in_organisation(
    client: AsyncClient, db, owner, make_owner
):
    globex = await make_owner("admin@globex.test")
    headers = sign_in(client, globex)
    await client.post("/app/settings", data={}, headers=headers)
    async with db.session() as session:
        assert (await session.get(Organization, globex.org_id)).daily_enforce is False
        assert (await session.get(Organization, owner.org_id)).daily_enforce is True


async def test_only_one_deployment_row_per_apply(client: AsyncClient, db, mail, designing):
    await apply_everyone(client, designing)
    async with db.session() as session:
        total = (await session.execute(select(func.count()).select_from(Deployment))).scalar_one()
    assert total == 1
    assert DeploymentTrigger.MANUAL == "manual"


# --- The employees page while signatures are being written ----------------------------


async def test_employees_page_keeps_updating_until_signatures_are_written(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    await apply_everyone(client, designing)  # queued, not yet run

    busy = await client.get("/app/employees/sync-status", headers=HTMX)
    assert f"Updating signatures: 0 of {ELIGIBLE} done" in " ".join(busy.text.split())
    assert 'hx-trigger="every 2s"' in busy.text  # keeps polling
    assert "HX-Trigger" not in busy.headers
    assert "Not applied yet" in (await client.get("/app/employees/rows")).text

    await run_jobs()
    done = await client.get("/app/employees/sync-status", headers=HTMX)
    assert "every 2s" not in done.text
    assert done.headers["HX-Trigger"] == "employees-changed"  # the table reloads now
    assert "Not applied yet" not in (await client.get("/app/employees/rows")).text


async def test_new_hire_is_shown_as_applied_once_the_page_settles(
    app: FastAPI, client: AsyncClient, db, make_owner, run_jobs
):
    from tests.fakes import ScriptedDirectory, gateways, person

    directory = ScriptedDirectory([person("admin"), person("priya.raman")])
    gmail = ScriptedGmail()
    app.state.google = gateways(directory, gmail)
    owner = await make_owner("admin@acme.test")
    await sync_org(db, app.state.google, app.state.storage, owner.org_id)
    headers = {**sign_in(client, owner), **HTMX}
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    await apply_everyone(client, headers)
    await run_jobs()

    directory.users.append(person("new.hire", job_title="Analyst"))
    await client.post("/app/employees/sync", headers=headers)
    await run_jobs()  # the sync, then the new hire's signature

    done = await client.get("/app/employees/sync-status", headers=HTMX)
    assert done.headers["HX-Trigger"] == "employees-changed"
    rows = (await client.get("/app/employees/rows", params={"q": "new.hire"})).text
    assert "Applied" in rows and "Not applied yet" not in rows
    assert gmail.written_to[-1] == "new.hire@acme.test"


# --- Excluding removes the signature; including applies it ----------------------------


async def exclude(client: AsyncClient, headers, employee_id, excluded: bool):
    return await client.post(
        f"/app/employees/{employee_id}/excluded",
        data={"excluded": "true" if excluded else "false"},
        headers=headers,
    )


async def test_excluding_someone_removes_the_signature_we_gave_them(
    client: AsyncClient, db, owner, mail, designing, run_jobs, job_queue
):
    await apply_everyone(client, designing)
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    assert "James Okafor" in mail.signature(james.email)

    # The button warns before removing something from a mailbox.
    rows = (await client.get("/app/employees/rows", params={"q": "james"})).text
    assert "The company signature will be removed from their Gmail" in rows

    response = await exclude(client, designing, james.id, True)
    assert response.status_code == 200 and "Excluded" in response.text
    await run_jobs()

    assert mail.signature(james.email) == ""  # their Gmail signature is now empty
    james = await employee(db, james.email)
    assert james.excluded is True
    assert (james.apply_status, james.applied_hash, james.applied_at) == ("never", None, None)
    # Nobody else was touched.
    assert mail.written_to.count("priya.raman@acme.test") == 1
    assert "Priya Raman" in mail.signature("priya.raman@acme.test")


async def test_including_someone_again_applies_the_current_signature(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    await apply_everyone(client, designing, company_tagline="Version one")
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    await exclude(client, designing, james.id, True)
    await run_jobs()

    # The design moves on while they are excluded.
    await apply_everyone(client, designing, company_tagline="Version two")
    await run_jobs()
    assert mail.signature(james.email) == ""  # still excluded, still empty

    response = await exclude(client, designing, james.id, False)
    assert "Included" in response.text
    # The page is told to re-check, so it follows the signature being written.
    assert response.headers["HX-Trigger"] == "employee-toggled"
    busy = await client.get("/app/employees/sync-status", headers=HTMX)
    assert "Updating signatures: 0 of 1 done" in " ".join(busy.text.split())

    await run_jobs()
    assert "Version two" in mail.signature(james.email)
    assert "Version one" not in mail.signature(james.email)
    assert (await employee(db, james.email)).apply_status == ApplyStatus.APPLIED
    latest = (await deployments(db))[-1]
    assert (latest.trigger, latest.total, latest.status) == ("sync", 1, "completed")


async def test_excluding_someone_we_never_wrote_to_leaves_their_mailbox_alone(
    client: AsyncClient, db, owner, mail, designing, run_jobs, job_queue
):
    """For example an executive with their own signature, excluded before applying."""
    james = await employee(db, "james.okafor@acme.test")
    rows = (await client.get("/app/employees/rows", params={"q": "james"})).text
    assert "will be removed from their Gmail" not in rows  # nothing to warn about

    await exclude(client, designing, james.id, True)
    assert job_queue.jobs == {}  # no job at all
    await apply_everyone(client, designing)
    await run_jobs()
    assert james.email not in mail.written_to


async def test_including_before_anything_is_live_applies_nothing(
    client: AsyncClient, db, owner, mail, designing, job_queue
):
    james = await employee(db, "james.okafor@acme.test")
    await exclude(client, designing, james.id, True)
    await exclude(client, designing, james.id, False)
    assert job_queue.jobs == {} and mail.writes == [] and await deployments(db) == []


async def test_excluding_then_including_at_once_keeps_the_signature(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    await apply_everyone(client, designing)
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    before = mail.signature(james.email)

    await exclude(client, designing, james.id, True)
    await exclude(client, designing, james.id, False)  # changed their mind before the job ran
    await run_jobs()

    assert mail.signature(james.email) == before  # never cleared
    james = await employee(db, james.email)
    assert (james.excluded, james.apply_status) == (False, ApplyStatus.APPLIED)


async def test_signature_that_cannot_be_removed_is_reported(
    client: AsyncClient, db, owner, mail, designing, run_jobs
):
    await apply_everyone(client, designing)
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    mail.fail[james.email] = PermanentError("Google refused the change (HTTP 403)")
    await exclude(client, designing, james.id, True)
    await run_jobs()

    james = await employee(db, james.email)
    assert james.apply_status == ApplyStatus.FAILED
    assert "could not be removed" in james.apply_error
    assert james.applied_hash is not None  # we still know our signature is in there
    rows = (await client.get("/app/employees/rows", params={"q": "james"})).text
    assert "Not removed" in rows


async def test_removal_is_retried_after_a_temporary_error(db, owner, mail, designing, client):
    from app.services.deploy import clear_signature
    from tests.fakes import ScriptedDirectory, gateways

    await apply_everyone(client, designing)
    james = await employee(db, "james.okafor@acme.test")
    async with db.session() as session:  # as if the deployment had reached them
        row = await session.get(Employee, james.id)
        row.applied_hash, row.apply_status, row.excluded = "abc", ApplyStatus.APPLIED, True
        await session.commit()
    google = gateways(ScriptedDirectory(), mail)

    mail.fail[james.email] = [RetryableError("HTTP 429"), None]
    with pytest.raises(RetryableError):  # the queue will try again
        await clear_signature(db, google, james.id)
    assert (await employee(db, james.email)).applied_hash == "abc"
    assert await clear_signature(db, google, james.id) is True
    assert mail.signature(james.email) == ""

    # On the last try a temporary error is recorded instead of raised.
    async with db.session() as session:
        row = await session.get(Employee, james.id)
        row.applied_hash, row.apply_status = "abc", ApplyStatus.APPLIED
        await session.commit()
    mail.fail[james.email] = RetryableError("HTTP 503")
    assert await clear_signature(db, google, james.id, final_attempt=True) is False
    assert (await employee(db, james.email)).apply_status == ApplyStatus.FAILED


async def test_removal_stops_when_access_is_lost(client, db, owner, mail, designing, run_jobs):
    await apply_everyone(client, designing)
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    mail.fail[james.email] = NotAuthorizedError("unauthorized_client")
    await exclude(client, designing, james.id, True)
    await run_jobs()
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).status == OrgStatus.DISCONNECTED


async def test_excluding_never_reaches_another_organisations_mailbox(
    app, client: AsyncClient, db, make_owner, owner, mail, designing, run_jobs, job_queue
):
    await apply_everyone(client, designing)
    await run_jobs()
    james = await employee(db, "james.okafor@acme.test")
    before = mail.signature(james.email)

    globex = await make_owner("admin@globex.test")
    headers = {**sign_in(client, globex), **HTMX}
    response = await exclude(client, headers, james.id, True)
    assert response.status_code == 404
    await run_jobs()
    assert mail.signature(james.email) == before
    assert (await employee(db, james.email)).excluded is False
