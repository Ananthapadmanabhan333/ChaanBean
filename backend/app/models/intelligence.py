"""Risk scores, payment behaviour and promises.

Scores are **append-only history**, never updated in place. You have to be able
to answer "what did we know when we escalated this account on 12 March", and an
overwritten score cannot.

Every score carries its factors — name, value, weight, contribution and a
sentence of explanation. A score that changes how someone is treated without a
stated reason is not defensible, which is why this is a transparent weighted
rules engine rather than a model.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, _uuid_pk


class RiskScore(Base):
    __tablename__ = "risk_scores"
    __table_args__ = (
        Index("ix_risk_scores_subject", "subject_type", "subject_id", "computed_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    band: Mapped[str] = mapped_column(String(16), nullable=False)
    factors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    model_version: Mapped[str] = mapped_column(String(24), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentBehaviour(Base, TimestampMixin):
    """Derived from your own ledger — the most predictive data you hold, and the
    only data no competitor can buy."""

    __tablename__ = "payment_behaviours"
    __table_args__ = (Index("ix_payment_behaviour_buyer", "buyer_id", "as_of"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)

    invoices_settled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mean_days_to_pay: Mapped[float | None] = mapped_column(Numeric(7, 2))
    median_days_to_pay: Mapped[float | None] = mapped_column(Numeric(7, 2))
    # A debtor drifting from 30 to 75 days is the earliest reliable distress
    # signal in the system.
    days_to_pay_trend: Mapped[float | None] = mapped_column(Numeric(7, 2))
    part_payment_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))
    promise_kept_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))
    dispute_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))
    contact_response_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))


class CreditAssessment(Base):
    """A creditworthiness assessment, and what the human decided afterwards.

    Append-only, like RiskScore and for the same reason: you have to be able to
    answer "what did we know when we gave them a 5 lakh limit in March", and an
    overwritten row cannot.

    The declared application is stored alongside the result. Without it the
    score is unreconstructable — the inputs are the buyer's own claims at a
    point in time, and they change.

    `decided_*` is deliberately separate from `suggested_limit_paise`. The
    system recommends; a person decides; both are recorded. When those two
    disagree, that disagreement is the audit trail.
    """

    __tablename__ = "credit_assessments"
    __table_args__ = (
        Index("ix_credit_assessments_buyer", "company_id", "buyer_id", "computed_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)

    requested_limit_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    suggested_limit_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False)

    factors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    blockers: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    application: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    model_version: Mapped[str] = mapped_column(String(24), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))


class Promise(Base, TimestampMixin):
    """A promise to pay, and whether it was kept.

    The kept-rate is the single strongest predictor of whether the next promise
    means anything.
    """

    __tablename__ = "promises"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_accounts.id"))
    promised_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promised_on: Mapped[date] = mapped_column(Date, nullable=False)
    promised_by_date: Mapped[date] = mapped_column(Date, nullable=False)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # OPEN -> KEPT | BROKEN
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
