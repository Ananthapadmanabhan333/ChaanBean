"""The tick worker.

`SELECT ... FOR UPDATE SKIP LOCKED` over buyers whose `next_action_at` has come
due. The buyer is the unit, not the account: a debtor with five overdue invoices
is still one person with one phone, and scheduling per account is how you call
someone five times in a morning.

This module is the swap point if escalation ever needs durable multi-day sagas.
Nothing else in the codebase knows how work is picked up.

Why a tick worker rather than Temporal — the reason is **state ownership**, not
setup effort. Escalation state has to be SQL-queryable: the dashboard filters
"which accounts are at L3", reports join it against credit accounts, and
operators mutate it. Workflow state is not queryable that way, so it would be
written to PostgreSQL anyway — and then there are two sources of truth for a
compliance-relevant fact, diverging on the path that ends in legal notices.

The durable timer is not carrying state here either. A three-day sleep wakes at
02:00 and must re-consult PostgreSQL for the calling window, DND freshness,
caps, payment arrival and dispute status regardless. That is what
`next_action_at` already is.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Buyer, Campaign, CampaignStatus, CampaignTarget
from app.scheduler.dispatch import DispatchResult, dispatch_buyer

log = logging.getLogger(__name__)


def due_buyers(session: Session, *, limit: int, now: datetime) -> list[Buyer]:
    """Claim a batch. `SKIP LOCKED` lets workers scale without coordinating."""
    return list(
        session.execute(
            select(Buyer)
            .join(CampaignTarget, CampaignTarget.buyer_id == Buyer.id)
            .join(Campaign, Campaign.id == CampaignTarget.campaign_id)
            .where(
                Buyer.next_action_at.isnot(None),
                Buyer.next_action_at <= now,
                CampaignTarget.is_active.is_(True),
                Campaign.status == CampaignStatus.ACTIVE,
            )
            .order_by(Buyer.next_action_at)
            .limit(limit)
            .with_for_update(of=Buyer, skip_locked=True)
        ).scalars()
    )


def campaign_for(session: Session, buyer_id: UUID) -> Campaign | None:
    return session.execute(
        select(Campaign)
        .join(CampaignTarget, CampaignTarget.campaign_id == Campaign.id)
        .where(CampaignTarget.buyer_id == buyer_id, CampaignTarget.is_active.is_(True))
    ).scalar_one_or_none()


def run_once(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    **dispatch_kwargs,
) -> list[DispatchResult]:
    """One pass. Returns what happened to each buyer it claimed."""
    now = now or datetime.now(timezone.utc)
    limit = limit or settings.scheduler_batch_size

    results: list[DispatchResult] = []
    for buyer in due_buyers(session, limit=limit, now=now):
        campaign = campaign_for(session, buyer.id)
        if campaign is None:
            buyer.next_action_at = None
            continue
        try:
            results.append(
                dispatch_buyer(
                    session, buyer=buyer, campaign=campaign, now=now, **dispatch_kwargs
                )
            )
        except Exception as exc:  # one bad buyer must not stop the batch
            log.exception("dispatch failed for buyer %s", buyer.id)
            session.rollback()
            results.append(
                DispatchResult(buyer.id, False, reason="DISPATCH_ERROR", detail=str(exc))
            )
    session.commit()
    return results


def run_forever(session_factory, *, interval: int | None = None, **dispatch_kwargs) -> None:
    """Loop until told to stop. Handles SIGTERM so a deploy does not sever a
    dispatch halfway through."""
    interval = interval or settings.scheduler_tick_seconds
    stopping = {"now": False}

    def _stop(*_):
        log.info("stop requested; finishing the current tick")
        stopping["now"] = True

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):  # pragma: no cover - platform dependent
        pass

    log.info("scheduler started; tick every %ss", interval)
    while not stopping["now"]:
        started = time.monotonic()
        try:
            with session_factory() as session:
                results = run_once(session, **dispatch_kwargs)
            if results:
                allowed = sum(1 for r in results if r.allowed)
                log.info("tick: %s dispatched, %s blocked", allowed, len(results) - allowed)
        except Exception:
            log.exception("tick failed; continuing")
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))
    log.info("scheduler stopped")
