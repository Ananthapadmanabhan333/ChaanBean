"""Correcting what we hold about a debtor, and stopping when they tell us to.

Everything else in this codebase asserts: it dials, it messages, it escalates.
This is the other direction — the debtor saying "that is not my name" or "stop
calling me" — and until now only one shape of that could be recorded at all, a
keypress during a live call. A withdrawal that arrives by letter, by email or
across a counter is the ordinary case, not the exotic one.

Three separations run through the file. Each of them is a thing that goes
wrong when it is collapsed:

* **Consent is not the debt.** Withdrawing consent stops contact and nothing
  else. If anything here could reach `CreditAccount` or `Invoice`, a debtor's
  opt-out would quietly write off their balance — so those names do not appear
  in this module, and a test asserts they never do.
* **A channel opt-out is not a withdrawal.** STOP on SMS suppresses SMS.
  Reading it as a withdrawal over-blocks, silencing a channel the debtor never
  objected to; recording a real withdrawal as one channel under-blocks, and
  that is the compliance incident.
* **The correction is not the record of it.** The edit is one row; the audit is
  what answers "whose name was on the notice we sent in March, and who changed
  it". So every function here returns the before and after of what it moved and
  writes no audit itself. Not a layering rule — `app.identity` sits *below*
  `app.trade` and could be imported — but an accuracy one: the actor lives in
  the request, and this module has no way to know who asked.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Buyer, BuyerPhone, CampaignTarget, Channel, ChannelOptOut


class ConsentRefusal(str, enum.Enum):
    """Why a correction or a cessation was refused. Closed set, defined once.

    A refusal that reaches a person as "invalid input" is not auditable, and
    every one of these is reachable from a form somebody will fill in wrong.
    """

    REASON_REQUIRED = "REASON_REQUIRED"
    SOURCE_NOT_RECOGNISED = "SOURCE_NOT_RECOGNISED"
    CHANNEL_NOT_RECOGNISED = "CHANNEL_NOT_RECOGNISED"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    SUPPRESSION_NOT_IN_FUTURE = "SUPPRESSION_NOT_IN_FUTURE"
    FIELD_NOT_CORRECTABLE = "FIELD_NOT_CORRECTABLE"
    VALUE_REJECTED = "VALUE_REJECTED"


class ConsentError(ValueError):
    """Refused. Carries the enum member so a caller can map it without parsing."""

    def __init__(self, refusal: ConsentRefusal, detail: str):
        super().__init__(f"{refusal.value}: {detail}")
        self.refusal = refusal
        self.detail = detail


class ConsentSource(str, enum.Enum):
    """How the instruction reached us.

    Closed, because "who told you, and how" is the first question asked when a
    withdrawal is disputed — and because the answer decides what evidence has to
    exist behind it. Values are lowercase to match `ChannelOptOut.source`.
    """

    DTMF = "dtmf"  # pressed 9 during a live call
    SMS_STOP = "sms_stop"
    EMAIL = "email"
    LETTER = "letter"
    IN_PERSON = "in_person"
    PHONE = "phone"  # said so to a human on the line
    PORTAL = "portal"
    WEBHOOK = "webhook"
    IMPORT = "import"


@dataclass(frozen=True)
class ContactChange:
    """What moved, in the shape an audit row wants.

    `before` and `after` land in a JSONB column, so they carry ISO strings and
    plain values rather than datetimes.
    """

    entity_type: str
    entity_id: uuid.UUID
    before: dict[str, Any]
    after: dict[str, Any]
    detail: str

    @property
    def changed(self) -> bool:
        return self.before != self.after


# ------------------------------------------------------------------- small things


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware(value: datetime, label: str) -> datetime:
    """Refuse a timestamp with no offset.

    This codebase is UTC internally and Asia/Kolkata to every human who reads a
    date, so a naive value is a guess about which of the two it is — and the
    guess is worth five and a half hours of contact in one direction or the
    other.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ConsentError(
            ConsentRefusal.NAIVE_TIMESTAMP, f"{label} must carry a timezone"
        )
    return value


