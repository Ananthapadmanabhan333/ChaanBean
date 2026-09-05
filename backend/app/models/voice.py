"""Calls and the append-only event log they are projected from."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CallStatus,
    EscalationLevel,
    EventSource,
    TimestampMixin,
    _uuid_pk,
    call_status_t,
    escalation_level_t,
    event_source_t,
)


class Call(Base, TimestampMixin):
    """One attempt.

    Three delivery facts are tracked separately and never conflated:
      connected     — SIP 200 OK. What the carrier CDR agrees with.
      played        — PlaybackFinished with no preceding StasisEnd.
      acknowledged  — the debtor pressed a key. The only evidentially strong one.

    A call answered at second 0 and dropped at second 2 of a 22-second message
    connected and delivered nothing.
    """

    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_account_created", "account_id", "created_at"),
        Index("ix_calls_buyer_created", "buyer_id", "created_at"),
        Index("ix_calls_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credit_accounts.id"), nullable=False)
    phone_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buyer_phones.id"))

    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    level: Mapped[EscalationLevel] = mapped_column(escalation_level_t, nullable=False)
    to_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    from_e164: Mapped[str | None] = mapped_column(String(20))

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    originated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    playback_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[CallStatus] = mapped_column(
        call_status_t, default=CallStatus.SCHEDULED, nullable=False
    )
    playback_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dtmf_ack: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    block_reason: Mapped[str | None] = mapped_column(String(64))
    hangup_cause: Mapped[int | None] = mapped_column(Integer)  # Q.850
    sip_response_code: Mapped[int | None] = mapped_column(Integer)
    counts_against_cap: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Evidence: exactly what was played, and which approved words it came from.
    audio_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("audio_assets.id"))
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("template_versions.id")
    )
    message_hash: Mapped[str | None] = mapped_column(String(64))

    # `idempotency_key` is DERIVED, never random — a retrying worker must compute
    # the same value. It is also used as the ARI channelId, so Asterisk returns
    # 409 Conflict rather than placing a second call.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), index=True)

    @property
    def connected(self) -> bool:
        return self.answered_at is not None

    @property
    def delivered(self) -> bool:
        """Played to a human. A machine answering is not a delivery — counting it
        would let three voicemails escalate a debtor to legal content they never
        heard."""
        return (
            self.status is CallStatus.ANSWERED
            and self.playback_completed
            and not self.opted_out
        )

    @property
    def acknowledged(self) -> bool:
        return self.dtmf_ack


class CallEvent(Base):
    """Append-only event log. `Call` is a projection of this table.

    ARI does not replay missed events on reconnect — there is no cursor and no
    resume — so events arrive late, out of order, duplicated, or never. The
    projection must therefore be idempotent and order-independent, and status may
    only advance forward.
    """

    __tablename__ = "call_events"
    __table_args__ = (
        Index("ix_call_events_call", "call_id", "occurred_at"),
        UniqueConstraint("call_id", "source", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), nullable=False)
    source: Mapped[EventSource] = mapped_column(event_source_t, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
