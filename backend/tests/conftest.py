"""Shared fixtures.

Anything touching the database goes through `admin_session` for setup, because
RLS is on and setup code has no tenant of its own. Tests then read back through
`tenant_session` — which is the path the application actually uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, text

from app.db import admin_session, engine, worker_engine
from app.identity.auth import hash_password
from app.models import (
    AuditLog,
    BlackoutDate,
    Buyer,
    BuyerPhone,
    Call,
    CallEvent,
    Campaign,
    CampaignTarget,
    CaseParty,
    ChannelOptOut,
    Company,
    CompanyProfile,
    CourtCase,
    CreditAccount,
    CreditAssessment,
    CreditNote,
    EntityCandidate,
    EscalationState,
    GstRecord,
    Invoice,
    DndStatus,
    LegalHistory,
    LegalLink,
    LegalMatter,
    LegalNotice,
    McaRecord,
    Message,
    MessageEvent,
    Payment,
    PaymentAllocation,
    PaymentBehaviour,
    PrelegalAssessment,
    Promise,
    ProviderFetch,
    Return,
    RoleGrant,
    Seller,
    Statement,
    User,
    VerificationReport,
)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session", autouse=True)
def _require_database():
    """Skip the whole DB suite with a clear message rather than 40 opaque errors."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="session", autouse=True)
def _require_bypassrls(_require_database):
    """The worker role must genuinely bypass RLS, or half these tests prove nothing."""
    try:
        with worker_engine().connect() as conn:
            ok = conn.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).scalar()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"worker role unavailable: {exc}")
    if not ok:
        pytest.skip("worker role lacks BYPASSRLS; run `python -m app.db rls`")


