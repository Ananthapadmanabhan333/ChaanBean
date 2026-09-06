"""Deriving how a buyer actually pays, from your own ledger.

`app.intelligence.scoring` weights these factors heaviest because they are the
only evidence in the system that nobody typed in hoping for a particular answer.
With no writer for `payment_behaviours` every one of them read None, and both
scorecards quietly collapsed onto the ageing factor — which says how late an
invoice is, and nothing at all about the debtor.

Every figure is computed **as of a date** and appended as a new row, never
updated in place. "What did we know on 12 March" has to stay answerable, so a
re-run for an earlier date must reproduce that date rather than today: nothing
here reads the clock.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from statistics import mean, median
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.intelligence.promises import PromiseStatus
from app.models import (
    AccountStatus,
    Buyer,
    CreditAccount,
    CreditNote,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentBehaviour,
    Promise,
)

# Two settlements are a pair of points, not a direction. Below this the trend
# refuses to guess rather than reporting a slope drawn through noise.
MIN_SETTLEMENTS_FOR_TREND = 4


class RollupRefusal(str, enum.Enum):
    UNKNOWN_BUYER = "UNKNOWN_BUYER"


class RollupRefused(ValueError):
    def __init__(self, reason: RollupRefusal, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Settlement:
    """One invoice that was fully paid off, and how it got there."""

    issue_date: date
    settled_on: date
    payment_count: int

    @property
    def days_to_pay(self) -> int:
        """Measured from issue, not from the due date.

        `scoring` reads 30 days as unremarkable and 120 as severe, which is a
        net-30 invoice paid on time and one paid four months late. Measured from
        the due date the same invoice would read as 0 and 90, and every
        threshold downstream would be wrong by the payment terms.
        """
        return max(0, (self.settled_on - self.issue_date).days)


# ------------------------------------------------------------------ pure maths


def days_to_pay_trend(settlements) -> float | None:
    """How many more days the recent half took than the earlier half.

    Positive means slowing down. This is a trend rather than a running mean
    because a debtor drifting from 30 days to 75 keeps an unremarkable-looking
    average for months — by the time the mean moves, the distress it was meant
    to signal has already arrived.
    """
    if len(settlements) < MIN_SETTLEMENTS_FOR_TREND:
        return None
    # Settled date first, then duration, so two runs over the same data split
    # the halves the same way.
    ordered = sorted(settlements, key=lambda s: (s.settled_on, s.days_to_pay))
    half = len(ordered) // 2
    earlier = [s.days_to_pay for s in ordered[:half]]
    later = [s.days_to_pay for s in ordered[-half:]]
    return round(mean(later) - mean(earlier), 2)


def part_payment_rate(settlements) -> float | None:
    """Share of settled invoices that took more than one payment."""
    if not settlements:
        return None
    return round(
        sum(1 for s in settlements if s.payment_count > 1) / len(settlements), 3
    )


# ------------------------------------------------------------------- the ledger


def _settlements(session: Session, buyer_id: UUID, as_of: date) -> list[Settlement]:
    """Invoices this buyer had fully paid off by `as_of`.

    Reconstructed from the movements rather than read off `Invoice.status`, so a
    rollup for an earlier date sees what was true then rather than what is true
    now.
    """
    invoices = list(
        session.execute(
            select(Invoice).where(
                Invoice.buyer_id == buyer_id,
                Invoice.issue_date <= as_of,
                # `app.trade.VOID_STATUSES` says this once, but app.intelligence
                # sits below app.trade in the enforced layer contract and cannot
                # read it. Spelled out, and it must stay in step.
                Invoice.status != InvoiceStatus.CANCELLED,
            )
        ).scalars()
    )
    if not invoices:
        return []

    invoice_ids = [i.id for i in invoices]
    paid = {
        invoice_id: (int(total), last_paid, int(payments))
        for invoice_id, total, last_paid, payments in session.execute(
            select(
                PaymentAllocation.invoice_id,
                func.sum(PaymentAllocation.amount_paise),
                func.max(Payment.received_date),
                func.count(),
            )
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(
                PaymentAllocation.invoice_id.in_(invoice_ids),
                Payment.received_date <= as_of,
            )
            .group_by(PaymentAllocation.invoice_id)
        ).all()
    }
    credited = {
        invoice_id: int(total)
        for invoice_id, total in session.execute(
            select(CreditNote.invoice_id, func.sum(CreditNote.amount_paise))
            .where(
                CreditNote.invoice_id.in_(invoice_ids),
                CreditNote.issue_date <= as_of,
            )
            .group_by(CreditNote.invoice_id)
        ).all()
    }

    settlements: list[Settlement] = []
    for invoice in invoices:
        allocated, last_paid, payments = paid.get(invoice.id, (0, None, 0))
        # No payment at all, which includes an invoice cleared entirely by a
        # credit note: the seller reduced the bill, and that is evidence about
        # the seller and none at all about how this buyer pays.
        if last_paid is None:
            continue
        if allocated + credited.get(invoice.id, 0) < invoice.net_paise:
            continue
        settlements.append(
            Settlement(
                issue_date=invoice.issue_date,
                settled_on=last_paid,
                payment_count=payments,
            )
        )
    return settlements


def _promise_kept_rate(session: Session, buyer_id: UUID, as_of: date) -> float | None:
    rows = session.execute(
        select(Promise.status, func.count())
        .where(Promise.buyer_id == buyer_id, Promise.promised_by_date <= as_of)
        .group_by(Promise.status)
    ).all()
    counts = {status: int(n) for status, n in rows}

    kept = counts.get(PromiseStatus.KEPT.value, 0)
    # A promise still OPEN past its date has not been judged yet — the sweep in
    # `app.intelligence.promises` has not run. Scoring it as broken would mark a
    # debtor down for our own scheduling.
    judged = kept + counts.get(PromiseStatus.BROKEN.value, 0)
    return round(kept / judged, 3) if judged else None


def _dispute_rate(session: Session, buyer_id: UUID) -> float | None:
    """Share of this buyer's accounts currently in dispute.

    Currently, not historically: `clear_dispute` leaves no trace on the account,
    so a resolved dispute stops counting. That under-reports rather than
    over-reports, which is the direction a factor that raises risk should err —
    and it is the reason this one figure cannot be reconstructed for a past
    date the way the rest of this row can.
    """
    rows = session.execute(
        select(CreditAccount.status, func.count())
        .where(CreditAccount.buyer_id == buyer_id)
        .group_by(CreditAccount.status)
    ).all()
    total = sum(int(n) for _, n in rows)
    if not total:
        return None
    disputed = sum(int(n) for status, n in rows if status is AccountStatus.IN_DISPUTE)
    return round(disputed / total, 3)


def rollup_payment_behaviour(
    session: Session, buyer_id: UUID, *, as_of: date
) -> PaymentBehaviour:
    """Derive one buyer's payment behaviour and append it as a new row."""
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        # Under RLS a buyer belonging to another tenant is indistinguishable
        # from one that does not exist. Both are refusals, and neither gets a
        # row written against a company_id we guessed.
        raise RollupRefused(RollupRefusal.UNKNOWN_BUYER, f"no buyer {buyer_id}")

    settlements = _settlements(session, buyer_id, as_of)
    days = [s.days_to_pay for s in settlements]

    behaviour = PaymentBehaviour(
        company_id=buyer.company_id,
        buyer_id=buyer.id,
        as_of=as_of,
        invoices_settled=len(settlements),
        mean_days_to_pay=round(mean(days), 2) if days else None,
        median_days_to_pay=round(median(days), 2) if days else None,
        days_to_pay_trend=days_to_pay_trend(settlements),
        part_payment_rate=part_payment_rate(settlements),
        promise_kept_rate=_promise_kept_rate(session, buyer_id, as_of),
        dispute_rate=_dispute_rate(session, buyer_id),
        # contact_response_rate is left None. Whether a delivered SMS or an
        # answering machine counts as a debtor responding is a decision about
        # call and message outcomes, not about the ledger, and it belongs
        # wherever those outcomes are projected rather than smuggled in here.
    )
    session.add(behaviour)
    session.flush()
    return behaviour
