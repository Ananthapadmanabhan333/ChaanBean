"""Verifying that a debtor is the company somebody says it is.

Most of these assert a refusal. That is the shape of the module: a verification
that confirms the wrong company does not produce a wrong row, it produces a
demand letter addressed to a business that owes nothing, and the tier ladder
exists so that outcome needs a person's signature rather than a good guess.

The tests that matter most are the provenance ones. Data an operator typed off a
government portal is honest data entry, and it becomes dangerous the moment it is
stored without saying so — so `USER_PROVIDED` must never reach
`Tier.IDENTIFIER`, must never be publishable, and must never write a profile.
Three tests cover that from three directions, because a single one is a single
line for somebody to delete.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.company import verification
from app.company.gst import GST_FIXTURES, LocalGstBackend, ManualGstBackend
from app.company.mca import LocalMcaBackend
from app.company.resolution import Tier, hash_pan
from app.company.verification import (
    BuyerNotFound,
    ProvenanceViolation,
    cap_tier_for_provenance,
    verify_buyer,
)
from app.db import admin_session, tenant_session
from app.models import (
    Buyer,
    CompanyProfile,
    EntityCandidate,
    GstRecord,
    McaRecord,
    ProviderFetch,
    VerificationReport,
)
from app.providers.base import REGISTRY, USER_PROVIDED, GstLookup

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

# Sharma Traders, Mumbai, from the GST fixture set. Its PAN is embedded in
# characters 3-12, which is the whole reason a GSTIN can corroborate a PAN.
SHARMA_GSTIN = "27AABCS1429B1ZU"
SHARMA_PAN = "AABCS1429B"
SHARMA_ADDRESS = "14 Kalbadevi Road, Marine Lines, Mumbai, Maharashtra 400002"
SHARMA_GUJARAT_GSTIN = "24AABCS1429B1Z0"  # same PAN, second state

VERMA_GSTIN = "29AACCV3456D1ZB"  # registration Cancelled …
VERMA_CIN = "U27109KA2009PTC050123"  # … while the MCA still lists it Active
NAIR_GSTIN = "33AADCN7890F1ZB"  # GST live …
NAIR_CIN = "U31200TN2007PTC064512"  # … while the MCA has struck it off

# Well-formed, and in no fixture set. A CIN has no check digit, so the only way
# to catch a mistyped one is failing to find it.
UNFOUND_CIN = "U12345MH2015PTC000111"

# A different real business in the same state, checksum recomputed so it is a
# GSTIN a registry would accept rather than one the validator rejects for us.
STRANGER_GSTIN = "27AAECX1234C1ZP"


# ------------------------------------------------------------------ test doubles


class FixedGstBackend:
    """Answers with one record whatever it is asked.

    Stands in for a registry that searches rather than fetches by key: the
    interesting failures are the ones where what comes back is *not* the GSTIN
    that was asked about, and a fixture keyed by GSTIN can never produce them.
    """

    name = "fixed"

    def __init__(self, lookup: GstLookup | None):
        self._lookup = lookup
        self.calls: list[str] = []

    def lookup(self, gstin: str) -> GstLookup | None:
        self.calls.append(gstin)
        return self._lookup


def _gst_lookup(gstin: str, **over) -> GstLookup:
    row = {
        "gstin": gstin,
        "legal_name": "SHARMA TRADERS PRIVATE LIMITED",
        "trade_name": "Sharma Traders",
        "status": "Active",
        "registration_date": date(2017, 7, 1),
        "address": SHARMA_ADDRESS,
        "state_code": gstin[:2],
        "filing_history": [],
        "provenance": REGISTRY,
    }
    row.update(over)
    return GstLookup(**row)


# ---------------------------------------------------------------------- helpers


def _declare(
    t,
    *,
    name=None,
    gstin=None,
    cin=None,
    address=None,
    state_code=None,
    legal_name=None,
    on_buyer=False,
):
    """Record what the operator says, the way the identity endpoint would.

    A declaration lands on the buyer-scoped `CompanyProfile`; `on_buyer` writes
    the columns on `Buyer` instead, which is where an import puts them.
    """
    with admin_session() as s:
        buyer = s.get(Buyer, t.buyer_id)
        if name:
            buyer.name = name
        if on_buyer:
            buyer.gstin = gstin
            buyer.cin = cin
            return
        s.add(
            CompanyProfile(
                company_id=t.company_id,
                buyer_id=t.buyer_id,
                legal_name=legal_name or name or buyer.name,
                gstin=gstin,
                cin=cin,
                registered_address=address,
                state_code=state_code or (gstin[:2] if gstin else None),
                resolution_tier="self_declared",
            )
        )


def _verify(t, *, gst=None, mca=None, now=MARCH, buyer_id=None):
    with tenant_session(t.company_id) as s:
        return verify_buyer(
            s,
            buyer_id or t.buyer_id,
            company_id=t.company_id,
            actor_label="ops@example.test",
            gst=gst,
            mca=mca,
            now=now,
        )


def _rows(t, model, order=None):
    with tenant_session(t.company_id) as s:
        query = select(model).where(model.company_id == t.company_id)
        if order is not None:
            query = query.order_by(order)
        return list(s.execute(query).scalars())


def _profile(t):
    with tenant_session(t.company_id) as s:
        return s.execute(
            select(CompanyProfile).where(
                CompanyProfile.company_id == t.company_id,
                CompanyProfile.buyer_id == t.buyer_id,
            )
        ).scalars().first()


def _blob(text_parts) -> str:
    return " ".join(text_parts).lower()


# ------------------------------------------------------- the provenance rule


def test_operator_supplied_evidence_cannot_reach_the_identifying_tier():
    """The one rule the rest of the module is arranged around."""
    assert cap_tier_for_provenance(Tier.IDENTIFIER, REGISTRY) is Tier.IDENTIFIER
    assert cap_tier_for_provenance(Tier.IDENTIFIER, USER_PROVIDED) is Tier.STRUCTURAL


def test_an_unrecognised_provenance_is_read_as_self_declared():
    """The reference implementation called its typed-in adapters a "public-data
    mode". A rule written as "not USER_PROVIDED" would have let that through."""
    for claimed in ("V0_PUBLIC_DATA", "registry", "", None, "REGISTRY "):
        assert cap_tier_for_provenance(Tier.IDENTIFIER, claimed) is Tier.STRUCTURAL


