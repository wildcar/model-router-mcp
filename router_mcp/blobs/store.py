"""Disk-backed blob store.

Binary payloads (generated images, synthesized/uploaded audio) never go into
SQLite — they are written here and referenced by an opaque id. The web layer
serves them back via GET /blobs/{ref}.
"""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path


class BlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        # ref is "<uuid>.<ext>"; reject anything with path separators.
        if "/" in ref or "\\" in ref or ".." in ref:
            raise ValueError(f"Invalid blob ref: {ref!r}")
        return self.root / ref

    def save(self, data: bytes, mime: str) -> str:
        ext = mimetypes.guess_extension(mime) or ".bin"
        ref = f"{uuid.uuid4().hex}{ext}"
        self._path(ref).write_bytes(data)
        return ref

    def load(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()

    def exists(self, ref: str) -> bool:
        return self._path(ref).is_file()

    def mime_of(self, ref: str) -> str:
        return mimetypes.guess_type(ref)[0] or "application/octet-stream"

    def delete(self, ref: str) -> bool:
        """Remove one blob. Missing is success: the goal is that it is gone."""
        try:
            self._path(ref).unlink(missing_ok=True)
        except (OSError, ValueError):
            return False
        return True

    def list_refs(self) -> list[str]:
        """Everything stored, for orphan collection by the retention sweep."""
        try:
            return sorted(p.name for p in self.root.iterdir() if p.is_file())
        except OSError:  # pragma: no cover - the directory is created in __init__
            return []
