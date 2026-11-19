"""Directory sync: what gets created, updated, removed, and how photos are handled."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app.db import Database
from app.google.base import (
    DirectoryPhoto,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
)
from app.models import Employee, EmployeeStatus, Organization, OrgStatus, SyncStatus
from app.services.sync import (
    STALE_AFTER,
    new_short_id,
    sync_in_progress,
    sync_is_stale,
    sync_org,
)
from app.tasks.sync import request_sync
from tests.fakes import MemoryStorage, ScriptedDirectory, gateways, image_bytes, person

PRIYA = person(
    "priya.raman",
    job_title="Head of Operations",
    department="Operations",
    phone_work="+1 415 555 0101",
    phone_mobile="+1 415 555 0191",
)
JAMES = person("james.okafor", job_title="Account Executive")
CHEN = person("chen.wei")


async def employees(db: Database, org_id) -> dict[str, Employee]:
    async with db.session() as session:
        rows = await session.execute(select(Employee).where(Employee.org_id == org_id))
        return {employee.google_user_id: employee for employee in rows.scalars()}


async def organization(db: Database, org_id) -> Organization:
    async with db.session() as session:
        return await session.get(Organization, org_id)


@pytest.fixture
async def owner(make_owner):
    return await make_owner("admin@acme.test")


@pytest.fixture
def directory() -> ScriptedDirectory:
    return ScriptedDirectory([PRIYA, JAMES, CHEN])


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def sync(db: Database, directory: ScriptedDirectory, storage: MemoryStorage, owner):
    async def _sync(org_id=None):
        return await sync_org(db, gateways(directory), storage, org_id or owner.org_id)

    return _sync


# --- Creating and updating ------------------------------------------------------------


async def test_first_sync_creates_everyone(db, sync, owner, directory):
    result = await sync()

    assert result.ok
    assert (result.total, result.created, result.updated, result.removed) == (3, 3, 0, 0)
    people = await employees(db, owner.org_id)
    assert set(people) == {"g-priya.raman", "g-james.okafor", "g-chen.wei"}
    assert result.changed_ids == {p.id for p in people.values()}

    priya = people["g-priya.raman"]
    assert priya.email == "priya.raman@acme.test"
    assert (priya.first_name, priya.last_name, priya.full_name) == ("Priya", "Raman", "Priya Raman")
    assert (priya.job_title, priya.department) == ("Head of Operations", "Operations")
    assert (priya.phone_work, priya.phone_mobile) == ("+1 415 555 0101", "+1 415 555 0191")
    assert priya.status == EmployeeStatus.ACTIVE
    assert priya.excluded is False
    assert priya.apply_status == "never"
    assert priya.synced_at is not None
    assert len({p.short_id for p in people.values()}) == 3
    assert all(len(p.short_id) == 8 and p.short_id.isalnum() for p in people.values())

    org = await organization(db, owner.org_id)
    assert org.last_sync_status == SyncStatus.OK
    assert org.last_sync_at is not None
    assert org.last_sync_error is None
    # The directory is read as the organisation's owner.
    assert directory.asked_as == ["admin@acme.test"]


async def test_unchanged_directory_reports_no_changes(db, sync, owner):
    await sync()
    before = await employees(db, owner.org_id)
    result = await sync()
    assert (result.created, result.updated, result.removed) == (0, 0, 0)
    assert result.changed_ids == set()
    after = await employees(db, owner.org_id)
    assert {k: v.short_id for k, v in after.items()} == {k: v.short_id for k, v in before.items()}


async def test_title_change_updates_the_row_and_is_reported(db, sync, owner, directory):
    await sync()
    directory.replace("g-james.okafor", job_title="Senior Account Executive")
    result = await sync()

    people = await employees(db, owner.org_id)
    assert people["g-james.okafor"].job_title == "Senior Account Executive"
    assert (result.created, result.updated) == (0, 1)
    assert result.changed_ids == {people["g-james.okafor"].id}


@pytest.mark.parametrize(
    "change",
    [
        {"full_name": "Priya R. Raman"},
        {"department": "Ops"},
        {"phone_work": "+1 415 555 9999"},
        {"phone_mobile": ""},
        {"has_mailbox": False},
    ],
)
async def test_any_signature_field_change_is_reported(db, sync, owner, directory, change):
    await sync()
    directory.replace("g-priya.raman", **change)
    result = await sync()
    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert result.changed_ids == {priya.id}
    for name, value in change.items():
        assert getattr(priya, name) == value


async def test_renamed_address_updates_the_same_person(db, sync, owner, directory):
    await sync()
    original = (await employees(db, owner.org_id))["g-priya.raman"]
    directory.replace("g-priya.raman", email="priya.sharma@acme.test", last_name="Sharma")
    await sync()

    people = await employees(db, owner.org_id)
    assert len(people) == 3  # matched on Google's id, so no duplicate
    assert people["g-priya.raman"].id == original.id
    assert people["g-priya.raman"].email == "priya.sharma@acme.test"
    assert people["g-priya.raman"].short_id == original.short_id


async def test_new_hire_appears(db, sync, owner, directory):
    await sync()
    directory.users.append(person("new.hire", job_title="Analyst"))
    result = await sync()
    people = await employees(db, owner.org_id)
    assert result.created == 1
    assert result.changed_ids == {people["g-new.hire"].id}


async def test_overlong_directory_values_are_cut_to_fit(db, sync, owner, directory):
    directory.users = [person("wordy", job_title="T" * 400, phone_work="9" * 100)]
    assert (await sync()).ok
    wordy = (await employees(db, owner.org_id))["g-wordy"]
    assert len(wordy.job_title) == 255
    assert len(wordy.phone_work) == 64


# --- Leaving and suspension -----------------------------------------------------------


async def test_missing_person_is_marked_removed_not_deleted(db, sync, owner, directory):
    await sync()
    directory.remove("g-chen.wei")
    result = await sync()

    people = await employees(db, owner.org_id)
    assert people["g-chen.wei"].status == EmployeeStatus.REMOVED
    assert people["g-chen.wei"].full_name == "Chen Wei"  # the record is kept
    assert result.removed == 1
    assert result.changed_ids == {people["g-chen.wei"].id}

    # Already removed: nothing new to report next time.
    again = await sync()
    assert (again.removed, again.changed_ids) == (0, set())


async def test_person_who_returns_becomes_active_again(db, sync, owner, directory):
    await sync()
    directory.remove("g-chen.wei")
    await sync()
    directory.users.append(CHEN)
    result = await sync()
    chen = (await employees(db, owner.org_id))["g-chen.wei"]
    assert chen.status == EmployeeStatus.ACTIVE
    assert result.changed_ids == {chen.id}


@pytest.mark.parametrize("flag", ["suspended", "archived"])
async def test_suspended_or_archived_person_is_marked_suspended(db, sync, owner, directory, flag):
    await sync()
    directory.replace("g-james.okafor", **{flag: True})
    result = await sync()
    james = (await employees(db, owner.org_id))["g-james.okafor"]
    assert james.status == EmployeeStatus.SUSPENDED
    assert result.changed_ids == {james.id}


async def test_admin_exclusion_survives_a_sync(db, sync, owner, directory):
    await sync()
    async with db.session() as session:
        james = (
            await session.execute(
                select(Employee).where(Employee.google_user_id == "g-james.okafor")
            )
        ).scalar_one()
        james.excluded = True
        await session.commit()
    directory.replace("g-james.okafor", job_title="Changed")
    await sync()
    assert (await employees(db, owner.org_id))["g-james.okafor"].excluded is True


# --- Photos ---------------------------------------------------------------------------


async def test_photo_is_normalised_stored_and_linked(db, sync, owner, directory, storage):
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes("blue", (640, 480)), "image/png")
    result = await sync()

    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert priya.photo_etag == "etag-1"
    assert len(storage.objects) == 1
    key, (data, content_type) = next(iter(storage.objects.items()))
    assert key.startswith(f"orgs/{owner.org_id}/photos/") and key.endswith(".jpg")
    assert priya.photo_url == f"https://cdn.test/{key}"
    assert content_type == "image/jpeg"
    stored = Image.open(io.BytesIO(data))
    assert (stored.format, stored.size) == ("JPEG", (160, 160))
    assert priya.id in result.changed_ids


async def test_unchanged_photo_is_not_downloaded_again(sync, directory):
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes())
    await sync()
    assert directory.photo_requests == ["g-priya.raman"]

    result = await sync()
    assert directory.photo_requests == ["g-priya.raman"]  # no second download
    assert result.changed_ids == set()


async def test_new_photo_replaces_the_old_one(db, sync, owner, directory, storage):
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes("red"))
    await sync()
    first_url = (await employees(db, owner.org_id))["g-priya.raman"].photo_url

    directory.replace("g-priya.raman", photo_etag="etag-2")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes("green"))
    result = await sync()

    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert priya.photo_url != first_url  # the key includes a content hash
    assert priya.photo_etag == "etag-2"
    assert result.changed_ids == {priya.id}


async def test_removed_photo_clears_the_link(db, sync, owner, directory):
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes())
    await sync()
    directory.replace("g-priya.raman", photo_etag=None)
    result = await sync()

    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert (priya.photo_url, priya.photo_etag) == (None, None)
    assert result.changed_ids == {priya.id}


@pytest.mark.parametrize(
    "broken",
    [DirectoryPhoto(b"this is not an image"), GoogleUnavailableError("timeout")],
    ids=["unreadable image", "google error"],
)
async def test_photo_that_fails_is_skipped_and_retried_next_time(
    db, sync, owner, directory, broken
):
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = broken
    result = await sync()
    assert result.ok  # one bad photo never fails the sync
    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert (priya.photo_url, priya.photo_etag) == (None, None)

    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes())
    await sync()
    priya = (await employees(db, owner.org_id))["g-priya.raman"]
    assert priya.photo_url is not None
    assert directory.photo_requests == ["g-priya.raman", "g-priya.raman"]


async def test_failed_upload_does_not_fail_the_sync(db, sync, owner, directory, storage):
    async def broken_put(key, data, content_type):
        raise ConnectionError("storage is down")

    storage.put = broken_put
    directory.replace("g-priya.raman", photo_etag="etag-1")
    directory.photos["g-priya.raman"] = DirectoryPhoto(image_bytes())
    assert (await sync()).ok
    assert (await employees(db, owner.org_id))["g-priya.raman"].photo_url is None


# --- Failures -------------------------------------------------------------------------


async def test_lost_delegation_disconnects_the_organisation(db, sync, owner, directory):
    await sync()
    directory.error = NotAuthorizedError("unauthorized_client")
    result = await sync()

    assert (result.ok, result.error) == (False, "not_authorized")
    org = await organization(db, owner.org_id)
    assert org.status == OrgStatus.DISCONNECTED
    assert (org.last_sync_status, org.last_sync_error) == (SyncStatus.FAILED, "not_authorized")
    # Nobody is touched by a failed sync.
    people = await employees(db, owner.org_id)
    assert all(p.status == EmployeeStatus.ACTIVE for p in people.values())


@pytest.mark.parametrize(
    ("error", "code"),
    [(GoogleUnavailableError("timeout"), "unavailable"), (NotAdminError("demoted"), "not_admin")],
)
async def test_other_failures_keep_the_organisation_connected(
    db, sync, owner, directory, error, code
):
    await sync()
    last_good = (await organization(db, owner.org_id)).last_sync_at
    directory.error = error
    result = await sync()

    assert (result.ok, result.error) == (False, code)
    org = await organization(db, owner.org_id)
    assert org.status == OrgStatus.CONNECTED
    assert (org.last_sync_status, org.last_sync_error) == (SyncStatus.FAILED, code)
    assert org.last_sync_at == last_good  # still the time of the last good sync
    assert len(await employees(db, owner.org_id)) == 3


async def test_a_later_success_clears_the_error(db, sync, owner, directory):
    directory.error = GoogleUnavailableError("timeout")
    await sync()
    directory.error = None
    assert (await sync()).ok
    org = await organization(db, owner.org_id)
    assert (org.last_sync_status, org.last_sync_error) == (SyncStatus.OK, None)


async def test_unconnected_organisation_is_not_synced(db, directory, storage, make_user):
    pending = await make_user("someone@pending.test")
    result = await sync_org(db, gateways(directory), storage, pending.org_id)
    assert (result.ok, result.error) == (False, "not_connected")
    assert directory.asked_as == []  # Google was never contacted
    assert (await organization(db, pending.org_id)).last_sync_status is None


# --- Tenant isolation -----------------------------------------------------------------


async def test_two_organisations_never_share_employees(db, storage, make_owner):
    acme = await make_owner("admin@acme.test")
    globex = await make_owner("admin@globex.test")
    # Both directories contain a user with the same Google id.
    acme_dir = ScriptedDirectory([person("sam", "acme.test", id="shared-id", job_title="Acme")])
    globex_dir = ScriptedDirectory(
        [
            person("sam", "globex.test", id="shared-id", job_title="Globex"),
            person("kim", "globex.test"),
        ]
    )

    await sync_org(db, gateways(acme_dir), storage, acme.org_id)
    await sync_org(db, gateways(globex_dir), storage, globex.org_id)

    acme_people = await employees(db, acme.org_id)
    globex_people = await employees(db, globex.org_id)
    assert {p.email for p in acme_people.values()} == {"sam@acme.test"}
    assert {p.email for p in globex_people.values()} == {"sam@globex.test", "kim@globex.test"}
    assert acme_people["shared-id"].job_title == "Acme"
    assert globex_people["shared-id"].job_title == "Globex"
    assert acme_dir.asked_as == ["admin@acme.test"]
    assert globex_dir.asked_as == ["admin@globex.test"]

    # Someone leaving Globex does not remove anyone at Acme.
    globex_dir.remove("shared-id")
    await sync_org(db, gateways(globex_dir), storage, globex.org_id)
    assert (await employees(db, acme.org_id))["shared-id"].status == EmployeeStatus.ACTIVE


# --- Queueing -------------------------------------------------------------------------


async def test_requested_sync_runs_in_the_worker(app, db, owner, job_queue, run_jobs, directory):
    app.state.google = gateways(directory)
    async with db.session() as session:
        await request_sync(session, await session.get(Organization, owner.org_id))

    org = await organization(db, owner.org_id)
    assert org.last_sync_status == SyncStatus.QUEUED
    assert org.sync_requested_at is not None
    assert [job["task_name"] for job in job_queue.jobs.values()] == ["sync.sync_org"]

    await run_jobs()
    assert (await organization(db, owner.org_id)).last_sync_status == SyncStatus.OK
    assert len(await employees(db, owner.org_id)) == 3


async def test_requesting_twice_queues_one_job(db, owner, job_queue):
    async with db.session() as session:
        org = await session.get(Organization, owner.org_id)
        await request_sync(session, org)
        await request_sync(session, org)
    assert len(job_queue.jobs) == 1


def test_sync_progress_and_staleness():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    org = Organization(domain="acme.test", name="Acme")

    assert not sync_in_progress(org, now) and not sync_is_stale(org, now)  # never synced

    org.last_sync_status = SyncStatus.QUEUED
    org.sync_requested_at = now - timedelta(seconds=30)
    assert sync_in_progress(org, now) and not sync_is_stale(org, now)

    org.last_sync_status = SyncStatus.RUNNING
    assert sync_in_progress(org, now)

    org.sync_requested_at = now - STALE_AFTER - timedelta(seconds=1)
    assert not sync_in_progress(org, now) and sync_is_stale(org, now)

    org.last_sync_status = SyncStatus.OK
    assert not sync_in_progress(org, now) and not sync_is_stale(org, now)


def test_short_ids_are_eight_alphanumeric_characters():
    ids = {new_short_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 8 and i.isalnum() and i.isascii() for i in ids)