def test_the_cap_lands_somewhere_that_cannot_be_published():
    capped = cap_tier_for_provenance(Tier.IDENTIFIER, USER_PROVIDED)
    assert capped.publishable is False


def test_the_cap_does_not_promote_weaker_evidence():
    for tier in (Tier.STRUCTURAL, Tier.NAME_STRONG, Tier.NAME_WEAK, Tier.NONE):
        assert cap_tier_for_provenance(tier, REGISTRY) is tier
        assert cap_tier_for_provenance(tier, USER_PROVIDED) is tier


# ------------------------------------------------------------ the clean path


def test_a_registry_identifier_match_writes_a_profile(tenants):
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend())

    assert outcome.tier == Tier.IDENTIFIER.value
    assert outcome.publishable is True
    assert outcome.blockers == []
    assert outcome.gst_status == "Active"
    assert outcome.candidate_id is None

    profile = _profile(tenants.a)
    assert profile.id == outcome.profile_id
    assert profile.resolution_tier == Tier.IDENTIFIER.value
    assert profile.legal_name == "SHARMA TRADERS PRIVATE LIMITED"
    assert profile.gstin == SHARMA_GSTIN
    assert profile.state_code == "27"
    assert profile.status == "Active"
    assert profile.resolved_at == MARCH
    assert float(profile.confidence) == pytest.approx(1.0)

    # The evidence, kept and attached to the entity it resolved.
    (record,) = _rows(tenants.a, GstRecord)
    assert (record.gstin, record.status, record.provenance) == (
        SHARMA_GSTIN,
        "Active",
        REGISTRY,
    )
    assert record.fetched_at == MARCH
    assert record.profile_id == profile.id

    (fetch,) = _rows(tenants.a, ProviderFetch)
    assert fetch.provider == "gst"
    assert fetch.raw["gstin"] == SHARMA_GSTIN
    assert fetch.parsed["provenance"] == REGISTRY

    assert _rows(tenants.a, EntityCandidate) == []


