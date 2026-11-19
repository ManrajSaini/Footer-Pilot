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

    def _path(self, key: str) -> Path:
        """The file for a key, which must be plain `folder/folder/name` segments.

        Checked on the text of the key, not by resolving the path on disk: resolving
        while other uploads are creating the same folders is not stable on Windows
        (the result can gain a \\\\?\\ prefix), which made valid keys look like escapes.
        """
        parts = key.split("/")
        if "\\" in key or ":" in key or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"invalid storage key: {key!r}")
        return self.root.joinpath(*parts)

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        await asyncio.to_thread(self._write, self._path(key), data)
        return self.url(key)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
