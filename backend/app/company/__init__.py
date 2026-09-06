"""Company-registry adapter selection.

The same seam as `app.tts` and `app.storage`: which backend runs is a settings
value, not a code path.

With one difference that must not be glossed over. A TTS engine swap changes how
something sounds. This swap changes what the system is entitled to claim. The
`manual` backends return USER_PROVIDED data, which can never satisfy
`Tier.IDENTIFIER` and therefore can never be published — so a deployment running
GST_BACKEND=manual gets "a human needs to look at this" everywhere the registry
backend would have said "verified". That is the correct trade for a mode where
nobody actually called a registry, and it should be a visible one.

**What neither shipped backend can do yet, stated so nobody reads a passing test
as coverage of it.** Both answer by exact key: `local` returns the row filed
under the GSTIN it was handed, `manual` returns nothing at all until somebody
loads it. So a lookup that finds anything has, by construction, found a record
whose identifier equals the declared one — `resolution.resolve` short-circuits to
`Tier.IDENTIFIER`, and `verification`'s `elif consulted:` branch never runs. No
`EntityCandidate` can be written through HTTP today, which leaves the review
route, `Permission.ENTITY_CONFIRM` and the portal's confirm/reject block
correct but inert; the tests that cover them write the candidate row directly.

The provenance guards are in the same position: with `local` every lookup is
REGISTRY, and `manual` answers nothing, so `cap_tier_for_provenance` and
`_assert_registry_backed` are exercised only by tests. The invariant holds partly
because the path it guards is currently unreachable. All of it goes live the
moment a real registry client — one that *searches*, and so can return a record
that is not the identifier it was asked about — is wired into either builder
below, and that is the change to review the candidate seam under.
"""

from __future__ import annotations

from app.company.gst import GST_FIXTURES, LocalGstBackend, ManualGstBackend
from app.company.mca import MCA_FIXTURES, LocalMcaBackend, ManualMcaBackend
from app.config import settings


def build_gst_backend():
    if settings.gst_backend == "manual":
        # Empty, and therefore silent, until an operator supplies what they read.
        # A manual backend that invented answers would be worse than none.
        return ManualGstBackend()
    return LocalGstBackend()


def build_mca_backend():
    if settings.mca_backend == "manual":
        return ManualMcaBackend()
    return LocalMcaBackend()


__all__ = [
    "GST_FIXTURES",
    "MCA_FIXTURES",
    "LocalGstBackend",
    "LocalMcaBackend",
    "ManualGstBackend",
    "ManualMcaBackend",
    "build_gst_backend",
    "build_mca_backend",
]