def _reason(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise ConsentError(
            ConsentRefusal.REASON_REQUIRED,
            "say why: this is the sentence a person reads back when the record "
            "is questioned",
        )
    return text


def _source(value: ConsentSource | str) -> ConsentSource:
    try:
        return ConsentSource(value)
    except ValueError:
        raise ConsentError(
            ConsentRefusal.SOURCE_NOT_RECOGNISED,
            f"{value!r} is not a recorded source; one of "
            f"{sorted(s.value for s in ConsentSource)}",
        ) from None


def _channel(value: Channel | str) -> Channel:
    try:
        return Channel(value)
    except ValueError:
        raise ConsentError(
            ConsentRefusal.CHANNEL_NOT_RECOGNISED,
            f"{value!r} is not a channel; one of {sorted(c.value for c in Channel)}",
        ) from None


# ------------------------------------------------------------------------ consent


def _consent_state(buyer: Buyer) -> dict[str, Any]:
    return {
        "consent_withdrawn": buyer.consent_withdrawn,
        "consent_withdrawn_at": _iso(buyer.consent_withdrawn_at),
        "next_action_at": _iso(buyer.next_action_at),
    }


def record_consent_withdrawal(
    session: Session,
    buyer: Buyer,
    *,
    reason: str,
    source: ConsentSource | str,
    now: datetime | None = None,
) -> ContactChange:
    """Record that this person has told us to stop contacting them.

    Idempotent on the flag but never on the date: a second letter leaves
    `consent_withdrawn_at` where it is, because the date that matters is the
    first one — it is the date from which every later contact was contact they
    had already refused.

    Stopping contact is the whole of the effect. Nothing here reads or writes a
    balance, an invoice or an account status; a debtor who says "stop calling"
    still owes exactly what they owed a moment before.
    """
    now = _aware(now or datetime.now(timezone.utc), "now")
    reason = _reason(reason)
    src = _source(source)

    before = _consent_state(buyer)
    if not buyer.consent_withdrawn:
        buyer.consent_withdrawn = True
        buyer.consent_withdrawn_at = now
    # `None` is already how this codebase says "nothing further is due for this
    # buyer" — app.calls.outcomes parks an undialable buyer that way and the
    # campaign sweep reads it as exhausted. Without it the scheduler wakes on
    # this buyer every few hours to write the same CONSENT_WITHDRAWN refusal.
    buyer.next_action_at = None
    session.flush()

    return ContactChange(
        entity_type="buyer",
        entity_id=buyer.id,
        before=before,
        after=_consent_state(buyer),
        detail=f"consent withdrawn via {src.value}: {reason}",
    )


def restore_consent(
    session: Session,
    buyer: Buyer,
    *,
    reason: str,
    source: ConsentSource | str,
    now: datetime | None = None,
) -> ContactChange:
    """Record that the person has agreed to be contacted again.

    The buyer row carries current state, not history: the flag goes back to
    false and the date is cleared, because `consent_withdrawn=False` sitting
    beside a withdrawal date reads as "withdrawn" to the next person to open the
    row. What happened survives in the audit entry the caller writes from
    `before` — which is why this refuses to run without a reason and a source.

    The withdrawal parked the buyer, and only this unparks them. The scheduler
    claims on a non-null `next_action_at`, and every other writer of it is now
    guarded against a withdrawn buyer, so a restore that left it null would turn
    contact back on in name only — and this permission is documented as the one
    act in the product that turns contact back on.

    Only for a buyer some campaign is still working, and only when nothing is
    already scheduled. A null wake-up on an untargeted buyer means nobody is
    chasing them at all, and inventing one here would enrol them by side effect.
    """
    now = _aware(now or datetime.now(timezone.utc), "now")
    reason = _reason(reason)
    src = _source(source)

    before = _consent_state(buyer)
    buyer.consent_withdrawn = False
    buyer.consent_withdrawn_at = None
    if buyer.next_action_at is None and _actively_targeted(session, buyer):
        buyer.next_action_at = now
    session.flush()

    return ContactChange(
        entity_type="buyer",
        entity_id=buyer.id,
        before=before,
        after=_consent_state(buyer),
        # `now` is when the person agreed, which is not when the row was
        # written: a letter dated last week is processed today, and the audit
        # entry's own `occurred_at` records only the second of those.
        detail=f"consent restored via {src.value} on {now.isoformat()}: {reason}",
    )


def _actively_targeted(session: Session, buyer: Buyer) -> bool:
    return (
        session.execute(
            select(CampaignTarget).where(
                CampaignTarget.buyer_id == buyer.id,
                CampaignTarget.is_active.is_(True),
            )
        ).scalar_one_or_none()
        is not None
    )


def suppress_until(
    session: Session,
    buyer: Buyer,
    *,
    until: datetime,
    reason: str,
    now: datetime | None = None,
) -> ContactChange:
    """Hold all contact until a date. Temporary, and never shortened here.

    A hold that already runs past `until` stays where it is. Two people work the
    same file — one records a fortnight of hospital leave, the other a promise
    to pay on Friday — and the shorter of the two must not silently resume
    calling in the middle of the longer. Lifting a hold early is a deliberate
    act with its own evidence; it is not something this function can be talked
    into doing by accident, so the effective date is returned rather than
    assumed.
    """
    now = _aware(now or datetime.now(timezone.utc), "now")
    until = _aware(until, "until")
    reason = _reason(reason)

    if until <= now:
        raise ConsentError(
            ConsentRefusal.SUPPRESSION_NOT_IN_FUTURE,
            f"a hold until {until.isoformat()} has already expired at {now.isoformat()}",
        )

    existing = buyer.suppressed_until
    effective = until if existing is None else max(until, existing)

    before = {
        "suppressed_until": _iso(existing),
        "next_action_at": _iso(buyer.next_action_at),
    }
    buyer.suppressed_until = effective
    # Pushed forward, never created. `None` means a human has parked this buyer
    # and the scheduler is not to pick them up at all; inventing a wake-up here
    # would undo that.
    if buyer.next_action_at is not None and buyer.next_action_at < effective:
        buyer.next_action_at = effective
    session.flush()

    detail = f"contact suppressed until {effective.isoformat()}: {reason}"
    if effective != until:
        detail += f" (requested {until.isoformat()}; a longer hold already stood)"

    return ContactChange(
        entity_type="buyer",
        entity_id=buyer.id,
        before=before,
        after={
            "suppressed_until": _iso(buyer.suppressed_until),
            "next_action_at": _iso(buyer.next_action_at),
        },
        detail=detail,
    )


# ------------------------------------------------------------------- opt-out


def record_channel_optout(
    session: Session,
    buyer: Buyer,
    *,
    channel: Channel | str,
    source: ConsentSource | str,
    now: datetime | None = None,
) -> ContactChange:
    """Suppress one channel. The missing writer for `ChannelOptOut`.

    Nothing constructed this row, so the Policy Engine's CHANNEL_OPTED_OUT
    refusal could never fire and a STOP could only be honoured by escalating it
    into a full withdrawal.

    It stays one channel. STOP on SMS says nothing about the phone ringing, and
    the row deliberately does not touch the buyer: consent is a property of the
    person, a channel opt-out is a preference about how to reach them.

    Repeating is not re-recording. A second STOP finds the existing row and
    leaves its date alone — the first refusal is the one every later message was
    sent in defiance of.
    """
    now = _aware(now or datetime.now(timezone.utc), "now")
    chan = _channel(channel)
    src = _source(source)

    existing = session.execute(
        select(ChannelOptOut).where(
            ChannelOptOut.buyer_id == buyer.id, ChannelOptOut.channel == chan
        )
    ).scalar_one_or_none()

    before = _optout_state(chan, existing)
    row = existing
    if row is None:
        row = ChannelOptOut(
            company_id=buyer.company_id,
            buyer_id=buyer.id,
            channel=chan,
            source=src.value,
            opted_out_at=now,
        )
        session.add(row)
        session.flush()

    return ContactChange(
        entity_type="channel_opt_out",
        entity_id=row.id,
        before=before,
        after=_optout_state(chan, row),
        detail=f"{chan.value} opted out via {src.value}",
    )


def _optout_state(channel: Channel, row: ChannelOptOut | None) -> dict[str, Any]:
    return {
        "channel": channel.value,
        "opted_out": row is not None,
        "source": row.source if row is not None else None,
        "opted_out_at": _iso(row.opted_out_at) if row is not None else None,
    }


# --------------------------------------------------------------------- numbers


def invalidate_phone(
    session: Session, phone: BuyerPhone, *, reason: str
) -> ContactChange:
    """Retire a number without hiding what was done with it.

    `is_valid` had no writer outside a carrier hangup cause, so a number typed
    wrong at import could be dialled forever: the Policy Engine reads the flag
    when it picks a number, and the dialler spends the debtor's daily cap on a
    stranger's phone until somebody notices.

    Flagged, never deleted. Calls point at this row, and "who did we ring on 12
    March, and on what number" has to stay answerable afterwards. The reason
    goes back to the caller rather than onto the row, because there is no column
    for it — it belongs in the audit entry, beside the person who decided.
    """
    reason = _reason(reason)

    before = {"e164": phone.e164, "is_valid": phone.is_valid}
    phone.is_valid = False
    session.flush()

    return ContactChange(
        entity_type="buyer_phone",
        entity_id=phone.id,
        before=before,
        after={"e164": phone.e164, "is_valid": phone.is_valid},
        detail=f"number retired: {reason}",
    )


# ------------------------------------------------------------------ correction


_NAME_MAX = 200
_EMAIL_MAX = 255
_LANGUAGE_MAX = 10

# `hi-IN`, `ta`, not `hindi` and not `hi_IN`.
_LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED,
            f"{field} must be text, not {type(value).__name__}",
        )
    return value.strip()


