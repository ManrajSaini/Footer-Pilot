"""Gateways backed by Google's APIs, using a service account with domain-wide delegation.

The service account impersonates a user of the customer's domain (`subject`). Google's
client libraries are synchronous, so every call runs in a worker thread.
"""

import asyncio
import base64
import binascii
import json
import logging
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
    GoogleError,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
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


class RealDirectoryGateway(_DelegatedClient):
    async def verify_admin(self, admin_email: str) -> AdminInfo:
        user = await self._run(self._get_user, admin_email)
        if not user.get("isAdmin"):
            raise NotAdminError(f"{admin_email} is not a super admin")
        return AdminInfo(
            email=user.get("primaryEmail", admin_email), customer_id=user["customerId"]
        )

    def _get_user(self, email: str) -> dict[str, Any]:
        http = google_auth_httplib2.AuthorizedHttp(
            self._credentials(email, SCOPE_DIRECTORY), http=self._http()
        )
        service = build("admin", "directory_v1", http=http, cache_discovery=False)
        # The default (admin) view fails with 403 for anyone who is not an admin.
        request = service.users().get(
            userKey=email, fields="primaryEmail,isAdmin,isDelegatedAdmin,customerId"
        )
        return request.execute()


class RealGmailGateway(_DelegatedClient):
    async def verify_access(self, email: str) -> None:
        await self._run(self._mint_token, email)

    def _mint_token(self, email: str) -> None:
        # Minting a token is enough to prove the scope is delegated, and works even if
        # this user has no Gmail mailbox.
        credentials = self._credentials(email, SCOPE_GMAIL_SETTINGS)
        credentials.refresh(google_auth_httplib2.Request(self._http()))
