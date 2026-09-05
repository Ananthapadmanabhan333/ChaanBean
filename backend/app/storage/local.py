"""Filesystem storage.

Writes are atomic: a temporary file on the same filesystem, then `os.replace`.
Without that, Asterisk eventually opens a half-written file and plays noise on a
legal call — a failure that is intermittent, unreproducible, and lands on a real
debtor.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class LocalStorage:
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        # A key is never user input today, but a traversal here would write
        # outside the audio directory, so it is checked rather than assumed.
        if not str(path).startswith(str(root)):
            raise ValueError(f"key escapes the storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory, so the replace is a rename within one filesystem and
        # therefore atomic.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            # Leave no partial file behind for Asterisk to find.
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return str(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def local_path(self, key: str) -> str | None:
        return str(self._path(key))

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    @staticmethod
    def checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
