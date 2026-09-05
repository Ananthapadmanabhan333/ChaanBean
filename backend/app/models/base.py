"""Shared declarative base, mixins and enums.

Money is stored in paise as an integer. Never float. An outstanding amount that
drifts by a rounding error is an amount you cannot put in a legal notice.

Times are stored as timezone-aware UTC. A calling window is resolved against the
campaign's timezone at decision time, not at storage time.

Every SQL enum type is instantiated **once** here and reused across columns. A
fresh `Enum(...)` per column makes PostgreSQL attempt `CREATE TYPE` more than
once for the same type name and schema creation fails — `escalation_level` alone
is referenced by four tables.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- enums


class EscalationLevel(str, enum.Enum):
    L1 = "L1"  # courtesy reminder
    L2 = "L2"  # firm escalation
    L3 = "L3"  # legal / compliance — pre-approved content, DTMF-gated


class DndStatus(str, enum.Enum):
    """Fail closed: UNKNOWN blocks the call, same as REGISTERED."""

    UNKNOWN = "UNKNOWN"
    REGISTERED = "REGISTERED"  # on the registry — do not call
    CLEAR = "CLEAR"  # scrubbed and callable


class AccountStatus(str, enum.Enum):
    CURRENT = "CURRENT"
    OVERDUE = "OVERDUE"
    IN_DISPUTE = "IN_DISPUTE"  # halts all automated contact
    SETTLED = "SETTLED"
    WRITTEN_OFF = "WRITTEN_OFF"


class InvoiceStatus(str, enum.Enum):
    OPEN = "OPEN"
    PART_PAID = "PART_PAID"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    WRITTEN_OFF = "WRITTEN_OFF"


class AgeingBucket(str, enum.Enum):
    CURRENT = "CURRENT"  # not yet due
    B1_30 = "B1_30"
    B31_60 = "B31_60"
    B61_90 = "B61_90"
    B90_PLUS = "B90_PLUS"


class AllocationRule(str, enum.Enum):
    """How a payment came to be applied to an invoice.

    Recorded because a debtor who intended to pay a specific invoice will say so
    later, and "the system chose" is not an answer.
    """

    MANUAL = "MANUAL"  # the user said so
    OLDEST_FIRST = "OLDEST_FIRST"  # default when no instruction arrived
    EXACT_MATCH = "EXACT_MATCH"  # amount matched one invoice exactly
    REFERENCE = "REFERENCE"  # the remittance quoted an invoice number


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class CallStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    DIALING = "DIALING"
    ANSWERED = "ANSWERED"
    ANSWERED_MACHINE = "ANSWERED_MACHINE"  # AMD fired; not a delivery
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"  # carrier/network failure — see AttemptClass
    BLOCKED = "BLOCKED"  # policy refused; never reached the carrier
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # originated, outcome unrecoverable — counts as an attempt

    @property
    def is_terminal(self) -> bool:
        return self not in (CallStatus.SCHEDULED, CallStatus.DIALING)


class MessageStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class Channel(str, enum.Enum):
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    VOICE = "VOICE"


class AttemptClass(str, enum.Enum):
    """Whether an outcome consumes the debtor's frequency budget.

    Congestion is *our carrier* failing, not the debtor's phone ringing. Charging
    it against a per-buyer cap lets one bad carrier hour silently cancel a day of
    collections.
    """

    COUNTS = "COUNTS"  # the phone rang, or we cannot prove it didn't
    CARRIER_FAULT = "CARRIER_FAULT"  # does not consume the cap; retry with backoff
    TERMINAL_BAD_NUMBER = "TERMINAL_BAD_NUMBER"  # counts once, then retire the number


class EventSource(str, enum.Enum):
    ARI = "ari"
    CDR = "cdr"
    WEBHOOK = "webhook"
    WORKER = "worker"


class AssetStatus(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


# ----------------------------------------------------------------- sql enum types

escalation_level_t = Enum(EscalationLevel, name="escalation_level")
invoice_status_t = Enum(InvoiceStatus, name="invoice_status")
ageing_bucket_t = Enum(AgeingBucket, name="ageing_bucket")
allocation_rule_t = Enum(AllocationRule, name="allocation_rule")
dnd_status_t = Enum(DndStatus, name="dnd_status")
account_status_t = Enum(AccountStatus, name="account_status")
campaign_status_t = Enum(CampaignStatus, name="campaign_status")
call_status_t = Enum(CallStatus, name="call_status")
message_status_t = Enum(MessageStatus, name="message_status")
channel_t = Enum(Channel, name="channel")
event_source_t = Enum(EventSource, name="event_source")
asset_status_t = Enum(AssetStatus, name="asset_status")
