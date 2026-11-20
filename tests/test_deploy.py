"""Deployment: writing signatures to mailboxes, retries, failures and automatic updates.

Mailboxes are a ScriptedGmail (tests/fakes.py), so every write is recorded and nothing
leaves the test process.
"""

import json
from datetime import UTC, datetime, timedelta

import httplib2
import pytest
from fastapi import FastAPI
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from sqlalchemy import select

from app.db import Database
from app.google.base import (
    SCOPE_GMAIL_SETTINGS,
    GoogleGateways,
    NotAuthorizedError,
    PermanentError,
    RetryableError,
    SkipError,
)
from app.google.fake import FakeGmailGateway
from app.google.real import RealGmailGateway, translate_write_error
from app.models import (
    ApplyStatus,
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
    Employee,
    Organization,
    OrgStatus,
    SyncStatus,
)
from app.services import deploy as deploy_service
from app.services import signatures as signature_service
from app.services.deploy import (
    FAIL_TEMPORARY,
    FAIL_TOO_LONG,
    MAX_ATTEMPTS,
    SKIP_NO_MAILBOX,
    SKIP_NOT_ELIGIBLE,
    STALE_AFTER,
    DeploymentInProgress,
    NothingPublished,
    NotInDirectory,
    apply_signature,
    signature_hash,
)
from app.services.sync import sync_org
from app.signatures.render import render_signature
from app.tasks.deploy import apply_signature_task, enforce_all, start_deployment
from app.tasks.periodic import sync_all
from app.tasks.sync import request_sync
from tests.fakes import ScriptedDirectory, ScriptedGmail, gateways, person

MANUAL, SYNC = DeploymentTrigger.MANUAL, DeploymentTrigger.SYNC
# The fake directory has twelve people: the admin and eleven others. One is suspended,
# so eleven are eligible, and one of those (Tom Baker) has no mailbox.
ELIGIBLE, WITH_MAILBOX = 11, 10


@pytest.fixture
def mail(app: FastAPI) -> ScriptedGmail:
    """Replace the mailboxes with ones that record writes; keep the fake directory."""
    gmail = ScriptedGmail()
    app.state.google = GoogleGateways(directory=app.state.google.directory, gmail=gmail)
    return gmail


async def make_live(app: FastAPI, db: Database, make_owner, email: str = "admin@acme.test"):
    """A connected, synced organisation whose signature has been published."""
    owner = await make_owner(email)
    assert (await sync_org(db, app.state.google, app.state.storage, owner.org_id)).ok
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        signature = await signature_service.create_signature(session, organization, "classic")
        await deploy_service.publish_draft(session, signature)
    return owner


@pytest.fixture
async def owner(app: FastAPI, db: Database, make_owner, mail):
    return await make_live(app, db, make_owner)


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


async def deployment_of(db: Database, deployment_id) -> Deployment:
    async with db.session() as session:
        return await session.get(Deployment, deployment_id)


async def organization_of(db: Database, org_id) -> Organization:
    async with db.session() as session:
        return await session.get(Organization, org_id)


async def start(db: Database, org_id, trigger=MANUAL, **options) -> Deployment:
    async with db.session() as session:
        organization = await session.get(Organization, org_id)
        return await start_deployment(session, organization, trigger, **options)


async def run(db: Database, run_jobs, org_id, trigger=MANUAL, **options) -> Deployment:
    """Start a deployment, run its jobs and return it as stored afterwards."""
    deployment = await start(db, org_id, trigger, **options)
    await run_jobs()
    return await deployment_of(db, deployment.id)


# --- A full deployment ----------------------------------------------------------------


async def test_full_deployment_writes_every_included_mailbox(db, owner, mail, run_jobs, job_queue):
    deployment = await start(db, owner.org_id)
    assert deployment.status == DeploymentStatus.RUNNING
    assert deployment.total == ELIGIBLE
    assert len(job_queue.jobs) == ELIGIBLE  # one job per mailbox

    await run_jobs()
    done = await deployment_of(db, deployment.id)
    assert done.status == DeploymentStatus.COMPLETED
    assert (done.succeeded, done.failed, done.skipped) == (WITH_MAILBOX, 0, 1)
    assert done.finished_at is not None

    assert len(mail.writes) == WITH_MAILBOX
    assert len(set(mail.written_to)) == WITH_MAILBOX
    assert "olivia.brown@acme.test" not in mail.written_to  # suspended
    assert "tom.baker@acme.test" not in mail.written_to  # no mailbox


