"""Court records, and the rule that a name is not an identity.

Almost every test here asserts a refusal, because that is the shape of the
module. A court search takes a name and returns every case filed against that
name; the searching is trivial and the attribution is where the harm is. Attach
a stranger's recovery suit to a debtor's file and the output is not a bad row,
it is a defamation claim.

Three fixtures carry most of the weight, and they are chosen to look alike:

* `COMM.SUIT/412/2024` pleads this company's GSTIN against the respondent —
  the only kind of evidence that may ever be confirmed.
* `OA/119/2023` carries the same GSTIN in the recital and names a director as
  a respondent. `app.legal.matching` scores it 0.95, the same as the first,
  and it must still never be confirmable.
* `OS/778/2025` is a different Sharma Traders entirely, in Chennai, with a
  perfect name match.

If the grade ever stops distinguishing those three, the tests below stop
passing before a customer notices.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.company.resolution import Tier
from app.db import admin_session, tenant_session
from app.identity.auth import hash_password
from app.intelligence.scoring import ScoringContext, score_buyer
from app.legal.assessment import assess_account
from app.legal.cases import (
    CONFIRMED,
    PROPOSED,
    REJECTED,
    LegalSignals,
    LinkRefused,
    Refusal,
    confirm_link,
    grade_match,
    is_insolvency_case_type,
    is_recovery_case_type,
    legal_signals,
    reject_link,
    search_and_link,
    verified_identifiers,
)
from app.legal.prelegal import Outcome
from app.models import (
    AuditLog,
    Buyer,
    CaseParty,
    Company,
    CompanyProfile,
    CourtCase,
    CreditAccount,
    Invoice,
    LegalHistory,
    LegalLink,
    McaRecord,
    PrelegalAssessment,
    ProviderFetch,
    User,
)
from app.providers.base import REGISTRY
from app.providers.courts import (
    COURT_FIXTURES,
    CaseQuery,
    CourtBackend,
    LocalCourtBackend,
    ManualCourtBackend,
)
from app.trade.accounts import sync_account_from_invoice

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)

SHARMA_GSTIN = "27AABCS1429B1ZU"
SHARMA_CIN = "U51909MH2011PTC219876"

MUMBAI_SUIT = "COMM.SUIT/412/2024"  # GSTIN pleaded against the respondent
DRT_APPLICATION = "OA/119/2023"  # same GSTIN, recital only, director as party
CHENNAI_SUIT = "OS/778/2025"  # a different business with the same name


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _row(case_number: str) -> dict:
    return next(r for r in COURT_FIXTURES if r["case_number"] == case_number)


def _parties(*items) -> list[dict]:
    return [{"name": n, "role": r, "identifier": i} for n, r, i in items]


# ------------------------------------------------------------- the grade alone


def test_only_an_identifier_recorded_against_a_party_reaches_the_top_tier():
    grade = grade_match(
        identifiers=frozenset({SHARMA_GSTIN}),
        parties=_parties(("Sharma Traders Private Limited", "respondent", SHARMA_GSTIN)),
        provenance=REGISTRY,
        signals={},
    )
    assert grade.tier is Tier.IDENTIFIER
    assert grade.confirmable


def test_a_perfect_name_match_never_becomes_identifier_grade():
    """The whole point. A clerk-typed name is not an identity, at any score."""
    grade = grade_match(
        identifiers=frozenset({SHARMA_GSTIN}),
        parties=_parties(("Sharma Traders", "respondent", None)),
        provenance=REGISTRY,
        signals={"name_similarity": 1.0, "exact_normalised_name": "sharma traders"},
    )
    assert grade.tier is Tier.NAME_WEAK
    assert not grade.confirmable


def test_an_identifier_in_the_recital_is_not_attributed_to_a_party():
    """0.95 from matching, and still not confirmable: it says the number is in
    the papers, not which side of the case it is on."""
    grade = grade_match(
        identifiers=frozenset({SHARMA_GSTIN}),
        parties=_parties(("Rajesh Sharma", "respondent", None)),
        provenance=REGISTRY,
        signals={"identifier_in_text": SHARMA_GSTIN, "director_is_party": "RAJESH SHARMA"},
    )
    assert grade.tier is Tier.STRUCTURAL
    assert not grade.confirmable


def test_an_operator_typed_case_cannot_identify_anybody():
    grade = grade_match(
        identifiers=frozenset({SHARMA_GSTIN}),
        parties=_parties(("Sharma Traders Private Limited", "respondent", SHARMA_GSTIN)),
        provenance="USER_PROVIDED",
        signals={},
    )
    assert grade.tier is Tier.STRUCTURAL
    assert not grade.confirmable


def test_a_profile_below_the_top_tier_holds_no_identifiers():
    class Profile:
        resolved_at = NOW
        resolution_tier = Tier.NAME_STRONG.value
        gstin = SHARMA_GSTIN
        cin = SHARMA_CIN
        additional_gstins: list = []

    assert verified_identifiers(Profile()) == frozenset()
    assert verified_identifiers(None) == frozenset()


def test_an_unrecognised_case_type_does_not_collect_the_recovery_weight():
    assert is_recovery_case_type("Commercial Suit")
    assert is_recovery_case_type("CP(IB)/233 IBC Section 9") is False
    assert is_recovery_case_type("IBC SECTION 9")
    assert is_insolvency_case_type("IBC Section 9")
    assert not is_recovery_case_type("WRIT PETITION")
    assert not is_recovery_case_type(None)


def test_unreviewed_links_are_not_offered_to_the_scoring_engine():
    signals = LegalSignals(awaiting_review=7)
    assert signals.for_scoring() == {"confirmed_legal_cases": 0, "recovery_suits": 0}


def test_the_fixture_backend_searches_by_name_and_returns_the_noise():
    found = LocalCourtBackend().search(CaseQuery(party_name="Sharma Traders"))
    assert {r.case_number for r in found} == {MUMBAI_SUIT, DRT_APPLICATION, CHENNAI_SUIT}
    assert all(r.provenance == REGISTRY for r in found)
    assert isinstance(LocalCourtBackend(), CourtBackend)


def test_a_manual_backend_never_claims_a_court_fetched_its_records():
    backend = ManualCourtBackend(rows=(_row(MUMBAI_SUIT),))
    found = backend.search(CaseQuery(party_name="Sharma Traders"))
    assert [r.provenance for r in found] == ["USER_PROVIDED"]


# ------------------------------------------------------------------- the tenant


@pytest.fixture
def tenant():
    """One company, three buyers, and its own teardown.

    The shared `tenants` fixture in conftest knows nothing about court cases,
    links or histories, and its teardown deletes the company those rows point
    at — so this module builds and removes its own rather than leaving a
    foreign key to fail in whichever test runs next.
    """

    class T:
        pass

    t = T()
    suffix = uuid.uuid4().hex[:5]

    with admin_session() as s:
        company = Company(name=_uniq("court-tenant"))
        s.add(company)
        s.flush()
        t.company_id = company.id

        user = User(
            company_id=company.id,
            email=f"{_uniq('reviewer')}@example.test",
            password_hash=hash_password("correct-horse"),
            phone_e164=f"+9199{int(suffix, 16) % 100000000:08d}",
        )
        s.add(user)
        s.flush()
        t.user_id = user.id

        # Verified: an identifier the registry holds, stamped IDENTIFIER.
        verified = Buyer(
            company_id=company.id,
            name="Sharma Traders Private Limited",
            external_ref=_uniq("ref"),
            gstin=SHARMA_GSTIN,
            cin=SHARMA_CIN,
        )
        s.add(verified)
        s.flush()
        profile = CompanyProfile(
            company_id=company.id,
            buyer_id=verified.id,
            legal_name="SHARMA TRADERS PRIVATE LIMITED",
            trade_names=["Sharma Traders"],
            gstin=SHARMA_GSTIN,
            cin=SHARMA_CIN,
            state_code="27",
            confidence=1.0,
            resolution_tier=Tier.IDENTIFIER.value,
            resolved_at=NOW - timedelta(days=1),
        )
        s.add(profile)
        s.flush()
        s.add(
            McaRecord(
                company_id=company.id,
                profile_id=profile.id,
                cin=SHARMA_CIN,
                legal_name="SHARMA TRADERS PRIVATE LIMITED",
                status="Active",
                directors=[{"name": "RAJESH SHARMA", "din": "01234567"}],
                charges=[],
                provenance=REGISTRY,
                fetched_at=NOW - timedelta(days=1),
            )
        )
        t.buyer_id = verified.id
        t.profile_id = profile.id

        # The neighbour: the same trading name, and nobody has established who
        # they are. This is the pair the merge tests are about.
        neighbour = Buyer(
            company_id=company.id, name="Sharma Traders", external_ref=_uniq("ref")
        )
        s.add(neighbour)
        s.flush()
        neighbour_profile = CompanyProfile(
            company_id=company.id, buyer_id=neighbour.id, legal_name="Sharma Traders"
        )
        s.add(neighbour_profile)
        s.flush()
        t.neighbour_id = neighbour.id
        t.neighbour_profile_id = neighbour_profile.id

        # No profile at all.
        undeclared = Buyer(
            company_id=company.id,
            name="Nair Electricals Private Limited",
            external_ref=_uniq("ref"),
        )
        s.add(undeclared)
        s.flush()
        t.undeclared_id = undeclared.id

        # Created whole and never adjusted afterwards: `sync_account_from_invoice`
        # stays the only writer of the balance, as it is everywhere else.
        invoice = Invoice(
            company_id=company.id,
            buyer_id=verified.id,
            invoice_number=_uniq("INV"),
            issue_date=date(2025, 1, 5),
            due_date=date(2025, 2, 4),
            gross_paise=5_000_000,
            tax_paise=0,
            net_paise=5_000_000,
            outstanding_paise=5_000_000,
        )
        s.add(invoice)
        s.flush()
        account = sync_account_from_invoice(s, invoice, now=NOW)
        t.account_id = account.id

    yield t

    with admin_session() as s:
        cid = t.company_id
        s.execute(delete(LegalHistory).where(LegalHistory.company_id == cid))
        s.execute(delete(LegalLink).where(LegalLink.company_id == cid))
        s.execute(delete(CaseParty).where(CaseParty.company_id == cid))
        s.execute(delete(CourtCase).where(CourtCase.company_id == cid))
        s.execute(delete(PrelegalAssessment).where(PrelegalAssessment.company_id == cid))
        s.execute(delete(ProviderFetch).where(ProviderFetch.company_id == cid))
        s.execute(delete(AuditLog).where(AuditLog.company_id == cid))
        s.execute(delete(McaRecord).where(McaRecord.company_id == cid))
        s.execute(delete(CompanyProfile).where(CompanyProfile.company_id == cid))
        s.execute(delete(CreditAccount).where(CreditAccount.company_id == cid))
        s.execute(delete(Invoice).where(Invoice.company_id == cid))
        s.execute(delete(Buyer).where(Buyer.company_id == cid))
        s.execute(delete(User).where(User.company_id == cid))
        s.execute(delete(Company).where(Company.id == cid))


def _search(t, backend=None, buyer_id=None, now=NOW):
    with tenant_session(t.company_id) as s:
        return search_and_link(
            s,
            buyer_id or t.buyer_id,
            company_id=t.company_id,
            backend=backend if backend is not None else LocalCourtBackend(),
            now=now,
        )


def _link_for(t, case_number: str, profile_id=None):
    with tenant_session(t.company_id) as s:
        return s.execute(
            select(LegalLink)
            .join(CourtCase, CourtCase.id == LegalLink.case_id)
            .where(
                CourtCase.case_number == case_number,
                LegalLink.profile_id == (profile_id or t.profile_id),
            )
        ).scalar_one()


# ------------------------------------------------------------------ the search


def test_a_search_records_every_case_and_grades_each_link(tenant):
    outcome = _search(tenant)

    assert outcome.cases_seen == 3
    graded = {link.case_number: link.tier for link in outcome.links}
    assert graded == {
        MUMBAI_SUIT: Tier.IDENTIFIER.value,
        DRT_APPLICATION: Tier.STRUCTURAL.value,
        CHENNAI_SUIT: Tier.NAME_WEAK.value,
    }
    assert all(link.status == PROPOSED for link in outcome.links)
    assert not outcome.refusals

    with tenant_session(tenant.company_id) as s:
        assert len(s.execute(select(CourtCase)).scalars().all()) == 3
        # Both sides of every case, kept as the court wrote them.
        assert len(s.execute(select(CaseParty)).scalars().all()) == 6


def test_the_search_itself_confirms_nothing(tenant):
    _search(tenant)
    with tenant_session(tenant.company_id) as s:
        signals = legal_signals(s, tenant.profile_id)
    assert signals.confirmed_legal_cases == 0
    assert signals.awaiting_review == 3


def test_a_name_only_link_cannot_be_confirmed(tenant):
    """The headline refusal. A perfect name match, and the answer is still no."""
    _search(tenant)
    link = _link_for(tenant, CHENNAI_SUIT)

    with tenant_session(tenant.company_id) as s:
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                link.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.MATCH_NOT_IDENTIFIER_GRADE
    assert _link_for(tenant, CHENNAI_SUIT).status == PROPOSED


def test_an_identifier_in_the_recital_cannot_be_confirmed_either(tenant):
    """Scored 0.95 by `matching`, identical to the case that may be confirmed."""
    _search(tenant)
    link = _link_for(tenant, DRT_APPLICATION)
    assert float(link.confidence) >= 0.95

    with tenant_session(tenant.company_id) as s:
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                link.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.MATCH_NOT_IDENTIFIER_GRADE


def test_a_pleaded_identifier_may_be_confirmed(tenant):
    _search(tenant)
    link = _link_for(tenant, MUMBAI_SUIT)

    with tenant_session(tenant.company_id) as s:
        confirmed = confirm_link(
            s, link.id, company_id=tenant.company_id, reviewed_by=tenant.user_id, now=NOW
        )
        assert confirmed.status == CONFIRMED
        assert confirmed.reviewed_by == tenant.user_id
        assert confirmed.signals["matched_identifier"] == SHARMA_GSTIN


def test_a_confirmation_re_grades_against_the_profile_as_it_stands_now(tenant):
    """The recompute, which nothing else here discriminates.

    Every other test agrees with the grade stored at search time, so replacing
    the recompute with a read of `link.signals` would leave them all green. This
    one takes the identifier resolution away between the search and the review
    — a re-verification that came back weaker, a profile reset — and the claim
    the link records is then stale: it was graded on identifiers the registry is
    no longer said to hold.
    """
    _search(tenant)
    link = _link_for(tenant, MUMBAI_SUIT)

    with tenant_session(tenant.company_id) as s:
        profile = s.get(CompanyProfile, tenant.profile_id)
        profile.resolved_at = None
        s.flush()

        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                link.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.MATCH_NOT_IDENTIFIER_GRADE


def test_a_rejected_link_cannot_be_confirmed_over_the_top(tenant):
    _search(tenant)
    link = _link_for(tenant, MUMBAI_SUIT)
    with tenant_session(tenant.company_id) as s:
        reject_link(
            s,
            link.id,
            company_id=tenant.company_id,
            reviewed_by=tenant.user_id,
            reason="the goods went to the Chennai firm",
            now=NOW,
        )
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                link.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.ALREADY_REJECTED


def test_an_unknown_link_is_a_named_refusal(tenant):
    with tenant_session(tenant.company_id) as s:
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                uuid.uuid4(),
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.LINK_NOT_FOUND


def test_an_operator_supplied_case_is_refused_by_name(tenant):
    _search(tenant, backend=ManualCourtBackend(rows=(_row(MUMBAI_SUIT),)))
    link = _link_for(tenant, MUMBAI_SUIT)

    with tenant_session(tenant.company_id) as s:
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                link.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
    assert raised.value.reason is Refusal.RECORD_NOT_REGISTRY_BACKED


# --------------------------------------------------------------- what scoring sees


def test_only_confirmed_links_reach_the_score(tenant):
    _search(tenant)
    link = _link_for(tenant, MUMBAI_SUIT)
    with tenant_session(tenant.company_id) as s:
        confirm_link(
            s, link.id, company_id=tenant.company_id, reviewed_by=tenant.user_id, now=NOW
        )
        signals = legal_signals(s, tenant.profile_id)

    # One confirmed commercial suit. The other two matched by name and by a
    # recital, and neither of them counts for anything here.
    assert signals.confirmed_legal_cases == 1
    assert signals.recovery_suits == 1
    assert signals.awaiting_review == 2

    scored = score_buyer(
        ScoringContext(as_of=NOW.date(), max_days_past_due=100, **signals.for_scoring())
    )
    factor = next(f for f in scored.factors if f.name == "recovery_suits")
    assert "1 confirmed recovery suit" in factor.explanation

    unreviewed_only = score_buyer(
        ScoringContext(
            as_of=NOW.date(),
            max_days_past_due=100,
            **LegalSignals(awaiting_review=2).for_scoring(),
        )
    )
    assert not [
        f for f in unreviewed_only.factors if f.name in ("recovery_suits", "legal_cases")
    ]


# ----------------------------------------------------------------- re-running


def test_a_re_run_appends_rather_than_overwriting(tenant):
    later = NOW + timedelta(days=90)
    _search(tenant)
    with tenant_session(tenant.company_id) as s:
        first = s.execute(select(LegalHistory)).scalars().one()
        first_id, first_issued, first_payload = first.id, first.issued_at, first.payload

    _search(tenant, now=later)

    with tenant_session(tenant.company_id) as s:
        histories = (
            s.execute(select(LegalHistory).order_by(LegalHistory.issued_at)).scalars().all()
        )
        assert len(histories) == 2
        assert histories[0].id == first_id
        assert histories[0].issued_at == first_issued
        assert histories[0].payload == first_payload
        assert histories[0].caveat

        # The case row is the current position and is refreshed, so the trail of
        # what each run actually saw lives in the fetch rows.
        assert len(s.execute(select(CourtCase)).scalars().all()) == 3
        assert len(s.execute(select(ProviderFetch)).scalars().all()) == 6


def test_a_re_run_does_not_reopen_a_rejected_link(tenant):
    _search(tenant)
    link = _link_for(tenant, CHENNAI_SUIT)
    with tenant_session(tenant.company_id) as s:
        reject_link(
            s,
            link.id,
            company_id=tenant.company_id,
            reviewed_by=tenant.user_id,
            reason="different business; Chennai proprietorship",
            now=NOW,
        )

    _search(tenant, now=NOW + timedelta(days=30))

    after = _link_for(tenant, CHENNAI_SUIT)
    assert after.status == REJECTED
    assert after.reject_reason == "different business; Chennai proprietorship"


# -------------------------------------------------------------- the same name


def test_two_companies_sharing_a_common_name_do_not_merge(tenant):
    """Same tenant, same case, two candidate companies. Only one can be confirmed."""
    _search(tenant)
    outcome = _search(tenant, buyer_id=tenant.neighbour_id)

    assert Refusal.PROFILE_NOT_IDENTIFIER_RESOLVED in outcome.refusals
    neighbour_grades = {link.case_number: link.tier for link in outcome.links}
    assert neighbour_grades[MUMBAI_SUIT] == Tier.NAME_WEAK.value
    assert not outcome.confirmable

    theirs = _link_for(tenant, MUMBAI_SUIT, profile_id=tenant.neighbour_profile_id)
    with tenant_session(tenant.company_id) as s:
        with pytest.raises(LinkRefused) as raised:
            confirm_link(
                s,
                theirs.id,
                company_id=tenant.company_id,
                reviewed_by=tenant.user_id,
                now=NOW,
            )
        assert raised.value.reason is Refusal.MATCH_NOT_IDENTIFIER_GRADE

        # And confirming it for the company whose GSTIN is on the filing leaves
        # the neighbour's own count at zero.
        confirm_link(
            s,
            _link_for(tenant, MUMBAI_SUIT).id,
            company_id=tenant.company_id,
            reviewed_by=tenant.user_id,
            now=NOW,
        )
        assert legal_signals(s, tenant.profile_id).confirmed_legal_cases == 1
        assert legal_signals(s, tenant.neighbour_profile_id).confirmed_legal_cases == 0


def test_a_buyer_with_no_profile_is_refused_before_anything_is_searched(tenant):
    outcome = _search(tenant, buyer_id=tenant.undeclared_id)
    assert outcome.refusals == (Refusal.NO_PROFILE,)
    assert outcome.cases_seen == 0
    with tenant_session(tenant.company_id) as s:
        assert s.execute(select(CourtCase)).scalars().all() == []


def test_a_missing_backend_is_a_named_refusal(tenant):
    with tenant_session(tenant.company_id) as s:
        outcome = search_and_link(
            s, tenant.buyer_id, company_id=tenant.company_id, backend=None, now=NOW
        )
    assert Refusal.NO_BACKEND in outcome.refusals


# ------------------------------------------------------------- the assessment


def _assess(t, now=NOW, **over):
    args = dict(
        company_id=t.company_id,
        now=now,
        delivered_contacts_at_l2=1,
        has_delivery_proof=True,
        assessed_by=t.user_id,
    )
    args.update(over)
    with tenant_session(t.company_id) as s:
        return assess_account(s, t.account_id, **args)


def test_the_assessment_counts_confirmed_cases_and_only_reports_the_rest(tenant):
    _search(tenant)
    with tenant_session(tenant.company_id) as s:
        confirm_link(
            s,
            _link_for(tenant, MUMBAI_SUIT).id,
            company_id=tenant.company_id,
            reviewed_by=tenant.user_id,
            now=NOW,
        )

    result = _assess(tenant)

    assert result.legal.confirmed_legal_cases == 1
    assert result.legal.awaiting_review == 2
    assert result.assessment.outcome is Outcome.RECOMMEND_NOTICE
    assert any("1 confirmed recovery matter" in f for f in result.assessment.factors)
    assert any("awaiting review" in f for f in result.assessment.factors)
    assert not result.assessment.blockers


def test_a_confirmed_insolvency_routes_the_recommendation_to_a_human(tenant):
    petition = {
        **_row("CP(IB)/233/2025"),
        "parties": (
            {"name": "Coromandel Cables Private Limited", "role": "operational creditor"},
            {
                "name": "Sharma Traders Private Limited",
                "role": "corporate debtor",
                "identifier": SHARMA_CIN,
            },
        ),
    }
    _search(tenant, backend=LocalCourtBackend(rows=(petition,)))
    with tenant_session(tenant.company_id) as s:
        confirm_link(
            s,
            _link_for(tenant, "CP(IB)/233/2025").id,
            company_id=tenant.company_id,
            reviewed_by=tenant.user_id,
            now=NOW,
        )

    result = _assess(tenant)

    assert result.legal.insolvency_cases == 1
    assert result.assessment.outcome is Outcome.ROUTE_TO_HUMAN
    assert any("moratorium" in b.lower() for b in result.assessment.blockers)


def test_an_unreached_debtor_is_never_ready_for_a_notice(tenant):
    result = _assess(tenant, delivered_contacts_at_l2=0)
    assert result.assessment.outcome is Outcome.NOT_READY
    assert any("never actually been reached" in b for b in result.assessment.blockers)


def test_assessments_append_rather_than_overwrite(tenant):
    first = _assess(tenant)
    with tenant_session(tenant.company_id) as s:
        written = s.execute(select(PrelegalAssessment)).scalars().one()
        assessed_at, outcome = written.assessed_at, written.outcome

    second = _assess(tenant, now=NOW + timedelta(days=45))

    assert first.assessment_id != second.assessment_id
    with tenant_session(tenant.company_id) as s:
        rows = (
            s.execute(select(PrelegalAssessment).order_by(PrelegalAssessment.assessed_at))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert rows[0].id == first.assessment_id
    assert rows[0].assessed_at == assessed_at
    assert rows[0].outcome == outcome


def test_limitation_is_measured_on_the_court_s_calendar(tenant):
    """The ledger is UTC; the date a court reads is Asia/Kolkata."""
    result = _assess(tenant)
    # Due 4 February 2025 in Kolkata, three years under the Limitation Act.
    assert result.assessment.limitation_expires_on == date(2028, 2, 4)
    assert not result.assessment.limitation_urgent
