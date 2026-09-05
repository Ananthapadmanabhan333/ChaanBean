"""Debtors, their phone numbers, and the ledger of what they owe.

The ledger is the data spine: recovery cases hang off invoices, calls hang off
cases, legal action hangs off calls. If the ledger is wrong, every layer above it
is confidently wrong — and you are telling a debtor they owe money they have
already paid.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    AccountStatus,
    AllocationRule,
    Base,
    DndStatus,
    InvoiceStatus,
    TimestampMixin,
    _uuid_pk,
    account_status_t,
    allocation_rule_t,
    dnd_status_t,
    invoice_status_t,
)

if TYPE_CHECKING:
    from app.models.identity import Company
    from app.models.recovery import EscalationState


class Buyer(Base, TimestampMixin):
    __tablename__ = "buyers"
    __table_args__ = (
        UniqueConstraint("company_id", "external_ref"),
        Index("ix_buyers_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(100))  # id in their ERP
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))

    # Consent is a property of the person, not of a number.
    consent_withdrawn: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The buyer is the scheduling unit: one conversation at a time with one person,
    # however many invoices they owe. See `next_action_at` on this row, not on the
    # per-account escalation ladder.
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    company: Mapped[Company] = relationship(back_populates="buyers")
    phones: Mapped[list[BuyerPhone]] = relationship(
        back_populates="buyer", cascade="all, delete-orphan"
    )
    accounts: Mapped[list[CreditAccount]] = relationship(back_populates="buyer")


class BuyerPhone(Base, TimestampMixin):
    """DND status belongs here, not on Buyer — it is a property of the number.

    A debtor with a scrubbed mobile and a clear office landline is the normal case,
    and putting the flag on the buyer either blocks a callable number or dials a
    scrubbed one.
    """

    __tablename__ = "buyer_phones"
    __table_args__ = (UniqueConstraint("buyer_id", "e164"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    e164: Mapped[str] = mapped_column(String(20), nullable=False)
    number_type: Mapped[str | None] = mapped_column(String(20))  # mobile | fixed_line
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    dnd_status: Mapped[DndStatus] = mapped_column(
        dnd_status_t, default=DndStatus.UNKNOWN, nullable=False
    )
    dnd_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_outcome: Mapped[str | None] = mapped_column(String(32))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    buyer: Mapped[Buyer] = relationship(back_populates="phones")


class CreditAccount(Base, TimestampMixin):
    """The recovery-facing projection of an invoice: what is owed, since when, and
    where it stands with collections."""

    __tablename__ = "credit_accounts"
    __table_args__ = (
        CheckConstraint("outstanding_paise >= 0", name="ck_outstanding_non_negative"),
        Index("ix_credit_accounts_buyer", "buyer_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), unique=True)
    invoice_ref: Mapped[str | None] = mapped_column(String(100))
    outstanding_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        account_status_t, default=AccountStatus.OVERDUE, nullable=False
    )
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disputed_reason: Mapped[str | None] = mapped_column(Text)

    buyer: Mapped[Buyer] = relationship(back_populates="accounts")
    escalation: Mapped[EscalationState | None] = relationship(
        back_populates="account", uselist=False
    )

    def days_past_due(self, now: datetime) -> int:
        return max(0, (now - self.due_date).days)


# ------------------------------------------------------------------- the ledger


class Seller(Base, TimestampMixin):
    """A selling entity belonging to the company. One company may invoice under
    several — different GSTINs, divisions or legal entities."""

    __tablename__ = "sellers"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Invoice(Base, TimestampMixin):
    """One invoice.

    outstanding_paise is a cached projection, not a field to update in place.
    app.trade.allocation.recompute_invoice is its only writer; two code paths
    adjusting a balance is how ledgers drift.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("company_id", "invoice_number"),
        CheckConstraint("gross_paise >= 0", name="ck_invoice_gross_non_negative"),
        CheckConstraint("net_paise >= 0", name="ck_invoice_net_non_negative"),
        CheckConstraint("outstanding_paise >= 0", name="ck_invoice_outstanding_non_negative"),
        CheckConstraint(
            "outstanding_paise <= net_paise", name="ck_invoice_outstanding_within_net"
        ),
        Index("ix_invoices_buyer_due", "buyer_id", "due_date"),
        Index("ix_invoices_company_status", "company_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    seller_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sellers.id"))
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)

    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    gross_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    net_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outstanding_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        invoice_status_t, default=InvoiceStatus.OPEN, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.status in (InvoiceStatus.OPEN, InvoiceStatus.PART_PAID)


class InvoiceLine(Base, TimestampMixin):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Quantity in thousandths, so 1.5 units is 1500 and nothing here is a float.
    quantity_milli: Mapped[int] = mapped_column(BigInteger, default=1000, nullable=False)
    rate_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_rate_bp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # basis points
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Payment(Base, TimestampMixin):
    """Money received.

    unallocated_paise is on-account credit: visible and applicable, never
    silently absorbed into whatever invoice happened to be nearest.
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payment_positive"),
        CheckConstraint("unallocated_paise >= 0", name="ck_payment_unallocated_non_negative"),
        CheckConstraint(
            "unallocated_paise <= amount_paise", name="ck_payment_unallocated_within_amount"
        ),
        Index("ix_payments_buyer_received", "buyer_id", "received_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unallocated_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str | None] = mapped_column(String(32))  # neft | upi | cheque | cash
    reference: Mapped[str | None] = mapped_column(String(100))
    external_ref: Mapped[str | None] = mapped_column(String(100))

    allocations: Mapped[list[PaymentAllocation]] = relationship(
        back_populates="payment", cascade="all, delete-orphan"
    )


class PaymentAllocation(Base, TimestampMixin):
    """Which payment settled which invoice, and by how much.

    Part-payment across several invoices is the norm in Indian B2B trade, not the
    exception, so this cannot collapse into a foreign key on the payment.
    """

    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id"),
        CheckConstraint("amount_paise > 0", name="ck_allocation_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule: Mapped[AllocationRule] = mapped_column(
        allocation_rule_t, default=AllocationRule.MANUAL, nullable=False
    )

    payment: Mapped[Payment] = relationship(back_populates="allocations")


class CreditNote(Base, TimestampMixin):
    """Reduces what is owed. Against a specific invoice, or on account."""

    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint("company_id", "note_number"),
        CheckConstraint("amount_paise > 0", name="ck_credit_note_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    note_number: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(100))


class Return(Base, TimestampMixin):
    """Goods sent back. Becomes a credit note once accepted."""

    __tablename__ = "returns"
    __table_args__ = (CheckConstraint("amount_paise >= 0", name="ck_return_non_negative"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_notes.id"))
    description: Mapped[str | None] = mapped_column(Text)
    quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)


class Statement(Base, TimestampMixin):
    """A generated point-in-time position for a buyer.

    Stored rather than recomputed on demand: a statement that was sent is
    evidence of what you claimed, and recomputing it later against a changed
    ledger would quietly rewrite history.
    """

    __tablename__ = "statements"
    __table_args__ = (Index("ix_statements_buyer_period", "buyer_id", "period_end"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    buyer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    opening_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    closing_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lines: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
