"""Is this string an identifier at all?

A GSTIN carries its own check digit, and that is not a formality. Mistype one
character of a real GSTIN and there is roughly a one-in-thirty-six chance the
result is still a *structurally valid* GSTIN — one that belongs to a different
real business. Every step downstream then does its job perfectly against the
wrong company: it fetches their filings, matches their name, scores them, and
eventually a dunning call, a legal notice or a registry listing carries their
identifier. A typo here is not a data-entry error. It is an adverse claim about
a stranger who has never heard of the debt.

So this module refuses early and names which check failed, in the order a reader
would want to hear it: shape, state code, embedded PAN, then the check digit.

Kept out of `resolution.py` deliberately. That module compares two entities and
holds no opinion about whether either identifier is real; everything in it needs
a second record to mean anything. Everything here answers from one string.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.company.resolution import CIN_RE, GSTIN_RE, PAN_RE

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# GST state codes run 01 (Jammu & Kashmir) through 38 (Ladakh). 97 ("other
# territory") and 99 (centre jurisdiction) exist for a small set of central
# registrations and are deliberately not accepted: this system verifies trading
# counterparties, and a code that resolves to no state would silently disarm the
# geographic contradiction that `resolution.py` relies on to veto a bad match.
MIN_STATE_CODE = 1
MAX_STATE_CODE = 38


class IdentifierProblem(str, Enum):
    """Why an identifier was refused. Closed set, defined here and nowhere else."""

    EMPTY = "EMPTY"
    GSTIN_LENGTH = "GSTIN_LENGTH"
    GSTIN_STATE_CODE = "GSTIN_STATE_CODE"
    GSTIN_PAN_SEGMENT = "GSTIN_PAN_SEGMENT"
    GSTIN_STRUCTURE = "GSTIN_STRUCTURE"
    GSTIN_CHECKSUM = "GSTIN_CHECKSUM"
    CIN_STRUCTURE = "CIN_STRUCTURE"


@dataclass(frozen=True)
class IdentifierCheck:
    """`value` is the normalised form — trimmed and upper-cased — so a caller
    that accepts the check can store what was checked rather than what was typed."""

    ok: bool
    value: str
    problem: IdentifierProblem | None = None
    detail: str = ""


def _refuse(value: str, problem: IdentifierProblem, detail: str) -> IdentifierCheck:
    return IdentifierCheck(False, value, problem, detail)


def gstin_check_digit(first_fourteen: str) -> str:
    """The fifteenth character, computed from the first fourteen.

    Base 36 over the characters as printed, weights alternating 1, 2 from the
    left. The `product // 36 + product % 36` step is what makes the scheme catch
    a transposition; replace it with the raw product and it degrades into a
    weighted sum, which does not.
    """
    total = 0
    for index, char in enumerate(first_fourteen):
        position = _CHARSET.find(char)
        if position < 0:
            raise ValueError(f"not a base-36 character: {char!r}")
        product = position * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def validate_gstin(raw: str | None) -> IdentifierCheck:
    """Refuse anything that is not a well-formed, self-consistent GSTIN."""
    value = (raw or "").strip().upper()
    if not value:
        return _refuse(value, IdentifierProblem.EMPTY, "no GSTIN given")
    if len(value) != 15:
        return _refuse(
            value,
            IdentifierProblem.GSTIN_LENGTH,
            f"a GSTIN is 15 characters; this is {len(value)}",
        )

    state = value[:2]
    if not state.isdigit() or not (MIN_STATE_CODE <= int(state) <= MAX_STATE_CODE):
        return _refuse(
            value,
            IdentifierProblem.GSTIN_STATE_CODE,
            f"state code {state!r} is outside 01-38",
        )

    # Characters 3-12 are the holder's PAN, which is why a GSTIN can corroborate
    # a PAN at all. Checked on its own so the refusal says "the PAN part is
    # wrong" rather than "malformed".
    pan = value[2:12]
    if not PAN_RE.match(pan):
        return _refuse(
            value,
            IdentifierProblem.GSTIN_PAN_SEGMENT,
            f"characters 3-12 are not a PAN: {pan!r}",
        )

    # The registration counter, the fixed 'Z' and the check character. Reusing
    # the module's one regex rather than restating its shape here.
    if not GSTIN_RE.match(value):
        return _refuse(
            value,
            IdentifierProblem.GSTIN_STRUCTURE,
            "the registration counter, the fixed 'Z' or the check character is malformed",
        )

    expected = gstin_check_digit(value[:14])
    if value[14] != expected:
        return _refuse(
            value,
            IdentifierProblem.GSTIN_CHECKSUM,
            f"check digit is {value[14]!r} but the first fourteen characters require "
            f"{expected!r} — one character of this GSTIN is wrong, and the number as "
            f"typed may belong to someone else",
        )
    return IdentifierCheck(True, value)


def is_valid_gstin(raw: str | None) -> bool:
    return validate_gstin(raw).ok


def validate_cin(raw: str | None) -> IdentifierCheck:
    """Structure only — a CIN has no check digit.

    Worth knowing rather than glossing over: a mistyped CIN cannot be caught
    here at all, so an MCA match on CIN alone is weaker evidence than a GST match
    on GSTIN, even though both look like identifier matches from the outside.
    """
    value = (raw or "").strip().upper()
    if not value:
        return _refuse(value, IdentifierProblem.EMPTY, "no CIN given")
    if not CIN_RE.match(value):
        return _refuse(
            value,
            IdentifierProblem.CIN_STRUCTURE,
            "a CIN is 21 characters: listing status, 5-digit activity code, state, "
            "year, ownership code, 6-digit registration number",
        )
    return IdentifierCheck(True, value)


def is_valid_cin(raw: str | None) -> bool:
    return validate_cin(raw).ok
