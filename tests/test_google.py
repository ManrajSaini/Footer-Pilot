"""Google gateways: error translation, configuration and the fake's behaviours.

Nothing here talks to Google. The real gateway's network calls are replaced.
"""

import base64
import io
import json

import httplib2
import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from PIL import Image
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
    parse_directory_user,
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


# --- Directory listing ----------------------------------------------------------------

FULL_RESOURCE = {
    "id": "1001",
    "primaryEmail": "Priya.Raman@Acme.test",
    "name": {"givenName": "Priya", "familyName": "Raman", "fullName": "Priya Raman"},
    "organizations": [
        {"title": "Advisor", "department": "Board"},
        {"title": "Head of Operations", "department": "Operations", "primary": True},
    ],
    "phones": [
        {"value": "+1 415 555 0191", "type": "mobile"},
        {"value": "+1 415 555 0000", "type": "home"},
        {"value": "+1 415 555 0101", "type": "work"},
    ],
    "suspended": False,
    "isMailboxSetup": True,
    "thumbnailPhotoEtag": "etag-1",
}


def test_directory_resource_is_reduced_to_signature_fields():
    user = parse_directory_user(FULL_RESOURCE)
    assert user.id == "1001"
    assert user.email == "priya.raman@acme.test"
    assert (user.first_name, user.last_name, user.full_name) == ("Priya", "Raman", "Priya Raman")
    # The organisation marked primary wins, wherever it sits in the list.
    assert (user.job_title, user.department) == ("Head of Operations", "Operations")
    assert (user.phone_work, user.phone_mobile) == ("+1 415 555 0101", "+1 415 555 0191")
    assert (user.suspended, user.archived, user.has_mailbox) == (False, False, True)
    assert user.photo_etag == "etag-1"


def test_sparse_directory_resource():
    user = parse_directory_user({"id": 7, "primaryEmail": "new@acme.test"})
    assert user.id == "7"
    assert (user.full_name, user.job_title, user.phone_work, user.phone_mobile) == ("", "", "", "")
    assert user.photo_etag is None
    assert user.has_mailbox is True


@pytest.mark.parametrize(
    ("phones", "work", "mobile"),
    [
        ([{"value": "111", "type": "work"}], "111", ""),
        ([{"value": "222", "type": "mobile"}], "", "222"),
        # No phone typed "work": another business-type number is used, never a home one.
        ([{"value": "333", "type": "main"}, {"value": "444", "type": "home"}], "333", ""),
        ([{"value": "444", "type": "home"}], "", ""),
        ([{"value": "", "type": "work"}, {"value": "555", "type": "work"}], "555", ""),
        ([{"value": "666"}], "666", ""),
    ],
)
def test_phone_selection(phones, work, mobile):
    user = parse_directory_user({"id": "1", "primaryEmail": "a@acme.test", "phones": phones})
    assert (user.phone_work, user.phone_mobile) == (work, mobile)


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ({"suspended": True}, (True, False, True)),
        ({"archived": True}, (False, True, True)),
        ({"isMailboxSetup": False}, (False, False, False)),
    ],
)
def test_account_state_flags(resource, expected):
    user = parse_directory_user({"id": "1", "primaryEmail": "a@acme.test", **resource})
    assert (user.suspended, user.archived, user.has_mailbox) == expected


async def test_real_directory_follows_every_page(monkeypatch):
    gateway = RealDirectoryGateway(KEY)
    pages = {
        None: {"users": [{"id": "1", "primaryEmail": "a@acme.test"}], "nextPageToken": "page-2"},
        "page-2": {
            "users": [{"id": "2", "primaryEmail": "b@acme.test"}],
            "nextPageToken": "page-3",
        },
        "page-3": {"users": [{"id": "3", "primaryEmail": "c@acme.test"}]},
    }
    requested = []

    def list_page(admin_email, page_token):
        requested.append((admin_email, page_token))
        return pages[page_token]

    monkeypatch.setattr(gateway, "_list_page", list_page)
    users = await gateway.list_users("admin@acme.test")
    assert [u.email for u in users] == ["a@acme.test", "b@acme.test", "c@acme.test"]
    assert requested == [
        ("admin@acme.test", None),
        ("admin@acme.test", "page-2"),
        ("admin@acme.test", "page-3"),
    ]


async def test_real_directory_handles_an_empty_domain(monkeypatch):
    gateway = RealDirectoryGateway(KEY)
    monkeypatch.setattr(gateway, "_list_page", lambda admin_email, page_token: {})
    assert await gateway.list_users("admin@acme.test") == []


