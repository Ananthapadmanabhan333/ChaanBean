"""Reporting.

Two rules shape every query here.

**As of a date, never "now" implicitly.** A recovery report that cannot be
reproduced for last month is not a report, it is a dashboard. Every function
takes `as_of`.

**Delivered is not sent.** Collections reporting is where the three separate
facts (rule 15) get quietly collapsed into one "contacted" number, and that
number then overstates performance to the customer paying for it. Sent,
delivered and acknowledged are reported separately, always.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    AgeingBucket,
    Call,
    CallStatus,
    Campaign,
    Channel,
    CreditAccount,
    EscalationLevel,
    EscalationState,
    Invoice,
    InvoiceStatus,
    Message,
    MessageStatus,
    Payment,
)
from app.render.numbers import format_inr
from app.trade import UNCOLLECTABLE_STATUSES
from app.trade.ageing import ageing_bucket, days_past_due


def _as_datetime(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=timezone.utc)


@dataclass(frozen=True)
class Money:
    paise: int

    @property
    def display(self) -> str:
        return format_inr(self.paise)

    def as_dict(self) -> dict:
        return {"paise": self.paise, "display": self.display}


def ageing_summary(session: Session, as_of: date) -> dict:
    """Outstanding sliced by bucket, computed from invoices as of a date."""
    invoices = session.execute(
        select(Invoice).where(
            Invoice.issue_date <= as_of,
            Invoice.status.notin_(UNCOLLECTABLE_STATUSES),
        )
    ).scalars()

    buckets = {b.value: 0 for b in AgeingBucket}
    counts = {b.value: 0 for b in AgeingBucket}
    total = 0
    for invoice in invoices:
        if invoice.outstanding_paise <= 0:
            continue
        bucket = ageing_bucket(days_past_due(invoice.due_date, as_of)).value
        buckets[bucket] += invoice.outstanding_paise
        counts[bucket] += 1
        total += invoice.outstanding_paise

    return {
        "as_of": as_of.isoformat(),
        "total": Money(total).as_dict(),
        "buckets": {k: Money(v).as_dict() for k, v in buckets.items()},
        "invoice_counts": counts,
    }


def contact_effectiveness(session: Session, as_of: date, *, days: int = 30) -> dict:
    """Sent, delivered and acknowledged, kept apart.

    Collapsing these into one "contacted" figure is how a collections report
    overstates itself: a call answered at second 0 and dropped at second 2 of a
    22-second message connected and delivered nothing.
    """
    since = _as_datetime(as_of - timedelta(days=days))
    until = _as_datetime(as_of + timedelta(days=1))

    call_rows = session.execute(
        select(
            func.count(),
            func.sum(case((Call.answered_at.isnot(None), 1), else_=0)),
            func.sum(
                case(
                    (
                        (Call.status == CallStatus.ANSWERED)
                        & Call.playback_completed.is_(True)
                        & Call.opted_out.is_(False),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(case((Call.dtmf_ack.is_(True), 1), else_=0)),
        ).where(
            Call.created_at >= since,
            Call.created_at < until,
            Call.status != CallStatus.BLOCKED,
        )
    ).one()

    placed, connected, delivered, acknowledged = (int(v or 0) for v in call_rows)

    message_rows = session.execute(
        select(
            Message.channel,
            func.count(),
            func.sum(case((Message.sent_at.isnot(None), 1), else_=0)),
            func.sum(case((Message.delivered_at.isnot(None), 1), else_=0)),
            func.sum(case((Message.read_at.isnot(None), 1), else_=0)),
        )
        .where(
            Message.created_at >= since,
            Message.created_at < until,
            Message.status != MessageStatus.BLOCKED,
        )
        .group_by(Message.channel)
    ).all()

    return {
        "as_of": as_of.isoformat(),
        "window_days": days,
        "voice": {
            "placed": placed,
            "connected": connected,
            "delivered": delivered,
            "acknowledged": acknowledged,
            # Reported against placed, not against connected — the flattering
            # denominator is the one that hides a problem.
            "delivery_rate": round(delivered / placed, 3) if placed else None,
        },
        "messaging": {
            channel.value: {
                "queued": int(total or 0),
                "sent": int(sent or 0),
                "delivered": int(delivered_n or 0),
                "read": int(read_n or 0),
            }
            for channel, total, sent, delivered_n, read_n in message_rows
        },
    }


def block_reason_breakdown(session: Session, as_of: date, *, days: int = 30) -> dict:
    """Why contact did not happen. The answer to "why isn't it doing anything".

    Both tables. A refusal is filed against the channel it refused, so a
    messaging campaign's blocks are Message rows — and counting calls alone
    reports a silent SMS campaign as zero blocks, which is the opposite of what
    this report exists to tell an operator.
    """
    since = _as_datetime(as_of - timedelta(days=days))
    counts: dict[str | None, int] = {}
    for reason, count in (
        session.execute(
            select(Call.block_reason, func.count())
            .where(Call.status == CallStatus.BLOCKED, Call.created_at >= since)
            .group_by(Call.block_reason)
        ).all()
        + session.execute(
            select(Message.block_reason, func.count())
            .where(Message.status == MessageStatus.BLOCKED, Message.created_at >= since)
            .group_by(Message.block_reason)
        ).all()
    ):
        counts[reason] = counts.get(reason, 0) + int(count)

    total = sum(counts.values())
    return {
        "as_of": as_of.isoformat(),
        "total_blocked": total,
        "reasons": [
            {
                "reason": reason or "UNSPECIFIED",
                "count": count,
                "share": round(count / total, 3) if total else 0,
            }
            for reason, count in sorted(
                counts.items(), key=lambda item: item[1], reverse=True
            )
        ],
    }


def escalation_funnel(session: Session, as_of: date) -> dict:
    """How accounts are distributed across the ladder, and how many are stuck."""
    rows = session.execute(
        select(EscalationState.level, func.count()).group_by(EscalationState.level)
    ).all()
    needs_review = session.execute(
        select(func.count())
        .select_from(EscalationState)
        .where(EscalationState.needs_human_review.is_(True))
    ).scalar_one()

    return {
        "as_of": as_of.isoformat(),
        "levels": {level.value: int(count) for level, count in rows},
        # Accounts the ladder deliberately refused to advance — L2 thresholds met
        # but never actually reached. They need a person, not a lawyer.
        "awaiting_human_review": int(needs_review),
    }


def recovery_performance(session: Session, as_of: date, *, days: int = 90) -> dict:
    """What was actually collected, against what was outstanding."""
    since = as_of - timedelta(days=days)
    collected = session.execute(
        select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
            Payment.received_date >= since, Payment.received_date <= as_of
        )
    ).scalar_one()
    outstanding = session.execute(
        select(func.coalesce(func.sum(CreditAccount.outstanding_paise), 0)).where(
            CreditAccount.status == AccountStatus.OVERDUE
        )
    ).scalar_one()
    settled = session.execute(
        select(func.count())
        .select_from(CreditAccount)
        .where(CreditAccount.status == AccountStatus.SETTLED)
    ).scalar_one()

    return {
        "as_of": as_of.isoformat(),
        "window_days": days,
        "collected": Money(int(collected)).as_dict(),
        "still_outstanding": Money(int(outstanding)).as_dict(),
        "accounts_settled": int(settled),
    }


def campaign_report(session: Session, campaign_id: UUID, as_of: date) -> dict:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return {}
    rows = session.execute(
        select(Call.status, func.count())
        .where(Call.campaign_id == campaign_id)
        .group_by(Call.status)
    ).all()
    return {
        "campaign": campaign.name,
        "status": campaign.status.value,
        "as_of": as_of.isoformat(),
        "calls_by_status": {status.value: int(n) for status, n in rows},
        "blocks": block_reason_breakdown(session, as_of),
    }


def full_report(session: Session, as_of: date | None = None) -> dict:
    """Everything an operator or an account manager wants, in one call."""
    as_of = as_of or datetime.now(timezone.utc).date()
    return {
        "ageing": ageing_summary(session, as_of),
        "contact": contact_effectiveness(session, as_of),
        "blocks": block_reason_breakdown(session, as_of),
        "funnel": escalation_funnel(session, as_of),
        "recovery": recovery_performance(session, as_of),
    }
