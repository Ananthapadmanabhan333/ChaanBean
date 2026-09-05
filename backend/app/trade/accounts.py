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


def sync_account_from_invoice(
    session: Session, invoice: Invoice, *, now: datetime | None = None
) -> CreditAccount:
    """Create or update the recovery projection of one invoice.

    Status transitions, and what each means for the ladder:

    * fully settled  -> SETTLED, and the ladder stops
    * partly paid    -> stays OVERDUE, but the cadence resets. Someone who just
      paid something is engaging, and escalating at the same pace is how you lose
      a customer who was cooperating.
    * written off    -> WRITTEN_OFF, ladder stops
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
    # cleared deliberately, never by a payment landing.
    if account.status is AccountStatus.IN_DISPUTE:
        return account

    if invoice.status is InvoiceStatus.WRITTEN_OFF:
        account.status = AccountStatus.WRITTEN_OFF
    elif invoice.outstanding_paise == 0:
        account.status = AccountStatus.SETTLED
    elif invoice.due_date and _as_datetime(invoice.due_date) <= now:
        account.status = AccountStatus.OVERDUE
    else:
        account.status = AccountStatus.CURRENT

    if account.status is not previous_status:
        account.status_updated_at = now

    paid_something = invoice.outstanding_paise < previous_outstanding
    if account.status is AccountStatus.SETTLED:
        _stop_ladder(session, account, now)
    elif paid_something:
        _reset_cadence(session, account, now)

    session.flush()
    return account


def _escalation(session: Session, account: CreditAccount) -> EscalationState | None:
    return session.execute(
        select(EscalationState).where(EscalationState.account_id == account.id)
    ).scalar_one_or_none()


def _stop_ladder(session: Session, account: CreditAccount, now: datetime) -> None:
    state = _escalation(session, account)
    if state is None:
        return
    state.needs_human_review = False
    state.history = list(state.history or []) + [
        {"at": now.isoformat(), "event": "settled", "level": state.level.value}
    ]


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


def raise_dispute(
    session: Session, account: CreditAccount, *, reason: str, now: datetime | None = None
) -> CreditAccount:
    """Halt all automated contact for this account."""
    now = now or datetime.now(timezone.utc)
    account.status = AccountStatus.IN_DISPUTE
    account.disputed_reason = reason
    account.status_updated_at = now
    state = _escalation(session, account)
    if state is not None:
        state.needs_human_review = True
        state.history = list(state.history or []) + [
            {"at": now.isoformat(), "event": "disputed", "reason": reason}
        ]
    session.flush()
    return account


def clear_dispute(
    session: Session, account: CreditAccount, *, now: datetime | None = None
) -> CreditAccount:
    now = now or datetime.now(timezone.utc)
    if account.status is not AccountStatus.IN_DISPUTE:
        return account
    account.status = AccountStatus.OVERDUE if account.outstanding_paise else AccountStatus.SETTLED
    account.disputed_reason = None
    account.status_updated_at = now
    session.flush()
    return account


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
