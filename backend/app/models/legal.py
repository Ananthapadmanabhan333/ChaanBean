"""Court records, the reviewable links to companies, and pre-legal work.

The design decision that keeps this safe: **a case is never directly
foreign-keyed to a company.** Every association is a `LegalLink` — a reviewable
claim carrying its evidence, which can be rejected without deleting the case.

Indian court records identify parties by name strings typed by court clerks,
with no GSTIN, no CIN and no stable identifier. "Sharma Traders" appears in
hundreds of cases belonging to dozens of unrelated businesses. Attaching a
recovery or criminal matter to the wrong business is defamation, and it is the
most likely way this platform gets sued.

**Design bias: prefer a missed match to a false one.** An incomplete legal
history is a product limitation. A wrong one is a lawsuit.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, _uuid_pk


class CourtCase(Base, TimestampMixin):
    __tablename__ = "court_cases"
    __table_args__ = (
        UniqueConstraint("company_id", "court_id", "case_number"),
        Index("ix_court_cases_filed", "company_id", "filing_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    court_id: Mapped[str] = mapped_column(String(64), nullable=False)
    court_name: Mapped[str | None] = mapped_column(String(200))
    case_number: Mapped[str] = mapped_column(String(100), nullable=False)
    case_type: Mapped[str | None] = mapped_column(String(64))
    filing_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(40))
    stage: Mapped[str | None] = mapped_column(String(80))
    next_hearing: Mapped[date | None] = mapped_column(Date)
    parties: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class CaseParty(Base):
    """A party exactly as the court recorded it, plus a normalised form.

    Both are kept. The normalised form is what matching works on; the original
    is what you show a reviewer, because "is this the same company" is a
    judgement made on what was actually filed.
    """

    __tablename__ = "case_parties"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("court_cases.id"), nullable=False)
    raw_name: Mapped[str] = mapped_column(String(400), nullable=False)
    normalised_name: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(40))  # petitioner | respondent | accused


class LegalLink(Base, TimestampMixin):
    """A reviewable claim that a case belongs to a company profile.

    Never a foreign key from case to company. Statuses are
    PROPOSED -> CONFIRMED | REJECTED, and only CONFIRMED links may appear in a
    report or influence a score.
    """

    __tablename__ = "legal_links"
    __table_args__ = (UniqueConstraint("case_id", "profile_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("court_cases.id"), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_profiles.id"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    signals: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)


class LegalHistory(Base, TimestampMixin):
    """A generated point-in-time report. Immutable once issued."""

    __tablename__ = "legal_histories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_profiles.id"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed_case_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Stated on the report itself: a history is only as complete as the links a
    # human confirmed, and saying so is the honest position.
    caveat: Mapped[str | None] = mapped_column(Text)


class PrelegalAssessment(Base, TimestampMixin):
    __tablename__ = "prelegal_assessments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_accounts.id"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    factors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    blockers: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    estimated_cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    recoverable_paise: Mapped[int | None] = mapped_column(BigInteger)
    limitation_expires_on: Mapped[date | None] = mapped_column(Date)
    limitation_urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assessed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class LegalNotice(Base, TimestampMixin):
    """A demand notice.

    `figures_snapshot` freezes what was claimed at the moment it was approved.
    The ledger keeps moving; the notice must not, or you cannot answer "what did
    this notice say" six months later.
    """

    __tablename__ = "legal_notices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_accounts.id"), nullable=False
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prelegal_assessments.id")
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("template_versions.id")
    )
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    figures_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # DRAFT -> APPROVED -> DISPATCHED -> {RESPONDED, EXPIRED} | WITHDRAWN
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_method: Mapped[str | None] = mapped_column(String(32))  # speed_post | email
    tracking_reference: Mapped[str | None] = mapped_column(String(100))
    delivery_proof: Mapped[dict | None] = mapped_column(JSONB)
    response_deadline: Mapped[date | None] = mapped_column(Date)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def delivered(self) -> bool:
        """Proof of delivery, not merely dispatch. The registry depends on this
        distinction and so does any court."""
        return bool(self.delivery_proof)


class LegalMatter(Base, TimestampMixin):
    """An escalated case, once an advocate has actually filed."""

    __tablename__ = "legal_matters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_accounts.id"), nullable=False
    )
    notice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_notices.id"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("court_cases.id"))
    advocate_name: Mapped[str | None] = mapped_column(String(200))
    advocate_contact: Mapped[str | None] = mapped_column(String(200))
    court_name: Mapped[str | None] = mapped_column(String(200))
    case_number: Mapped[str | None] = mapped_column(String(100))
    stage: Mapped[str | None] = mapped_column(String(80))
    hearings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PREPARING", nullable=False)
