"""Applying money to invoices.

A payment is not "against an invoice". A 50,000 rupee payment against three
invoices of 30,000, 15,000 and 20,000 needs an allocation table, because
part-payment is the norm in Indian B2B trade rather than the exception.

Two rules shape everything here:

* **`outstanding_paise` is derived, never edited in place.** `recompute_invoice`
  is its only writer. Two code paths adjusting a balance is how ledgers drift,
  and a drifted ledger is how you tell a debtor they owe money they have paid.
* **Over-allocation is an error, not a negative balance.** Money that cannot be
  applied stays on the payment as on-account credit — visible and applicable,
  never silently absorbed.

The planning half is pure: `plan_allocation` takes values and returns a plan, so
every allocation decision is testable and reproducible without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AllocationRule,
    CreditNote,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
)


class AllocationError(ValueError):
    """Refused: the requested allocation would corrupt the ledger."""


# ---------------------------------------------------------------- pure planning


@dataclass(frozen=True)
class InvoiceRef:
    id: UUID
    invoice_number: str
    issue_date: date
    due_date: date
    net_paise: int
    allocated_paise: int
    credited_paise: int

    @property
    def outstanding_paise(self) -> int:
        return self.net_paise - self.allocated_paise - self.credited_paise


@dataclass(frozen=True)
class Allocation:
    invoice_id: UUID
    amount_paise: int
    rule: AllocationRule


@dataclass(frozen=True)
class AllocationPlan:
    allocations: tuple[Allocation, ...]
    unallocated_paise: int

    @property
    def allocated_paise(self) -> int:
        return sum(a.amount_paise for a in self.allocations)


def _oldest_first(invoices):
    # Due date, then issue date, then number: a total order, so two runs over the
    # same data always allocate the same way.
    return sorted(invoices, key=lambda i: (i.due_date, i.issue_date, i.invoice_number))


def plan_allocation(
    amount_paise: int,
    invoices,
    *,
    instructions: dict[UUID, int] | None = None,
) -> AllocationPlan:
    """Decide how one payment lands across a buyer's open invoices.

    `instructions` is the user's explicit intent and always wins. Without it the
    default is oldest-invoice-first, recorded as such — a debtor who meant to pay
    a specific invoice will say so later, and "the system chose" is not an answer.
    """
    if amount_paise <= 0:
        raise AllocationError("payment amount must be positive")

    open_invoices = [i for i in invoices if i.outstanding_paise > 0]
    by_id = {i.id: i for i in invoices}

    if instructions:
        allocations = []
        total = 0
        for invoice_id, requested in instructions.items():
            if requested <= 0:
                raise AllocationError(f"allocation to {invoice_id} must be positive")
            invoice = by_id.get(invoice_id)
            if invoice is None:
                raise AllocationError(f"invoice {invoice_id} is not this buyer's")
            if requested > invoice.outstanding_paise:
                raise AllocationError(
                    f"cannot allocate {requested} to invoice {invoice.invoice_number}: "
                    f"only {invoice.outstanding_paise} outstanding"
                )
            total += requested
            allocations.append(Allocation(invoice_id, requested, AllocationRule.MANUAL))
        if total > amount_paise:
            raise AllocationError(
                f"allocations total {total} but the payment is only {amount_paise}"
            )
        return AllocationPlan(tuple(allocations), amount_paise - total)

    # An amount that exactly matches one open invoice is almost always meant for
    # it, whatever its age.
    exact = [i for i in open_invoices if i.outstanding_paise == amount_paise]
    if len(exact) == 1:
        return AllocationPlan(
            (Allocation(exact[0].id, amount_paise, AllocationRule.EXACT_MATCH),), 0
        )

    remaining = amount_paise
    allocations = []
    for invoice in _oldest_first(open_invoices):
        if remaining <= 0:
            break
        take = min(remaining, invoice.outstanding_paise)
        allocations.append(Allocation(invoice.id, take, AllocationRule.OLDEST_FIRST))
        remaining -= take

    return AllocationPlan(tuple(allocations), remaining)


# -------------------------------------------------------------- the only writer


def _sum_allocations(session: Session, invoice_id: UUID) -> int:
    return (
        session.execute(
            select(func.coalesce(func.sum(PaymentAllocation.amount_paise), 0)).where(
                PaymentAllocation.invoice_id == invoice_id
            )
        ).scalar_one()
        or 0
    )


def _sum_credit_notes(session: Session, invoice_id: UUID) -> int:
    return (
        session.execute(
            select(func.coalesce(func.sum(CreditNote.amount_paise), 0)).where(
                CreditNote.invoice_id == invoice_id
            )
        ).scalar_one()
        or 0
    )


def recompute_invoice(session: Session, invoice: Invoice) -> Invoice:
    """Recompute `outstanding_paise` and `status` from the movements.

    The single writer. Nothing else in the codebase assigns to
    `Invoice.outstanding_paise`, which is what makes the number trustworthy.
    """
    if invoice.status is InvoiceStatus.CANCELLED:
        invoice.outstanding_paise = 0
        return invoice

    settled = _sum_allocations(session, invoice.id) + _sum_credit_notes(session, invoice.id)
    outstanding = invoice.net_paise - settled
    if outstanding < 0:
        raise AllocationError(
            f"invoice {invoice.invoice_number} over-settled by {-outstanding} paise"
        )

    invoice.outstanding_paise = outstanding
    if invoice.status is not InvoiceStatus.WRITTEN_OFF:
        if outstanding == 0:
            invoice.status = InvoiceStatus.PAID
        elif settled > 0:
            invoice.status = InvoiceStatus.PART_PAID
        else:
            invoice.status = InvoiceStatus.OPEN
    return invoice


def invoice_refs(session: Session, buyer_id: UUID, *, lock: bool = False) -> list[InvoiceRef]:
    """Load a buyer's invoices as plain values for the planner.

    `lock` takes `FOR UPDATE` on the invoice rows so two overlapping ERP syncs
    cannot both read the same balance and both allocate against it. Two sync runs
    overlapping is the normal case, not an exotic race.
    """
    stmt = select(Invoice).where(
        Invoice.buyer_id == buyer_id,
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF]),
    )
    if lock:
        stmt = stmt.with_for_update()
    invoices = list(session.execute(stmt).scalars())

    return [
        InvoiceRef(
            id=i.id,
            invoice_number=i.invoice_number,
            issue_date=i.issue_date,
            due_date=i.due_date,
            net_paise=i.net_paise,
            allocated_paise=_sum_allocations(session, i.id),
            credited_paise=_sum_credit_notes(session, i.id),
        )
        for i in invoices
    ]


def apply_payment(
    session: Session,
    payment: Payment,
    *,
    instructions: dict[UUID, int] | None = None,
) -> AllocationPlan:
    """Allocate a payment and bring every touched invoice back into agreement."""
    refs = invoice_refs(session, payment.buyer_id, lock=True)
    plan = plan_allocation(payment.amount_paise, refs, instructions=instructions)

    for allocation in plan.allocations:
        session.add(
            PaymentAllocation(
                company_id=payment.company_id,
                payment_id=payment.id,
                invoice_id=allocation.invoice_id,
                amount_paise=allocation.amount_paise,
                rule=allocation.rule,
            )
        )
    payment.unallocated_paise = plan.unallocated_paise
    session.flush()

    for allocation in plan.allocations:
        recompute_invoice(session, session.get(Invoice, allocation.invoice_id))
    session.flush()
    return plan


def apply_credit_note(session: Session, note: CreditNote) -> None:
    """Apply a credit note to its invoice, refusing to over-credit.

    A note larger than the balance is a data error worth surfacing, not a
    negative invoice to be explained away later.
    """
    if note.invoice_id is None:
        return  # on-account credit; nothing to recompute

    invoice = session.get(Invoice, note.invoice_id)
    if invoice is None:
        raise AllocationError(f"credit note {note.note_number} references a missing invoice")

    session.flush()
    settled = _sum_allocations(session, invoice.id) + _sum_credit_notes(session, invoice.id)
    if settled > invoice.net_paise:
        raise AllocationError(
            f"credit note {note.note_number} would over-credit invoice "
            f"{invoice.invoice_number} by {settled - invoice.net_paise} paise"
        )
    recompute_invoice(session, invoice)
    # Flush for the same reason `apply_payment` does: callers reasonably expect
    # the recomputed balance to be readable straight afterwards.
    session.flush()
