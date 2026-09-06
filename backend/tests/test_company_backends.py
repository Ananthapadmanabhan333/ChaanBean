"""The GST and MCA backends, and the provenance rule they exist to carry.

The rule, stated once: data a human typed off a government portal is
USER_PROVIDED, it can never support `Tier.IDENTIFIER`, and `Tier.IDENTIFIER` is
the only tier that may be published. Every test below exists so that removing a
piece of that sentence from the code breaks something loudly, rather than
quietly converting a self-declaration into a verification and pointing a legal
notice at a company that has nothing to do with the debt.
"""

from __future__ import annotations

import inspect

import pytest

from app.company import build_gst_backend, build_mca_backend
from app.company.gst import GST_FIXTURES, LocalGstBackend, ManualGstBackend
from app.company.mca import MCA_FIXTURES, LocalMcaBackend, ManualMcaBackend
from app.company.resolution import Tier
from app.company.verification import cap_tier_for_provenance
from app.config import settings
from app.providers.base import (
    REGISTRY,
    USER_PROVIDED,
    GstBackend,
    GstLookup,
    McaBackend,
    McaLookup,
    UnknownProvenance,
    is_registry_provenance,
)

ACTIVE = "27AABCS1429B1ZU"      # Sharma Traders, Maharashtra, GST Active
CANCELLED = "29AACCV3456D1ZB"   # Verma Steel Works, GST Cancelled
STRUCK_OFF_GST = "33AADCN7890F1ZB"  # GST Active, MCA struck off
STRUCK_OFF_CIN = "U31200TN2007PTC064512"
ACTIVE_CIN = "U51909MH2011PTC219876"

TYPO = "27AABCS1429B1ZR"  # ACTIVE with the last character mistyped
UNKNOWN_BUT_VALID = "19AABCS1429B1ZR"  # correct check digit, no record anywhere


# ------------------------------------------------------------------ provenance


def test_the_registry_backend_says_registry():
    lookup = LocalGstBackend().lookup(ACTIVE)
    assert lookup is not None
    assert lookup.provenance == REGISTRY
    assert is_registry_provenance(lookup.provenance)


def test_the_manual_backend_says_user_provided():
    manual = ManualGstBackend({ACTIVE: {"legal_name": "SHARMA TRADERS PRIVATE LIMITED"}})
    lookup = manual.lookup(ACTIVE)
    assert lookup is not None
    assert lookup.provenance == USER_PROVIDED
    assert not is_registry_provenance(lookup.provenance)


def test_manual_data_can_never_reach_the_tier_that_publishes():
    """The provenance rule, driven end to end rather than asserted in halves.

    A lookup from each manual backend goes through the cap the resolver applies,
    and neither comes out publishable. Asserting "manual is USER_PROVIDED" and
    "only IDENTIFIER publishes" side by side would leave the join between them —
    `cap_tier_for_provenance` — deletable with this test still green.
    """
    gst = ManualGstBackend({ACTIVE: dict(GST_FIXTURES[ACTIVE])}).lookup(ACTIVE)
    mca = ManualMcaBackend({ACTIVE_CIN: dict(MCA_FIXTURES[ACTIVE_CIN])}).lookup(ACTIVE_CIN)

    assert not is_registry_provenance(gst.provenance)
    assert not is_registry_provenance(mca.provenance)

    for lookup in (gst, mca):
        capped = cap_tier_for_provenance(Tier.IDENTIFIER, lookup.provenance)
        assert capped is not Tier.IDENTIFIER
        assert capped.publishable is False

    assert Tier.IDENTIFIER.publishable
    assert not any(tier.publishable for tier in Tier if tier is not Tier.IDENTIFIER)


@pytest.mark.parametrize(
    "backend", [ManualGstBackend, ManualMcaBackend, LocalGstBackend, LocalMcaBackend]
)
def test_no_backend_takes_provenance_as_an_argument(backend):
    """An operator supplies the values. They do not supply the claim about where
    the values came from — a `provenance` argument here is the bug, not the fix.
    """
    assert "provenance" not in inspect.signature(backend.__init__).parameters


@pytest.mark.parametrize(
    "value", [REGISTRY, USER_PROVIDED, "registry", "REGISTRY ", "VERIFIED", "", None]
)
def test_only_the_exact_registry_literal_counts_as_registry(value):
    """Written as a positive test because `!= USER_PROVIDED` would read a typo,
    a None or an invented third value as evidence."""
    assert is_registry_provenance(value) is (value == REGISTRY)


def test_a_lookup_cannot_declare_an_invented_provenance():
    with pytest.raises(UnknownProvenance):
        GstLookup(
            gstin=ACTIVE,
            legal_name="SHARMA TRADERS PRIVATE LIMITED",
            trade_name=None,
            status="Active",
            registration_date=None,
            address=None,
            state_code="27",
            filing_history=[],
            provenance="VERIFIED",
        )
    with pytest.raises(UnknownProvenance):
        McaLookup(
            cin=ACTIVE_CIN,
            legal_name="SHARMA TRADERS PRIVATE LIMITED",
            status="Active",
            incorporation_date=None,
            registered_address=None,
            directors=[],
            charges=[],
            provenance="registry",
        )


