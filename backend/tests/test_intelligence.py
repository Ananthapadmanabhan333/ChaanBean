"""Entity resolution, scoring, legal matching, pre-legal, registry, tracing.

Every module here shares one failure mode: being confidently wrong about *which
company* something belongs to. That is defamation rather than a data-quality
issue, so most of these tests assert a refusal rather than a result.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.company.resolution import (
    EntityInput,
    SourceRecord,
    Tier,
    best_match,
    hash_pan,
    normalise_company_name,
    pan_from_gstin,
    resolve,
    token_set_similarity,
)
from app.intelligence.scoring import Band, ScoringContext, score_buyer
from app.legal.matching import (
    AUTO_CONFIRM_THRESHOLD,
    CaseRef,
    PartyRef,
    ProfileRef,
    confirmed_only,
    link_candidates,
)
from app.legal.prelegal import (
    AssessmentInput,
    Outcome,
    assess,
    limitation_expiry,
)
from app.registry.eligibility import ListingInput, evaluate, should_delist
from app.tracing.sources import (
    ForbiddenSourceError,
    GstRegistrationSource,
    McaFilingSource,
    OwnLedgerSource,
    run_trace,
)

TODAY = date(2026, 6, 1)


# ------------------------------------------------------------ name handling


def test_legal_suffixes_are_stripped_but_identifying_words_are_not():
    """Stripping "Trading Company" would collapse two unrelated businesses."""
    assert normalise_company_name("M/s. Sharma Trading Company Pvt. Ltd.") == (
        "sharma trading company"
    )
    assert normalise_company_name("SHARMA TRADERS LLP") == "sharma traders"
    assert normalise_company_name("Nair Electricals & Co.") == "nair electricals"
    # The pair that must stay distinguishable.
    assert normalise_company_name("Sharma Trading Company") != normalise_company_name(
        "Sharma Steel Company"
    )


def test_token_set_beats_edit_distance_for_word_order():
    """Two clerks writing the same firm in a different order."""
    assert token_set_similarity("Sharma Steel Traders", "Traders Sharma Steel") == 1.0


def test_pan_is_never_stored_in_the_clear():
    digest, last4 = hash_pan("AABCS1429B")
    assert "AABCS1429B" not in digest
    assert last4 == "429B"
    assert len(digest) == 64


# -------------------------------------------------------- entity resolution


def test_identifier_match_is_decisive_and_publishable():
    r = resolve(
        EntityInput(name="Totally Different Name", gstin="27AABCS1429B1ZR"),
        SourceRecord(name="Sharma Trading Co", gstin="27AABCS1429B1ZR"),
    )
    assert r.tier is Tier.IDENTIFIER
    assert r.confidence == 1.0
    assert r.publishable is True


def test_gstin_embeds_the_pan_and_that_is_structural_evidence():
    assert pan_from_gstin("27AABCS1429B1ZR") == "AABCS1429B"
    r = resolve(
        EntityInput(name="Sharma Traders", gstin="27AABCS1429B1ZR"),
        SourceRecord(name="Sharma Traders Mumbai", gstin="29AABCS1429B1ZP"),
    )
    assert r.tier is Tier.STRUCTURAL
    assert r.publishable is False, "structural is strong but not publishable"


def test_a_name_only_match_is_never_publishable():
    """The gate that stops a mismatched entity becoming defamation."""
    r = resolve(
        EntityInput(name="Sharma Traders"), SourceRecord(name="Sharma Traders")
    )
    assert r.tier in (Tier.NAME_STRONG, Tier.NAME_WEAK)
    assert r.publishable is False


def test_a_state_contradiction_is_evidence_against_not_weakly_for():
    """A blended score would let name similarity outvote this. Tiers do not."""
    r = resolve(
        EntityInput(name="Sharma Traders", gstin="27AABCS1429B1ZR"),
        SourceRecord(name="Sharma Traders", gstin="29AAACX9999X1ZQ"),
    )
    assert r.vetoed is True
    assert r.publishable is False
    assert any(s.supports is False for s in r.signals)


def test_two_equally_good_candidates_resolve_to_nothing():
    """A coin toss with a defamation claim on one side is not a match."""
    candidate = EntityInput(name="Sharma Traders")
    records = [
        SourceRecord(name="Sharma Traders", address="12 MG Road"),
        SourceRecord(name="Sharma Traders", address="88 Link Road"),
    ]
    record, resolution = best_match(candidate, records)
    assert record is None
    assert resolution.vetoed is True
    assert "human" in resolution.veto_reason


def test_best_match_prefers_identifier_over_a_better_name():
    candidate = EntityInput(name="Sharma Traders", gstin="27AABCS1429B1ZR")
    records = [
        SourceRecord(name="Sharma Traders"),  # perfect name, no identifier
        SourceRecord(name="S T Enterprises", gstin="27AABCS1429B1ZR"),
    ]
    record, resolution = best_match(candidate, records)
    assert resolution.tier is Tier.IDENTIFIER
    assert record.name == "S T Enterprises"


# ------------------------------------------------------------------ scoring


def _ctx(**over) -> ScoringContext:
    base = dict(
        as_of=TODAY,
        mean_days_to_pay=35,
        days_to_pay_trend=0,
        promise_kept_rate=0.9,
        part_payment_rate=0.1,
        dispute_rate=0.0,
        contact_response_rate=0.8,
        outstanding_paise=5_000_000,
        historical_average_paise=5_000_000,
        max_days_past_due=20,
    )
    base.update(over)
    return ScoringContext(**base)


def test_a_good_payer_scores_low_and_a_bad_one_high():
    good = score_buyer(_ctx())
    bad = score_buyer(
        _ctx(
            mean_days_to_pay=140,
            days_to_pay_trend=50,
            promise_kept_rate=0.0,
            contact_response_rate=0.05,
            max_days_past_due=200,
            company_status="Struck Off",
            recovery_suits=3,
        )
    )
    assert good.score < bad.score
    assert good.band is Band.LOW
    assert bad.band in (Band.ELEVATED, Band.HIGH)


def test_every_point_of_the_score_is_attributable():
    """A score that changes treatment without a stated reason is indefensible."""
    result = score_buyer(_ctx(promise_kept_rate=0.2, mean_days_to_pay=90))
    assert result.factors
    for factor in result.factors:
        assert factor.explanation
        assert factor.contribution == pytest.approx(factor.value * factor.weight)
    assert "promise" in result.explanation.lower()


def test_the_ledger_outweighs_external_signals():
    """Your own payment history is the most predictive data you hold."""
    ledger_only = score_buyer(_ctx(promise_kept_rate=0.0, mean_days_to_pay=150))
    external_only = score_buyer(_ctx(company_status="Struck Off", recovery_suits=3))
    assert ledger_only.score > external_only.score


def test_part_payment_is_weighted_lightly():
    """It usually signals cash-flow strain, not unwillingness."""
    heavy = score_buyer(_ctx(part_payment_rate=1.0))
    none = score_buyer(_ctx(part_payment_rate=0.0))
    assert heavy.score - none.score < 6


def test_score_is_reproducible_as_of_a_date():
    ctx = _ctx()
    assert score_buyer(ctx).score == score_buyer(ctx).score
    assert score_buyer(ctx).as_of == TODAY


# ----------------------------------------------------------- legal matching


PROFILE = ProfileRef(
    profile_id="p1",
    legal_name="Sharma Steel Traders Private Limited",
    trade_names=("Sharma Steel",),
    gstin="27AABCS1429B1ZR",
    director_names=("Rajesh Sharma",),
)


def _case(case_id: str, *parties: str, text: str = "") -> CaseRef:
    return CaseRef(
        case_id=case_id,
        court_id="DLHC",
        case_number=f"CS/{case_id}/2026",
        parties=tuple(PartyRef(p) for p in parties),
        text=text,
    )


def test_an_identifier_in_the_case_text_is_near_conclusive():
    proposals = link_candidates(
        PROFILE, [_case("c1", "Some Party", text="GSTIN 27AABCS1429B1ZR of the respondent")]
    )
    assert proposals[0].confidence >= 0.95
    assert proposals[0].signals["identifier_in_text"] == "27AABCS1429B1ZR"


def test_a_name_alone_never_reaches_certainty():
    """Prefer a missed match to a false one."""
    proposals = link_candidates(PROFILE, [_case("c2", "Sharma Steel Traders")])
    assert proposals
    assert proposals[0].confidence <= 0.75, (
        "a clerk-typed name must not produce a confident link"
    )


def test_nothing_is_ever_auto_confirmed():
    proposals = link_candidates(
        PROFILE, [_case("c3", "Sharma Steel Traders Private Limited")]
    )
    assert AUTO_CONFIRM_THRESHOLD is None
    assert all(p.status == "PROPOSED" for p in proposals)
    assert confirmed_only(proposals) == []


def test_an_unrelated_company_sharing_a_word_is_not_proposed():
    proposals = link_candidates(PROFILE, [_case("c4", "Verma Cement Industries")])
    assert proposals == []


def test_a_director_named_as_a_party_is_strong():
    proposals = link_candidates(PROFILE, [_case("c5", "Rajesh Sharma")])
    assert proposals[0].signals["director_is_party"] == "Rajesh Sharma"
    assert proposals[0].confidence >= 0.8


def test_a_generic_exact_name_is_worth_less_than_a_distinctive_one():
    generic = ProfileRef(profile_id="g", legal_name="Sharma Traders")
    distinctive = ProfileRef(profile_id="d", legal_name="Sharma Steel Traders Kolkata")
    g = link_candidates(generic, [_case("c6", "Sharma Traders")])[0]
    d = link_candidates(distinctive, [_case("c7", "Sharma Steel Traders Kolkata")])[0]
    assert d.confidence > g.confidence


# --------------------------------------------------------------- pre-legal


def _assessment(**over) -> AssessmentInput:
    base = dict(
        as_of=TODAY,
        account_status="OVERDUE",
        outstanding_paise=50_000_000,
        due_date=date(2025, 1, 1),
        delivered_contacts_at_l2=1,
        has_invoice=True,
        has_delivery_proof=True,
    )
    base.update(over)
    return AssessmentInput(**base)


def test_limitation_runs_three_years_from_the_due_date():
    assert limitation_expiry(_assessment(due_date=date(2025, 1, 1))) == date(2028, 1, 1)


def test_a_part_payment_restarts_the_limitation_clock():
    """Limitation Act ss. 18-19. Verified by counsel, not by this test."""
    assert limitation_expiry(
        _assessment(due_date=date(2023, 1, 1), last_part_payment=date(2025, 6, 1))
    ) == date(2028, 6, 1)


def test_expiry_within_six_months_is_flagged_urgent():
    """A claim that lapses in a queue is a total loss and entirely preventable."""
    result = assess(_assessment(due_date=date(2023, 8, 1)))
    assert result.limitation_urgent is True
    assert any("URGENT" in f for f in result.factors)


def test_a_time_barred_claim_goes_to_a_human_not_a_notice():
    result = assess(_assessment(due_date=date(2020, 1, 1)))
    assert result.outcome is Outcome.ROUTE_TO_HUMAN
    assert result.limitation_expired is True


def test_a_notice_requires_a_delivered_contact_at_l2():
    """A notice to someone never reached is procedurally weak and ethically poor."""
    result = assess(_assessment(delivered_contacts_at_l2=0))
    assert result.outcome is Outcome.NOT_READY
    assert any("never actually been reached" in b for b in result.blockers)


def test_a_small_balance_is_a_write_off_recommendation_with_the_arithmetic():
    result = assess(_assessment(outstanding_paise=200_000))
    assert result.outcome is Outcome.RECOMMEND_WRITE_OFF
    assert any("recoverable" in f for f in result.factors)


def test_a_dispute_blocks_legal_escalation():
    result = assess(_assessment(account_status="IN_DISPUTE"))
    assert result.outcome is Outcome.NOT_READY
    assert any("dispute" in b for b in result.blockers)


def test_a_clean_account_is_recommended_for_a_notice():
    result = assess(_assessment())
    assert result.outcome is Outcome.RECOMMEND_NOTICE
    assert result.recoverable_paise > 0


# ---------------------------------------------------------------- registry


def _listing(**over) -> ListingInput:
    base = dict(
        as_of=TODAY,
        account_status="OVERDUE",
        ever_disputed=False,
        outstanding_paise=50_000_000,
        notice_dispatched=True,
        notice_delivery_proof=True,
        notice_response_deadline=date(2026, 5, 1),
        notice_response_received=False,
        entity_confidence_publishable=True,
        ledger_reconciled_on=date(2026, 5, 30),
        last_payment_on=None,
        human_signed_off_by="ops@acme.test",
    )
    base.update(over)
    return ListingInput(**base)


def test_all_eight_gates_must_pass_to_publish():
    result = evaluate(_listing())
    assert result.eligible is True
    assert len(result.checked) >= 8


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"ever_disputed": True}, "disputed"),
        ({"entity_confidence_publishable": False}, "name-only match"),
        ({"notice_delivery_proof": False}, "proof of delivery"),
        ({"notice_dispatched": False}, "no legal notice"),
        ({"outstanding_paise": 100_000}, "below the"),
        ({"ledger_reconciled_on": date(2026, 1, 1)}, "stale"),
        ({"last_payment_on": date(2026, 5, 25)}, "payment was received"),
        ({"human_signed_off_by": None}, "human sign-off"),
        ({"notice_response_received": True}, "responded"),
        ({"account_status": "SETTLED"}, "not OVERDUE"),
    ],
)
def test_each_gate_blocks_publication_on_its_own(override, fragment):
    result = evaluate(_listing(**override))
    assert result.eligible is False
    assert any(fragment in b for b in result.blockers), result.blockers


def test_a_name_only_entity_match_can_never_be_published():
    """The single most dangerous mistake this module can make."""
    result = evaluate(_listing(entity_confidence_publishable=False))
    assert result.eligible is False


def test_removal_needs_only_one_reason_where_listing_needs_all():
    """The asymmetry, stated as a test: hard to publish, easy to remove."""
    assert should_delist(paid_in_full=True) is not None
    assert should_delist(dispute_raised=True) is not None
    assert should_delist(entity_challenged=True) is not None
    assert should_delist(listing_party_withdrew=True) is not None
    assert should_delist() is None


def test_a_dispute_suspends_publication_immediately():
    reason = should_delist(dispute_raised=True)
    assert reason.immediate is True
    assert "suspended while" in reason.detail


# ----------------------------------------------------------------- tracing


class _Profile:
    profile_id = "p1"
    gstin = "27AABCS1429B1ZR"
    cin = "U27100MH2010PTC123456"


def test_only_self_published_and_own_sources_are_used():
    hits, audit = run_trace(
        [
            McaFilingSource(
                {
                    "U27100MH2010PTC123456": {
                        "cin": "U27100MH2010PTC123456",
                        "registered_address": "12 MG Road, Mumbai",
                        "directors": [{"name": "Rajesh Sharma", "phone": "+919876543210"}],
                    }
                }
            ),
            GstRegistrationSource(
                {"27AABCS1429B1ZR": {"gstin": "27AABCS1429B1ZR", "address": "12 MG Road"}}
            ),
            OwnLedgerSource([{"type": "email", "value": "ap@sharma.test"}]),
        ],
        _Profile(),
    )
    assert {h.source for h in hits} == {"mca_filing", "gst_registration", "own_ledger"}
    assert all(h.lawful_basis for h in hits), "every hit states its lawful basis"
    assert len(audit) == 3


def test_a_forbidden_source_is_refused():
    class Broker:
        name = "data_broker"
        lawful_basis = "none"

        def lookup(self, profile):
            return []

    with pytest.raises(ForbiddenSourceError, match="not a lawful source"):
        run_trace([Broker()], _Profile())


def test_every_source_queried_is_recorded_even_when_it_returns_nothing():
    """The audit is what distinguishes a recovery tool from a lookup service."""
    hits, audit = run_trace([McaFilingSource({})], _Profile())
    assert hits == []
    assert audit[0]["source"] == "mca_filing"
    assert audit[0]["result_count"] == 0
    assert audit[0]["lawful_basis"]
