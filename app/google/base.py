"""Interfaces for everything FooterPilot asks of Google.

All Google calls go through these two gateways, so the rest of the app never imports
a Google library and tests never touch the network.
"""

from dataclasses import dataclass
from typing import Protocol

# The only two scopes FooterPilot ever uses. Neither can read, send or delete mail.
SCOPE_DIRECTORY = "https://www.googleapis.com/auth/admin.directory.user.readonly"
SCOPE_GMAIL_SETTINGS = "https://www.googleapis.com/auth/gmail.settings.basic"
SCOPES = (SCOPE_DIRECTORY, SCOPE_GMAIL_SETTINGS)

ADMIN_CONSOLE_DELEGATION_URL = "https://admin.google.com/ac/owl/domainwidedelegation"


class GoogleError(Exception):
    """Base class for gateway failures."""


class NotAuthorizedError(GoogleError):
    """Domain-wide delegation is missing, or does not cover the scope requested."""


class NotAdminError(GoogleError):
    """The impersonated user is not a Workspace super admin."""


class GoogleUnavailableError(GoogleError):
    """Anything else: network trouble, a Google outage, an unexpected response."""


# Outcomes of writing one mailbox's signature (besides NotAuthorizedError, which means
# access to the whole domain is gone).


class RetryableError(GoogleError):
    """Temporary: rate limiting, a server error or a network problem. Try again later."""


class SkipError(GoogleError):
    """This mailbox cannot take a signature (no Gmail, or the account is gone)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PermanentError(GoogleError):
    """Google refused the change and retrying will not help."""


@dataclass(frozen=True)
class AdminInfo:
    email: str
    customer_id: str


@dataclass(frozen=True)
class DirectoryUser:
    """One person from the customer's directory, reduced to what signatures use."""

    id: str  # Google's stable user id; survives renames and address changes
    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    job_title: str = ""
    department: str = ""
    phone_work: str = ""
    phone_mobile: str = ""
    suspended: bool = False
    archived: bool = False
    has_mailbox: bool = True
    photo_etag: str | None = None  # changes when the photo changes; None means no photo


@dataclass(frozen=True)
class DirectoryPhoto:
    data: bytes
    mime_type: str = "image/jpeg"


class DirectoryGateway(Protocol):
    """Every method impersonates `admin_email`, the organisation's owner.

    All raise NotAuthorizedError, NotAdminError or GoogleUnavailableError.
    """

    async def verify_admin(self, admin_email: str) -> AdminInfo:
        """Confirm directory access works and `admin_email` is a super admin."""
        ...

    async def list_users(self, admin_email: str) -> list[DirectoryUser]:
        """Everyone in the directory, including suspended users."""
        ...

    async def get_photo(self, admin_email: str, user_id: str) -> DirectoryPhoto | None:
        """A user's profile photo, or None if they have not set one."""
        ...


class GmailGateway(Protocol):
    async def verify_access(self, email: str) -> None:
        """Confirm the Gmail settings scope is delegated for `email`'s domain.

        Raises NotAuthorizedError or GoogleUnavailableError.
        """
        ...

    async def set_signature(self, email: str, html: str) -> None:
        """Replace the signature on `email`'s primary address, acting as that user.

        Raises NotAuthorizedError (delegation is gone), RetryableError, SkipError or
        PermanentError.
        """
        ...


@dataclass(frozen=True)
class GoogleGateways:
    directory: DirectoryGateway
    gmail: GmailGateway
