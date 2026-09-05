"""Campaigns, ladder state and no-call days."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    CampaignStatus,
    EscalationLevel,
    TimestampMixin,
    _uuid_pk,
    campaign_status_t,
    escalation_level_t,
)

if TYPE_CHECKING:
    from app.models.identity import Company
    from app.models.trade import CreditAccount


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("window_end > window_start", name="ck_window_ordered"),
        # A ceiling the operator cannot raise. Recovery-contact hours are a system
        # property, not a per-campaign preference.
        CheckConstraint(
            "window_start >= TIME '08:00' AND window_end <= TIME '19:00'",
            name="ck_window_within_permitted_hours",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        campaign_status_t, default=CampaignStatus.DRAFT, nullable=False
    )
    caller_id_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("caller_ids.id"))

    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    window_start: Mapped[time] = mapped_column(Time, default=time(10, 0), nullable=False)
    window_end: Mapped[time] = mapped_column(Time, default=time(19, 0), nullable=False)
    call_on_weekends: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Caps are enforced per *buyer*, across every campaign, so two campaigns cannot
    # combine to exceed what one allows.
    max_attempts_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts_per_week: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    min_hours_between_calls: Mapped[int] = mapped_column(Integer, default=24, nullable=False)

    # Channels this campaign may use, in ladder order. Voice is not the only rung.
    channels: Mapped[list] = mapped_column(
        JSONB, default=lambda: ["SMS", "WHATSAPP", "EMAIL", "VOICE"], nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="campaigns")
    targets: Mapped[list[CampaignTarget]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignTarget(Base, TimestampMixin):
    """Campaign membership, keyed on the buyer.

    The buyer is the schedulable unit rather than the credit account: a debtor with
    five overdue invoices is still one person with one phone. The Policy Engine
    picks which account to speak about; the scheduler decides when to ring.

    The partial unique index on active membership stops two campaigns from both
    owning the same buyer, which would make per-buyer caps a contention problem
    rather than a rule.
    """

    __tablename__ = "campaign_targets"
    __table_args__ = (
        UniqueConstraint("campaign_id", "buyer_id"),
        Index(
            "uq_active_target_per_buyer",
            "buyer_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    campaign: Mapped[Campaign] = relationship(back_populates="targets")


class EscalationState(Base, TimestampMixin):
    """Position on the L1 → L2 → L3 ladder for one credit account.

    Separate from CreditAccount so the ladder has its own audit trail and can be
    reset (dispute, partial payment, promise) without touching the ledger.

    Note `delivered_at_level` alongside `attempts_at_level`: escalating to legal
    content because three calls went unanswered is both unfair and evidentially
    weak. Advancement requires contact actually having been made.
    """

    __tablename__ = "escalation_states"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_accounts.id"), unique=True, nullable=False
    )
    level: Mapped[EscalationLevel] = mapped_column(
        escalation_level_t, default=EscalationLevel.L1, nullable=False
    )
    level_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    attempts_at_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_at_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    history: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    account: Mapped[CreditAccount] = relationship(back_populates="escalation")


class BlackoutDate(Base, TimestampMixin):
    """Company-scoped no-call days. Collection calls on a major festival generate
    complaints that cost more than the day of calling was worth."""

    __tablename__ = "blackout_dates"
    __table_args__ = (UniqueConstraint("company_id", "day"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str | None] = mapped_column(String(100))
