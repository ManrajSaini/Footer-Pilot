"""Gateways backed by Google's APIs, using a service account with domain-wide delegation.

The service account impersonates a user of the customer's domain (`subject`). Google's
client libraries are synchronous, so every call runs in a worker thread.
"""

import asyncio
import base64
import binascii
import json
import logging
import threading
from typing import Any

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.google.base import (
    SCOPE_DIRECTORY,
    SCOPE_GMAIL_SETTINGS,
    AdminInfo,
    DirectoryPhoto,
    DirectoryUser,
    GoogleError,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
    PermanentError,
    RetryableError,
    SkipError,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# OAuth error codes Google returns when the Admin console has no matching delegation.
_NOT_DELEGATED = ("unauthorized_client", "access_denied")


def load_service_account_info(encoded: str) -> dict[str, Any]:
    """Decode GOOGLE_SERVICE_ACCOUNT_JSON (base64 of the downloaded key file)."""
    try:
        info = json.loads(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError) as error:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not base64-encoded JSON") from error
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key")
    return info


def translate_error(error: Exception) -> GoogleError:
    """Map a Google library exception onto the gateway's own error types."""
    if isinstance(error, GoogleError):
        return error
    if isinstance(error, RefreshError):
        # Raised while minting the delegated token, before any API call is made.
        if any(code in str(error) for code in _NOT_DELEGATED):
            return NotAuthorizedError(str(error))
        return GoogleUnavailableError(str(error))
    if isinstance(error, HttpError):
        if error.resp.status == 403:
            return NotAdminError(f"HTTP 403: {error.reason}")
        return GoogleUnavailableError(f"HTTP {error.resp.status}: {error.reason}")
    return GoogleUnavailableError(f"{type(error).__name__}: {error}")


class _DelegatedClient:
    def __init__(self, service_account_info: dict[str, Any]) -> None:
        self._info = service_account_info

    def _credentials(self, subject: str, scope: str) -> service_account.Credentials:
        return service_account.Credentials.from_service_account_info(
            self._info, scopes=[scope], subject=subject
        )

    @staticmethod
    def _http() -> httplib2.Http:
        return httplib2.Http(timeout=TIMEOUT_SECONDS)

    async def _run(self, call, *args):
        try:
            return await asyncio.to_thread(call, *args)
        except (GoogleAuthError, HttpError, OSError, httplib2.HttpLib2Error) as error:
            raise translate_error(error) from error


# thumbnailPhotoUrl is requested although it is never used: Google omits
# thumbnailPhotoEtag from the response unless the URL is asked for alongside it
# (observed against the live API), and without the etag no photo is ever fetched.
_USER_FIELDS = (
    "id,primaryEmail,name(givenName,familyName,fullName),organizations(title,department,primary),"
    "phones(value,type,primary),suspended,archived,isMailboxSetup,"
    "thumbnailPhotoEtag,thumbnailPhotoUrl"
)
PAGE_SIZE = 500  # the Directory API's maximum


def _first(items: list[dict[str, Any]], *, where=lambda item: True) -> dict[str, Any]:
    return next((item for item in items if where(item)), {})


def parse_directory_user(resource: dict[str, Any]) -> DirectoryUser:
    """Reduce a Directory API user resource to the fields signatures use."""
    name = resource.get("name") or {}
    organizations = resource.get("organizations") or []
    organization = _first(organizations, where=lambda o: o.get("primary")) or _first(organizations)

    phones = [phone for phone in resource.get("phones") or [] if phone.get("value")]
    work = _first(phones, where=lambda p: p.get("type") == "work") or _first(
        phones, where=lambda p: p.get("type") not in ("mobile", "home")
    )
    mobile = _first(phones, where=lambda p: p.get("type") == "mobile")

    return DirectoryUser(
        id=str(resource["id"]),
        email=str(resource["primaryEmail"]).lower(),
        first_name=name.get("givenName") or "",
        last_name=name.get("familyName") or "",
        full_name=name.get("fullName") or "",
        job_title=organization.get("title") or "",
        department=organization.get("department") or "",
        phone_work=work.get("value") or "",
        phone_mobile=mobile.get("value") or "",
        suspended=bool(resource.get("suspended")),
        archived=bool(resource.get("archived")),
        has_mailbox=bool(resource.get("isMailboxSetup", True)),
        photo_etag=resource.get("thumbnailPhotoEtag") or None,
    )


class RealDirectoryGateway(_DelegatedClient):
    def __init__(self, service_account_info: dict[str, Any]) -> None:
        super().__init__(service_account_info)
        # A sync makes many calls as the same admin; reuse the token between them.
        self._cached: dict[str, service_account.Credentials] = {}
        self._lock = threading.Lock()

    def _service(self, admin_email: str):
        with self._lock:
            credentials = self._cached.get(admin_email)
            if credentials is None:
                credentials = self._credentials(admin_email, SCOPE_DIRECTORY)
                self._cached[admin_email] = credentials
            if not credentials.valid:
                credentials.refresh(google_auth_httplib2.Request(self._http()))
        # httplib2 connections are not thread-safe, so each call gets its own.
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=self._http())
        return build("admin", "directory_v1", http=http, cache_discovery=False)

    async def verify_admin(self, admin_email: str) -> AdminInfo:
        user = await self._run(self._get_user, admin_email)
        if not user.get("isAdmin"):
            raise NotAdminError(f"{admin_email} is not a super admin")
        return AdminInfo(
            email=user.get("primaryEmail", admin_email), customer_id=user["customerId"]
        )

    def _get_user(self, email: str) -> dict[str, Any]:
        # The default (admin) view fails with 403 for anyone who is not an admin.
        request = (
            self._service(email)
            .users()
            .get(userKey=email, fields="primaryEmail,isAdmin,isDelegatedAdmin,customerId")
        )
        return request.execute()

    async def list_users(self, admin_email: str) -> list[DirectoryUser]:
        users: list[DirectoryUser] = []
        page_token: str | None = None
        while True:
            page = await self._run(self._list_page, admin_email, page_token)
            users.extend(parse_directory_user(resource) for resource in page.get("users") or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                return users

    def _list_page(self, admin_email: str, page_token: str | None) -> dict[str, Any]:
        request = (
            self._service(admin_email)
            .users()
            .list(
                customer="my_customer",
                maxResults=PAGE_SIZE,
                projection="full",
                orderBy="email",
                pageToken=page_token,
                fields=f"nextPageToken,users({_USER_FIELDS})",
            )
        )
        return request.execute()

    async def get_photo(self, admin_email: str, user_id: str) -> DirectoryPhoto | None:
        photo = await self._run(self._get_photo, admin_email, user_id)
        if photo is None or not photo.get("photoData"):
            return None
        # Google sends the image as URL-safe base64, sometimes without padding.
        encoded = photo["photoData"]
        data = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return DirectoryPhoto(data=data, mime_type=photo.get("mimeType") or "image/jpeg")

    def _get_photo(self, admin_email: str, user_id: str) -> dict[str, Any] | None:
        try:
            return self._service(admin_email).users().photos().get(userKey=user_id).execute()
        except HttpError as error:
            if error.resp.status == 404:  # no photo set
                return None
            raise


# Google reports quota problems as 403 or 429 with one of these reasons.
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
)


