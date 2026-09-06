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
from datetime import date
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


# ----------------------------------------------------- company registries

# Where a fact came from. Two answers, and no third one.
#
#   REGISTRY       a backend fetched it from the source of record.
#   USER_PROVIDED  a human opened the portal, read the screen, and typed it in.
#
# This field is not bookkeeping. Self-declared data wearing a verification label
# is how a dunning call, a legal notice or a public registry listing ends up
# aimed at a company that has nothing to do with the debt. So: only REGISTRY data
# may support `app.company.resolution.Tier.IDENTIFIER`, and only that tier is
# publishable. Everything downstream leans on this one string being honest.
REGISTRY = "REGISTRY"
USER_PROVIDED = "USER_PROVIDED"
PROVENANCES = (REGISTRY, USER_PROVIDED)


class UnknownProvenance(ValueError):
    """A lookup declared a provenance outside the closed set."""


def is_registry_provenance(provenance: str | None) -> bool:
    """True for the exact REGISTRY literal and nothing else.

    Written as a positive test on purpose. The tempting form is
    `provenance != USER_PROVIDED`, which reads a typo, a None or an invented
    third value as evidence — the answer to "is this verified" has to fail
    towards no.
    """
    return provenance == REGISTRY


def _assert_provenance(value: str, what: str) -> None:
    if value not in PROVENANCES:
        raise UnknownProvenance(
            f"{what}.provenance must be one of {PROVENANCES}; got {value!r}. "
            f"A source that cannot say where its data came from does not get to "
            f"claim it came from the registry."
        )


@dataclass(frozen=True)
class GstLookup:
    """One GSTIN as some source describes it."""

    gstin: str
    legal_name: str | None
    trade_name: str | None
    status: str | None  # "Active" | "Cancelled" | "Suspended"
    registration_date: date | None
    address: str | None
    state_code: str | None
    filing_history: list[dict]
    provenance: str  # REGISTRY | USER_PROVIDED

    def __post_init__(self) -> None:
        _assert_provenance(self.provenance, "GstLookup")


@runtime_checkable
class GstBackend(Protocol):
    name: str

    def lookup(self, gstin: str) -> GstLookup | None: ...


@dataclass(frozen=True)
class McaLookup:
    """One CIN as some source describes it."""

    cin: str
    legal_name: str | None
    status: str | None  # "Active" | "Struck Off" | "Under Liquidation"
    incorporation_date: date | None
    registered_address: str | None
    directors: list[dict]
    charges: list[dict]
    provenance: str  # REGISTRY | USER_PROVIDED

    def __post_init__(self) -> None:
        _assert_provenance(self.provenance, "McaLookup")


@runtime_checkable
class McaBackend(Protocol):
    name: str

    def lookup(self, cin: str) -> McaLookup | None: ...