def _clean_name(value: Any) -> str:
    text = _text(value, "name")
    if not text:
        raise ConsentError(ConsentRefusal.VALUE_REJECTED, "a buyer must have a name")
    if len(text) > _NAME_MAX:
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED,
            f"name is {len(text)} characters; the column holds {_NAME_MAX}",
        )
    return text


def _clean_email(value: Any) -> str | None:
    """Shape only — nobody has checked that this address exists.

    Checked at all because a correction that stores `ramesh@` sends the next
    demand nowhere and reports success. Clearing the address is allowed: no
    address is better than one that delivers a debt notice to a stranger.
    """
    if value is None:
        return None
    text = _text(value, "email")
    if not text:
        return None
    if len(text) > _EMAIL_MAX:
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED,
            f"email is {len(text)} characters; the column holds {_EMAIL_MAX}",
        )
    local, _, domain = text.partition("@")
    if not local or not domain or "@" in domain or "." not in domain:
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED, f"{text!r} is not shaped like an address"
        )
    if any(ch.isspace() for ch in text):
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED, f"{text!r} contains whitespace"
        )
    return text


def _clean_language(value: Any) -> str:
    """A typo here does not fail closed, which is why the shape is checked.

    When no template matches the buyer's language the engine falls back to any
    template at that level — so `hindi` instead of `hi-IN` does not stop the
    call, it plays the wrong language at the debtor.
    """
    text = _text(value, "language")
    if not _LANGUAGE_TAG.match(text) or len(text) > _LANGUAGE_MAX:
        raise ConsentError(
            ConsentRefusal.VALUE_REJECTED,
            f"{text!r} is not a language tag like 'en-IN' or 'ta'",
        )
    return text


