"""Promises to pay: recording one, pausing chasing, and judging it afterwards.

A broken promise is recorded as BROKEN, never deleted. The kept-rate is the
strongest single predictor this system has of whether the next promise means
anything, and deleting the failures would flatter the debtor at exactly the
moment the evidence should be getting harsher.

There is a second reason nothing here removes a row. A promise to pay is capable
of being an acknowledgement under Section 18 of the Limitation Act 1963, which
restarts the three-year clock `app.legal.prelegal` computes from
`last_acknowledgement`. Whether a particular promise qualifies is a question for
counsel; discarding the record forecloses the question.

The pause is bounded on purpose. Chasing stops until the promised date plus a
grace period, so an unbounded horizon would make "I will pay in March 2029" a
way to switch the ladder off from the debtor's side. A promise further out than
`MAX_PROMISE_HORIZON_DAYS` is refused with a named reason instead.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Buyer, CreditAccount, Payment, Promise

# The promised date is a date a person named, and every date a human or a court
# reads in this system is Asia/Kolkata. Same default as `Campaign.timezone`,
# overridable for the same reason.
PROMISE_TIMEZONE = "Asia/Kolkata"

# A cheque or an NEFT initiated on the promised day arrives after it. Chasing
# someone the next morning for money already in transit is how you lose a
# customer who was cooperating, so the grace applies to the pause *and* to
# whether the promise counts as kept.
DEFAULT_GRACE_DAYS = 3

MAX_PROMISE_HORIZON_DAYS = 90


class PromiseStatus(str, enum.Enum):
    """The closed set stored in `Promise.status`. OPEN -> KEPT | BROKEN."""

    OPEN = "OPEN"
    KEPT = "KEPT"
    BROKEN = "BROKEN"


class PromiseRefusal(str, enum.Enum):
    """Why a promise was not recorded, or not settled. Closed set."""

    AMOUNT_NOT_POSITIVE = "AMOUNT_NOT_POSITIVE"
    DATE_IN_PAST = "DATE_IN_PAST"
    HORIZON_TOO_FAR = "HORIZON_TOO_FAR"
    PROMISED_ON_IN_FUTURE = "PROMISED_ON_IN_FUTURE"
    ACCOUNT_NOT_THIS_BUYERS = "ACCOUNT_NOT_THIS_BUYERS"
    ALREADY_SETTLED = "ALREADY_SETTLED"


class PromiseRefused(ValueError):
    """Refused, with the reason on the exception rather than only in the text."""

    def __init__(self, reason: PromiseRefusal, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


def chase_resumes_at(
    promised_by_date: date,
    *,
    grace_days: int = DEFAULT_GRACE_DAYS,
    timezone_name: str = PROMISE_TIMEZONE,
) -> datetime:
    """The UTC instant chasing may resume after a promise.

    The promised date is local, the stored suppression is UTC, and the
    conversion happens here for the same reason `app.policy.engine` converts
    before checking the calling window: resuming at UTC midnight would ring a
    debtor at 05:30 their time on the morning their grace ran out.
    """
    resumes_on = promised_by_date + timedelta(days=grace_days + 1)
    local_midnight = datetime.combine(
        resumes_on, time(0, 0), tzinfo=ZoneInfo(timezone_name)
    )
    return local_midnight.astimezone(timezone.utc)


def record_promise(
    session: Session,
    *,
    buyer: Buyer,
    promised_amount_paise: int,
    promised_on: date,
    promised_by_date: date,
    account: CreditAccount | None = None,
    recorded_by: UUID | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
    timezone_name: str = PROMISE_TIMEZONE,
    now: datetime | None = None,
) -> Promise:
    """Record a promise to pay and pause chasing until it falls due.

    The horizon is measured from today, not from `promised_on`. `promised_on` is
    what the caller says the promise was given on, and measuring the cap against
    it makes the cap self-referential: a pair of dates ninety days apart in 2029
    passes every check and suppresses the buyer for years, which is precisely
    the switch-the-ladder-off move the cap exists to prevent. `promised_on`
    stays as the record of when the promise was given, and it may be backdated —
    a note written up the next morning is ordinary — but not forward-dated,
    because a promise cannot have been given on a day that has not happened.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name)).date()

    if promised_amount_paise <= 0:
        raise PromiseRefused(
            PromiseRefusal.AMOUNT_NOT_POSITIVE,
            f"promised amount was {promised_amount_paise} paise",
        )
    if promised_on > today:
        raise PromiseRefused(
            PromiseRefusal.PROMISED_ON_IN_FUTURE,
            f"promise dated {promised_on}, which is after {today} in {timezone_name}",
        )
    if promised_by_date < promised_on:
        raise PromiseRefused(
            PromiseRefusal.DATE_IN_PAST,
            f"promised to pay by {promised_by_date} on {promised_on}",
        )
    horizon = (promised_by_date - today).days
    if horizon > MAX_PROMISE_HORIZON_DAYS:
        raise PromiseRefused(
            PromiseRefusal.HORIZON_TOO_FAR,
            f"{promised_by_date} is {horizon} days from {today}, beyond the "
            f"{MAX_PROMISE_HORIZON_DAYS}-day limit",
        )
    if account is not None and account.buyer_id != buyer.id:
        raise PromiseRefused(
            PromiseRefusal.ACCOUNT_NOT_THIS_BUYERS,
            f"account {account.id} belongs to another buyer",
        )

    promise = Promise(
        company_id=buyer.company_id,
        buyer_id=buyer.id,
        account_id=account.id if account is not None else None,
        promised_amount_paise=promised_amount_paise,
        promised_on=promised_on,
        promised_by_date=promised_by_date,
        recorded_by=recorded_by,
        status=PromiseStatus.OPEN.value,
    )
    session.add(promise)

    resumes_at = chase_resumes_at(
        promised_by_date, grace_days=grace_days, timezone_name=timezone_name
    )
    # Never shortens an existing suppression. A promise is a reason to wait
    # longer; it is never a reason to start chasing someone sooner than whatever
    # already stopped us.
    if buyer.suppressed_until is None or buyer.suppressed_until < resumes_at:
        buyer.suppressed_until = resumes_at

    session.flush()
    return promise


