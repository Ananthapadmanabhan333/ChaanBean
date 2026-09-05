"""Getting the audio onto the box that will play it.

Staging happens at schedule time, never on the call path. And "asset present
with the right byte size" is a **policy gate**, not a best-effort optimisation:
a call whose audio is missing must block with a recorded reason rather than dial
and hope. Dialling a debtor and playing silence is worse than not calling.

The byte-size check exists because a truncated file is the failure that survives
a naive `os.path.exists`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from app.models import AudioAsset

log = logging.getLogger(__name__)


class StagingError(RuntimeError):
    pass


class LocalStager:
    """Writes into the directory Asterisk serves sounds from.

    In the compose stack this is a shared volume, so "copy to the Asterisk box"
    is a local write. With Asterisk on its own host it becomes an upload, and
    only this class changes.
    """

    def __init__(self, sounds_dir: str):
        self.sounds_dir = Path(sounds_dir)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, asset: AudioAsset, suffix: str = ".sln") -> Path:
        return self.sounds_dir / f"{asset.message_hash}{suffix}"

    def media_name(self, asset: AudioAsset) -> str:
        """What ARI is told to play: no extension, so Asterisk picks the format."""
        return str(self.sounds_dir / asset.message_hash).replace("\\", "/")

    def stage(self, asset: AudioAsset, pcm: bytes, alaw: bytes | None = None) -> Path:
        target = self.path_for(asset, ".sln")
        self._atomic_write(target, pcm)
        if alaw is not None:
            self._atomic_write(self.path_for(asset, ".alaw"), alaw)
        return target

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def is_staged(self, asset: AudioAsset) -> bool:
        """Present *and* the right size. Existence alone misses a truncated file."""
        target = self.path_for(asset, ".sln")
        if not target.exists():
            return False
        if asset.byte_size is None:
            return False
        return target.stat().st_size == asset.byte_size

    def assert_staged(self, asset: AudioAsset) -> None:
        if not self.is_staged(asset):
            raise StagingError(
                f"audio {asset.message_hash} is not staged with the expected "
                f"{asset.byte_size} bytes"
            )
