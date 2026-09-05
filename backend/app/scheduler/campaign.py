"""Campaign control: start, pause, resume, complete.

Starting a campaign enrols buyers into `campaign_targets` and seeds
`next_action_at`. The partial unique index on active membership is what stops
two campaigns owning the same buyer — without it, per-buyer caps become a
contention problem between campaigns rather than a rule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Buyer,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    CampaignTarget,
    CreditAccount,
)

log = logging.getLogger(__name__)


class CampaignError(RuntimeError):
    pass


def enrol(
    session: Session, campaign: Campaign, buyer_ids: list[UUID], *, now: datetime | None = None
) -> dict:
    """Add buyers to a campaign, skipping any already actively targeted.

    A buyer already owned by another campaign is reported rather than stolen —
    silently moving them would let two campaigns alternate and double the
    contact rate the caps were set to prevent.
    """
    now = now or datetime.now(timezone.utc)
    added, skipped = 0, []

    for buyer_id in buyer_ids:
        active = session.execute(
            select(CampaignTarget).where(
                CampaignTarget.buyer_id == buyer_id, CampaignTarget.is_active.is_(True)
            )
        ).scalar_one_or_none()
        if active is not None:
            if active.campaign_id != campaign.id:
                skipped.append({"buyer_id": str(buyer_id), "reason": "in another campaign"})
            continue

        session.add(
            CampaignTarget(
                company_id=campaign.company_id,
                campaign_id=campaign.id,
                buyer_id=buyer_id,
                is_active=True,
            )
        )
        buyer = session.get(Buyer, buyer_id)
        if buyer is not None and buyer.next_action_at is None:
            buyer.next_action_at = now
        added += 1

    session.flush()
    return {"added": added, "skipped": skipped}


def eligible_buyers(session: Session, company_id: UUID) -> list[UUID]:
    """Buyers with at least one account worth chasing."""
    rows = session.execute(
        select(CreditAccount.buyer_id)
        .where(
            CreditAccount.company_id == company_id,
            CreditAccount.status == AccountStatus.OVERDUE,
            CreditAccount.outstanding_paise > 0,
        )
        .distinct()
    ).scalars()
    return list(rows)


def start(session: Session, campaign: Campaign, *, now: datetime | None = None) -> Campaign:
    if campaign.status is CampaignStatus.COMPLETED:
        raise CampaignError("a completed campaign cannot be restarted")
    now = now or datetime.now(timezone.utc)
    campaign.status = CampaignStatus.ACTIVE
    for target in session.execute(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign.id)
    ).scalars():
        if target.is_active:
            buyer = session.get(Buyer, target.buyer_id)
            if buyer is not None and buyer.next_action_at is None:
                buyer.next_action_at = now
    session.flush()
    return campaign


def pause(session: Session, campaign: Campaign) -> Campaign:
    """Stop dispatching. Targets keep their place in the ladder."""
    campaign.status = CampaignStatus.PAUSED
    session.flush()
    return campaign


def resume(session: Session, campaign: Campaign, *, now: datetime | None = None) -> Campaign:
    return start(session, campaign, now=now)


def progress(session: Session, campaign: Campaign) -> dict:
    """What an operator needs to see, including why nothing is happening."""
    targets = session.execute(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign.id)
    ).scalars().all()
    buyer_ids = [t.buyer_id for t in targets]

    if not buyer_ids:
        return {
            "targets": 0, "active": 0, "settled": 0, "calls": 0, "blocked": 0,
            "block_reasons": {}, "complete": False,
        }

    settled = session.execute(
        select(func.count(func.distinct(CreditAccount.buyer_id))).where(
            CreditAccount.buyer_id.in_(buyer_ids),
            CreditAccount.status.in_(
                [AccountStatus.SETTLED, AccountStatus.WRITTEN_OFF]
            ),
        )
    ).scalar_one()

    calls = session.execute(
        select(func.count()).select_from(Call).where(Call.campaign_id == campaign.id)
    ).scalar_one()

    reason_rows = session.execute(
        select(Call.block_reason, func.count())
        .where(Call.campaign_id == campaign.id, Call.status == CallStatus.BLOCKED)
        .group_by(Call.block_reason)
    ).all()
    block_reasons = {reason or "UNSPECIFIED": int(n) for reason, n in reason_rows}

    return {
        "targets": len(targets),
        "active": sum(1 for t in targets if t.is_active),
        "settled": int(settled),
        "calls": int(calls),
        "blocked": sum(block_reasons.values()),
        "block_reasons": block_reasons,
        "complete": all(not t.is_active for t in targets),
    }


def complete_if_done(session: Session, campaign: Campaign) -> Campaign:
    """A campaign is finished when every target is settled, written off or
    exhausted."""
    targets = session.execute(
        select(CampaignTarget).where(
            CampaignTarget.campaign_id == campaign.id, CampaignTarget.is_active.is_(True)
        )
    ).scalars().all()

    for target in targets:
        buyer = session.get(Buyer, target.buyer_id)
        open_accounts = session.execute(
            select(func.count())
            .select_from(CreditAccount)
            .where(
                CreditAccount.buyer_id == target.buyer_id,
                CreditAccount.status == AccountStatus.OVERDUE,
                CreditAccount.outstanding_paise > 0,
            )
        ).scalar_one()
        exhausted = buyer is not None and buyer.next_action_at is None
        if not open_accounts or exhausted:
            target.is_active = False

    remaining = session.execute(
        select(func.count())
        .select_from(CampaignTarget)
        .where(
            CampaignTarget.campaign_id == campaign.id, CampaignTarget.is_active.is_(True)
        )
    ).scalar_one()
    if remaining == 0 and campaign.status is CampaignStatus.ACTIVE:
        campaign.status = CampaignStatus.COMPLETED
        log.info("campaign %s completed", campaign.id)
    session.flush()
    return campaign