def settle_promise(
    session: Session, promise: Promise, *, kept: bool, settled_at: datetime
) -> Promise:
    """Close a promise as KEPT or BROKEN. Never deletes, never re-opens."""
    if promise.status != PromiseStatus.OPEN.value:
        raise PromiseRefused(
            PromiseRefusal.ALREADY_SETTLED,
            f"promise {promise.id} is already {promise.status}",
        )

    promise.status = (PromiseStatus.KEPT if kept else PromiseStatus.BROKEN).value
    promise.settled_at = settled_at
    # The suppression is deliberately left alone. `Buyer.suppressed_until` has
    # other writers, and cutting a pause short because one promise went bad is
    # how a debtor gets called the same afternoon they explained why they cannot
    # pay. It expires on its own.
    session.flush()
    return promise


def _paid_towards(session: Session, promise: Promise, *, deadline: date) -> int:
    """Money received from this buyer between the promise and its deadline.

    Measured against money received rather than against the promised account's
    balance: a debtor who paid the promised sum but let it land on a different
    invoice kept their promise, and arguing otherwise is a bookkeeping
    technicality no person on either side would accept.
    """
    return int(
        session.execute(
            select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
                Payment.buyer_id == promise.buyer_id,
                Payment.received_date >= promise.promised_on,
                Payment.received_date <= deadline,
            )
        ).scalar_one()
    )


def resolve_due_promises(
    session: Session,
    *,
    as_of: datetime,
    grace_days: int = DEFAULT_GRACE_DAYS,
    timezone_name: str = PROMISE_TIMEZONE,
) -> list[Promise]:
    """Judge every open promise whose grace has run out, from the ledger.

    Without this nothing ever writes BROKEN, and a kept-rate that only counts
    successes is worse than no kept-rate at all — it would report every debtor
    as perfectly reliable right up to the day they stop answering.
    """
    # The grace day itself belongs to the debtor, so a promise is only judged
    # once that day has fully passed in their own timezone — hence the extra day.
    local_today = as_of.astimezone(ZoneInfo(timezone_name)).date()
    latest_due = local_today - timedelta(days=grace_days + 1)

    due = list(
        session.execute(
            select(Promise).where(
                Promise.status == PromiseStatus.OPEN.value,
                Promise.promised_by_date <= latest_due,
            )
        ).scalars()
    )

    settled = []
    for promise in due:
        deadline = promise.promised_by_date + timedelta(days=grace_days)
        paid = _paid_towards(session, promise, deadline=deadline)
        settled.append(
            settle_promise(
                session,
                promise,
                kept=paid >= promise.promised_amount_paise,
                settled_at=as_of,
            )
        )
    return settled
