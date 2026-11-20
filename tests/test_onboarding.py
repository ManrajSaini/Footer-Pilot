"""The setup wizard, ownership rules and tenant isolation."""

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient

from app.db import Database
from app.google.base import AdminInfo, GoogleGateways
from app.models import Organization, OrgStatus, User
from app.services.connection import VerifyResult, verify_connection
from tests.helpers import HTMX, sign_in


async def load_org(db: Database, user: User) -> Organization:
    async with db.session() as session:
        return await session.get(Organization, user.org_id)


async def connect(client: AsyncClient, user: User) -> None:
    """Sign in as `user` and complete verification."""
    headers = sign_in(client, user)
    response = await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert response.headers.get("HX-Redirect") == "/app/setup/sync", response.text


# --- Anonymous visitors ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/app", "/app/setup"])
async def test_app_pages_redirect_anonymous_visitors(client: AsyncClient, db, path):
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/signup"


async def test_verify_rejects_anonymous_visitors(client: AsyncClient, db):
    # No session at all: stopped by the CSRF check.
    assert (await client.post("/app/setup/verify")).status_code == 403
    # A session that passes CSRF but has nobody signed in.
    headers = sign_in(client, None)
    plain = await client.post("/app/setup/verify", headers=headers)
    assert plain.status_code == 303
    assert plain.headers["location"] == "/signup"
    # HTMX gets a header it follows with a full page load.
    htmx = await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"] == "/signup"


# --- The wizard -----------------------------------------------------------------------


async def test_unconnected_organisation_is_sent_to_the_wizard(client: AsyncClient, make_user):
    sign_in(client, await make_user("admin@acme.test"))
    response = await client.get("/app")
    assert response.status_code == 303
    assert response.headers["location"] == "/app/setup"


async def test_wizard_shows_everything_the_admin_needs(client: AsyncClient, make_user):
    sign_in(client, await make_user("admin@acme.test"))
    response = await client.get("/app/setup")
    assert response.status_code == 200
    page = response.text
    assert "acme.test" in page
    assert "admin@acme.test" in page
    assert "123456789012345678901" in page  # the Client ID to paste
    assert (
        "https://www.googleapis.com/auth/admin.directory.user.readonly,"
        "https://www.googleapis.com/auth/gmail.settings.basic"
    ) in page
    assert 'href="https://admin.google.com/ac/owl/domainwidedelegation"' in page
    assert "cannot read, send or delete" in page
    assert 'href="https://example.com/book-a-call"' in page
    assert 'hx-post="/app/setup/verify"' in page
    assert 'content="noindex"' in page


async def test_successful_verification_connects_the_organisation(
    client: AsyncClient, db: Database, make_user
):
    user = await make_user("admin@acme.test")
    headers = sign_in(client, user)

    response = await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert response.status_code == 200
    # On to step 4, where the first directory sync is shown.
    assert response.headers["HX-Redirect"] == "/app/setup/sync"

    organization = await load_org(db, user)
    assert organization.status == OrgStatus.CONNECTED
    assert organization.owner_user_id == user.id
    assert organization.google_customer_id.startswith("C")
    assert organization.connected_at is not None

    dashboard = await client.get("/app")
    assert dashboard.status_code == 200
    assert "Connected" in dashboard.text
    assert "acme.test" in dashboard.text
    # The wizard is finished, so it sends the owner on to the dashboard.
    assert (await client.get("/app/setup")).headers["location"] == "/app"


async def test_verification_without_htmx_redirects(client: AsyncClient, make_user):
    headers = sign_in(client, await make_user("admin@acme.test"))
    response = await client.post("/app/setup/verify", headers=headers)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/setup/sync"


@pytest.mark.parametrize(
    ("email", "message"),
    [
        ("nodelegation@acme.test", "FooterPilot is not authorised yet"),
        ("nogmail@acme.test", "FooterPilot is not authorised yet"),
        ("notadmin@acme.test", "This account is not a Workspace super admin"),
        ("outage@acme.test", "The check with Google did not complete"),
    ],
)
async def test_each_verification_failure_explains_itself(
    client: AsyncClient, db: Database, make_user, email: str, message: str
):
    user = await make_user(email)
    headers = sign_in(client, user)
    response = await client.post("/app/setup/verify", headers={**headers, **HTMX})

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert message in response.text
    assert email in response.text
    assert 'role="alert"' in response.text

    organization = await load_org(db, user)
    assert organization.status == OrgStatus.PENDING
    assert organization.owner_user_id is None


async def test_not_authorised_message_mentions_the_delay(client: AsyncClient, make_user):
    headers = sign_in(client, await make_user("nodelegation@acme.test"))
    response = await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert "wait a few minutes" in response.text


async def test_verify_requires_csrf_token(client: AsyncClient, db: Database, make_user):
    user = await make_user("admin@acme.test")
    sign_in(client, user)
    assert (await client.post("/app/setup/verify")).status_code == 403
    assert (await load_org(db, user)).status == OrgStatus.PENDING


async def test_a_failed_attempt_can_be_retried(client: AsyncClient, app: FastAPI, db, make_user):
    """Delegation missing at first, then added: the same user succeeds on retry."""
    user = await make_user("admin@acme.test")
    headers = {**sign_in(client, user), **HTMX}
    working = app.state.google

    class NotYetDelegated:
        async def verify_admin(self, admin_email):
            from app.google.base import NotAuthorizedError

            raise NotAuthorizedError("unauthorized_client")

    app.state.google = GoogleGateways(directory=NotYetDelegated(), gmail=working.gmail)
    first = await client.post("/app/setup/verify", headers=headers)
    assert "not authorised yet" in first.text

    app.state.google = working
    second = await client.post("/app/setup/verify", headers=headers)
    assert second.headers["HX-Redirect"] == "/app/setup/sync"


