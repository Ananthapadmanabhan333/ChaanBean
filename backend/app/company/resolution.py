"""Deciding whether two names are the same legal entity.

Signals are **tiers, not a blended score**. That distinction is the whole design.
A blend lets three weak signals outvote one strong contradiction — which is
precisely how you attach one company's tax record to another, and that is
defamation rather than a data-quality issue.

So: an identifier match is decisive on its own. A structural contradiction is
evidence *against* and can veto. Name similarity alone never reaches the tier
that publication requires.

`4,50,000` grouping and `Pvt Ltd` suffixes are not the hard part. The hard part
is that Indian trade names repeat endlessly — "Sharma Traders" is hundreds of
unrelated businesses — and word order varies freely, which is why this uses
token-set similarity rather than edit distance.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from enum import Enum

# **Legal forms only.** A legal form carries no identifying information: two
# firms called "Sharma Traders" are not related because both are "Pvt Ltd".
#
# Words like "Trading", "Enterprises", "Industries" and "Steel" are deliberately
# NOT here, even though they are common. They are what distinguishes "Sharma
# Trading Company" from "Sharma Steel Company", and stripping them collapses two
# unrelated businesses into the same key — a false match, in the module whose
# false matches are defamation.
_SUFFIXES = (
    "private limited", "pvt ltd", "pvt limited", "public limited",
    "limited liability partnership", "limited", "ltd", "llp",
    "corporation", "corp", "incorporated", "inc",
    "and company", "& company", "and co", "& co",
)
_HONORIFICS = ("m/s", "m s", "messrs", "shri", "sri", "smt", "mr", "mrs", "ms")

# GSTIN: 2-digit state code, 10-char PAN, entity number, Z, checksum.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")


class Tier(str, Enum):
    """What kind of evidence produced the match. Ordered, and meaningful."""

    IDENTIFIER = "IDENTIFIER"  # decisive on its own
    STRUCTURAL = "STRUCTURAL"  # identifier-derived corroboration
    NAME_STRONG = "NAME_STRONG"  # high name similarity plus corroboration
    NAME_WEAK = "NAME_WEAK"  # name similarity alone — never publishable
    NONE = "NONE"

    @property
    def publishable(self) -> bool:
        """Only the top tier may reach the registry.

        A name-only match must never be published. This property is where that
        rule lives, so there is one place to audit it.
        """
        return self is Tier.IDENTIFIER


@dataclass(frozen=True)
class Signal:
    name: str
    value: str
    supports: bool
    weight: float
    note: str = ""


@dataclass(frozen=True)
class Resolution:
    tier: Tier
    confidence: float
    signals: tuple[Signal, ...] = ()
    vetoed: bool = False
    veto_reason: str = ""

    @property
    def publishable(self) -> bool:
        return self.tier.publishable and not self.vetoed


@dataclass(frozen=True)
class EntityInput:
    name: str
    gstin: str | None = None
    cin: str | None = None
    pan: str | None = None
    state_code: str | None = None
    address: str | None = None


@dataclass(frozen=True)
class SourceRecord:
    """A candidate from GST, MCA or elsewhere."""

    name: str
    gstin: str | None = None
    cin: str | None = None
    pan: str | None = None
    state_code: str | None = None
    address: str | None = None
    source: str = "fixture"
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------- names


def normalise_company_name(raw: str) -> str:
    """Lowercase, strip honorifics and legal suffixes, collapse punctuation."""
    text = (raw or "").lower().strip()
    text = re.sub(r"[.,;:'\"()\[\]/\\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    for honorific in _HONORIFICS:
        if text.startswith(honorific + " "):
            text = text[len(honorific) + 1 :]
            break

    changed = True
    while changed:
        changed = False
        for suffix in sorted(_SUFFIXES, key=len, reverse=True):
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return re.sub(r"\s+", " ", text).strip()


def token_set_similarity(a: str, b: str) -> float:
    """Jaccard over token sets.

    Token-set rather than edit distance: "Sharma Steel Traders" and "Traders
    Sharma Steel" are the same business written by two clerks, and Levenshtein
    scores that pair very low.
    """
    left = set(normalise_company_name(a).split())
    right = set(normalise_company_name(b).split())
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# --------------------------------------------------------------- identifiers


def pan_from_gstin(gstin: str | None) -> str | None:
    """A GSTIN embeds the PAN at positions 3-12. Free structural evidence."""
    if not gstin or not GSTIN_RE.match(gstin.upper()):
        return None
    return gstin.upper()[2:12]


def state_from_gstin(gstin: str | None) -> str | None:
    if not gstin or len(gstin) < 2 or not gstin[:2].isdigit():
        return None
    return gstin[:2]


def hash_pan(pan: str) -> tuple[str, str]:
    """(keyed digest, last four). The full number is never stored in the clear.

    Keyed, not a bare digest, because the row keeps `pan_last4` beside the hash.
    Those four characters fix the trailing digits and the check letter, leaving
    26^5 x 10 ≈ 10^8 candidates for the rest — which an unsalted SHA-256 gives up
    in seconds, and with it the national identifier the column exists to protect.
    Under an HMAC a dump without the pepper yields nothing.

    Rotating `pan_pepper` invalidates every digest already written. Nothing
    compares one against a number typed later today, so that costs a
    re-verification rather than a lookup.
    """
    # Imported here rather than at module scope: everything else in this file is
    # pure comparison logic, and importing it should not require a configured
    # environment.
    from app.config import settings

    clean = pan.upper().strip()
    digest = hmac.new(
        settings.pan_pepper.encode(), f"pan:{clean}".encode(), hashlib.sha256
    ).hexdigest()
    return digest, clean[-4:]


# ---------------------------------------------------------------- resolution


def resolve(candidate: EntityInput, record: SourceRecord) -> Resolution:
    """Compare one input against one source record."""
    signals: list[Signal] = []

    # Tier 1 — an identifier match is decisive. Nothing outweighs it and
    # nothing else is needed.
    for kind, left, right in (
        ("gstin", candidate.gstin, record.gstin),
        ("cin", candidate.cin, record.cin),
        ("pan", candidate.pan, record.pan),
    ):
        if left and right and left.upper() == right.upper():
            signals.append(Signal(kind, left.upper(), True, 1.0, "exact identifier match"))
            # Computed even though the tier is already settled, and reported
            # rather than weighed. A mistyped but checksum-valid GSTIN matches a
            # stranger's record exactly, and the only trace of that on the screen
            # is a name the two do not share — so it has to be on the screen.
            overlap = token_set_similarity(candidate.name, record.name)
            signals.append(
                Signal("name_similarity", f"{overlap:.2f}", overlap >= 0.6, overlap)
            )
            return Resolution(Tier.IDENTIFIER, 1.0, tuple(signals))

    # Tier 2 — structural. A GSTIN embeds a PAN, so two GSTINs sharing a PAN are
    # the same legal person in different states.
    candidate_pan = candidate.pan or pan_from_gstin(candidate.gstin)
    record_pan = record.pan or pan_from_gstin(record.gstin)
    if candidate_pan and record_pan and candidate_pan == record_pan:
        signals.append(
            Signal("pan_from_gstin", candidate_pan, True, 0.9, "same PAN inside the GSTIN")
        )
        return Resolution(Tier.STRUCTURAL, 0.9, tuple(signals))

    # A state contradiction is evidence AGAINST, and it vetoes. This is the
    # asymmetry a blended score would lose.
    candidate_state = candidate.state_code or state_from_gstin(candidate.gstin)
    record_state = record.state_code or state_from_gstin(record.gstin)
    if candidate_state and record_state and candidate_state != record_state:
        signals.append(
            Signal(
                "state_code",
                f"{candidate_state} vs {record_state}",
                False,
                -1.0,
                "GSTIN state contradicts the stated state",
            )
        )
        if candidate_pan and record_pan and candidate_pan != record_pan:
            return Resolution(
                Tier.NONE,
                0.0,
                tuple(signals),
                vetoed=True,
                veto_reason="different PAN in a different state",
            )

    # Tier 3/4 — name similarity. On its own this never reaches publishable.
    similarity = token_set_similarity(candidate.name, record.name)
    signals.append(
        Signal("name_similarity", f"{similarity:.2f}", similarity >= 0.6, similarity)
    )

    address_match = False
    if candidate.address and record.address:
        address_match = token_set_similarity(candidate.address, record.address) >= 0.5
        signals.append(
            Signal(
                "address",
                "similar" if address_match else "different",
                address_match,
                0.1 if address_match else 0.0,
                # Shared commercial addresses are extremely common in India.
                "weak corroborator only; shared premises are normal",
            )
        )

    if similarity >= 0.85 and (address_match or candidate_state == record_state):
        return Resolution(Tier.NAME_STRONG, min(0.75, similarity), tuple(signals))
    if similarity >= 0.6:
        return Resolution(Tier.NAME_WEAK, min(0.5, similarity), tuple(signals))
    return Resolution(Tier.NONE, similarity, tuple(signals))


def best_match(candidate: EntityInput, records: list[SourceRecord]):
    """Highest tier wins, then confidence. Vetoed records are discarded."""
    order = {
        Tier.IDENTIFIER: 4,
        Tier.STRUCTURAL: 3,
        Tier.NAME_STRONG: 2,
        Tier.NAME_WEAK: 1,
        Tier.NONE: 0,
    }
    scored = [
        (record, resolution)
        for record, resolution in ((r, resolve(candidate, r)) for r in records)
        if not resolution.vetoed
    ]
    if not scored:
        return None, Resolution(Tier.NONE, 0.0)

    scored.sort(key=lambda pair: (order[pair[1].tier], pair[1].confidence), reverse=True)
    best_record, best_resolution = scored[0]

    # Two records at the same tier with near-identical confidence is an
    # ambiguity, not a match. Returning either would be a coin toss with a
    # defamation claim on one side.
    if len(scored) > 1:
        runner_up = scored[1][1]
        if (
            order[runner_up.tier] == order[best_resolution.tier]
            and abs(runner_up.confidence - best_resolution.confidence) < 0.05
            and best_resolution.tier is not Tier.IDENTIFIER
        ):
            return None, Resolution(
                Tier.NONE,
                0.0,
                best_resolution.signals,
                vetoed=True,
                veto_reason="two candidates match equally well; needs a human",
            )

    return best_record, best_resolution