async def test_each_mailbox_gets_its_own_details(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)

    priya = mail.signature("priya.raman@acme.test")
    assert "Priya Raman" in priya and "Head of Operations" in priya
    assert "+1 415 555 0101" in priya and "mailto:priya.raman@acme.test" in priya
    assert "James Okafor" not in priya
    assert "James Okafor" in mail.signature("james.okafor@acme.test")

    row = await employee(db, "priya.raman@acme.test")
    assert row.apply_status == ApplyStatus.APPLIED
    assert row.applied_hash == signature_hash(priya)
    assert row.applied_at is not None and row.apply_error is None


async def test_mailbox_gets_exactly_what_the_preview_shows(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)
    row = await employee(db, "sofia.martinez@acme.test")
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        signature = await signature_service.get_signature(session, owner.org_id)
        design = signature_service.draft_of(signature)
        org_ctx = await signature_service.build_org_context(session, organization, design)
    preview = render_signature(design, signature_service.employee_data(row), org_ctx)
    assert mail.signature("sofia.martinez@acme.test") == preview


async def test_person_without_a_mailbox_is_skipped_without_calling_google(
    db, owner, mail, run_jobs
):
    await run(db, run_jobs, owner.org_id)
    tom = await employee(db, "tom.baker@acme.test")
    assert (tom.apply_status, tom.apply_error) == (ApplyStatus.SKIPPED, SKIP_NO_MAILBOX)
    assert tom.applied_hash is None


async def test_excluded_and_suspended_people_are_left_alone(db, owner, mail, run_jobs):
    async with db.session() as session:
        james = (
            await session.execute(
                select(Employee).where(Employee.email == "james.okafor@acme.test")
            )
        ).scalar_one()
        james.excluded = True
        await session.commit()

    done = await run(db, run_jobs, owner.org_id)
    assert done.total == ELIGIBLE - 1
    assert "james.okafor@acme.test" not in mail.written_to
    assert (await employee(db, "james.okafor@acme.test")).apply_status == ApplyStatus.NEVER
    assert (await employee(db, "olivia.brown@acme.test")).apply_status == ApplyStatus.NEVER


async def test_nothing_can_be_deployed_before_a_design_is_published(app, db, make_owner, mail):
    unpublished = await make_owner("admin@new.test")
    with pytest.raises(NothingPublished):
        await start(db, unpublished.org_id)
    async with db.session() as session:
        organization = await session.get(Organization, unpublished.org_id)
        await signature_service.create_signature(session, organization, "classic")
    with pytest.raises(NothingPublished):  # a draft alone is not enough
        await start(db, unpublished.org_id)
    assert mail.writes == []


async def test_deployment_with_nobody_to_update_completes_at_once(app, db, make_owner, mail):
    lonely = await make_owner("admin@empty.test")  # connected, never synced: no employees
    async with db.session() as session:
        organization = await session.get(Organization, lonely.org_id)
        signature = await signature_service.create_signature(session, organization, "classic")
        await deploy_service.publish_draft(session, signature)
    deployment = await start(db, lonely.org_id)
    assert (deployment.status, deployment.total) == (DeploymentStatus.COMPLETED, 0)
    assert deployment.finished_at is not None


# --- Only writing what changed --------------------------------------------------------


async def test_unchanged_signatures_make_no_call_to_google(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)
    assert len(mail.writes) == WITH_MAILBOX

    again = await run(db, run_jobs, owner.org_id, SYNC)
    assert len(mail.writes) == WITH_MAILBOX  # nothing new was written
    assert again.status == DeploymentStatus.COMPLETED
    assert (again.succeeded, again.skipped) == (WITH_MAILBOX, 1)  # still counted as fine


async def test_forced_run_writes_every_mailbox_again(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)
    await run(db, run_jobs, owner.org_id, force=True)
    assert len(mail.writes) == 2 * WITH_MAILBOX


async def test_design_change_reaches_everyone(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)
    async with db.session() as session:
        signature = await signature_service.get_signature(session, owner.org_id)
        design = signature_service.draft_of(signature)
        design.company.tagline = "A brand new tagline"
        await signature_service.save_draft(session, signature, design)
        await deploy_service.publish_draft(session, signature)

    await run(db, run_jobs, owner.org_id, SYNC)  # not forced: the hash decides
    assert len(mail.writes) == 2 * WITH_MAILBOX
    assert "A brand new tagline" in mail.signature("chen.wei@acme.test")


