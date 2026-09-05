"""The adapter seam every external vendor sits behind.

Each protocol here has a local implementation that needs no vendor account, and
a production implementation that needs credentials and nothing else. Going live
is a settings change, not a development phase — which is why the whole platform
can be built and tested before a single contract is signed.

This module also owns the non-production contact guard. It lives here, at the
one point every outbound channel passes through, rather than in each channel —
four copies of a safety check is three chances to forget one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.config import settings


class ContactBlocked(RuntimeError):
    """Raised when a non-production environment tries to contact a real person."""


def assert_contactable(destination: str) -> None:
    """Refuse to contact anyone outside the allowlist outside production.

    Twenty lines that stop a development environment from calling or messaging
    the production debtor list. The failure is loud and at the boundary, because
    the alternative is discovering it from the recipient.
    """
    if not settings.enforce_allowlist:
        return
    if destination in settings.contact_allowlist:
        return
    raise ContactBlocked(
        f"env={settings.env!r} may only contact the allowlist; {destination!r} is not on it. "
        f"Add it to CONTACT_ALLOWLIST, or set CONTACT_ALLOWLIST_ENFORCED=false if you "
        f"genuinely mean to reach real numbers."
    )


def is_contactable(destination: str) -> bool:
    try:
        assert_contactable(destination)
        return True
    except ContactBlocked:
        return False


# ------------------------------------------------------------------------- tts


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    sample_rate: int
    duration_ms: int | None = None


@runtime_checkable
class TtsBackend(Protocol):
    """Text to 16-bit signed little-endian mono PCM at `sample_rate`.

    That layout is Asterisk's `sln`, so the bytes can be written straight to a
    `.sln` file with no transcoding hop on the call path.
    """

    name: str

    def synthesize(self, text: str, *, voice_id: str, sample_rate: int) -> SynthesisResult: ...


# --------------------------------------------------------------------- storage


@runtime_checkable
class StorageBackend(Protocol):
    name: str

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def local_path(self, key: str) -> str | None:
        """Filesystem path when one exists, else None (object storage)."""
        ...


# ------------------------------------------------------------------ messaging


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    accepted: bool
    detail: str = ""


@runtime_checkable
class MessageBackend(Protocol):
    """SMS, WhatsApp and email all reduce to this."""

    name: str
    channel: str

    def send(self, *, to: str, body: str, subject: str | None = None) -> SendResult: ...


# ------------------------------------------------------------------ telephony


@dataclass(frozen=True)
class OriginateResult:
    channel_id: str
    accepted: bool
    detail: str = ""


@runtime_checkable
class TelephonyBackend(Protocol):
    name: str

    def originate(
        self, *, to: str, from_: str | None, channel_id: str, variables: dict
    ) -> OriginateResult: ...

    def hangup(self, channel_id: str) -> None: ...
