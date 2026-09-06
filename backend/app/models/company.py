"""Company intelligence: resolved entities, source records, verification reports.

The hard part of this domain is not fetching — that is an HTTP call. It is
deciding that "Sharma Trading Co", "Sharma Trading Company Pvt Ltd" and GSTIN
27AABCS1429B1ZR are the same legal entity, or importantly that they are *not*.

Getting it wrong is not a data-quality issue. Attaching one company's tax or
legal record to another is a defamation exposure, and the same failure is what
makes the legal-history and registry modules dangerous.

PAN is stored as a hash plus the last four characters, never in the clear. It is
a national identifier and a database dump should not leak one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
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


class CompanyProfile(Base, TimestampMixin):
    """A resolved legal entity."""

    __tablename__ = "company_profiles"
    __table_args__ = (
        Index("ix_company_profiles_gstin", "company_id", "gstin"),
        Index("ix_company_profiles_cin", "company_id", "cin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buyers.id"))

    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    trade_names: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    additional_gstins: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    cin: Mapped[str | None] = mapped_column(String(21))
    # Never the full number in the clear.
    pan_hash: Mapped[str | None] = mapped_column(String(64))
    pan_last4: Mapped[str | None] = mapped_column(String(4))

    registered_address: Mapped[str | None] = mapped_column(Text)
    state_code: Mapped[str | None] = mapped_column(String(2))
    incorporation_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(40))  # Active, Struck Off, …

    # How sure we are this profile is one real entity. Below the top tier it may
    # not be used for anything published.
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    resolution_tier: Mapped[str | None] = mapped_column(String(24))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GstRecord(Base, TimestampMixin):
    __tablename__ = "gst_records"
    __table_args__ = (UniqueConstraint("company_id", "gstin", "fetched_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company_profiles.id"))
    gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    trade_name: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str | None] = mapped_column(String(40))
    registration_date: Mapped[date | None] = mapped_column(Date)
    address: Mapped[str | None] = mapped_column(Text)
    filing_history: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # REGISTRY or USER_PROVIDED — see app.providers.base. These rows are
    # append-only evidence, so a year from now "what did we know in March" has to
    # distinguish a record fetched from the register from one a person read off a
    # portal and typed. Without this column the two are indistinguishable, and a
    # self-declared GSTIN quietly acquires the standing of a verified one.
    #
    # Defaults to USER_PROVIDED, not REGISTRY: a writer that forgets to say where
    # its data came from has not earned the stronger label, and understating
    # provenance costs a re-verification while overstating it publishes a claim
    # about a company nobody checked.
    provenance: Mapped[str] = mapped_column(
        String(16), default="USER_PROVIDED", server_default="USER_PROVIDED", nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class McaRecord(Base, TimestampMixin):
    __tablename__ = "mca_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company_profiles.id"))
    cin: Mapped[str] = mapped_column(String(21), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str | None] = mapped_column(String(40))
    incorporation_date: Mapped[date | None] = mapped_column(Date)
    registered_address: Mapped[str | None] = mapped_column(Text)
    directors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    charges: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    paid_up_capital_paise: Mapped[int | None] = mapped_column(String(32))
    # As on GstRecord: where this came from, defaulting to the weaker claim.
    provenance: Mapped[str] = mapped_column(
        String(16), default="USER_PROVIDED", server_default="USER_PROVIDED", nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EntityCandidate(Base, TimestampMixin):
    """A proposed match awaiting confirmation.

    Candidates exist rather than auto-merging because a wrong merge attaches one
    company's record to another, and that is the failure this whole module is
    arranged to avoid.
    """

    __tablename__ = "entity_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    candidate_name: Mapped[str] = mapped_column(String(300), nullable=False)
    identifier_kind: Mapped[str | None] = mapped_column(String(10))  # gstin | cin | pan
    identifier_value: Mapped[str | None] = mapped_column(String(21))
    score: Mapped[float] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    tier: Mapped[str | None] = mapped_column(String(24))
    signals: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationReport(Base, TimestampMixin):
    """A point-in-time report. Immutable once issued — it is what was claimed."""

    __tablename__ = "verification_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_profiles.id"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    issued_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
