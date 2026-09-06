"""Making a debt smaller.

Every other write path in this package can only grow what a buyer owes, and that
asymmetry is dangerous in a product whose outputs are adverse acts. A dunning
script, a pre-legal assessment and the figure in a legal notice are all computed
from a balance; a balance nobody can reduce keeps producing them long after the
reason for them has gone.

Two rules hold throughout:

* `outstanding_paise` is never assigned here. Every function ends at
  `allocation.recompute_invoice`, its single writer, even where the answer is
  obviously zero.
* The recovery projection follows in the same call. Reducing the ledger and
  leaving `credit_accounts` behind is the specific bug that keeps the scheduler
  dialling a debt the ledger has already closed.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CreditNote, Invoice, InvoiceStatus, Payment, PaymentAllocation
from app.trade import accounts, allocation
from app.trade.allocation import (
    AllocationError,
    AllocationPlan,
    invoice_refs,
    plan_allocation,
    recompute_invoice,
)


class ReductionRefusal(str, enum.Enum):
    """Why a reduction did not happen. Closed set, defined once.

    Over-crediting is deliberately absent. `allocation.apply_credit_note`
    already refuses it and names the excess in paise; a second guard here would
    be a second message for one rule, and the two would drift.
    """

    NO_UNALLOCATED_BALANCE = "NO_UNALLOCATED_BALANCE"
    REASON_REQUIRED = "REASON_REQUIRED"
    INVOICE_ALREADY_CANCELLED = "INVOICE_ALREADY_CANCELLED"
    INVOICE_HAS_SETTLEMENTS = "INVOICE_HAS_SETTLEMENTS"


class ReductionError(AllocationError):
    """Refused, naming itself from `ReductionRefusal`.

    Subclasses AllocationError so a caller already handling ledger refusals —
    the ERP reconciler, the payments route — catches these too rather than
    letting a 500 out.
    """

    def __init__(self, refusal: ReductionRefusal, detail: str) -> None:
        super().__init__(f"{refusal.value}: {detail}")
        self.refusal = refusal
        self.detail = detail


# ------------------------------------------------------------------ credit notes


def apply_credit_note(
    session: Session, note: CreditNote, *, now: datetime | None = None
) -> CreditNote:
    """Apply a credit note to its invoice and bring the projection with it.

    The over-credit guard is `allocation.apply_credit_note`'s, called rather
    than repeated — it is the message a refused API call quotes back, and there
    must be exactly one of it.

    That guard works by summing the `credit_notes` rows, so a note the caller
    built but never added to the session would be invisible to it: the invoice
    would be recomputed without the very note being applied, and the credit
    would land unchecked. Hence the add.
    """
    if note.invoice_id is None:
        return note  # on-account credit, not against any invoice

    # Refused before the note is written, so a rejected credit leaves no row
    # behind for the next reader to wonder about.
    invoice = session.get(Invoice, note.invoice_id, with_for_update=True)
    if invoice is not None and invoice.status is InvoiceStatus.CANCELLED:
        raise ReductionError(
            ReductionRefusal.INVOICE_ALREADY_CANCELLED,
            f"invoice {invoice.invoice_number} is cancelled; crediting it would "
            f"reduce nothing and leave note {note.note_number} unaccounted for",
        )

    if note not in session:
        session.add(note)
    session.flush()

    allocation.apply_credit_note(session, note)
    accounts.sync_account_from_invoice(session, invoice, now=now)
    session.flush()
    return note


# --------------------------------------------------------------- money on account


def apply_on_account(
    session: Session,
    payment: Payment,
    *,
    instructions: dict[UUID, int] | None = None,
    now: datetime | None = None,
) -> AllocationPlan:
    """Apply money the creditor is already holding to the invoices named.

    Emphatically not a second call to `apply_payment`. That function plans
    against `payment.amount_paise`, so running it again on a payment that has
    already been allocated plans the *whole* amount a second time and writes a
    second set of allocations — the invoice is then over-settled, or, worse,
    settled twice across different invoices and the buyer is credited with money
    that was never received. This plans against `unallocated_paise`: what is
    actually left.

    Without `instructions` the remainder falls oldest-first, the same default
    and the same recorded rule as the original allocation.

    The payment row is locked before its balance is read. The invoice lock taken
    below serialises two allocations landing on one invoice; it says nothing
    about two allocations of one *payment*, which read the same
    `unallocated_paise`, plan against it separately and then settle different
    invoices — no unique constraint fires, and one receipt has discharged twice
    the debt that arrived.
    """
    session.flush()  # a payment the caller has not written yet has no id to lock by
    payment = session.get(Payment, payment.id, with_for_update=True)
    available = payment.unallocated_paise
    if available <= 0:
        raise ReductionError(
            ReductionRefusal.NO_UNALLOCATED_BALANCE,
            f"payment {payment.reference or payment.id} has nothing left on account",
        )

    refs = invoice_refs(session, payment.buyer_id, lock=True)
    plan = plan_allocation(available, refs, instructions=instructions)

    existing = {
        row.invoice_id: row
        for row in session.execute(
            select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id)
        ).scalars()
    }

    for entry in plan.allocations:
        row = existing.get(entry.invoice_id)
        if row is None:
            session.add(
                PaymentAllocation(
                    company_id=payment.company_id,
                    payment_id=payment.id,
                    invoice_id=entry.invoice_id,
                    amount_paise=entry.amount_paise,
                    rule=entry.rule,
                )
            )
        else:
            # (payment_id, invoice_id) is unique, so topping up an invoice this
            # payment already touched cannot be a second row. The amount grows;
            # what survives as evidence of the change is the recomputed invoice
            # and the caller's audit entry.
            row.amount_paise += entry.amount_paise

    payment.unallocated_paise = plan.unallocated_paise
    session.flush()

    for entry in plan.allocations:
        invoice = session.get(Invoice, entry.invoice_id)
        recompute_invoice(session, invoice)
        accounts.sync_account_from_invoice(session, invoice, now=now)
    session.flush()
    return plan


# ------------------------------------------------------------- closing an invoice


def _require_reason(reason: str | None, act: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ReductionError(
            ReductionRefusal.REASON_REQUIRED,
            f"{act} needs a recorded reason: it is the only account of why this "
            f"debt stopped being pursued, and it is what a later audit reads",
        )
    return reason


def write_off(
    session: Session, invoice: Invoice, *, reason: str, now: datetime | None = None
) -> Invoice:
    """Stop expecting the money, and stop the ladder that was chasing it.

    A write-off does not extinguish the debt. `recompute_invoice` deliberately
    leaves `outstanding_paise` at what is genuinely still owed, and the invoice
    stays on the buyer's statement, because the sale happened and their books
    still carry the payable. What changes is that this side stops pursuing it —
    so the account sync is the point of the function, not a tidy-up after it.
    """
    reason = _require_reason(reason, "a write-off")
    if invoice.status is InvoiceStatus.CANCELLED:
        raise ReductionError(
            ReductionRefusal.INVOICE_ALREADY_CANCELLED,
            f"invoice {invoice.invoice_number} was cancelled; there is nothing "
            f"left to write off",
        )

    invoice.status = InvoiceStatus.WRITTEN_OFF
    recompute_invoice(session, invoice)
    session.flush()
    accounts.sync_account_from_invoice(session, invoice, reason=reason, now=now)
    session.flush()
    return invoice


def cancel_invoice(
    session: Session, invoice: Invoice, *, reason: str, now: datetime | None = None
) -> Invoice:
    """Void an invoice that should never have stood.

    Distinct from a write-off, and the distinction is the whole ledger: writing
    off says the sale happened and will not be paid, so the invoice keeps its
    balance and stays on the statement; cancelling says it did not happen, so
    the balance goes to zero and it leaves the statement entirely.

    Which is why an invoice with money or credit against it cannot be cancelled.
    Removing the debit while the payment that settled it stays as a credit would
    leave the buyer's balance short by the invoice — reading, on a statement they
    have been sent, as though they had overpaid.
    """
    reason = _require_reason(reason, "a cancellation")
    if invoice.status is InvoiceStatus.CANCELLED:
        raise ReductionError(
            ReductionRefusal.INVOICE_ALREADY_CANCELLED,
            f"invoice {invoice.invoice_number} is already cancelled",
        )

    # Through the single writer rather than trusting the cached balance: the
    # question "has anything been applied to this" has to be asked of the
    # movements, not of a projection that may be a sync behind.
    recompute_invoice(session, invoice)
    applied = invoice.net_paise - invoice.outstanding_paise
    if applied:
        raise ReductionError(
            ReductionRefusal.INVOICE_HAS_SETTLEMENTS,
            f"invoice {invoice.invoice_number} has {applied} paise applied to it; "
            f"reverse the payment or credit note first, or write it off instead",
        )

    invoice.status = InvoiceStatus.CANCELLED
    recompute_invoice(session, invoice)
    session.flush()
    accounts.sync_account_from_invoice(session, invoice, reason=reason, now=now)
    session.flush()
    return invoice
