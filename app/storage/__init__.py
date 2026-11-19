"""Public file storage for images that signatures link to.

Gmail can only show images that sit at a public URL, so every logo, banner and
employee photo is stored here and referenced by the URL `put` returns.

    r2     Cloudflare R2, for anything a real mailbox will see
    local  ./var/media served at /media, for development and tests
"""

from typing import Protocol

from fastapi import Request

from app.config import Settings


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store `data` under `key` and return its public URL.

        Keys are chosen by the app (never taken from user input) and should include a
        content hash, so a URL never changes what it points at.
        """
        ...

    def url(self, key: str) -> str: ...


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "r2":
        from app.storage.r2 import R2Storage

        return R2Storage(
            account_id=settings.r2_account_id,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key.get_secret_value(),
            bucket=settings.r2_bucket,
            public_base_url=settings.r2_public_base_url,
        )

    from app.storage.local import LocalStorage

    return LocalStorage(root=settings.media_root, public_base_url=f"{settings.app_base_url}/media")


def get_storage(request: Request) -> Storage:
    """FastAPI dependency."""
    return request.app.state.storage