@pytest.fixture
def tenants():
    """Two companies, each with a user and a buyer with one clear phone.

    Returns a small object rather than a tuple so tests read as
    `tenants.a.buyer_id` instead of `fixture[3]`.
    """

    class Tenant:
        pass

    class Pair:
        pass

    created: list[uuid.UUID] = []
    pair = Pair()

    with admin_session() as s:
        for label in ("a", "b"):
            t = Tenant()
            company = Company(name=_uniq(f"company-{label}"))
            s.add(company)
            s.flush()
            created.append(company.id)

            user = User(
                company_id=company.id,
                email=f"{label}@{_uniq('example')}.test",
                password_hash=hash_password("correct-horse"),
                phone_e164=f"+9190000{len(created):05d}",
            )
            s.add(user)
            s.flush()
            s.add(
                RoleGrant(company_id=company.id, user_id=user.id, role="admin")
            )

            buyer = Buyer(
                company_id=company.id,
                name=f"Buyer {label.upper()}",
                external_ref=_uniq("ref"),
            )
            s.add(buyer)
            s.flush()
            s.add(
                BuyerPhone(
                    company_id=company.id,
                    buyer_id=buyer.id,
                    e164=f"+9198765{len(created):05d}",
                    dnd_status=DndStatus.CLEAR,
                    dnd_checked_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
            )
            account = CreditAccount(
                company_id=company.id,
                buyer_id=buyer.id,
                invoice_ref=_uniq("inv"),
                outstanding_paise=5_000_000,
                due_date=datetime.now(timezone.utc) - timedelta(days=30),
            )
            s.add(account)
            s.flush()

            t.company_id = company.id
            t.user_id = user.id
            t.user_email = user.email
            t.buyer_id = buyer.id
            t.account_id = account.id
            setattr(pair, label, t)

    yield pair

    with admin_session() as s:
        for cid in created:
            s.execute(delete(AuditLog).where(AuditLog.company_id == cid))
            # Company intelligence, innermost first. Verification reports, GST
            # and MCA records all point at a company_profiles row, so every one
            # of them has to be gone before the profiles are — and the profiles
            # (both the creditor's own and the buyer-scoped ones) before the
            # company and the buyers they reference.
            #
            # This list is hand-maintained and nothing checks it, so a table
            # added upstream and forgotten here does not fail its own test: it
            # fails teardown on a foreign key, in whichever unrelated test
            # happens to run next.
            s.execute(
                delete(VerificationReport).where(VerificationReport.company_id == cid)
            )
            s.execute(delete(GstRecord).where(GstRecord.company_id == cid))
            s.execute(delete(McaRecord).where(McaRecord.company_id == cid))
            # Court records join the same chain: a legal link and an issued
            # legal history both name a company_profiles row, so they belong
            # above the profiles too — not down with the rest of the legal
            # tables, which hang off credit accounts instead. Innermost first: a
            # matter cites a notice, a notice cites an assessment, and links and
            # parties both hang off the case.
            s.execute(delete(LegalMatter).where(LegalMatter.company_id == cid))
            s.execute(delete(LegalNotice).where(LegalNotice.company_id == cid))
            s.execute(
                delete(PrelegalAssessment).where(PrelegalAssessment.company_id == cid)
            )
            s.execute(delete(LegalHistory).where(LegalHistory.company_id == cid))
            s.execute(delete(LegalLink).where(LegalLink.company_id == cid))
            s.execute(delete(CaseParty).where(CaseParty.company_id == cid))
            s.execute(delete(CourtCase).where(CourtCase.company_id == cid))
            s.execute(delete(CompanyProfile).where(CompanyProfile.company_id == cid))
            # Candidates reference buyers and users rather than profiles, so
            # they need only precede those two.
            s.execute(delete(EntityCandidate).where(EntityCandidate.company_id == cid))
            s.execute(delete(ProviderFetch).where(ProviderFetch.company_id == cid))
            s.execute(delete(CreditAssessment).where(CreditAssessment.company_id == cid))
            # Everything from here down is what the HTTP surface can now create,
            # traced foreign key by foreign key rather than guessed at. The order
            # is one chain: contact rows point at campaigns and accounts, legal
            # rows point at accounts and profiles, ledger movements point at
            # payments and invoices, and all of them point at buyers.
            s.execute(delete(CallEvent).where(CallEvent.company_id == cid))
            s.execute(delete(MessageEvent).where(MessageEvent.company_id == cid))
            s.execute(delete(Call).where(Call.company_id == cid))
            s.execute(delete(Message).where(Message.company_id == cid))
            s.execute(delete(CampaignTarget).where(CampaignTarget.company_id == cid))
            s.execute(delete(Campaign).where(Campaign.company_id == cid))
            s.execute(delete(ChannelOptOut).where(ChannelOptOut.company_id == cid))
            s.execute(delete(BlackoutDate).where(BlackoutDate.company_id == cid))
            s.execute(delete(Promise).where(Promise.company_id == cid))
            s.execute(delete(PaymentBehaviour).where(PaymentBehaviour.company_id == cid))
            # Ledger movements before the documents they move against: a return
            # cites a credit note, and both an allocation and a note cite the
            # invoice.
            s.execute(delete(Return).where(Return.company_id == cid))
            s.execute(delete(CreditNote).where(CreditNote.company_id == cid))
            s.execute(
                delete(PaymentAllocation).where(PaymentAllocation.company_id == cid)
            )
            s.execute(delete(Payment).where(Payment.company_id == cid))
            s.execute(delete(Statement).where(Statement.company_id == cid))
            # The ladder is keyed on the account, so it goes immediately before
            # it. Raising *or* clearing a dispute creates this row even for an
            # account that never had one.
            s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
            s.execute(delete(CreditAccount).where(CreditAccount.company_id == cid))
            # Buyers can now be created with an opening invoice, and the
            # account references it, so invoices go after accounts and before
            # the buyers that own them.
            s.execute(delete(Invoice).where(Invoice.company_id == cid))
            s.execute(delete(Seller).where(Seller.company_id == cid))
            s.execute(delete(BuyerPhone).where(BuyerPhone.company_id == cid))
            s.execute(delete(Buyer).where(Buyer.company_id == cid))
            s.execute(delete(RoleGrant).where(RoleGrant.company_id == cid))
            # Users go last but one: `invoices.closed_by` and `promises.recorded_by`
            # both name the person who decided, so every row above has to have
            # gone first.
            s.execute(delete(User).where(User.company_id == cid))
            s.execute(delete(Company).where(Company.id == cid))