@pytest.mark.parametrize("backend", ["registry", "manual"])
def test_every_signal_carries_a_sentence_somebody_could_read_out(tenants, backend):
    """A verification that cannot say why it decided is not defensible.

    Both directions: the run that confirms and the run that refuses owe the same
    account of themselves, and the refusing one owes it more.
    """
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    gst = (
        LocalGstBackend()
        if backend == "registry"
        else ManualGstBackend({SHARMA_GSTIN: GST_FIXTURES[SHARMA_GSTIN]})
    )
    outcome = _verify(tenants.a, gst=gst)

    assert outcome.signals
    for signal in outcome.signals:
        assert signal["sentence"].strip(), signal
        assert signal["sentence"].endswith("."), signal
        assert set(signal) >= {"name", "value", "supports", "sentence"}
    for blocker in outcome.blockers:
        assert blocker.endswith("."), blocker


def test_a_report_is_issued_and_can_be_read_back(tenants):
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend())

    (report,) = _rows(tenants.a, VerificationReport)
    assert report.is_current is True
    assert report.profile_id == outcome.profile_id
    assert report.payload["tier"] == Tier.IDENTIFIER.value
    assert report.payload["publishable"] is True
    assert report.payload["verified_at"] == MARCH.isoformat()
    assert report.sources[0]["provenance"] == REGISTRY


# --------------------------------------------------- below the top of the ladder


def test_a_name_match_proposes_a_candidate_and_never_a_profile(tenants):
    """A machine that merges on a name match is how one company's tax record
    gets attached to another."""
    _declare(
        tenants.a,
        name="Sharma Traders",
        gstin=STRANGER_GSTIN,
        address=SHARMA_ADDRESS,
        state_code="27",
    )
    backend = FixedGstBackend(_gst_lookup(SHARMA_GSTIN))
    outcome = _verify(tenants.a, gst=backend)

    assert outcome.tier == Tier.NAME_STRONG.value
    assert outcome.publishable is False
    assert outcome.profile_id is None
    assert outcome.candidate_id is not None
    assert any("IDENTIFIER" in b for b in outcome.blockers)

    (candidate,) = _rows(tenants.a, EntityCandidate)
    assert candidate.id == outcome.candidate_id
    assert candidate.status == "PROPOSED"
    assert candidate.tier == Tier.NAME_STRONG.value
    assert candidate.identifier_kind == "gstin"
    assert candidate.identifier_value == SHARMA_GSTIN
    assert candidate.candidate_name == "SHARMA TRADERS PRIVATE LIMITED"

    # The declared row is exactly as the operator left it.
    profile = _profile(tenants.a)
    assert profile.resolution_tier == "self_declared"
    assert profile.gstin == STRANGER_GSTIN
    assert profile.resolved_at is None
    assert profile.status is None

    # And the record fetched for a buyer we could not identify stays unattached.
    (record,) = _rows(tenants.a, GstRecord)
    assert record.profile_id is None


def test_a_buyer_with_no_declared_identifier_is_not_guessed_at(tenants):
    backend = FixedGstBackend(_gst_lookup(SHARMA_GSTIN))
    outcome = _verify(tenants.a, gst=backend)

    assert outcome.tier == Tier.NONE.value
    assert outcome.publishable is False
    assert outcome.candidate_id is None
    assert any("no declared GSTIN or CIN" in b for b in outcome.blockers)
    assert backend.calls == [], "a name alone must not send anybody to a registry"
    assert _rows(tenants.a, ProviderFetch) == []
    assert _rows(tenants.a, EntityCandidate) == []


def test_a_gstin_that_fails_its_check_digit_is_refused_before_it_is_looked_up(tenants):
    """One mistyped character usually still yields a valid-looking GSTIN, which
    belongs to somebody else. The refusal has to name the check that failed."""
    _declare(tenants.a, name="Sharma Traders", gstin="27AABCS1429B1ZR")
    backend = FixedGstBackend(_gst_lookup(SHARMA_GSTIN))
    outcome = _verify(tenants.a, gst=backend)

    assert outcome.tier == Tier.NONE.value
    assert "check digit" in _blob(outcome.blockers)
    assert backend.calls == []
    assert _rows(tenants.a, GstRecord) == []


