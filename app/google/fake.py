"""In-memory stand-ins for Google, used when GOOGLE_MODE=fake and in tests.

Behaviour is driven by the email address, so every wizard state can be reached from
/dev/login without any Google account:

    nodelegation...@   domain-wide delegation has not been added
    nogmail...@        delegation is missing the Gmail scope
    notadmin...@       the user is not a Workspace admin
    outage...@         Google is unreachable
    anything else      everything works
"""

import hashlib

from app.google.base import AdminInfo, GoogleUnavailableError, NotAdminError, NotAuthorizedError


def _local_part(email: str) -> str:
    return email.partition("@")[0].lower()


def fake_customer_id(email: str) -> str:
    domain = email.partition("@")[2].lower()
    return "C" + hashlib.sha256(domain.encode()).hexdigest()[:9]


class FakeDirectoryGateway:
    async def verify_admin(self, admin_email: str) -> AdminInfo:
        local = _local_part(admin_email)
        if local.startswith("nodelegation"):
            raise NotAuthorizedError("unauthorized_client (fake)")
        if local.startswith("notadmin"):
            raise NotAdminError("not a super admin (fake)")
        if local.startswith("outage"):
            raise GoogleUnavailableError("Google is unreachable (fake)")
        return AdminInfo(email=admin_email, customer_id=fake_customer_id(admin_email))


class FakeGmailGateway:
    async def verify_access(self, email: str) -> None:
        local = _local_part(email)
        if local.startswith(("nodelegation", "nogmail")):
            raise NotAuthorizedError("unauthorized_client (fake)")
        if local.startswith("outage"):
            raise GoogleUnavailableError("Google is unreachable (fake)")