async def test_deployment_limited_to_some_employees(db, owner, mail, run_jobs):
    priya = await employee(db, "priya.raman@acme.test")
    olivia = await employee(db, "olivia.brown@acme.test")  # suspended: filtered out
    done = await run(db, run_jobs, owner.org_id, SYNC, employee_ids={priya.id, olivia.id})
    assert done.total == 1
    assert mail.written_to == ["priya.raman@acme.test"]


async def test_draft_changes_are_not_deployed_until_published(db, owner, mail, run_jobs):
    async with db.session() as session:
        signature = await signature_service.get_signature(session, owner.org_id)
        design = signature_service.draft_of(signature)
        design.company.tagline = "Unfinished idea"
        await signature_service.save_draft(session, signature, design)
    await run(db, run_jobs, owner.org_id)
    assert all("Unfinished idea" not in html for _, html in mail.writes)


# --- Failures -------------------------------------------------------------------------


async def test_temporary_error_is_retried_and_then_succeeds(db, owner, mail):
    """Service level: the first try raises so the queue retries; the second works."""
    mail.fail["priya.raman@acme.test"] = [RetryableError("HTTP 429"), None]
    priya = await employee(db, "priya.raman@acme.test")
    deployment = await start(db, owner.org_id, SYNC, employee_ids={priya.id})

    with pytest.raises(RetryableError):
        await apply_signature(db, gateways(ScriptedDirectory(), mail), priya.id, deployment.id)
    waiting = await deployment_of(db, deployment.id)
    assert (waiting.status, waiting.processed) == (DeploymentStatus.RUNNING, 0)
    assert (await employee(db, priya.email)).apply_status == ApplyStatus.NEVER

    outcome = await apply_signature(
        db, gateways(ScriptedDirectory(), mail), priya.id, deployment.id
    )
    assert outcome.status == ApplyStatus.APPLIED and outcome.wrote
    done = await deployment_of(db, deployment.id)
    assert (done.status, done.succeeded) == (DeploymentStatus.COMPLETED, 1)


async def test_temporary_error_on_the_last_try_is_recorded_as_a_failure(db, owner, mail):
    mail.fail["priya.raman@acme.test"] = RetryableError("HTTP 503")
    priya = await employee(db, "priya.raman@acme.test")
    deployment = await start(db, owner.org_id, SYNC, employee_ids={priya.id})

    outcome = await apply_signature(
        db, gateways(ScriptedDirectory(), mail), priya.id, deployment.id, final_attempt=True
    )
    assert (outcome.status, outcome.error) == (ApplyStatus.FAILED, FAIL_TEMPORARY)
    row = await employee(db, priya.email)
    assert (row.apply_status, row.apply_error) == (ApplyStatus.FAILED, FAIL_TEMPORARY)
    done = await deployment_of(db, deployment.id)
    assert (done.status, done.failed) == (DeploymentStatus.COMPLETED_WITH_ERRORS, 1)


async def test_the_job_queue_retries_a_temporary_error(db, owner, mail, run_jobs, job_queue):
    """Task level: after a temporary error the job waits to run again."""
    mail.fail["priya.raman@acme.test"] = [RetryableError("HTTP 429"), None]
    priya = await employee(db, "priya.raman@acme.test")
    deployment = await start(db, owner.org_id, SYNC, employee_ids={priya.id})
    await run_jobs()

    (job,) = job_queue.jobs.values()
    assert (job["status"], job["attempts"]) == ("todo", 1)  # scheduled again, not failed
    assert job["scheduled_at"] > datetime.now(UTC)
    assert (await deployment_of(db, deployment.id)).status == DeploymentStatus.RUNNING


def test_retry_policy():
    strategy = apply_signature_task.retry_strategy
    # MAX_ATTEMPTS tries in total; only temporary errors are retried.
    assert strategy.max_attempts == MAX_ATTEMPTS - 1 == 4
    assert tuple(strategy.retry_exceptions) == (RetryableError,)
    assert strategy.exponential_wait == 3


