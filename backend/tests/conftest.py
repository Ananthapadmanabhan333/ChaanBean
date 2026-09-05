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
    Buyer,
    BuyerPhone,
    Company,
    CompanyProfile,
    CreditAccount,
    CreditAssessment,
    Invoice,
    DndStatus,
    RoleGrant,
    User,
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
            # The creditor profile references the company, so it has to go
            # before the company does.
            s.execute(delete(CompanyProfile).where(CompanyProfile.company_id == cid))
            s.execute(delete(CreditAssessment).where(CreditAssessment.company_id == cid))
            s.execute(delete(CreditAccount).where(CreditAccount.company_id == cid))
            # Buyers can now be created with an opening invoice, and the
            # account references it, so invoices go after accounts and before
            # the buyers that own them.
            s.execute(delete(Invoice).where(Invoice.company_id == cid))
            s.execute(delete(BuyerPhone).where(BuyerPhone.company_id == cid))
            s.execute(delete(Buyer).where(Buyer.company_id == cid))
            s.execute(delete(RoleGrant).where(RoleGrant.company_id == cid))
            s.execute(delete(User).where(User.company_id == cid))
            s.execute(delete(Company).where(Company.id == cid))
