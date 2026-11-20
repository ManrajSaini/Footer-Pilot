"""In-memory stand-ins for Google, used when GOOGLE_MODE=fake and in tests.

Behaviour is driven by the email address, so every state can be reached from
/dev/login without any Google account:

    nodelegation...@   domain-wide delegation has not been added
    nogmail...@        delegation is missing the Gmail scope
    notadmin...@       the user is not a Workspace admin
    outage...@         Google is unreachable
    anything else      everything works

The fake directory is the signed-in admin plus the people in fixtures/directory.json,
which includes a suspended user, one with no title or phone, one with a very long
name and one without a mailbox.
"""

import hashlib
import io
import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

from app.google.base import (
    AdminInfo,
    DirectoryPhoto,
    DirectoryUser,
    GoogleUnavailableError,
    NotAdminError,
    NotAuthorizedError,
    PermanentError,
    RetryableError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "directory.json"
logger = logging.getLogger(__name__)


def _local_part(email: str) -> str:
    return email.partition("@")[0].lower()


def _domain(email: str) -> str:
    return email.partition("@")[2].lower()


def fake_customer_id(email: str) -> str:
    return "C" + hashlib.sha256(_domain(email).encode()).hexdigest()[:9]


def _check_directory_access(admin_email: str) -> None:
    local = _local_part(admin_email)
    if local.startswith("nodelegation"):
        raise NotAuthorizedError("unauthorized_client (fake)")
    if local.startswith("notadmin"):
        raise NotAdminError("not a super admin (fake)")
    if local.startswith("outage"):
        raise GoogleUnavailableError("Google is unreachable (fake)")


@cache
def _fixture() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _avatar(name: str) -> bytes:
    """A flat-colour square with initials, standing in for a profile photo."""
    from PIL import Image, ImageDraw, ImageFont

    digest = hashlib.sha256(name.encode()).digest()
    colour = (60 + digest[0] % 120, 60 + digest[1] % 120, 60 + digest[2] % 120)
    image = Image.new("RGB", (256, 256), colour)
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    draw = ImageDraw.Draw(image)
    draw.text((128, 128), initials, fill="white", anchor="mm", font=ImageFont.load_default(110))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeDirectoryGateway:
    async def verify_admin(self, admin_email: str) -> AdminInfo:
        _check_directory_access(admin_email)
        return AdminInfo(email=admin_email, customer_id=fake_customer_id(admin_email))

    async def list_users(self, admin_email: str) -> list[DirectoryUser]:
        _check_directory_access(admin_email)
        domain = _domain(admin_email)
        local = _local_part(admin_email)
        admin = DirectoryUser(
            id=f"fake-{local}",
            email=admin_email.lower(),
            first_name=local.replace(".", " ").title(),
            last_name="",
            full_name=local.replace(".", " ").title(),
            job_title="IT Administrator",
            department="IT",
            phone_work="+1 415 555 0100",
        )
        people = [
            DirectoryUser(
                id=f"fake-{entry['local']}",
                email=f"{entry['local']}@{domain}",
                first_name=entry["first_name"],
                last_name=entry["last_name"],
                full_name=f"{entry['first_name']} {entry['last_name']}",
                job_title=entry.get("job_title", ""),
                department=entry.get("department", ""),
                phone_work=entry.get("phone_work", ""),
                phone_mobile=entry.get("phone_mobile", ""),
                suspended=entry.get("suspended", False),
                has_mailbox=entry.get("has_mailbox", True),
                photo_etag=f"fake-photo-{entry['local']}" if entry.get("photo") else None,
            )
            for entry in _fixture()
            if entry["local"] != local
        ]
        return [admin, *people]

    async def get_photo(self, admin_email: str, user_id: str) -> DirectoryPhoto | None:
        _check_directory_access(admin_email)
        for entry in _fixture():
            if f"fake-{entry['local']}" == user_id and entry.get("photo"):
                name = f"{entry['first_name']} {entry['last_name']}"
                return DirectoryPhoto(data=_avatar(name), mime_type="image/png")
        return None


class FakeGmailGateway:
    """Pretend mailboxes. Signature writes are kept in memory, latest per address.

    Two fixture people misbehave, so the deployment summary has something to show:
    `"gmail": "error"` is refused every time, `"gmail": "retry_once"` fails on the
    first try and then works.
    """

    def __init__(self) -> None:
        self.signatures: dict[str, str] = {}
        self._tried: set[str] = set()

    async def verify_access(self, email: str) -> None:
        local = _local_part(email)
        if local.startswith(("nodelegation", "nogmail")):
            raise NotAuthorizedError("unauthorized_client (fake)")
        if local.startswith("outage"):
            raise GoogleUnavailableError("Google is unreachable (fake)")

    async def set_signature(self, email: str, html: str) -> None:
        local = _local_part(email)
        if local.startswith(("nodelegation", "nogmail")):
            raise NotAuthorizedError("unauthorized_client (fake)")
        behaviour = next(
            (entry.get("gmail") for entry in _fixture() if entry["local"] == local), None
        )
        if behaviour == "error":
            raise PermanentError("Google refused the change (fake)")
        if behaviour == "retry_once" and email not in self._tried:
            self._tried.add(email)
            raise RetryableError("HTTP 429: rate limited (fake)")
        self.signatures[email.lower()] = html
        logger.info("fake gmail: signature written chars=%s", len(html))