async def test_skip_is_recorded_with_its_reason(db, owner, mail, run_jobs):
    mail.fail["noah.fischer@acme.test"] = SkipError("Gmail is not enabled for this user")
    done = await run(db, run_jobs, owner.org_id)

    noah = await employee(db, "noah.fischer@acme.test")
    assert noah.apply_status == ApplyStatus.SKIPPED
    assert noah.apply_error == "Gmail is not enabled for this user"
    # Skips are expected and do not count as errors.
    assert done.status == DeploymentStatus.COMPLETED
    assert (done.succeeded, done.skipped) == (WITH_MAILBOX - 1, 2)


async def test_refused_mailbox_fails_without_stopping_the_others(db, owner, mail, run_jobs):
    mail.fail["liam.oconnor@acme.test"] = PermanentError("Google refused the change (HTTP 403)")
    done = await run(db, run_jobs, owner.org_id)

    assert done.status == DeploymentStatus.COMPLETED_WITH_ERRORS
    assert (done.succeeded, done.failed, done.skipped) == (WITH_MAILBOX - 1, 1, 1)
    liam = await employee(db, "liam.oconnor@acme.test")
    assert liam.apply_status == ApplyStatus.FAILED
    assert "refused" in liam.apply_error
    assert liam.applied_hash is None
    assert len(mail.writes) == WITH_MAILBOX - 1

    async with db.session() as session:
        attention = await deploy_service.employees_needing_attention(session, owner.org_id)
        assert {e.email for e in attention} == {"liam.oconnor@acme.test", "tom.baker@acme.test"}
        assert await deploy_service.count_failed(session, owner.org_id) == 1


async def test_failed_mailbox_recovers_on_a_later_run(db, owner, mail, run_jobs):
    mail.fail["liam.oconnor@acme.test"] = PermanentError("refused")
    await run(db, run_jobs, owner.org_id)
    del mail.fail["liam.oconnor@acme.test"]

    liam = await employee(db, "liam.oconnor@acme.test")
    done = await run(db, run_jobs, owner.org_id, employee_ids={liam.id}, force=True)
    assert (done.status, done.succeeded) == (DeploymentStatus.COMPLETED, 1)
    liam = await employee(db, liam.email)
    assert (liam.apply_status, liam.apply_error) == (ApplyStatus.APPLIED, None)


async def test_lost_access_disconnects_and_stops_the_deployment(db, owner, mail, run_jobs):
    for address in ("admin@acme.test", "aisha.khan@acme.test", "amelia.clarke@acme.test"):
        mail.fail[address] = NotAuthorizedError("unauthorized_client")
    done = await run(db, run_jobs, owner.org_id)

    assert done.status == DeploymentStatus.FAILED
    assert done.finished_at is not None
    assert (await organization_of(db, owner.org_id)).status == OrgStatus.DISCONNECTED
    # Once access is known to be gone, the remaining jobs do not call Google at all.
    assert mail.writes == []


async def test_signature_too_long_for_someone_is_a_failure(db, owner, mail, run_jobs):
    long_url = "https://example.com/" + "a" * 1900
    networks = ("linkedin", "x", "facebook", "instagram", "youtube", "github")
    async with db.session() as session:
        signature = await signature_service.get_signature(session, owner.org_id)
        published = dict(signature.published_design)
        published["socials"] = [{"network": n, "url": long_url} for n in networks]
        signature.published_design = published
        await session.commit()

    priya = await employee(db, "priya.raman@acme.test")
    done = await run(db, run_jobs, owner.org_id, employee_ids={priya.id})
    assert done.status == DeploymentStatus.COMPLETED_WITH_ERRORS
    assert (await employee(db, priya.email)).apply_error == FAIL_TOO_LONG
    assert mail.writes == []


async def test_person_excluded_after_queueing_is_skipped(db, owner, mail, job_queue, run_jobs):
    deployment = await start(db, owner.org_id)
    async with db.session() as session:
        james = (
            await session.execute(
                select(Employee).where(Employee.email == "james.okafor@acme.test")
            )
        ).scalar_one()
        james.excluded = True
        await session.commit()
    await run_jobs()

    assert "james.okafor@acme.test" not in mail.written_to
    james = await employee(db, "james.okafor@acme.test")
    assert (james.apply_status, james.apply_error) == (ApplyStatus.SKIPPED, SKIP_NOT_ELIGIBLE)
    assert (await deployment_of(db, deployment.id)).status == DeploymentStatus.COMPLETED


# --- One manual deployment at a time --------------------------------------------------


