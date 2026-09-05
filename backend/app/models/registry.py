"""The critical-dues registry, and skip-tracing.

Read the design principle before changing anything here.

**Every gate is a barrier to publication, never to removal.** Listing is slow,
evidenced and reversible; delisting is fast and can be triggered by the listed
party. When the two are asymmetric in that direction an error is a delay. The
other way round, an error is a defamation claim — because a listing is a
published statement that a named business owes money.

Skip-tracing carries its own constraint: under the DPDP Act 2023, finding
contact details is personal-data processing that needs a lawful basis and a
stated purpose. `trace_requests` therefore requires both, per request. That is
not bureaucracy; it is the difference between a recovery tool and a lookup
service, and it is the first thing anyone reviewing a complaint asks to see.
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
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, _uuid_pk


class RegistryListing(Base, TimestampMixin):
    __tablename__ = "registry_listings"
    __table_args__ = (Index("ix_registry_status", "status", "published_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_profiles.id"), nullable=False
    )
    listed_by_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_accounts.id"))
    notice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("legal_notices.id"))

    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    oldest_due_date: Mapped[date | None] = mapped_column(Date)

    # PROPOSED -> EVIDENCE_REVIEW -> PUBLISHED -> {DISPUTED, DELISTED}
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED", nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delist_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def is_public(self) -> bool:
        return self.status == "PUBLISHED" and self.delisted_at is None


class ListingEvidence(Base, TimestampMixin):
    """The specific artefacts behind a listing, not a summary of them."""

    __tablename__ = "listing_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registry_listings.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ListingDispute(Base, TimestampMixin):
    """Raised by the listed party.

    Raising one suspends publication immediately. The claim is examined while it
    is *not* being published, never while it is — which is the same asymmetry
    the whole module is built on.
    """

    __tablename__ = "listing_disputes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registry_listings.id"), nullable=False
    )
    raised_by: Mapped[str | None] = mapped_column(String(200))
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    documents: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ListingAudit(Base):
    """Append-only. Every state change, with actor and reason."""

    __tablename__ = "listing_audit"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registry_listings.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TraceRequest(Base, TimestampMixin):
    """A stated reason and lawful basis, per request. Required, not optional."""

    __tablename__ = "trace_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    lawful_basis: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceResult(Base, TimestampMixin):
    __tablename__ = "trace_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trace_requests.id"), nullable=False
    )
    contact_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(String(400), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_method: Mapped[str | None] = mapped_column(String(40))


class TraceAudit(Base):
    """Append-only: every source queried and every result returned."""

    __tablename__ = "trace_audit"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trace_requests.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    lawful_basis: Mapped[str | None] = mapped_column(String(80))
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