def test_a_missing_registry_record_is_a_named_refusal(tenants):
    _declare(tenants.a, name="Nobody Ltd", gstin=STRANGER_GSTIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend())

    assert outcome.tier == Tier.NONE.value
    assert any(STRANGER_GSTIN in b and "not found" in b for b in outcome.blockers)
    assert outcome.candidate_id is None
    assert _rows(tenants.a, GstRecord) == []


def test_no_configured_backend_reaches_no_tier_rather_than_assuming_one(tenants):
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    outcome = _verify(tenants.a, gst=None, mca=None)

    assert outcome.tier == Tier.NONE.value
    assert outcome.publishable is False
    assert "never checked" in _blob(outcome.blockers)

    # Still a report. "We checked and reached nothing" and "nobody ever checked"
    # are different facts, and only one of them is a 404.
    (report,) = _rows(tenants.a, VerificationReport)
    assert report.payload["tier"] == Tier.NONE.value
    assert report.sources == []


def test_a_run_against_a_buyer_with_no_declared_identity_is_still_recorded(tenants):
    """A report hangs off a profile row, and a buyer nobody has declared an
    identity for has none — so this run used to answer in full and leave nothing
    behind, which reads back as "nobody ever checked". That is the one
    distinction the 404 on the read route exists to preserve.
    """
    outcome = _verify(tenants.a, gst=LocalGstBackend())
    assert outcome.tier == Tier.NONE.value

    (report,) = _rows(tenants.a, VerificationReport)
    assert report.payload["tier"] == Tier.NONE.value
    assert report.payload["blockers"]

    # The row it hangs off is the empty declaration it is, and nothing
    # downstream may read it as a resolution.
    profile = _profile(tenants.a)
    assert profile.resolution_tier == "self_declared"
    assert (profile.gstin, profile.cin, profile.resolved_at) == (None, None, None)


def test_two_declared_identifiers_that_disagree_stop_the_run(tenants):
    """The buyer row and the identity row can both hold a declaration. Two
    different numbers for one debtor is a question, not a tie to be broken."""
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN, on_buyer=True)
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GUJARAT_GSTIN)
    backend = FixedGstBackend(_gst_lookup(SHARMA_GSTIN))
    outcome = _verify(tenants.a, gst=backend)

    assert outcome.tier == Tier.NONE.value
    said = " ".join(outcome.blockers)
    assert SHARMA_GSTIN in said and SHARMA_GUJARAT_GSTIN in said
    assert backend.calls == []


# ------------------------------------------------- self-declared data, three ways


def test_an_operator_typed_record_can_never_reach_the_identifying_tier(tenants):
    """The GSTIN matches exactly. It is still only what somebody typed."""
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    backend = ManualGstBackend({SHARMA_GSTIN: GST_FIXTURES[SHARMA_GSTIN]})
    outcome = _verify(tenants.a, gst=backend)

    assert outcome.tier == Tier.STRUCTURAL.value
    assert Tier(outcome.tier) is not Tier.IDENTIFIER
    assert Tier(outcome.tier).publishable is False
    assert outcome.publishable is False
    assert "operator" in _blob(outcome.blockers)

    # It was capped, and the cap is stated rather than silent.
    assert any(s["name"] == "provenance_cap" for s in outcome.signals)

    profile = _profile(tenants.a)
    assert profile.resolution_tier == "self_declared"
    assert profile.resolved_at is None
    assert outcome.profile_id is None
    assert outcome.candidate_id is not None

    # The lookup is still kept — it is useful context, labelled as what it is.
    (record,) = _rows(tenants.a, GstRecord)
    assert record.provenance == USER_PROVIDED
    assert record.profile_id is None


def test_the_provenance_gate_is_enforced_a_second_time_at_the_write(tenants, monkeypatch):
    """Delete the cap and this is what stops the profile being written.

    Two gates on purpose: one of them can be removed by accident, and a silently
    published self-declaration is the failure the whole module is arranged
    around.
    """
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    backend = ManualGstBackend({SHARMA_GSTIN: GST_FIXTURES[SHARMA_GSTIN]})

    monkeypatch.setattr(
        verification, "cap_tier_for_provenance", lambda tier, provenance: tier
    )
    with pytest.raises(ProvenanceViolation):
        _verify(tenants.a, gst=backend)

    profile = _profile(tenants.a)
    assert profile.resolution_tier == "self_declared"
    assert profile.resolved_at is None