async def test_second_manual_deployment_is_refused_while_one_runs(db, owner, mail, run_jobs):
    first = await start(db, owner.org_id)
    with pytest.raises(DeploymentInProgress):
        await start(db, owner.org_id)
    # Automatic deployments are not blocked by it.
    automatic = await start(db, owner.org_id, SYNC)
    assert automatic.status == DeploymentStatus.RUNNING

    await run_jobs()
    assert (await deployment_of(db, first.id)).status == DeploymentStatus.COMPLETED
    assert (await start(db, owner.org_id)).status == DeploymentStatus.RUNNING  # allowed again


async def test_a_deployment_that_never_finished_stops_blocking(db, owner, mail):
    stuck = await start(db, owner.org_id)
    async with db.session() as session:
        row = await session.get(Deployment, stuck.id)
        row.created_at = datetime.now(UTC) - STALE_AFTER - timedelta(minutes=1)
        await session.commit()

    fresh = await start(db, owner.org_id)  # the stale one is given up on first
    assert fresh.status == DeploymentStatus.RUNNING
    stuck = await deployment_of(db, stuck.id)
    assert stuck.status == DeploymentStatus.FAILED and stuck.finished_at is not None


async def test_jobs_of_a_finished_deployment_do_nothing(db, owner, mail):
    priya = await employee(db, "priya.raman@acme.test")
    deployment = await start(db, owner.org_id, SYNC, employee_ids={priya.id})
    async with db.session() as session:
        row = await session.get(Deployment, deployment.id)
        row.status = DeploymentStatus.FAILED
        await session.commit()
    outcome = await apply_signature(
        db, gateways(ScriptedDirectory(), mail), priya.id, deployment.id
    )
    assert outcome.status is None and mail.writes == []


# --- Apply to me only -----------------------------------------------------------------


async def test_apply_to_self_touches_one_mailbox_and_publishes_nothing(app, db, make_owner, mail):
    owner = await make_owner("admin@acme.test")
    await sync_org(db, app.state.google, app.state.storage, owner.org_id)
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        signature = await signature_service.create_signature(session, organization, "classic")
        design = signature_service.draft_of(signature)
        design.company.tagline = "Only in the draft"
        outcome = await deploy_service.apply_to_self(
            session, app.state.google, organization, owner, design
        )

    assert outcome.status == ApplyStatus.APPLIED and outcome.wrote
    assert mail.written_to == ["admin@acme.test"]
    assert "Only in the draft" in mail.signature("admin@acme.test")

    async with db.session() as session:
        signature = await signature_service.get_signature(session, owner.org_id)
        assert signature.published_design is None
        test_run = (await session.execute(select(Deployment))).scalar_one()
    assert test_run.trigger == DeploymentTrigger.TEST
    assert (test_run.status, test_run.total, test_run.succeeded) == ("completed", 1, 1)
    admin = await employee(db, "admin@acme.test")
    assert admin.applied_hash == signature_hash(mail.signature("admin@acme.test"))


async def test_apply_to_self_reports_a_temporary_failure_at_once(db, owner, mail):
    mail.fail["admin@acme.test"] = RetryableError("HTTP 503")
    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        design = signature_service.draft_of(
            await signature_service.get_signature(session, owner.org_id)
        )
        outcome = await deploy_service.apply_to_self(
            session, gateways(ScriptedDirectory(), mail), organization, owner, design
        )
    assert (outcome.status, outcome.error) == (ApplyStatus.FAILED, FAIL_TEMPORARY)


async def test_apply_to_self_needs_the_admin_in_the_directory(app, db, make_owner, mail):
    stranger = await make_owner("admin@empty.test")  # never synced
    async with db.session() as session:
        organization = await session.get(Organization, stranger.org_id)
        signature = await signature_service.create_signature(session, organization, "classic")
        with pytest.raises(NotInDirectory):
            await deploy_service.apply_to_self(
                session,
                app.state.google,
                organization,
                stranger,
                signature_service.draft_of(signature),
            )
    assert mail.writes == []


# --- Automatic updates ----------------------------------------------------------------


@pytest.fixture
async def scripted(app: FastAPI, db: Database, make_owner):
    """A live organisation whose directory the test can change."""
    directory = ScriptedDirectory(
        [
            person("admin", job_title="Founder"),
            person("priya.raman", job_title="Head of Operations", phone_work="+1 415 555 0101"),
            person("james.okafor", job_title="Account Executive"),
        ]
    )
    gmail = ScriptedGmail()
    app.state.google = gateways(directory, gmail)
    owner = await make_live(app, db, make_owner)
    return owner, directory, gmail


