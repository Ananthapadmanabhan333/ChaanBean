"""Keeping the DND answer fresh enough for the policy engine to use it.

The engine fails closed twice on DND: once on a number it has no scrub for, and
again on a scrub older than `DND_MAX_AGE`. Both are right, and together they are
fatal without this job — the CSV importer and the ERP reconciler both write
UNKNOWN, so on a freshly loaded dataset every phone channel blocks forever and
the system is correct and useless at the same time.

**The cadence is the design.** Re-scrubbing at exactly `DND_MAX_AGE` leaves no
room: the run that should refresh a number is the run that finds it already
stale, and any hiccup — a batch too small to reach it, a vendor outage, a worker
restart — becomes blocked calls with no failure anywhere to point at. So the
interval is half the window, which leaves a whole second interval of retries
before the gate stops trusting an answer. It is derived from the engine's own
constant rather than configured separately, because two numbers that must stay
in a fixed ratio are one number.

Which numbers are due is a pure function, kept apart from the claiming and the
writing, so the arithmetic that decides whether a campaign can dial at all is
testable without a database.

Nothing here calls `assert_contactable`. Asking a registry about a number is not
contacting the person behind it, and gating the scrub on the non-production
allowlist would leave every other number UNKNOWN — blocking, in a second place,
exactly the calls the allowlist already blocks, while hiding this pipeline from
every test.

Nothing starts this yet either: `app.worker` runs the dispatch tick only. Wiring
is one `scrub_due` call per tick, or a cron on the same interval, and until it
exists the platform cannot lawfully place its first call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import nulls_first, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BuyerPhone, DndStatus
from app.policy.engine import DND_MAX_AGE
from app.providers.base import DndBackend

log = logging.getLogger(__name__)

# Half the window the policy engine will tolerate. See the module docstring: the
# margin is what lets a run be missed without a number going stale.
SCRUB_INTERVAL = DND_MAX_AGE / 2


@dataclass(frozen=True)
class ScrubReport:
    """What one pass did. `registered` is the number of debtors this run learned
    it may no longer call, which is the figure an operator wants to see move."""

    claimed: int
    updated: int
    failed: int
    registered: int


# ------------------------------------------------------------------- planning


def scrub_cutoff(now: datetime, interval: timedelta = SCRUB_INTERVAL) -> datetime:
    """The instant a scrub must be newer than to still count as current."""
    return now - interval


def is_due(
    checked_at: datetime | None,
    *,
    now: datetime,
    interval: timedelta = SCRUB_INTERVAL,
) -> bool:
    """Does this number need re-scrubbing.

    Never scrubbed is due. A scrub landing exactly on the boundary is due as
    well — the comparison is `<=`, so equality re-scrubs rather than waiting a
    cycle. Erring this way costs one registry lookup; erring the other way lets
    a number drift into the staleness gate and silently block every channel it
    owns, which is a refusal nobody asked for and nobody can see coming.
    """
    if checked_at is None:
        return True
    return checked_at <= scrub_cutoff(now, interval)


# ---------------------------------------------------------------------- claim


def due_phones(
    session: Session,
    *,
    now: datetime,
    limit: int,
    interval: timedelta = SCRUB_INTERVAL,
) -> list[BuyerPhone]:
    """Claim a batch of numbers to re-scrub.

    `FOR UPDATE SKIP LOCKED`, like the rest of the scheduler: two workers may run
    this at once, and neither should pay a vendor twice for the same number or
    race the other's write.

    The predicate is `is_due` said in SQL, down to the `<=`. A `<` here and a
    `<=` there would create a number that is due to the planner and invisible to
    the query — due forever, scrubbed never.
    """
    cutoff = scrub_cutoff(now, interval)
    return list(
        session.execute(
            select(BuyerPhone)
            .where(
                # A number the carrier has already retired is never dialled, so
                # scrubbing it spends a paid lookup on nothing. If it is ever
                # revalidated it arrives back here carrying its old scrub, which
                # by then is due.
                BuyerPhone.is_valid.is_(True),
                or_(
                    BuyerPhone.dnd_checked_at.is_(None),
                    BuyerPhone.dnd_checked_at <= cutoff,
                ),
            )
            # Never-scrubbed first: those numbers are blocking a campaign
            # outright, not drifting towards it.
            .order_by(nulls_first(BuyerPhone.dnd_checked_at.asc()))
            .limit(limit)
            .with_for_update(of=BuyerPhone, skip_locked=True)
        ).scalars()
    )


def scrub_due(
    session: Session,
    backend: DndBackend,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    interval: timedelta = SCRUB_INTERVAL,
) -> ScrubReport:
    """Re-scrub one batch of numbers and commit.

    Commits, as `tick.run_once` does, because the claim holds a row lock until
    the transaction ends: a caller that forgot would keep every other worker off
    this batch for as long as it held the session open.

    This is a cross-tenant job — run it on `admin_session`. It carries no
    `company_id` predicate of its own by design; a number's registry status is
    not a tenant's opinion, and hand-written tenant filters are the pattern RLS
    exists to replace.
    """
    now = now or datetime.now(timezone.utc)
    limit = limit or settings.scheduler_batch_size

    phones = due_phones(session, now=now, limit=limit, interval=interval)
    updated = failed = registered = 0

    for phone in phones:
        try:
            check = backend.check(phone.e164)
        except Exception as exc:
            # Losing an answer is not the same as being told the answer changed.
            # Writing UNKNOWN here would take a number the registry cleared last
            # week and block it on the strength of our own outage, so the row is
            # left exactly as it stands — still due, for the next run to retry.
            log.warning(
                "DND scrub failed for phone %s; keeping the previous answer: %s",
                phone.id,
                exc,
            )
            failed += 1
            continue

        if check.e164 != phone.e164:
            # An answer about some other number is not an answer about this one.
            # Bulk vendor clients return rows in their own order, and one
            # off-by-one in that mapping marks the wrong debtor callable.
            log.warning(
                "DND backend answered about %s when asked about %s; discarding",
                check.e164,
                phone.e164,
            )
            failed += 1
            continue

        phone.dnd_status = check.status
        phone.dnd_checked_at = check.checked_at
        updated += 1
        if check.status is DndStatus.REGISTERED:
            registered += 1

    session.commit()
    return ScrubReport(
        claimed=len(phones), updated=updated, failed=failed, registered=registered
    )
