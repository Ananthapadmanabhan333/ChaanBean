"""Projecting the event log onto `Call`.

`call_events` is the source of truth; `Call` is a projection of it. That is not
architectural taste — ARI has no event replay and no cursor, so events arrive
late, duplicated, out of order, or never at all. Any design that treats the live
stream as authoritative loses a hangup permanently the first time a websocket
drops.

Three properties, all required:

* **Idempotent.** The same event applied twice changes nothing.
* **Order-independent.** `StasisEnd` can and does land before `PlaybackFinished`.
* **Forward-only.** Status advances through the lattice and never reopens. A late
  `StasisStart` must not drag a completed call back to ANSWERED.

The three delivery facts are set separately and never derived from one another
(rule 15). A call answered at second 0 and dropped at second 2 of a 22-second
message connected and delivered nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Call, CallEvent, CallStatus, EventSource

log = logging.getLogger(__name__)

# How far along the lifecycle each status sits. Only ever move up.
_RANK = {
    CallStatus.SCHEDULED: 0,
    CallStatus.DIALING: 1,
    CallStatus.ANSWERED: 2,
    CallStatus.ANSWERED_MACHINE: 2,
    # Terminal outcomes all sit above any in-progress state.
    CallStatus.NO_ANSWER: 3,
    CallStatus.BUSY: 3,
    CallStatus.FAILED: 3,
    CallStatus.UNKNOWN: 3,
    CallStatus.CANCELLED: 3,
    CallStatus.BLOCKED: 3,
}

TERMINAL = frozenset(
    {
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.FAILED,
        CallStatus.UNKNOWN,
        CallStatus.CANCELLED,
        CallStatus.BLOCKED,
    }
)

# Q.850 to the outcome a human would recognise.
_CAUSE_STATUS = {
    16: CallStatus.ANSWERED,  # normal clearing — only if we saw an answer
    17: CallStatus.BUSY,
    18: CallStatus.NO_ANSWER,  # no user responding
    19: CallStatus.NO_ANSWER,  # no answer from user
    21: CallStatus.FAILED,  # call rejected
    1: CallStatus.FAILED,  # unallocated number
    22: CallStatus.FAILED,  # number changed
    34: CallStatus.FAILED,  # no circuit available
    38: CallStatus.FAILED,  # network out of order
    41: CallStatus.FAILED,  # temporary failure
}


def record_event(
    session: Session,
    *,
    call: Call,
    source: EventSource,
    event_type: str,
    dedupe_key: str,
    payload: dict,
    occurred_at: datetime | None = None,
) -> CallEvent | None:
    """Append to the log. Returns None when this event was already recorded.

    Deduplication is enforced by the unique constraint on
    `(call_id, source, dedupe_key)`; this checks first so a duplicate is a
    no-op rather than an integrity error that poisons the transaction.
    """
    occurred_at = occurred_at or datetime.now(timezone.utc)
    existing = session.execute(
        select(CallEvent).where(
            CallEvent.call_id == call.id,
            CallEvent.source == source,
            CallEvent.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    event = CallEvent(
        company_id=call.company_id,
        call_id=call.id,
        source=source,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
        occurred_at=occurred_at,
    )
    session.add(event)
    session.flush()
    return event


def _advance(call: Call, status: CallStatus) -> None:
    """Move the status forward, never back."""
    if _RANK[status] >= _RANK[call.status]:
        # Among equally-ranked terminal states the first one recorded wins; a
        # later contradictory event does not get to rewrite the outcome.
        if call.status in TERMINAL and status in TERMINAL and call.status is not status:
            return
        call.status = status


def _earliest(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None else min(current, candidate)


def apply_event(call: Call, event: CallEvent) -> Call:
    """Fold one event into the call. Safe to run twice, in any order."""
    kind = event.event_type
    payload = event.payload or {}
    at = event.occurred_at

    if kind == "StasisStart":
        call.answered_at = _earliest(call.answered_at, at)
        _advance(call, CallStatus.ANSWERED)

    elif kind == "PlaybackStarted":
        call.playback_started_at = _earliest(call.playback_started_at, at)

    elif kind == "PlaybackFinished":
        # The only honest source of the "played" fact.
        call.playback_completed = True

    elif kind == "ChannelDtmfReceived":
        digit = str(payload.get("digit", ""))
        if digit == "1":
            call.dtmf_ack = True
        elif digit == "9":
            # Opt-out. Precisely what a regulator asks about, and nearly free
            # now that the DTMF path exists.
            call.opted_out = True

    elif kind == "AmdResult":
        if str(payload.get("status", "")).upper() == "MACHINE":
            _advance(call, CallStatus.ANSWERED_MACHINE)

    elif kind in ("StasisEnd", "ChannelDestroyed"):
        call.ended_at = _earliest(call.ended_at, at)
        cause = payload.get("cause")
        if cause is not None:
            call.hangup_cause = int(cause)
        sip = payload.get("sip_response_code")
        if sip is not None:
            call.sip_response_code = int(sip)

        if kind == "ChannelDestroyed":
            _finalise(call, cause, payload)

    if call.answered_at and call.ended_at and call.duration_sec is None:
        call.duration_sec = max(0, int((call.ended_at - call.answered_at).total_seconds()))

    return call


def _finalise(call: Call, cause, payload: dict) -> None:
    """Decide the terminal status once the channel is gone."""
    if call.answered_at is not None:
        # It was answered. Keep ANSWERED_MACHINE if AMD said so; otherwise the
        # call connected, whatever happened afterwards.
        if call.status is not CallStatus.ANSWERED_MACHINE:
            _advance(call, CallStatus.ANSWERED)
        return

    # The reconciler synthesises this event when it has *no* evidence — no live
    # channel, no CDR. That is not the same as knowing the phone rang unanswered,
    # so it must not borrow NO_ANSWER's meaning. UNKNOWN says what we actually
    # know, and still counts against the cap.
    if payload.get("source") == "reconciler":
        _advance(call, CallStatus.UNKNOWN)
        return

    # Destroyed without ever entering the Stasis app: the channel was never
    # answered. A naive implementation hangs here forever — this is a
    # first-class path, not an edge case.
    if cause is None:
        _advance(call, CallStatus.NO_ANSWER)
        return
    _advance(call, _CAUSE_STATUS.get(int(cause), CallStatus.FAILED))


def rebuild(session: Session, call: Call) -> Call:
    """Recompute a call purely by replaying its events.

    If this does not reproduce the live row, the event log is not actually the
    source of truth and the guarantee is decorative.
    """
    call.answered_at = None
    call.playback_started_at = None
    call.ended_at = None
    call.duration_sec = None
    call.playback_completed = False
    call.dtmf_ack = False
    call.opted_out = False
    call.hangup_cause = None
    call.sip_response_code = None
    call.status = CallStatus.DIALING if call.originated_at else CallStatus.SCHEDULED

    events = session.execute(
        select(CallEvent)
        .where(CallEvent.call_id == call.id)
        .order_by(CallEvent.occurred_at, CallEvent.received_at)
    ).scalars()
    for event in events:
        apply_event(call, event)
    session.flush()
    return call


def ingest(
    session: Session,
    *,
    call: Call,
    source: EventSource,
    event_type: str,
    dedupe_key: str,
    payload: dict,
    occurred_at: datetime | None = None,
) -> Call:
    """Append, then project. In that order, always."""
    event = record_event(
        session,
        call=call,
        source=source,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
        occurred_at=occurred_at,
    )
    if event is None:
        return call  # already seen; the projection already reflects it
    apply_event(call, event)
    session.flush()
    return call


def call_for_channel(session: Session, channel_id: str) -> Call | None:
    """Resolve an ARI channel id back to a call. The channel id *is* the call id."""
    try:
        return session.get(Call, UUID(channel_id))
    except (ValueError, AttributeError):
        return None