async def sync_and_run(db: Database, run_jobs, org_id) -> None:
    async with db.session() as session:
        await request_sync(session, await session.get(Organization, org_id))
    await run_jobs()  # the sync job, then the deployment jobs it queues


async def test_first_sync_after_publishing_gives_everyone_the_signature(db, scripted, run_jobs):
    owner, _, gmail = scripted
    # make_live synced before publishing, so nobody has a signature yet.
    await sync_and_run(db, run_jobs, owner.org_id)
    assert gmail.writes == []  # an unchanged directory triggers no deployment

    await run(db, run_jobs, owner.org_id)
    assert sorted(gmail.written_to) == [
        "admin@acme.test",
        "james.okafor@acme.test",
        "priya.raman@acme.test",
    ]


async def test_new_hire_gets_a_signature_after_the_next_sync(db, scripted, run_jobs):
    owner, directory, gmail = scripted
    await run(db, run_jobs, owner.org_id)
    gmail.writes.clear()

    directory.users.append(person("new.hire", job_title="Analyst"))
    await sync_and_run(db, run_jobs, owner.org_id)

    assert gmail.written_to == ["new.hire@acme.test"]  # only the new person
    assert "Analyst" in gmail.signature("new.hire@acme.test")
    assert (await employee(db, "new.hire@acme.test")).apply_status == ApplyStatus.APPLIED
    async with db.session() as session:
        latest = await deploy_service.latest_deployment(session, owner.org_id)
    assert (latest.trigger, latest.total, latest.status) == (SYNC, 1, DeploymentStatus.COMPLETED)


async def test_title_change_updates_that_persons_signature(db, scripted, run_jobs):
    owner, directory, gmail = scripted
    await run(db, run_jobs, owner.org_id)
    assert "Account Executive" in gmail.signature("james.okafor@acme.test")
    gmail.writes.clear()

    directory.replace("g-james.okafor", job_title="Sales Director")
    await sync_and_run(db, run_jobs, owner.org_id)

    assert gmail.written_to == ["james.okafor@acme.test"]
    assert "Sales Director" in gmail.signature("james.okafor@acme.test")


async def test_person_who_leaves_is_not_written_to(db, scripted, run_jobs):
    owner, directory, gmail = scripted
    await run(db, run_jobs, owner.org_id)
    gmail.writes.clear()

    directory.remove("g-james.okafor")
    directory.replace("g-priya.raman", suspended=True)
    await sync_and_run(db, run_jobs, owner.org_id)
    assert gmail.writes == []


async def test_sync_without_a_published_design_deploys_nothing(app, db, make_owner, run_jobs):
    directory = ScriptedDirectory([person("admin"), person("priya.raman")])
    gmail = ScriptedGmail()
    app.state.google = gateways(directory, gmail)
    owner = await make_owner("admin@acme.test")
    await sync_and_run(db, run_jobs, owner.org_id)

    assert (await organization_of(db, owner.org_id)).last_sync_status == SyncStatus.OK
    assert gmail.writes == []
    async with db.session() as session:
        assert await deploy_service.latest_deployment(session, owner.org_id) is None


async def test_failed_sync_deploys_nothing(db, scripted, run_jobs):
    owner, directory, gmail = scripted
    directory.users.append(person("new.hire"))
    directory.error = NotAuthorizedError("unauthorized_client")
    await sync_and_run(db, run_jobs, owner.org_id)
    assert gmail.writes == []


async def test_hourly_sync_covers_connected_organisations_only(
    app, db, make_owner, make_user, job_queue, mail
):
    connected = await make_owner("admin@acme.test")
    await make_user("someone@pending.test")  # never connected
    lost = await make_owner("admin@lost.test")
    async with db.session() as session:
        organization = await session.get(Organization, lost.org_id)
        organization.status = OrgStatus.DISCONNECTED
        await session.commit()

    assert await sync_all(db) == 1
    jobs = list(job_queue.jobs.values())
    assert [job["task_name"] for job in jobs] == ["sync.sync_org"]
    assert json.loads(json.dumps(jobs[0]["args"])) == {"org_id": str(connected.org_id)}
    assert (await organization_of(db, connected.org_id)).last_sync_status == SyncStatus.QUEUED


