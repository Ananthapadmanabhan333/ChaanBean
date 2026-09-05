"""Templates, their approved versions, and rendered audio."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    AssetStatus,
    Base,
    Channel,
    EscalationLevel,
    MessageStatus,
    TimestampMixin,
    _uuid_pk,
    asset_status_t,
    channel_t,
    escalation_level_t,
    message_status_t,
)


class MessageTemplate(Base, TimestampMixin):
    """The current pointer. The text that was actually spoken lives in
    `TemplateVersion`, which is append-only."""

    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("company_id", "key", "language"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[EscalationLevel] = mapped_column(escalation_level_t, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default="VOICE", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("template_versions.id", use_alter=True, name="fk_template_current_version")
    )

    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template", foreign_keys="TemplateVersion.template_id"
    )


class TemplateVersion(Base, TimestampMixin):
    """Append-only. Never updated, never deleted.

    An L3 version without `approved_by` and `approved_at` is refused by the Policy
    Engine. The engine may select approved legal content; it may never improvise it.

    Approval lives here rather than on `MessageTemplate` so that "who approved the
    words played on 12 March" survives someone editing the template afterwards.
    """

    __tablename__ = "template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message_templates.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # "{buyer_name} … {amount_words}"
    voice_id: Mapped[str] = mapped_column(String(40), default="Kajal", nullable=False)
    engine: Mapped[str] = mapped_column(String(20), default="neural", nullable=False)

    # TRAI DLT: an SMS template without a registered id is unsendable. That is a
    # Policy Engine refusal, not a provider error discovered at send time.
    dlt_template_id: Mapped[str | None] = mapped_column(String(64))
    dlt_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by_label: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    template: Mapped[MessageTemplate] = relationship(
        back_populates="versions", foreign_keys=[template_id]
    )

    @property
    def is_approved(self) -> bool:
        return self.approved_by is not None and self.approved_at is not None


class AudioAsset(Base, TimestampMixin):
    """One rendered message.

    The cache key is `(company_id, message_hash)` and `company_id` is folded into
    the hash input. Two tenants with identical template text must not share a
    stored object — otherwise one tenant's call evidence points at another
    tenant's file.

    `status` exists so TTS happens at schedule time, never on the call path. A call
    may not leave SCHEDULED until its asset is READY and staged on the Asterisk box.
    """

    __tablename__ = "audio_assets"
    __table_args__ = (UniqueConstraint("company_id", "message_hash"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    final_text: Mapped[str] = mapped_column(Text, nullable=False)
    voice_id: Mapped[str] = mapped_column(String(40), nullable=False)
    engine: Mapped[str] = mapped_column(String(20), default="neural", nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, default=8000, nullable=False)

    status: Mapped[AssetStatus] = mapped_column(
        asset_status_t, default=AssetStatus.PENDING, nullable=False
    )
    storage_key: Mapped[str | None] = mapped_column(String(512))
    content_sha256: Mapped[str | None] = mapped_column(String(64))  # tamper evidence
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    @property
    def is_ready(self) -> bool:
        return self.status is AssetStatus.READY and self.storage_key is not None


class Message(Base, TimestampMixin):
    """One outbound SMS, WhatsApp message or email.

    Deliberately the same shape as `Call`: derived idempotency key, evidence
    fields pointing at the approved words that were sent, and delivery recorded
    as separate facts rather than one boolean.

    Idempotency matters more here than for voice. A duplicate SMS to a debtor is
    a compliance incident rather than a duplicate row, and messaging retries are
    far more frequent than call retries.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_buyer_created", "buyer_id", "created_at"),
        Index("ix_messages_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"))
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_accounts.id"))
    phone_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buyer_phones.id"))

    channel: Mapped[Channel] = mapped_column(channel_t, nullable=False)
    level: Mapped[EscalationLevel] = mapped_column(escalation_level_t, nullable=False)
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("template_versions.id")
    )
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)
    message_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[MessageStatus] = mapped_column(
        message_status_t, default=MessageStatus.QUEUED, nullable=False
    )
    block_reason: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    cost_paise: Mapped[int | None] = mapped_column(BigInteger)
    counts_against_cap: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(Text)

    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    @property
    def sent(self) -> bool:
        return self.sent_at is not None

    @property
    def delivered(self) -> bool:
        """Handed to the recipient's device. Not the same as read, and neither is
        the same as sent — never collapse them into one number."""
        return self.delivered_at is not None

    @property
    def read(self) -> bool:
        return self.read_at is not None


class MessageEvent(Base):
    """Append-only. Provider webhooks arrive late, duplicated and out of order
    exactly like ARI events."""

    __tablename__ = "message_events"
    __table_args__ = (
        Index("ix_message_events_message", "message_id", "occurred_at"),
        UniqueConstraint("message_id", "source", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChannelOptOut(Base, TimestampMixin):
    """STOP on SMS suppresses SMS. It does not silently suppress every channel —
    and it does not withdraw consent to be contacted at all, which is a separate,
    stronger act recorded on the buyer."""

    __tablename__ = "channel_opt_outs"
    __table_args__ = (UniqueConstraint("buyer_id", "channel"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    channel: Mapped[Channel] = mapped_column(channel_t, nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))  # sms_stop | webhook | portal
    opted_out_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
