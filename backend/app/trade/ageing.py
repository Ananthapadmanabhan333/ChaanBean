"""Ageing and buyer position.

Everything here is computed **as of a date**, never "now" implicitly. Reports
must reproduce historically, and a function that reads the clock internally
cannot be tested or replayed — you could never answer "what did this look like
on the day we escalated them".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgeingBucket, Invoice
from app.trade import UNCOLLECTABLE_STATUSES


def days_past_due(due_date: date, as_of: date) -> int:
    """Zero until the due date passes; never negative."""
    return max(0, (as_of - due_date).days)


def ageing_bucket(dpd: int) -> AgeingBucket:
    if dpd <= 0:
        return AgeingBucket.CURRENT
    if dpd <= 30:
        return AgeingBucket.B1_30
    if dpd <= 60:
        return AgeingBucket.B31_60
    if dpd <= 90:
        return AgeingBucket.B61_90
    return AgeingBucket.B90_PLUS


@dataclass(frozen=True)
class BuyerPosition:
    """What one buyer owes, sliced the way every dashboard and the Policy Engine
    want it. Built once, here, rather than re-derived in each consumer."""

    buyer_id: UUID
    as_of: date
    total_outstanding_paise: int
    invoice_count: int
    oldest_invoice_date: date | None
    oldest_due_date: date | None
    max_days_past_due: int
    buckets: dict[AgeingBucket, int] = field(default_factory=dict)

    @property
    def is_overdue(self) -> bool:
        return self.max_days_past_due > 0


def position_from_invoices(buyer_id: UUID, invoices, as_of: date) -> BuyerPosition:
    """Pure: takes anything with `due_date`, `issue_date` and `outstanding_paise`."""
    buckets = {b: 0 for b in AgeingBucket}
    total = 0
    count = 0
    oldest_issue: date | None = None
    oldest_due: date | None = None
    max_dpd = 0

    for inv in invoices:
        outstanding = inv.outstanding_paise
        if outstanding <= 0:
            continue
        count += 1
        total += outstanding
        dpd = days_past_due(inv.due_date, as_of)
        buckets[ageing_bucket(dpd)] += outstanding
        max_dpd = max(max_dpd, dpd)
        if oldest_issue is None or inv.issue_date < oldest_issue:
            oldest_issue = inv.issue_date
        if oldest_due is None or inv.due_date < oldest_due:
            oldest_due = inv.due_date

    return BuyerPosition(
        buyer_id=buyer_id,
        as_of=as_of,
        total_outstanding_paise=total,
        invoice_count=count,
        oldest_invoice_date=oldest_issue,
        oldest_due_date=oldest_due,
        max_days_past_due=max_dpd,
        buckets=buckets,
    )


def buyer_position(session: Session, buyer_id: UUID, as_of: date) -> BuyerPosition:
    invoices = list(
        session.execute(
            select(Invoice).where(
                Invoice.buyer_id == buyer_id,
                Invoice.status.notin_(UNCOLLECTABLE_STATUSES),
            )
        ).scalars()
    )
    return position_from_invoices(buyer_id, invoices, as_of)
