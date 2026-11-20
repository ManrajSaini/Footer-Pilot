"""Scriptable stand-ins for Google and storage, for tests that change the directory."""

import io

from PIL import Image

from app.google.base import AdminInfo, DirectoryPhoto, DirectoryUser, GoogleGateways


def image_bytes(colour: str = "red", size: tuple[int, int] = (300, 200), fmt: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, format=fmt)
    return output.getvalue()


def person(local: str, domain: str = "acme.test", **fields) -> DirectoryUser:
    name = local.replace(".", " ").title()
    defaults = {
        "id": f"g-{local}",
        "email": f"{local}@{domain}",
        "first_name": name.split()[0],
        "last_name": name.split()[-1] if " " in name else "",
        "full_name": name,
    }
    return DirectoryUser(**{**defaults, **fields})


class ScriptedDirectory:
    """A directory whose contents, photos and failures the test controls."""

    def __init__(self, users: list[DirectoryUser] | None = None) -> None:
        self.users = list(users or [])
        self.photos: dict[str, DirectoryPhoto | Exception | None] = {}
        self.error: Exception | None = None
        self.asked_as: list[str] = []
        self.photo_requests: list[str] = []

    def replace(self, user_id: str, **changes) -> None:
        from dataclasses import replace

        self.users = [replace(u, **changes) if u.id == user_id else u for u in self.users]

    def remove(self, user_id: str) -> None:
        self.users = [u for u in self.users if u.id != user_id]

    async def verify_admin(self, admin_email: str) -> AdminInfo:
        return AdminInfo(email=admin_email, customer_id="C-scripted")

    async def list_users(self, admin_email: str) -> list[DirectoryUser]:
        self.asked_as.append(admin_email)
        if self.error is not None:
            raise self.error
        return list(self.users)

    async def get_photo(self, admin_email: str, user_id: str) -> DirectoryPhoto | None:
        self.photo_requests.append(user_id)
        photo = self.photos.get(user_id)
        if isinstance(photo, Exception):
            raise photo
        return photo


class ScriptedGmail:
    """Mailboxes that record every signature write and fail on request.

    `fail[email]` is an exception to raise on every write to that address, or a list
    consumed one item per write (an exception to raise, or None to succeed).
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.fail: dict[str, Exception | list[Exception | None]] = {}

    @property
    def written_to(self) -> list[str]:
        return [email for email, _ in self.writes]

    def signature(self, email: str) -> str | None:
        """What the mailbox holds now: the last thing written to it."""
        return next((html for address, html in reversed(self.writes) if address == email), None)

    async def verify_access(self, email: str) -> None:
        return None

    async def set_signature(self, email: str, html: str) -> None:
        planned = self.fail.get(email)
        if isinstance(planned, list):
            planned = planned.pop(0) if planned else None
        if planned is not None:
            raise planned
        self.writes.append((email, html))


def gateways(directory: ScriptedDirectory, gmail: ScriptedGmail | None = None) -> GoogleGateways:
    return GoogleGateways(directory=directory, gmail=gmail or ScriptedGmail())


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def url(self, key: str) -> str:
        return f"https://cdn.test/{key}"

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = (data, content_type)
        return self.url(key)