def test_a_self_declared_record_is_labelled_where_it_is_stored(tenants):
    """`GstRecord` alone has to be enough to tell the two apart, because in a
    year somebody will read it alone."""
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    _verify(
        tenants.a,
        gst=ManualGstBackend({SHARMA_GSTIN: GST_FIXTURES[SHARMA_GSTIN]}),
        now=MARCH,
    )
    _verify(tenants.a, gst=LocalGstBackend(), now=SEPTEMBER)

    records = _rows(tenants.a, GstRecord, order=GstRecord.fetched_at)
    assert [r.provenance for r in records] == [USER_PROVIDED, REGISTRY]

    fetches = _rows(tenants.a, ProviderFetch, order=ProviderFetch.fetched_at)
    assert [f.parsed["provenance"] for f in fetches] == [USER_PROVIDED, REGISTRY]


# ----------------------------------------------------------------- append-only


def test_re_verifying_appends_and_leaves_march_intact(tenants):
    """"What did we know in March" has to stay answerable in September — it is
    the answer to "why did you send that notice"."""
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    _verify(tenants.a, gst=LocalGstBackend(), now=MARCH)

    cancelled = {**GST_FIXTURES[SHARMA_GSTIN], "status": "Cancelled"}
    later = _verify(
        tenants.a,
        gst=LocalGstBackend({SHARMA_GSTIN: cancelled}),
        now=SEPTEMBER,
    )

    records = _rows(tenants.a, GstRecord, order=GstRecord.fetched_at)
    assert [r.status for r in records] == ["Active", "Cancelled"]
    assert [r.fetched_at for r in records] == [MARCH, SEPTEMBER]

    assert len(_rows(tenants.a, ProviderFetch)) == 2

    reports = _rows(tenants.a, VerificationReport, order=VerificationReport.issued_at)
    assert [r.is_current for r in reports] == [False, True]
    assert reports[0].payload["gst_status"] == "Active"
    assert reports[1].payload["gst_status"] == "Cancelled"

    # The profile is the current answer, so it moves; the evidence does not.
    profile = _profile(tenants.a)
    assert profile.status == "Cancelled"
    assert profile.resolved_at == SEPTEMBER
    assert later.publishable is False


# ------------------------------------------------------- statuses that stop us


def test_a_cancelled_registration_is_a_blocker_not_a_footnote(tenants):
    _declare(tenants.a, name="Verma Steel Works", gstin=VERMA_GSTIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend())

    # We know exactly who they are. That is not the same as being free to
    # publish anything about them.
    assert outcome.tier == Tier.IDENTIFIER.value
    assert outcome.gst_status == "Cancelled"
    assert outcome.publishable is False
    assert any("Cancelled" in b for b in outcome.blockers)
    assert _profile(tenants.a).status == "Cancelled"


def test_the_worse_of_two_registers_is_what_the_profile_records(tenants):
    """Two registers write one `status` column, so the column has to hold the
    worse news whichever order they were consulted in.

    Verma Steel Works is the shipped case: GST cancelled, and still Active at the
    MCA. Last-writer-wins files it as Active, and the weight-12 `company_status`
    factor — the heaviest registry input the credit engine has — never fires for
    a company whose registration the register itself has cancelled.
    """
    _declare(tenants.a, name="Verma Steel Works", gstin=VERMA_GSTIN, cin=VERMA_CIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend(), mca=LocalMcaBackend())

    assert outcome.tier == Tier.IDENTIFIER.value
    assert (outcome.gst_status, outcome.mca_status) == ("Cancelled", "Active")
    assert outcome.publishable is False
    assert any("Cancelled" in b for b in outcome.blockers)
    assert _profile(tenants.a).status == "Cancelled"


def test_a_register_that_did_not_answer_leaves_no_number_behind(tenants):
    """The tier is stamped on the whole row, so every column under it has to be
    something a registry actually returned.

    A CIN nobody found, sitting in a row labelled IDENTIFIER, is the provenance
    cap defeated one column further down — every downstream reader takes the
    stamp at its word rather than asking which field it covers.
    """
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN, cin=UNFOUND_CIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend(), mca=LocalMcaBackend())

    assert outcome.tier == Tier.IDENTIFIER.value
    profile = _profile(tenants.a)
    assert profile.gstin == SHARMA_GSTIN
    assert profile.cin is None, "an unchecked CIN survived under an IDENTIFIER stamp"
    assert profile.incorporation_date is None