def _error_reasons(error: HttpError) -> set[str]:
    """The machine-readable reasons in a Google API error body."""
    try:
        body = json.loads(error.content)["error"]
    except (ValueError, KeyError, TypeError):
        return set()
    if not isinstance(body, dict):
        return set()
    reasons = {
        str(item.get("reason")) for item in body.get("errors") or [] if isinstance(item, dict)
    }
    if body.get("status"):
        reasons.add(str(body["status"]))
    return reasons


def translate_write_error(error: Exception) -> GoogleError:
    """Map a failure while writing a signature onto what the deployment should do."""
    if isinstance(error, GoogleError):
        return error
    if isinstance(error, RefreshError):
        # Minting the token as this user failed, before Gmail was contacted.
        text = str(error)
        if any(code in text for code in _NOT_DELEGATED):
            return NotAuthorizedError(text)
        if "invalid_grant" in text:
            return SkipError("The Google account no longer exists or is disabled")
        return RetryableError(text)
    if isinstance(error, HttpError):
        status = error.resp.status
        reasons = _error_reasons(error)
        if status == 429 or status >= 500 or reasons & _RATE_LIMIT_REASONS:
            return RetryableError(f"HTTP {status}: {error.reason}")
        if status == 404:
            return SkipError("No Gmail mailbox was found for this address")
        if status == 400 and reasons & {"failedPrecondition", "FAILED_PRECONDITION"}:
            return SkipError("Gmail is not enabled for this user")
        return PermanentError(f"Google refused the change (HTTP {status}: {error.reason})")
    # Timeouts, connection resets and other transport trouble.
    return RetryableError(f"{type(error).__name__}: {error}")


class RealGmailGateway(_DelegatedClient):
    async def verify_access(self, email: str) -> None:
        await self._run(self._mint_token, email)

    async def set_signature(self, email: str, html: str) -> None:
        try:
            await asyncio.to_thread(self._patch_signature, email, html)
        except (GoogleAuthError, HttpError, OSError, httplib2.HttpLib2Error) as error:
            raise translate_write_error(error) from error

    def _patch_signature(self, email: str, html: str) -> None:
        http = google_auth_httplib2.AuthorizedHttp(
            self._credentials(email, SCOPE_GMAIL_SETTINGS), http=self._http()
        )
        service = build("gmail", "v1", http=http, cache_discovery=False)
        # The primary send-as address is the user's own address. `patch` changes the
        # signature and leaves every other send-as setting alone.
        request = (
            service.users()
            .settings()
            .sendAs()
            .patch(userId="me", sendAsEmail=email, body={"signature": html})
        )
        request.execute()

    def _mint_token(self, email: str) -> None:
        # Minting a token is enough to prove the scope is delegated, and works even if
        # this user has no Gmail mailbox.
        credentials = self._credentials(email, SCOPE_GMAIL_SETTINGS)
        credentials.refresh(google_auth_httplib2.Request(self._http()))