# --- Ownership ------------------------------------------------------------------------


async def test_colleague_may_attempt_setup_while_nobody_owns_the_organisation(
    client: AsyncClient, make_user
):
    await make_user("first@acme.test")
    sign_in(client, await make_user("second@acme.test"))
    assert (await client.get("/app/setup")).status_code == 200


async def test_second_person_is_locked_out_once_the_domain_has_an_owner(
    client: AsyncClient, db: Database, make_user
):
    owner = await make_user("admin@acme.test")
    colleague = await make_user("colleague@acme.test")
    await connect(client, owner)

    headers = sign_in(client, colleague)
    for path in ("/app", "/app/setup"):
        response = await client.get(path)
        assert response.status_code == 403, path
        assert "Your company is already set up" in response.text
        assert "admin@acme.test" in response.text  # who to contact
        assert "Connected" not in response.text  # nothing from the dashboard

    response = await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert response.status_code == 403
    assert (await load_org(db, owner)).owner_user_id == owner.id


async def test_owner_keeps_access(client: AsyncClient, make_user):
    owner = await make_user("admin@acme.test")
    await make_user("colleague@acme.test")
    await connect(client, owner)
    assert (await client.get("/app")).status_code == 200


async def test_losing_a_verification_race_gives_no_access(db: Database, app: FastAPI, make_user):
    """Two admins verify at once. The second finds the organisation already owned."""
    first = await make_user("admin@acme.test")
    second = await make_user("other@acme.test")

    async with db.session() as session_a, db.session() as session_b:
        # Both load their user while the organisation has no owner yet.
        user_a = await session_a.get(User, first.id)
        user_b = await session_b.get(User, second.id)
        assert await verify_connection(session_a, app.state.google, user_a) == "connected"
        result = await verify_connection(session_b, app.state.google, user_b)

    assert result == VerifyResult.ALREADY_OWNED
    assert (await load_org(db, first)).owner_user_id == first.id


async def test_non_admin_cannot_take_ownership(client: AsyncClient, db: Database, make_user):
    user = await make_user("notadmin@acme.test")
    headers = sign_in(client, user)
    await client.post("/app/setup/verify", headers={**headers, **HTMX})
    assert (await load_org(db, user)).owner_user_id is None
    assert (await client.get("/app")).headers["location"] == "/app/setup"


# --- Reconnecting ---------------------------------------------------------------------


async def test_owner_can_reconnect_a_disconnected_organisation(
    client: AsyncClient, db: Database, make_user
):
    owner = await make_user("admin@acme.test")
    await connect(client, owner)
    connected_at = (await load_org(db, owner)).connected_at

    async with db.session() as session:
        organization = await session.get(Organization, owner.org_id)
        organization.status = OrgStatus.DISCONNECTED
        await session.commit()

    # The dashboard explains what happened; other pages go to the wizard.
    dashboard = await client.get("/app")
    assert dashboard.status_code == 200
    assert "lost access" in dashboard.text and 'href="/app/setup"' in dashboard.text
    assert "Disconnected" in dashboard.text
    assert (await client.get("/app/employees")).headers["location"] == "/app/setup"
    page = await client.get("/app/setup")
    assert page.status_code == 200
    assert "lost access" in page.text

    await connect(client, owner)
    organization = await load_org(db, owner)
    assert organization.status == OrgStatus.CONNECTED
    assert organization.connected_at == connected_at  # first connection date is kept


# --- Tenant isolation -----------------------------------------------------------------


async def test_organisations_cannot_see_or_change_each_other(
    client: AsyncClient, db: Database, make_user
):
    acme = await make_user("admin@acme.test")
    globex = await make_user("admin@globex.test")
    assert acme.org_id != globex.org_id

    await connect(client, acme)
    dashboard = await client.get("/app")
    assert "acme.test" in dashboard.text
    assert "globex.test" not in dashboard.text

    # Acme connecting did nothing to Globex.
    other = await load_org(db, globex)
    assert other.status == OrgStatus.PENDING
    assert other.owner_user_id is None

    # Globex sees only its own, still unconnected, organisation.
    sign_in(client, globex)
    assert (await client.get("/app")).headers["location"] == "/app/setup"
    wizard = await client.get("/app/setup")
    assert "globex.test" in wizard.text
    assert "acme.test" not in wizard.text


async def test_verification_impersonates_only_the_signed_in_user(
    client: AsyncClient, app: FastAPI, make_user
):
    """Google is asked about the signed-in admin's own address and nobody else's."""
    asked: list[tuple[str, str]] = []

    class Recording:
        async def verify_admin(self, admin_email):
            asked.append(("directory", admin_email))
            return AdminInfo(email=admin_email, customer_id="C123")

        async def verify_access(self, email):
            asked.append(("gmail", email))

    app.state.google = GoogleGateways(directory=Recording(), gmail=Recording())
    await make_user("admin@globex.test")
    await connect(client, await make_user("admin@acme.test"))
    assert asked == [("directory", "admin@acme.test"), ("gmail", "admin@acme.test")]
