from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.integrations.base_client.errors import OAuthError
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import Database
from app.main import SESSION_COOKIE, create_app
from app.models import Organization, OrgStatus, User
from app.web.auth import WorkspaceAccountRequired, identity_from_claims
from tests.helpers import session_cookie, sign_in

CLAIMS = {
    "sub": "google-sub-1",
    "email": "Dana@Acme.test",
    "email_verified": True,
    "hd": "acme.test",
    "name": "Dana Admin",
    "picture": "https://images.example.com/dana.jpg",
}


def fake_google_response(app: FastAPI, monkeypatch, claims=None, error: Exception | None = None):
    """Make the OAuth callback behave as if Google returned these ID-token claims."""

    async def authorize_access_token(request):
        if error is not None:
            raise error
        return {"userinfo": claims}

    monkeypatch.setattr(app.state.oauth_google, "authorize_access_token", authorize_access_token)


async def count(db: Database, model) -> int:
    async with db.session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# --- Claims ---------------------------------------------------------------------------


def test_identity_from_workspace_claims():
    identity = identity_from_claims(CLAIMS)
    assert identity.sub == "google-sub-1"
    assert identity.domain == "acme.test"
    assert identity.name == "Dana Admin"


def test_personal_account_has_no_hd_claim():
    claims = {k: v for k, v in CLAIMS.items() if k != "hd"}
    with pytest.raises(WorkspaceAccountRequired):
        identity_from_claims(claims)


@pytest.mark.parametrize("override", [{"email_verified": False}, {"sub": ""}, {"email": None}])
def test_unverified_or_incomplete_claims_are_rejected(override):
    with pytest.raises(ValueError, match="verified email"):
        identity_from_claims({**CLAIMS, **override})


# --- Sign-in page and redirect --------------------------------------------------------


async def test_sign_in_page(client: AsyncClient):
    for path in ("/signup", "/login"):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'href="/auth/google/login"' in response.text
        assert "/dev/login" not in response.text


async def test_login_redirects_to_google_without_any_network_call(client: AsyncClient):
    response = await client.get("/auth/google/login")
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    query = parse_qs(location.query)
    assert query["client_id"] == ["test-client-id.apps.googleusercontent.com"]
    assert query["redirect_uri"] == ["http://testserver/auth/google/callback"]
    assert query["scope"] == ["openid email profile"]  # nothing beyond basic sign-in
    assert query["hd"] == ["*"]
    assert query["state"] and query["nonce"]


async def test_login_reports_missing_configuration(job_queue, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        ):
            page = await c.get("/signup")
            response = await c.get("/auth/google/login")
    finally:
        get_settings.cache_clear()
    assert "not configured" in page.text
    assert response.status_code == 503


# --- Callback -------------------------------------------------------------------------