async def test_daily_enforcement_rewrites_every_mailbox(db, owner, mail, run_jobs):
    await run(db, run_jobs, owner.org_id)
    assert await enforce_all(db) == 1
    await run_jobs()

    assert len(mail.writes) == 2 * WITH_MAILBOX  # forced, although nothing changed
    async with db.session() as session:
        latest = await deploy_service.latest_deployment(session, owner.org_id)
    assert (latest.trigger, latest.status) == (
        DeploymentTrigger.ENFORCE,
        DeploymentStatus.COMPLETED,
    )


async def test_daily_enforcement_respects_the_setting_and_needs_a_live_design(
    app, db, make_owner, mail, job_queue
):
    opted_out = await make_live(app, db, make_owner, "admin@acme.test")
    async with db.session() as session:
        organization = await session.get(Organization, opted_out.org_id)
        organization.daily_enforce = False
        await session.commit()
    await make_owner("admin@nodesign.test")  # connected, nothing published

    assert await enforce_all(db) == 0
    assert job_queue.jobs == {}
    assert mail.writes == []


# --- Tenant isolation -----------------------------------------------------------------


async def test_deployment_stays_inside_its_organisation(app, db, make_owner, mail, run_jobs):
    acme = await make_live(app, db, make_owner, "admin@acme.test")
    globex = await make_live(app, db, make_owner, "admin@globex.test")

    done = await run(db, run_jobs, acme.org_id)
    assert done.total == ELIGIBLE
    assert all(address.endswith("@acme.test") for address in mail.written_to)
    assert (await employee(db, "priya.raman@globex.test")).apply_status == ApplyStatus.NEVER

    # A job pairing one organisation's employee with another's deployment does nothing.
    outsider = await employee(db, "priya.raman@globex.test")
    other = await start(db, acme.org_id, SYNC)
    before = len(mail.writes)
    outcome = await apply_signature(db, app.state.google, outsider.id, other.id)
    assert outcome.status is None and len(mail.writes) == before
    assert globex.org_id != acme.org_id


async def test_employee_ids_from_another_organisation_are_ignored(app, db, make_owner, mail):
    acme = await make_live(app, db, make_owner, "admin@acme.test")
    await make_live(app, db, make_owner, "admin@globex.test")
    outsider = await employee(db, "priya.raman@globex.test")
    deployment = await start(db, acme.org_id, SYNC, employee_ids={outsider.id})
    assert deployment.total == 0


# --- The real Gmail gateway (network replaced) ----------------------------------------


def http_error(status: int, reason: str = "", api_status: str = "") -> HttpError:
    error = {"message": "nope", "errors": [{"reason": reason}] if reason else []}
    if api_status:
        error["status"] = api_status
    return HttpError(httplib2.Response({"status": status}), json.dumps({"error": error}).encode())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(429), RetryableError),
        (http_error(500), RetryableError),
        (http_error(503), RetryableError),
        (http_error(403, "rateLimitExceeded"), RetryableError),
        (http_error(403, "userRateLimitExceeded"), RetryableError),
        (http_error(400, "failedPrecondition"), SkipError),
        (http_error(400, api_status="FAILED_PRECONDITION"), SkipError),
        (http_error(404, "notFound"), SkipError),
        (http_error(400, "invalidArgument"), PermanentError),
        (http_error(403, "forbidden"), PermanentError),
        (http_error(403), PermanentError),
        (RefreshError("unauthorized_client: not delegated"), NotAuthorizedError),
        (RefreshError("access_denied: Requested client not authorized"), NotAuthorizedError),
        (RefreshError("invalid_grant: Invalid email or User ID"), SkipError),
        (RefreshError("something else went wrong"), RetryableError),
        (TransportError("connection reset"), RetryableError),
        (TimeoutError("timed out"), RetryableError),
    ],
)
def test_gmail_write_errors_map_to_deployment_outcomes(error, expected):
    assert type(translate_write_error(error)) is expected


def test_malformed_error_body_is_still_classified():
    error = HttpError(httplib2.Response({"status": 400}), b"<html>not json</html>")
    assert type(translate_write_error(error)) is PermanentError