def test_a_stranger_s_identifier_is_named_rather_than_quietly_adopted(tenants):
    """A checksum-valid GSTIN typed against the wrong buyer matches perfectly.

    The register answers about Sharma Traders; the debt is Bhatt Ceramics'. The
    identifier says yes and nothing else does — so the run has to say so, rather
    than hand back a publishable profile wearing a stranger's name.
    """
    _declare(tenants.a, name="Bhatt Ceramics", gstin=SHARMA_GSTIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend())

    # The number matched, so the tier is honest about the evidence — and nothing
    # may be published on the strength of it until a person has looked.
    assert outcome.tier == Tier.IDENTIFIER.value
    assert outcome.publishable is False
    assert any("shares no word" in b for b in outcome.blockers)

    # Computed on the identifier path too, so a reviewer can see the thing that
    # is wrong rather than a screen of agreement.
    similarity = next(s for s in outcome.signals if s["name"] == "name_similarity")
    assert similarity["value"] == "0.00"
    assert similarity["supports"] is False

    # And the buyer keeps their own name instead of acquiring the stranger's.
    assert _profile(tenants.a).legal_name == "Bhatt Ceramics"


def test_a_live_gstin_does_not_cover_a_struck_off_company(tenants):
    """Check one register and report "verified" and you have told the user
    something false in the most consequential direction."""
    _declare(tenants.a, name="Nair Electricals", gstin=NAIR_GSTIN, cin=NAIR_CIN)
    outcome = _verify(tenants.a, gst=LocalGstBackend(), mca=LocalMcaBackend())

    assert outcome.tier == Tier.IDENTIFIER.value
    assert outcome.gst_status == "Active"
    assert outcome.mca_status == "Struck Off"
    assert outcome.publishable is False
    assert any("Struck Off" in b for b in outcome.blockers)

    profile = _profile(tenants.a)
    assert profile.cin == NAIR_CIN
    assert profile.status == "Struck Off"
    (record,) = _rows(tenants.a, McaRecord)
    assert record.provenance == REGISTRY
    assert record.profile_id == profile.id


# ------------------------------------------------------------------------- PAN


def test_the_profile_stores_a_hashed_pan_and_never_the_number(tenants):
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GSTIN)
    _verify(tenants.a, gst=LocalGstBackend())

    profile = _profile(tenants.a)
    expected_hash, expected_last4 = hash_pan(SHARMA_PAN)
    assert profile.pan_hash == expected_hash
    assert profile.pan_last4 == expected_last4 == "429B"
    assert profile.pan_hash != SHARMA_PAN
    assert SHARMA_PAN not in {
        profile.legal_name,
        profile.registered_address,
        profile.status,
        profile.pan_hash,
        profile.pan_last4,
    }


def test_a_pan_derived_signal_is_masked_before_it_is_stored(tenants):
    """`resolution` puts the derived PAN in the signal value, which is right for
    a comparison in memory and wrong the moment it reaches JSONB or a screen."""
    _declare(tenants.a, name="Sharma Traders", gstin=SHARMA_GUJARAT_GSTIN)
    outcome = _verify(tenants.a, gst=FixedGstBackend(_gst_lookup(SHARMA_GSTIN)))

    assert outcome.tier == Tier.STRUCTURAL.value
    signal = next(s for s in outcome.signals if s["name"] == "pan_from_gstin")
    assert signal["value"] == "XXXXXX429B"
    assert SHARMA_PAN not in signal["value"]
    assert SHARMA_PAN not in signal["sentence"]

    (candidate,) = _rows(tenants.a, EntityCandidate)
    stored = next(s for s in candidate.signals["signals"] if s["name"] == "pan_from_gstin")
    assert SHARMA_PAN not in json.dumps(stored)


# -------------------------------------------------------------------- tenancy


def test_one_tenant_cannot_verify_anothers_buyer(tenants):
    _declare(tenants.b, name="Sharma Traders", gstin=SHARMA_GSTIN)
    with pytest.raises(BuyerNotFound):
        _verify(tenants.a, gst=LocalGstBackend(), buyer_id=tenants.b.buyer_id)