async def test_callback_creates_user_and_pending_organisation(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    fake_google_response(app, monkeypatch, CLAIMS)
    response = await client.get("/auth/google/callback?code=x&state=y")
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert SESSION_COOKIE in response.cookies

    async with db.session() as session:
        user = (await session.execute(select(User))).scalar_one()
        assert user.email == "dana@acme.test"  # stored in lowercase
        assert user.google_sub == "google-sub-1"
        assert user.name == "Dana Admin"
        assert user.last_login_at is not None
        assert user.organization.domain == "acme.test"
        assert user.organization.status == OrgStatus.PENDING
        assert user.organization.owner_user_id is None

    # The session works: an unconnected organisation is sent to the wizard.
    home = await client.get("/app")
    assert home.status_code == 303
    assert home.headers["location"] == "/app/setup"


async def test_signing_in_again_reuses_the_user(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    fake_google_response(app, monkeypatch, CLAIMS)
    await client.get("/auth/google/callback?code=x&state=y")
    fake_google_response(app, monkeypatch, {**CLAIMS, "name": "Dana Renamed"})
    await client.get("/auth/google/callback?code=x&state=y")

    assert await count(db, User) == 1
    assert await count(db, Organization) == 1
    async with db.session() as session:
        assert (await session.execute(select(User.name))).scalar_one() == "Dana Renamed"


async def test_colleagues_share_one_organisation(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    fake_google_response(app, monkeypatch, CLAIMS)
    await client.get("/auth/google/callback?code=x&state=y")
    fake_google_response(
        app, monkeypatch, {**CLAIMS, "sub": "google-sub-2", "email": "eli@acme.test"}
    )
    await client.get("/auth/google/callback?code=x&state=y")

    assert await count(db, User) == 2
    assert await count(db, Organization) == 1


async def test_personal_gmail_account_is_turned_away(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    claims = {**CLAIMS, "email": "someone@gmail.com"}
    del claims["hd"]
    fake_google_response(app, monkeypatch, claims)
    response = await client.get("/auth/google/callback?code=x&state=y")
    assert response.status_code == 403
    assert "A Google Workspace account is needed" in response.text
    assert SESSION_COOKIE not in response.cookies
    assert await count(db, User) == 0
    assert await count(db, Organization) == 0


async def test_unverified_email_is_rejected(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    fake_google_response(app, monkeypatch, {**CLAIMS, "email_verified": False})
    response = await client.get("/auth/google/callback?code=x&state=y")
    assert response.status_code == 502
    assert "could not complete the sign-in" in response.text
    assert await count(db, User) == 0


async def test_cancelled_or_tampered_sign_in_shows_a_message(
    app: FastAPI, client: AsyncClient, db: Database, monkeypatch
):
    fake_google_response(app, monkeypatch, error=OAuthError(error="mismatching_state"))
    response = await client.get("/auth/google/callback?code=x&state=forged")
    assert response.status_code == 400
    assert "Sign-in was not completed" in response.text
    assert await count(db, User) == 0


async def test_real_callback_rejects_a_request_without_matching_state(client: AsyncClient):
    # No monkeypatching: Authlib itself refuses a callback it did not start.
    response = await client.get("/auth/google/callback?code=stolen&state=forged")
    assert response.status_code == 400


# --- Session --------------------------------------------------------------------------


async def test_signed_in_user_skips_the_sign_in_page(client: AsyncClient, make_user):
    sign_in(client, await make_user("dana@acme.test"))
    response = await client.get("/signup")
    assert response.status_code == 303
    assert response.headers["location"] == "/app"


async def test_logout_ends_the_session(client: AsyncClient, make_user):
    headers = sign_in(client, await make_user("dana@acme.test"))
    response = await client.post("/auth/logout", headers=headers)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    after = await client.get("/app")
    assert after.status_code == 303
    assert after.headers["location"] == "/signup"


async def test_logout_requires_csrf_token(client: AsyncClient, make_user):
    sign_in(client, await make_user("dana@acme.test"))
    assert (await client.post("/auth/logout")).status_code == 403
    assert (await client.get("/app")).headers["location"] == "/app/setup"  # still signed in


async def test_session_for_a_deleted_user_is_discarded(client: AsyncClient, db, make_user):
    user = await make_user("dana@acme.test")
    sign_in(client, user)
    async with db.session() as session:
        await session.delete(await session.get(Organization, user.org_id))
        await session.commit()
    response = await client.get("/app")
    assert response.headers["location"] == "/signup"


@pytest.mark.parametrize("value", ["not-a-uuid", "", "00000000-0000-0000-0000-000000000000"])
async def test_garbage_session_user_id_is_treated_as_signed_out(client: AsyncClient, db, value):
    client.cookies.set(SESSION_COOKIE, session_cookie({"user_id": value}))
    response = await client.get("/app")
    assert response.status_code == 303
    assert response.headers["location"] == "/signup"


async def test_unsigned_session_cookie_is_ignored(client: AsyncClient, make_user):
    import base64
    import json

    user = await make_user("dana@acme.test")
    forged = base64.b64encode(json.dumps({"user_id": str(user.id)}).encode()).decode()
    client.cookies.set(SESSION_COOKIE, forged)
    assert (await client.get("/app")).headers["location"] == "/signup"


# --- Development sign-in --------------------------------------------------------------


async def test_dev_login_is_not_available_by_default(client: AsyncClient):
    assert (await client.get("/dev/login")).status_code == 404
    assert (await client.post("/dev/login", data={"email": "a@b.test"})).status_code in (403, 404)


async def test_dev_login_when_enabled(job_queue, schema, monkeypatch):
    monkeypatch.setenv("DEV_LOGIN", "true")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
        ):
            page = await c.get("/dev/login")
            assert page.status_code == 200
            assert "Development only" in page.text
            assert "/dev/login" in (await c.get("/signup")).text

            token = page.text.split('name="csrf_token" value="')[1].split('"')[0]
            bad = await c.post("/dev/login", data={"email": "nope", "csrf_token": token})
            assert bad.status_code == 400

            response = await c.post(
                "/dev/login", data={"email": "Admin@Acme.test", "csrf_token": token}
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/app"
            assert (await c.get("/app")).headers["location"] == "/app/setup"

            async with app.state.db.engine.begin() as connection:
                from sqlalchemy import text

                await connection.execute(text("TRUNCATE organizations, users CASCADE"))
    finally:
        get_settings.cache_clear()