# The whitelist, paired with what validates each field so the two cannot drift.
#
# Consent and suppression are absent because they have their own functions
# above, with their own evidence. The declared GSTIN and CIN are absent because
# replacing them is what voids a verification tier: written here, a profile
# would go on saying IDENTIFIER while carrying a number nobody has checked, and
# that label is what a listing or a notice is issued against.
_CORRECTORS = {
    "name": _clean_name,
    "email": _clean_email,
    "language": _clean_language,
}

CORRECTABLE_FIELDS = frozenset(_CORRECTORS)


def correct_buyer(
    session: Session, buyer: Buyer, *, fields: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a whitelist of correctable fields, and hand back what moved.

    The audit matters more than the edit. `name` is what a legal notice says, so
    "who was it addressed to before, and who changed it" has to survive the
    correction — hence a pair of dicts rather than a boolean.

    Only fields that actually change appear in them, so two empty dicts mean
    somebody re-submitted a form and there is nothing to record.
    """
    unknown = sorted(set(fields) - CORRECTABLE_FIELDS)
    if unknown:
        raise ConsentError(
            ConsentRefusal.FIELD_NOT_CORRECTABLE,
            f"{unknown} cannot be corrected here; correctable: "
            f"{sorted(CORRECTABLE_FIELDS)}",
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    # Every value is validated before anything is written: a request naming a
    # good name and a bad email must not leave the name changed and the caller
    # holding an exception.
    cleaned = {field: _CORRECTORS[field](fields[field]) for field in sorted(fields)}

    for field, value in cleaned.items():
        current = getattr(buyer, field)
        if value == current:
            continue
        before[field] = current
        after[field] = value
        setattr(buyer, field, value)

    session.flush()
    return before, after