async def test_real_directory_listing_translates_errors(monkeypatch):
    gateway = RealDirectoryGateway(KEY)

    def revoked(admin_email, page_token):
        raise RefreshError("unauthorized_client: revoked")

    monkeypatch.setattr(gateway, "_list_page", revoked)
    with pytest.raises(NotAuthorizedError):
        await gateway.list_users("admin@acme.test")


@pytest.mark.parametrize("padding", ["", "="])
async def test_real_photo_is_decoded_from_urlsafe_base64(monkeypatch, padding):
    gateway = RealDirectoryGateway(KEY)
    raw = b"\xff\xd8\xff\xfb\xfe photo bytes >>>"  # needs the URL-safe characters - and _
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=") + padding
    assert "-" in encoded or "_" in encoded
    response = {"photoData": encoded, "mimeType": "image/png"}
    monkeypatch.setattr(gateway, "_get_photo", lambda admin, user_id: response)
    photo = await gateway.get_photo("admin@acme.test", "1001")
    assert (photo.data, photo.mime_type) == (raw, "image/png")


@pytest.mark.parametrize("response", [None, {}, {"photoData": ""}])
async def test_real_photo_missing(monkeypatch, response):
    gateway = RealDirectoryGateway(KEY)
    monkeypatch.setattr(gateway, "_get_photo", lambda admin, user_id: response)
    assert await gateway.get_photo("admin@acme.test", "1001") is None


# --- Fake directory -------------------------------------------------------------------


async def test_fake_directory_contents():
    directory = FakeDirectoryGateway()
    users = await directory.list_users("Admin@Acme.test")
    by_local = {u.email.partition("@")[0]: u for u in users}

    assert len(users) == 12
    assert len({u.id for u in users}) == 12
    assert all(u.email.endswith("@acme.test") for u in users)
    assert users[0].email == "admin@acme.test"  # the signed-in admin is in the directory too
    assert by_local["olivia.brown"].suspended
    assert by_local["tom.baker"].has_mailbox is False
    noah = by_local["noah.fischer"]
    assert (noah.job_title, noah.phone_work, noah.phone_mobile) == ("", "", "")
    assert len(by_local["maximiliana.wolfe"].full_name) > 50
    assert sum(1 for u in users if u.photo_etag) == 5


async def test_fake_directory_lists_an_admin_only_once():
    users = await FakeDirectoryGateway().list_users("priya.raman@acme.test")
    assert len(users) == 11
    assert [u.email for u in users].count("priya.raman@acme.test") == 1


async def test_fake_photos_are_real_images():
    directory = FakeDirectoryGateway()
    photo = await directory.get_photo("admin@acme.test", "fake-priya.raman")
    assert Image.open(io.BytesIO(photo.data)).size == (256, 256)
    assert await directory.get_photo("admin@acme.test", "fake-chen.wei") is None
    assert await directory.get_photo("admin@acme.test", "fake-nobody") is None


async def test_fake_directory_listing_follows_the_address():
    directory = FakeDirectoryGateway()
    with pytest.raises(NotAuthorizedError):
        await directory.list_users("nodelegation@acme.test")
    with pytest.raises(NotAdminError):
        await directory.list_users("notadmin@acme.test")
    with pytest.raises(GoogleUnavailableError):
        await directory.get_photo("outage@acme.test", "fake-priya.raman")


def test_directory_field_mask_asks_for_the_photo_url_with_the_etag():
    # Google leaves thumbnailPhotoEtag out unless thumbnailPhotoUrl is requested too,
    # which silently stops every photo from being synced.
    from app.google.real import _USER_FIELDS

    fields = _USER_FIELDS.split(",")
    assert "thumbnailPhotoEtag" in fields
    assert "thumbnailPhotoUrl" in fields


def test_directory_listing_sends_the_field_mask(monkeypatch):
    """The mask reaches the API call, wrapped for a list response."""
    from app.google import real

    sent = {}

    class Users:
        def list(self, **kwargs):
            sent.update(kwargs)
            return self

        def execute(self):
            return {}

    class Service:
        def users(self):
            return Users()

    gateway = RealDirectoryGateway(KEY)
    monkeypatch.setattr(gateway, "_service", lambda admin_email: Service())
    assert gateway._list_page("admin@acme.test", "token-2") == {}
    assert sent["customer"] == "my_customer"
    assert sent["pageToken"] == "token-2"
    assert sent["maxResults"] == real.PAGE_SIZE
    assert sent["fields"] == f"nextPageToken,users({real._USER_FIELDS})"
