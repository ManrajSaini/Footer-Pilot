"""Google gateways: error translation, configuration and the fake's behaviours.

Nothing here talks to Google. The real gateway's network calls are replaced.
"""

import base64
import json

import httplib2
import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from pydantic import ValidationError

from app.config import Settings
from app.google import build_gateways
from app.google.base import (
    SCOPES,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
)
from app.google.fake import FakeDirectoryGateway, FakeGmailGateway
from app.google.real import (
    RealDirectoryGateway,
    RealGmailGateway,
    load_service_account_info,
    translate_error,
)

KEY = {"type": "service_account", "client_email": "x@p.iam.gserviceaccount.com", "private_key": "k"}
ENCODED_KEY = base64.b64encode(json.dumps(KEY).encode()).decode()


def http_error(status: int) -> HttpError:
    body = json.dumps({"error": {"message": "nope"}}).encode()
    return HttpError(httplib2.Response({"status": status}), body)


def test_only_two_scopes_and_neither_reads_mail():
    assert SCOPES == (
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
        "https://www.googleapis.com/auth/gmail.settings.basic",
    )


# --- Error translation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RefreshError(
                "unauthorized_client: Client is unauthorized to retrieve access tokens "
                "using this method, or client not authorized for any of the scopes requested."
            ),
            NotAuthorizedError,
        ),
        (RefreshError("access_denied: Requested client not authorized."), NotAuthorizedError),
        (RefreshError("invalid_grant: Invalid email or User ID"), GoogleUnavailableError),
        (http_error(403), NotAdminError),
        (http_error(404), GoogleUnavailableError),
        (http_error(500), GoogleUnavailableError),
        (TransportError("connection reset"), GoogleUnavailableError),
        (TimeoutError("timed out"), GoogleUnavailableError),
    ],
)
def test_google_errors_map_to_gateway_errors(error, expected):
    assert type(translate_error(error)) is expected


def test_gateway_errors_pass_through_unchanged():
    error = NotAdminError("x")
    assert translate_error(error) is error


# --- Real gateway, with the network call replaced -------------------------------------


async def test_real_directory_accepts_a_super_admin(monkeypatch):
    gateway = RealDirectoryGateway(KEY)
    monkeypatch.setattr(
        gateway,
        "_get_user",
        lambda email: {"primaryEmail": "admin@acme.test", "isAdmin": True, "customerId": "C01"},
    )
    info = await gateway.verify_admin("admin@acme.test")
    assert (info.email, info.customer_id) == ("admin@acme.test", "C01")


@pytest.mark.parametrize("user", [{"isAdmin": False, "customerId": "C01"}, {"customerId": "C01"}])
async def test_real_directory_rejects_a_non_admin(monkeypatch, user):
    gateway = RealDirectoryGateway(KEY)
    monkeypatch.setattr(gateway, "_get_user", lambda email: user)
    with pytest.raises(NotAdminError):
        await gateway.verify_admin("someone@acme.test")


async def test_real_directory_rejects_a_delegated_admin(monkeypatch):
    # Delegated admins cannot manage domain-wide delegation, so they cannot be owners.
    gateway = RealDirectoryGateway(KEY)
    user = {"isAdmin": False, "isDelegatedAdmin": True, "customerId": "C01"}
    monkeypatch.setattr(gateway, "_get_user", lambda email: user)
    with pytest.raises(NotAdminError):
        await gateway.verify_admin("helpdesk@acme.test")


async def test_real_gateways_translate_library_errors(monkeypatch):
    def not_delegated(email):
        raise RefreshError("unauthorized_client: nope")

    directory = RealDirectoryGateway(KEY)
    monkeypatch.setattr(directory, "_get_user", not_delegated)
    with pytest.raises(NotAuthorizedError):
        await directory.verify_admin("admin@acme.test")

    gmail = RealGmailGateway(KEY)
    monkeypatch.setattr(gmail, "_mint_token", not_delegated)
    with pytest.raises(NotAuthorizedError):
        await gmail.verify_access("admin@acme.test")


def test_real_credentials_impersonate_the_user_with_one_scope():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    info = {
        **KEY,
        "private_key": private_key.decode(),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    credentials = RealGmailGateway(info)._credentials("user@acme.test", SCOPES[1])
    assert credentials._subject == "user@acme.test"
    assert credentials.scopes == [SCOPES[1]]


# --- Configuration --------------------------------------------------------------------


def test_service_account_key_is_decoded():
    assert load_service_account_info(ENCODED_KEY) == KEY


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64!",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(json.dumps({"type": "authorized_user"}).encode()).decode(),
        base64.b64encode(json.dumps(["list"]).encode()).decode(),
    ],
)
def test_bad_service_account_key_is_rejected(value):
    with pytest.raises(ValueError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        load_service_account_info(value)


def test_real_mode_requires_its_settings():
    with pytest.raises(ValidationError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        Settings(_env_file=None, google_mode="real", google_service_account_json="")
    with pytest.raises(ValidationError, match="GOOGLE_DWD_CLIENT_ID"):
        Settings(
            _env_file=None,
            google_mode="real",
            google_service_account_json=ENCODED_KEY,
            google_dwd_client_id="",
        )


def test_gateways_follow_google_mode():
    fake = build_gateways(Settings(_env_file=None, google_mode="fake"))
    assert isinstance(fake.directory, FakeDirectoryGateway)
    assert isinstance(fake.gmail, FakeGmailGateway)

    real = build_gateways(
        Settings(
            _env_file=None,
            google_mode="real",
            google_service_account_json=ENCODED_KEY,
            google_dwd_client_id="1",
        )
    )
    assert isinstance(real.directory, RealDirectoryGateway)
    assert isinstance(real.gmail, RealGmailGateway)


# --- Fake gateway ---------------------------------------------------------------------


async def test_fake_behaviour_follows_the_address():
    directory, gmail = FakeDirectoryGateway(), FakeGmailGateway()

    info = await directory.verify_admin("admin@acme.test")
    assert info.customer_id == (await directory.verify_admin("other@acme.test")).customer_id
    assert info.customer_id != (await directory.verify_admin("admin@globex.test")).customer_id
    await gmail.verify_access("admin@acme.test")

    with pytest.raises(NotAuthorizedError):
        await directory.verify_admin("nodelegation@acme.test")
    with pytest.raises(NotAdminError):
        await directory.verify_admin("notadmin@acme.test")
    with pytest.raises(GoogleUnavailableError):
        await directory.verify_admin("outage@acme.test")

    await directory.verify_admin("nogmail@acme.test")  # directory scope is fine
    with pytest.raises(NotAuthorizedError):
        await gmail.verify_access("nogmail@acme.test")
