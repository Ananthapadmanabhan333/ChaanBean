"""Buyer statements.

A statement that does not balance is worse than no statement: it will be sent to
a debtor, disputed, and then used as evidence that the sender's books are
unreliable. So the generator asserts reconciliation and raises rather than emit
one that does not add up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CreditNote,
    Invoice,
    Payment,
    PaymentAllocation,
    Statement,
)
from app.trade import VOID_STATUSES


class StatementImbalance(AssertionError):
    """Opening plus movements did not equal closing."""


@dataclass(frozen=True)
class StatementLine:
    on: date
    kind: str  # invoice | payment | credit_note
    reference: str
    debit_paise: int  # increases what the buyer owes
    credit_paise: int  # reduces it

    def as_dict(self) -> dict:
        return {
            "on": self.on.isoformat(),
            "kind": self.kind,
            "reference": self.reference,
            "debit_paise": self.debit_paise,
            "credit_paise": self.credit_paise,
        }


@dataclass(frozen=True)
class StatementResult:
    buyer_id: UUID
    period_start: date
    period_end: date
    opening_paise: int
    closing_paise: int
    lines: tuple[StatementLine, ...]

    @property
    def debits_paise(self) -> int:
        return sum(line.debit_paise for line in self.lines)

    @property
    def credits_paise(self) -> int:
        return sum(line.credit_paise for line in self.lines)


def _movements(session: Session, buyer_id: UUID, start: date | None, end: date):
    """Every ledger movement for a buyer up to `end`, optionally from `start`.

    Invoices are debits; payments and credit notes are credits. A payment counts
    only to the extent it was allocated, plus whatever sits on account — money
    received is money received, whether or not it has been applied yet.

    Only cancelled invoices are dropped, and the omission of WRITTEN_OFF is the
    point: writing a debt off is this side deciding to stop expecting the money,
    not the invoice ceasing to exist. The buyer still carries the payable, so a
    statement that quietly dropped it would disagree with their books. Worse,
    the filter runs on today's status while the period is in the past — excluding
    written-off invoices would make a February statement stop showing an invoice
    that was live in February, rewriting a document already sent to the debtor.
    See app.trade for the same decision as it applies to ageing and allocation.
    """
    lines: list[StatementLine] = []

    invoice_q = select(Invoice).where(
        Invoice.buyer_id == buyer_id,
        Invoice.issue_date <= end,
        Invoice.status.notin_(VOID_STATUSES),
    )
    if start is not None:
        invoice_q = invoice_q.where(Invoice.issue_date >= start)
    for inv in session.execute(invoice_q).scalars():
        lines.append(
            StatementLine(inv.issue_date, "invoice", inv.invoice_number, inv.net_paise, 0)
        )

    payment_q = select(Payment).where(
        Payment.buyer_id == buyer_id, Payment.received_date <= end
    )
    if start is not None:
        payment_q = payment_q.where(Payment.received_date >= start)
    for pay in session.execute(payment_q).scalars():
        lines.append(
            StatementLine(
                pay.received_date,
                "payment",
                pay.reference or str(pay.id)[:8],
                0,
                pay.amount_paise,
            )
        )

    note_q = select(CreditNote).where(
        CreditNote.buyer_id == buyer_id, CreditNote.issue_date <= end
    )
    if start is not None:
        note_q = note_q.where(CreditNote.issue_date >= start)
    for note in session.execute(note_q).scalars():
        lines.append(
            StatementLine(note.issue_date, "credit_note", note.note_number, 0, note.amount_paise)
        )

    return sorted(lines, key=lambda line: (line.on, line.kind, line.reference))


def balance_as_of(session: Session, buyer_id: UUID, as_of: date) -> int:
    """Everything invoiced up to a date, less everything paid or credited."""
    lines = _movements(session, buyer_id, None, as_of)
    return sum(line.debit_paise for line in lines) - sum(line.credit_paise for line in lines)


def generate(
    session: Session, buyer_id: UUID, period_start: date, period_end: date
) -> StatementResult:
    if period_end < period_start:
        raise ValueError("period_end precedes period_start")

    opening = balance_as_of(session, buyer_id, date.fromordinal(period_start.toordinal() - 1))
    lines = tuple(_movements(session, buyer_id, period_start, period_end))
    closing = balance_as_of(session, buyer_id, period_end)

    movement = sum(line.debit_paise for line in lines) - sum(line.credit_paise for line in lines)
    if opening + movement != closing:
        raise StatementImbalance(
            f"statement does not reconcile: opening {opening} + movements {movement} "
            f"!= closing {closing} (difference {closing - opening - movement} paise)"
        )

    return StatementResult(buyer_id, period_start, period_end, opening, closing, lines)


def persist(session: Session, company_id: UUID, result: StatementResult) -> Statement:
    statement = Statement(
        company_id=company_id,
        buyer_id=result.buyer_id,
        period_start=result.period_start,
        period_end=result.period_end,
        opening_paise=result.opening_paise,
        closing_paise=result.closing_paise,
        lines=[line.as_dict() for line in result.lines],
    )
    session.add(statement)
    session.flush()
    return statement