async def test_real_gateway_patches_the_primary_address_as_that_user(monkeypatch):
    calls: dict = {}

    class Recorder:
        def __getattr__(self, name):
            def method(**kwargs):
                calls.setdefault("chain", []).append(name)
                if kwargs:
                    calls["kwargs"] = kwargs
                return self

            return method

    gateway = RealGmailGateway({"type": "service_account"})
    monkeypatch.setattr(
        gateway, "_credentials", lambda subject, scope: calls.update(subject=subject, scope=scope)
    )
    monkeypatch.setattr("app.google.real.google_auth_httplib2.AuthorizedHttp", lambda c, http: http)
    monkeypatch.setattr(
        "app.google.real.build",
        lambda api, version, **kw: calls.update(api=(api, version)) or Recorder(),
    )

    await gateway.set_signature("priya@acme.test", "<table>sig</table>")

    assert (calls["subject"], calls["scope"]) == ("priya@acme.test", SCOPE_GMAIL_SETTINGS)
    assert calls["api"] == ("gmail", "v1")
    assert calls["chain"] == ["users", "settings", "sendAs", "patch", "execute"]
    # Only the signature is sent: nothing else about the address is touched.
    assert calls["kwargs"] == {
        "userId": "me",
        "sendAsEmail": "priya@acme.test",
        "body": {"signature": "<table>sig</table>"},
    }


async def test_real_gateway_translates_write_failures(monkeypatch):
    gateway = RealGmailGateway({"type": "service_account"})

    def refuse(email, html):
        raise http_error(400, "failedPrecondition")

    monkeypatch.setattr(gateway, "_patch_signature", refuse)
    with pytest.raises(SkipError, match="not enabled"):
        await gateway.set_signature("tom@acme.test", "<table></table>")


# --- The fake Gmail gateway -----------------------------------------------------------


async def test_fake_gmail_keeps_the_latest_signature_and_has_two_awkward_mailboxes():
    gmail = FakeGmailGateway()
    await gmail.set_signature("Priya.Raman@acme.test", "<table>one</table>")
    await gmail.set_signature("priya.raman@acme.test", "<table>two</table>")
    assert gmail.signatures == {"priya.raman@acme.test": "<table>two</table>"}

    with pytest.raises(PermanentError):
        await gmail.set_signature("liam.oconnor@acme.test", "<table></table>")
    with pytest.raises(RetryableError):
        await gmail.set_signature("chen.wei@acme.test", "<table></table>")
    await gmail.set_signature("chen.wei@acme.test", "<table></table>")  # works the second time
    with pytest.raises(NotAuthorizedError):
        await gmail.set_signature("nodelegation@acme.test", "<table></table>")


def test_signature_hash_is_stable_and_content_sensitive():
    assert signature_hash("<table>a</table>") == signature_hash("<table>a</table>")
    assert signature_hash("<table>a</table>") != signature_hash("<table>b</table>")
    assert len(signature_hash("é")) == 64


# --- Sync and signature updates stay in step ------------------------------------------


async def test_signature_updates_are_queued_before_the_sync_is_marked_finished(app, db, scripted):
    """Otherwise a page watching the sync could see "finished" with the new hire's
    signature not yet started, and show them as not applied."""
    owner, directory, _ = scripted
    directory.users.append(person("new.hire"))
    seen = {}

    async def hook(result):
        seen["sync_status"] = (await organization_of(db, owner.org_id)).last_sync_status
        hire = await employee(db, "new.hire@acme.test")  # already saved
        seen["hire_reported"] = hire.id in result.changed_ids

    result = await sync_org(db, app.state.google, app.state.storage, owner.org_id, hook)
    assert result.ok
    assert seen == {"sync_status": SyncStatus.RUNNING, "hire_reported": True}
    assert (await organization_of(db, owner.org_id)).last_sync_status == SyncStatus.OK


async def test_failed_sync_does_not_run_the_after_changes_step(app, db, scripted):
    owner, directory, _ = scripted
    directory.error = NotAuthorizedError("unauthorized_client")
    called = []

    async def hook(result):
        called.append(result)

    await sync_org(db, app.state.google, app.state.storage, owner.org_id, hook)
    assert called == []


async def test_deployment_in_progress_ignores_personal_tests(db, owner, mail):
    async with db.session() as session:
        assert await deploy_service.deployment_in_progress(session, owner.org_id) is None
    running = await start(db, owner.org_id, SYNC)
    async with db.session() as session:
        found = await deploy_service.deployment_in_progress(session, owner.org_id)
        assert found.id == running.id
        row = await session.get(Deployment, running.id)
        row.trigger = DeploymentTrigger.TEST
        await session.commit()
        assert await deploy_service.deployment_in_progress(session, owner.org_id) is None
