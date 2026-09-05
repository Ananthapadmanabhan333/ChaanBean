"""What a finished call does to the rest of the system.

Answering-machine detection, and honesty about it
-------------------------------------------------
Asterisk's `AMD()` is wired in and mapped to `ANSWERED_MACHINE`, but it is off by
default per campaign, and that default is deliberate. Two things are both true:

* It is heuristic and unreliable against Indian networks. Operator ringback,
  regional intercept announcements and network audio all confuse it.
* **It needs two to four seconds of listening before it decides.** A human who
  answers and hears three seconds of silence hangs up. So AMD actively *reduces*
  the human delivery rate it is meant to protect.

Since the L3 gate already proves a human is present by asking for a keypress, AMD
earns its place on L1 and L2 at most. `ANSWERED_MACHINE` is therefore recorded as
a distinct, non-delivered outcome rather than trusted as truth — a machine answer
never counts as contact, so three voicemails can never escalate someone to legal
content they have not heard.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttemptClass,
    Buyer,
    BuyerPhone,
    Call,
    CallStatus,
    EscalationState,
)
from app.policy import classify_attempt, next_retry_delay, record_attempt


def apply_outcome(session: Session, call: Call, *, now: datetime | None = None) -> Call:
    """Fold a terminal call into phone health, ladder state and consent."""
    now = now or datetime.now(timezone.utc)
    if not call.status.is_terminal and call.status is not CallStatus.ANSWERED:
        return call

    klass = classify_attempt(call.status, call.hangup_cause)
    call.counts_against_cap = klass is not AttemptClass.CARRIER_FAULT

    phone = session.get(BuyerPhone, call.phone_id) if call.phone_id else None
    if phone is not None:
        phone.last_outcome = call.status.value
        phone.last_attempted_at = now
        if klass is AttemptClass.TERMINAL_BAD_NUMBER:
            # The carrier says this number is dead. Stop dialling it — and this
            # is why `last_outcome` exists rather than being decorative.
            phone.is_valid = False

    buyer = session.get(Buyer, call.buyer_id)

    if call.opted_out and buyer is not None and not buyer.consent_withdrawn:
        buyer.consent_withdrawn = True
        buyer.consent_withdrawn_at = now

    state = session.execute(
        select(EscalationState).where(EscalationState.account_id == call.account_id)
    ).scalar_one_or_none()
    if state is not None:
        delivered = call.delivered
        attempts, delivered_count = record_attempt(
            delivered, state.attempts_at_level, state.delivered_at_level
        )
        state.attempts_at_level = attempts
        state.delivered_at_level = delivered_count
        state.last_contact_at = now
        if delivered:
            state.last_delivered_at = now
        state.history = list(state.history or []) + [
            {
                "at": now.isoformat(),
                "event": "call_outcome",
                "call_id": str(call.id),
                "status": call.status.value,
                "delivered": delivered,
                "counts": call.counts_against_cap,
            }
        ]

    if buyer is not None:
        if klass is AttemptClass.TERMINAL_BAD_NUMBER and not _has_other_valid_phone(
            session, buyer, call.phone_id
        ):
            # Nothing left to dial. A human decides what happens next rather than
            # the scheduler retrying into a wall.
            buyer.next_action_at = None
        else:
            delay = next_retry_delay(call.status, call.hangup_cause, call.attempt_number)
            buyer.next_action_at = now + delay

    session.flush()
    return call


def _has_other_valid_phone(session: Session, buyer: Buyer, exclude_phone_id) -> bool:
    return bool(
        session.execute(
            select(BuyerPhone).where(
                BuyerPhone.buyer_id == buyer.id,
                BuyerPhone.is_valid.is_(True),
                BuyerPhone.id != exclude_phone_id,
            )
        )
        .scalars()
        .first()
    )
