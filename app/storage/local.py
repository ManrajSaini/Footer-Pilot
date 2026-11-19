"""Files on local disk, served by the app at /media. Development and tests only."""

import asyncio
from pathlib import Path


class LocalStorage:
    def __init__(self, root: str | Path, public_base_url: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._base_url = public_base_url.rstrip("/")

    def url(self, key: str) -> str:
        return f"{self._base_url}/{key}"

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"storage key escapes the media folder: {key!r}")
        await asyncio.to_thread(self._write, path, data)
        return self.url(key)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
