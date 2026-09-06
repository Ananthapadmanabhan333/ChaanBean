"""Keeping the recovery-facing projection in sync with the ledger.

`credit_accounts` is what the Policy Engine and the scheduler read. It has to
follow the invoice, and the transitions have to be explicit — the worst business
bug this product can commit is calling someone at L3 who paid yesterday.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    CreditAccount,
    EscalationLevel,
    EscalationState,
    Invoice,
    InvoiceStatus,
)


def _as_datetime(value) -> datetime:
    return datetime.combine(value, time(0, 0), tzinfo=timezone.utc)


# The invoice is finished with, one way or the other, and nothing more should be
# collected against it.
_DEAD_INVOICE = (InvoiceStatus.WRITTEN_OFF, InvoiceStatus.CANCELLED)

# Reaching one of these ends the ladder, and the event name it is recorded under.
_LADDER_STOPS_AT = {
    AccountStatus.SETTLED: "settled",
    AccountStatus.WRITTEN_OFF: "written_off",
}


def sync_account_from_invoice(
    session: Session,
    invoice: Invoice,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> CreditAccount:
    """Create or update the recovery projection of one invoice.

    Status transitions, and what each means for the ladder:

    * fully settled  -> SETTLED, and the ladder stops
    * partly paid    -> stays OVERDUE, but the cadence resets. Someone who just
      paid something is engaging, and escalating at the same pace is how you lose
      a customer who was cooperating.
    * written off    -> WRITTEN_OFF, ladder stops
    * cancelled      -> WRITTEN_OFF as well. AccountStatus has no CANCELLED, and
      SETTLED is the wrong home for it: a voided invoice was never paid, and an
      account that claims otherwise misreports the recovery rate.

    `reason` is carried onto the ladder history entry, so "why did this stop
    being chased" survives in the one place that keeps a durable trace.
    """
    now = now or datetime.now(timezone.utc)

    account = session.execute(
        select(CreditAccount).where(CreditAccount.invoice_id == invoice.id)
    ).scalar_one_or_none()

    if account is None:
        account = CreditAccount(
            company_id=invoice.company_id,
            buyer_id=invoice.buyer_id,
            invoice_id=invoice.id,
            invoice_ref=invoice.invoice_number,
            outstanding_paise=invoice.outstanding_paise,
            due_date=_as_datetime(invoice.due_date),
            status=AccountStatus.OVERDUE,
        )
        session.add(account)
        session.flush()

    previous_outstanding = account.outstanding_paise
    previous_status = account.status

    account.outstanding_paise = invoice.outstanding_paise
    account.due_date = _as_datetime(invoice.due_date)

    # A dispute is a human decision and outranks anything the ledger says. It is
    # cleared deliberately, never by a payment landing — but a write-off or a
    # cancellation is a *more* terminal human decision, and an account left
    # IN_DISPUTE over an invoice the creditor has abandoned sits in the review
    # queue forever.
    if account.status is AccountStatus.IN_DISPUTE and invoice.status not in _DEAD_INVOICE:
        return account

    if invoice.status in _DEAD_INVOICE:
        account.status = AccountStatus.WRITTEN_OFF
    elif invoice.outstanding_paise == 0:
        account.status = AccountStatus.SETTLED
    elif invoice.due_date and _as_datetime(invoice.due_date) <= now:
        account.status = AccountStatus.OVERDUE
    else:
        account.status = AccountStatus.CURRENT

    changed = account.status is not previous_status
    if changed:
        account.status_updated_at = now

    # Only on the transition. Syncing an already-closed account is routine — an
    # ERP re-sync, a second payment landing — and appending "settled" on a day
    # nothing settled turns the one durable trace this product has into noise.
    stopping_event = _LADDER_STOPS_AT.get(account.status) if changed else None
    if stopping_event is not None:
        _stop_ladder(session, account, now, event=stopping_event, reason=reason)
    elif (
        invoice.status not in _DEAD_INVOICE
        and invoice.outstanding_paise < previous_outstanding
    ):
        # Only a live debt has a cadence to soften. A credit note against an
        # abandoned invoice reduces the balance without anybody paying anything,
        # and rewinding the attempt counter there would record a part payment
        # that did not happen — and start the ladder from zero if the account
        # ever came back.
        _reset_cadence(session, account, now)

    session.flush()
    return account


def _escalation(session: Session, account: CreditAccount) -> EscalationState | None:
    return session.execute(
        select(EscalationState).where(EscalationState.account_id == account.id)
    ).scalar_one_or_none()


def _stop_ladder(
    session: Session,
    account: CreditAccount,
    now: datetime,
    *,
    event: str,
    reason: str | None = None,
) -> None:
    state = _escalation(session, account)
    if state is None:
        return
    state.needs_human_review = False
    entry = {"at": now.isoformat(), "event": event, "level": state.level.value}
    if reason:
        entry["reason"] = reason
    state.history = list(state.history or []) + [entry]


def _reset_cadence(session: Session, account: CreditAccount, now: datetime) -> None:
    """Part-payment resets the attempt counter but not the level.

    The level records how the relationship has actually gone; the counter records
    how hard we are currently pushing. Only the second one should soften.
    """
    state = _escalation(session, account)
    if state is None:
        return
    state.attempts_at_level = 0
    state.last_contact_at = None
    state.history = list(state.history or []) + [
        {"at": now.isoformat(), "event": "part_payment_cadence_reset"}
    ]


DISPUTE_RAISED = "disputed"
DISPUTE_CLEARED = "dispute_cleared"
_DISPUTE_EVENTS = frozenset({DISPUTE_RAISED, DISPUTE_CLEARED})


def raise_dispute(
    session: Session, account: CreditAccount, *, reason: str, now: datetime | None = None
) -> CreditAccount:
    """Halt all automated contact for this account."""
    now = now or datetime.now(timezone.utc)
    account.status = AccountStatus.IN_DISPUTE
    account.disputed_reason = reason
    account.status_updated_at = now
    # Made rather than looked up: the history is where "was this ever contested"
    # is answered, and an account with no ladder row yet would otherwise record
    # the dispute nowhere.
    state = ensure_escalation_state(session, account, now=now)
    state.needs_human_review = True
    state.history = list(state.history or []) + [
        {"at": now.isoformat(), "event": DISPUTE_RAISED, "reason": reason}
    ]
    session.flush()
    return account


def clear_dispute(
    session: Session,
    account: CreditAccount,
    *,
    resolution: str | None = None,
    now: datetime | None = None,
) -> CreditAccount:
    """Lift a dispute, leaving evidence that there was one.

    `disputed_reason` is nulled because the dispute is over, which makes the
    ladder history the *only* surviving record of it. That record is load
    bearing: app.registry.eligibility gates publication on ever-disputed rather
    than currently-disputed, because a debt that was contested is not a fact to
    publish even once the contest is withdrawn. Clear a dispute without a trace
    and the account silently becomes publishable.

    `needs_human_review` is deliberately left alone. Ending a dispute does not
    end whatever else asked for a person to look — an upstream amount change,
    say — and the caller releases the account for contact as a separate act.
    """
    now = now or datetime.now(timezone.utc)
    if account.status is not AccountStatus.IN_DISPUTE:
        return account

    disputed_reason = account.disputed_reason
    account.status = AccountStatus.OVERDUE if account.outstanding_paise else AccountStatus.SETTLED
    account.disputed_reason = None
    account.status_updated_at = now

    state = ensure_escalation_state(session, account, now=now)
    state.history = list(state.history or []) + [
        {
            "at": now.isoformat(),
            "event": DISPUTE_CLEARED,
            "disputed_reason": disputed_reason,
            "resolution": resolution,
        }
    ]
    session.flush()
    return account


def ever_disputed(session: Session, account: CreditAccount) -> bool:
    """Has this account been contested at any point, cleared or not?

    The answer app.registry.eligibility needs. It cannot come from
    `disputed_reason`, which `clear_dispute` nulls, so it comes from the ladder
    history — which is why that history must never be rewritten in place.
    """
    if account.status is AccountStatus.IN_DISPUTE or account.disputed_reason:
        return True
    state = _escalation(session, account)
    if state is None:
        return False
    return any(
        isinstance(entry, dict) and entry.get("event") in _DISPUTE_EVENTS
        for entry in state.history or []
    )


def ensure_escalation_state(
    session: Session, account: CreditAccount, *, now: datetime | None = None
) -> EscalationState:
    state = _escalation(session, account)
    if state is None:
        state = EscalationState(
            company_id=account.company_id,
            account_id=account.id,
            level=EscalationLevel.L1,
            level_entered_at=now or datetime.now(timezone.utc),
        )
        session.add(state)
        session.flush()
    return state


def account_for_invoice(session: Session, invoice_id: UUID) -> CreditAccount | None:
    return session.execute(
        select(CreditAccount).where(CreditAccount.invoice_id == invoice_id)
    ).scalar_one_or_none()
