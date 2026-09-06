"""DND scrubbing, and a registry stand-in that needs no vendor account.

TRAI's do-not-call registry is not an open API. Access runs through an access
provider or a licensed scrubbing vendor and starts with a signed agreement, so
this module holds the seam and a fixture that occupies it — the same arrangement
as `app.tts.local` and `app.company.gst`. The scrub job, the scheduler and the
policy gate are all built and tested before that agreement exists, and going
live is a settings change.

`LocalDnd` deliberately does not clear every number. A fixture that answers CLEAR
to everything lets the entire platform be demonstrated without the REGISTERED
branch of the policy gate ever executing, and that branch is the one keeping the
product lawful. It therefore reports REGISTERED for a share of numbers, chosen by
a stable hash so a number's answer does not change between runs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone

from app.config import settings
from app.models import DndStatus
from app.providers.base import DndCheck

# Roughly the share of numbers `LocalDnd` reports as registered. Large enough
# that any dataset worth demonstrating contains both answers, and the blocked
# path is exercised by accident rather than only on purpose.
REGISTERED_SHARE = 0.25


class DndUnavailable(RuntimeError):
    """The registry could not be asked, so the previous answer stands.

    Raised by a vendor client rather than by the fixture below, which cannot
    fail. It exists here so every backend reports an outage the same way and
    `app.scheduler.scrub` has one thing to recognise.
    """


def _share_bucket(e164: str) -> float:
    """A stable position in [0, 1) for a number.

    hashlib rather than the builtin `hash`: string hashing is salted per process,
    so `hash` would give the same number a different DND status after every
    restart — and a fixture that changes its mind is worse than no fixture,
    because a test that passed this morning fails this afternoon for no reason
    anybody can see.
    """
    digest = hashlib.blake2b(e164.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


class LocalDnd:
    """The registry, standing in for itself.

    What it knows is arithmetic; what the seam around it promises is a real
    registry lookup. `registered` and `clear` pin individual numbers for a demo
    or a test that needs a specific answer.
    """

    name = "local"

    def __init__(
        self,
        *,
        registered: Iterable[str] = (),
        clear: Iterable[str] = (),
        registered_share: float = REGISTERED_SHARE,
    ) -> None:
        self.registered = frozenset(registered)
        self.clear = frozenset(clear)
        self.registered_share = registered_share

    def check(self, e164: str) -> DndCheck:
        return DndCheck(
            e164=e164,
            status=self._status(e164),
            # This backend answers from memory, so the answer is as of now. A
            # vendor client fills this from the response it was given, not from
            # its own clock.
            checked_at=datetime.now(timezone.utc),
        )

    def _status(self, e164: str) -> DndStatus:
        # A number pinned to both lists is registered: between two configured
        # answers, take the one that does not place a call.
        if e164 in self.registered:
            return DndStatus.REGISTERED
        if e164 in self.clear:
            return DndStatus.CLEAR
        return (
            DndStatus.REGISTERED
            if _share_bucket(e164) < self.registered_share
            else DndStatus.CLEAR
        )


def build_dnd_backend():
    """Adapter selection, which refuses rather than substituting the fixture.

    Falling back to `LocalDnd` for an unrecognised setting is the one behaviour
    this factory must not have. A deployment naming a vendor nobody wired up
    would be handed invented CLEAR answers for real numbers and would dial every
    one of them — so an unknown backend is a startup failure, not a default.

    The same reasoning refuses the fixture itself once contact is possible.
    `local` answers from a hash — about three numbers in four come back CLEAR —
    and `scrub.scrub_due` writes that answer where the Policy Engine reads it as
    a valid scrub. A deployment that goes live by configuring a telephony vendor
    and changing nothing else would satisfy the compliance gate with a hash
    function, which is the same harm as the unknown backend arriving by a
    default rather than by a typo.
    """
    if settings.dnd_backend == "local":
        if settings.fixtures_are_dangerous:
            raise RuntimeError(
                "DND_BACKEND=local answers from a fixture, and this deployment "
                "can reach real numbers. Scrubbing against it would mark real "
                "phones CLEAR and dial them. Configure a licensed scrubbing "
                "vendor before going live."
            )
        return LocalDnd()
    raise RuntimeError(
        f"DND_BACKEND={settings.dnd_backend!r} has no client in this build. "
        f"Implement one behind app.providers.base.DndBackend, or set "
        f"DND_BACKEND=local to scrub against the fixture registry."
    )
