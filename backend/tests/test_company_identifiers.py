"""GSTIN and CIN validation.

These tests are about one claim: a mistyped identifier is not a typo, it is an
adverse statement about a company that has never heard of the debt. So most of
them assert a refusal, and the refusal has to name which check failed — "invalid"
is not an answer anyone can act on.
"""

from __future__ import annotations

import pytest

from app.company.gst import GST_FIXTURES
from app.company.identifiers import (
    IdentifierProblem,
    gstin_check_digit,
    is_valid_cin,
    is_valid_gstin,
    validate_cin,
    validate_gstin,
)
from app.company.mca import MCA_FIXTURES
from app.company.resolution import GSTIN_RE

# The GSTIN used in the published worked example of the check-digit algorithm.
# It anchors these tests to something outside this repository: without it, the
# suite would only prove that the validator agrees with itself.
PUBLISHED_EXAMPLE = "27AAPFU0939F1ZV"

REAL = "27AABCS1429B1ZU"  # Sharma Traders, Maharashtra — see GST_FIXTURES


# ------------------------------------------------------------------ check digit


def test_the_published_example_validates():
    assert validate_gstin(PUBLISHED_EXAMPLE).ok


def test_the_check_digit_is_computed_not_looked_up():
    assert gstin_check_digit(PUBLISHED_EXAMPLE[:14]) == PUBLISHED_EXAMPLE[14]
    assert gstin_check_digit(REAL[:14]) == REAL[14]


def test_every_fixture_gstin_carries_a_real_check_digit():
    """A hand-edited fixture should fail here, not quietly teach the system that
    an impossible GSTIN is fine."""
    for gstin in GST_FIXTURES:
        check = validate_gstin(gstin)
        assert check.ok, f"{gstin}: {check.problem} {check.detail}"


def test_every_fixture_cin_is_well_formed():
    for cin in MCA_FIXTURES:
        check = validate_cin(cin)
        assert check.ok, f"{cin}: {check.problem} {check.detail}"


# --------------------------------------------------------------------- refusals


def test_one_wrong_character_passes_the_shape_check_and_is_caught_by_the_checksum():
    """The entire reason this module exists.

    `27AABCS1429B1ZR` is what you get by mistyping the last character of a real
    GSTIN. It matches the pattern perfectly — the shape of a GSTIN cannot tell
    you that it is wrong. Only the check digit can.
    """
    typo = "27AABCS1429B1ZR"
    assert GSTIN_RE.match(typo)

    check = validate_gstin(typo)
    assert not check.ok
    assert check.problem is IdentifierProblem.GSTIN_CHECKSUM
    # The refusal has to say what was required, or nobody can correct the entry.
    assert "'U'" in check.detail


@pytest.mark.parametrize("state", ["00", "39", "41", "97", "99"])
def test_a_state_code_outside_01_38_is_refused(state):
    check = validate_gstin(state + REAL[2:])
    assert not check.ok
    assert check.problem is IdentifierProblem.GSTIN_STATE_CODE


@pytest.mark.parametrize("state", ["01", "27", "38"])
def test_the_ends_of_the_state_range_are_accepted(state):
    """Boundaries, because 01 and 38 are real states and an off-by-one here
    refuses a legitimate business."""
    candidate = state + REAL[2:14]
    assert validate_gstin(candidate + gstin_check_digit(candidate)).ok


def test_a_pan_segment_that_is_not_a_pan_is_named_as_such():
    check = validate_gstin("27AABC12345B1ZU")
    assert not check.ok
    assert check.problem is IdentifierProblem.GSTIN_PAN_SEGMENT


def test_the_fixed_z_is_part_of_the_structure():
    candidate = REAL[:13] + "Q"
    check = validate_gstin(candidate + gstin_check_digit(candidate))
    assert not check.ok
    assert check.problem is IdentifierProblem.GSTIN_STRUCTURE


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_nothing_is_refused_as_nothing(raw):
    check = validate_gstin(raw)
    assert not check.ok
    assert check.problem is IdentifierProblem.EMPTY


@pytest.mark.parametrize("raw", [REAL[:14], REAL + "X"])
def test_the_wrong_length_is_refused_before_anything_else(raw):
    check = validate_gstin(raw)
    assert not check.ok
    assert check.problem is IdentifierProblem.GSTIN_LENGTH


def test_case_and_surrounding_space_are_normalised_not_rejected():
    """A GSTIN pasted out of a PDF arrives with whitespace, and one typed in a
    hurry arrives in lower case. Neither is a different company."""
    check = validate_gstin("  27aabcs1429b1zu\n")
    assert check.ok
    assert check.value == REAL


def test_the_boolean_helper_agrees_with_the_detailed_one():
    assert is_valid_gstin(REAL)
    assert not is_valid_gstin("27AABCS1429B1ZR")


# -------------------------------------------------------------------------- CIN


def test_a_well_formed_cin_is_accepted_and_normalised():
    check = validate_cin("  u51909mh2011ptc219876 ")
    assert check.ok
    assert check.value == "U51909MH2011PTC219876"


@pytest.mark.parametrize(
    "cin",
    [
        "X51909MH2011PTC219876",  # not L or U
        "U51909MH2011PTC21987",   # 20 characters
        "U5190MH2011PTC2198765",  # 4-digit activity code
        "U51909MH20A1PTC219876",  # letter in the year
    ],
)
def test_a_malformed_cin_is_refused(cin):
    check = validate_cin(cin)
    assert not check.ok
    assert check.problem is IdentifierProblem.CIN_STRUCTURE


def test_an_empty_cin_is_refused_as_empty():
    assert validate_cin(None).problem is IdentifierProblem.EMPTY
    assert not is_valid_cin(None)
