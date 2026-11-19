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


@dataclass(frozen=True)
class AdminInfo:
    email: str
    customer_id: str


class DirectoryGateway(Protocol):
    async def verify_admin(self, admin_email: str) -> AdminInfo:
        """Confirm directory access works and `admin_email` is a super admin.

        Raises NotAuthorizedError, NotAdminError or GoogleUnavailableError.
        """
        ...


class GmailGateway(Protocol):
    async def verify_access(self, email: str) -> None:
        """Confirm the Gmail settings scope is delegated for `email`'s domain.

        Raises NotAuthorizedError or GoogleUnavailableError.
        """
        ...


@dataclass(frozen=True)
class GoogleGateways:
    directory: DirectoryGateway
    gmail: GmailGateway