# ---------------------------------------------------------------- typo defence


def test_a_backend_will_not_answer_for_a_gstin_that_fails_its_checksum():
    """Even when the caller planted that exact key in the fixture set. The
    checksum is checked before the lookup, because a GSTIN that cannot be real
    has no record to return and answering would attach a name to a number nobody
    can hold."""
    backend = LocalGstBackend({TYPO: {"legal_name": "SOMEBODY ELSE ENTIRELY"}})
    assert backend.lookup(TYPO) is None


def test_the_manual_path_is_checksummed_too():
    """This is the path where fifteen characters pass through human fingers."""
    manual = ManualGstBackend({TYPO: {"legal_name": "SOMEBODY ELSE ENTIRELY"}})
    assert manual.lookup(TYPO) is None


def test_a_malformed_cin_returns_nothing():
    assert LocalMcaBackend().lookup("U31200TN2007PTC06451") is None
    assert ManualMcaBackend({ACTIVE_CIN: {}}).lookup("not-a-cin") is None


def test_an_unknown_but_valid_identifier_is_simply_a_miss():
    """A well-formed GSTIN nobody has a record for. Distinct from a mistyped one,
    and both come back as None — which is why the caller validates first if it
    wants to tell the difference."""
    assert LocalGstBackend().lookup(UNKNOWN_BUT_VALID) is None


# -------------------------------------------------------------------- lookups


def test_lookup_is_case_and_whitespace_insensitive():
    assert LocalGstBackend().lookup("  27aabcs1429b1zu ") is not None
    assert LocalMcaBackend().lookup(" u51909mh2011ptc219876 ") is not None


def test_the_state_code_comes_from_the_gstin_itself():
    """Two characters that are already checksummed beat any second copy of the
    same fact typed into a row."""
    assert LocalGstBackend().lookup(ACTIVE).state_code == "27"
    assert LocalGstBackend().lookup(CANCELLED).state_code == "29"


def test_the_fixtures_include_the_cases_that_must_not_verify():
    """A fixture set where everything is Active proves nothing about a system
    whose job is knowing when it cannot vouch for a company."""
    assert LocalGstBackend().lookup(CANCELLED).status == "Cancelled"
    assert LocalMcaBackend().lookup(STRUCK_OFF_CIN).status == "Struck Off"


def test_the_two_registers_are_allowed_to_disagree():
    """Nair Electricals: GST live, struck off at the MCA. A system that checks
    one register and reports "verified" has said something false in the most
    consequential direction."""
    assert LocalGstBackend().lookup(STRUCK_OFF_GST).status == "Active"
    assert LocalMcaBackend().lookup(STRUCK_OFF_CIN).status == "Struck Off"


def test_the_filing_history_handed_out_is_a_copy():
    """A caller that edits what it was handed must not rewrite the fixture set
    for every later lookup in the process.

    The expected length is captured *before* the mutation. Comparing the next
    lookup against `GST_FIXTURES` as it stands afterwards compares the fixture
    with itself: without the copy both sides grow together and the test passes
    over exactly the corruption it is named for.
    """
    before = len(GST_FIXTURES[ACTIVE]["filing_history"])
    lookup = LocalGstBackend().lookup(ACTIVE)
    lookup.filing_history.append({"period": "9999-99"})

    assert len(GST_FIXTURES[ACTIVE]["filing_history"]) == before
    assert len(LocalGstBackend().lookup(ACTIVE).filing_history) == before


def test_a_manual_entry_may_use_an_iso_string_for_a_date():
    """Fixtures hold real dates; a person types text. Both arrive as a date."""
    manual = ManualGstBackend({ACTIVE: {"registration_date": "2017-07-01"}})
    assert manual.lookup(ACTIVE).registration_date.year == 2017


def test_an_unparseable_date_becomes_unknown_rather_than_an_error():
    manual = ManualGstBackend({ACTIVE: {"registration_date": "01/07/2017"}})
    assert manual.lookup(ACTIVE).registration_date is None


# ------------------------------------------------------------------- selection


def test_the_backends_satisfy_the_protocols():
    assert isinstance(LocalGstBackend(), GstBackend)
    assert isinstance(ManualGstBackend(), GstBackend)
    assert isinstance(LocalMcaBackend(), McaBackend)
    assert isinstance(ManualMcaBackend(), McaBackend)


def test_the_configured_backend_is_what_gets_built(monkeypatch):
    monkeypatch.setattr(settings, "gst_backend", "local")
    monkeypatch.setattr(settings, "mca_backend", "local")
    assert build_gst_backend().name == "local"
    assert build_mca_backend().name == "local"

    monkeypatch.setattr(settings, "gst_backend", "manual")
    monkeypatch.setattr(settings, "mca_backend", "manual")
    assert build_gst_backend().name == "manual"
    assert build_mca_backend().name == "manual"


def test_the_manual_backend_starts_empty():
    """It knows only what somebody has actually looked up. A manual backend that
    invented answers would be worse than having none."""
    assert ManualGstBackend().lookup(ACTIVE) is None
    assert ManualMcaBackend().lookup(ACTIVE_CIN) is None
