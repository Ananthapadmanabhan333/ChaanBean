"""Closing calls the event stream lost.

ARI has no replay and no cursor. A dropped websocket loses the hangup
permanently and the call sits in DIALING forever; reconnecting does not fix it,
because there is nothing to reconnect *to*. Three independent layers are needed,
and all three are needed:

1. **Reconnect diff** — on reconnect, ask Asterisk which channels are live and
   resolve every DIALING call that is not among them.
2. **Stale sweeper** — anything DIALING past (max message duration + grace) gets
   reconciled on a timer, whether or not a reconnect happened.
3. **CDR** — Asterisk writes its own record at hangup, entirely independent of
   the websocket. It joins on `userfield`, which origination set to the call id.
   This is both the answer to "the stream dropped" and the evidence trail for an
   L3 call.

Resolution rule when all three come up empty: `UNKNOWN`, classified `COUNTS`
against the cap. You cannot prove the phone did not ring, so fail safe toward
the debtor rather than granting a free attempt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.calls import outcomes, projection
from app.config import settings
from app.models import Call, CallStatus, EventSource

log = logging.getLogger(__name__)

# Longest plausible message plus dialling and teardown slack.
DEFAULT_GRACE_SECONDS = 90


def stale_threshold(*, max_message_ms: int = 60_000) -> timedelta:
    return timedelta(
        seconds=(max_message_ms / 1000) + DEFAULT_GRACE_SECONDS + settings.call_stuck_after_sec
    )


def in_flight(session: Session) -> list[Call]:
    return list(
        session.execute(
            select(Call).where(Call.status.in_([CallStatus.DIALING, CallStatus.ANSWERED]))
        ).scalars()
    )


def resolve_from_cdr(session: Session, call: Call) -> bool:
    """Look for Asterisk's own record of this call.

    Requires `cdr_adaptive_odbc` writing into this database. When the table is
    absent — a local stack without ODBC configured — this returns False rather
    than raising, and the sweeper falls through to UNKNOWN.
    """
    # A SAVEPOINT, not a bare try/except. A missing `cdr` table aborts the
    # PostgreSQL transaction, and recovering with `session.rollback()` would
    # discard everything the caller had pending — including the call row this
    # function is about to write an event against.
    try:
        with session.begin_nested():
            row = (
                session.execute(
                    text(
                        "SELECT disposition, duration, billsec, uniqueid, \"end\" "
                        "FROM cdr WHERE userfield = :uid ORDER BY \"end\" DESC LIMIT 1"
                    ),
                    {"uid": str(call.id)},
                )
                .mappings()
                .first()
            )
    except Exception:
        # No CDR table configured, or the query failed. Fall through to the
        # sweeper's own resolution rather than pretending we know the outcome.
        return False

    if row is None:
        return False

    disposition = (row.get("disposition") or "").upper()
    status = {
        "ANSWERED": CallStatus.ANSWERED,
        "NO ANSWER": CallStatus.NO_ANSWER,
        "BUSY": CallStatus.BUSY,
        "FAILED": CallStatus.FAILED,
        "CONGESTION": CallStatus.FAILED,
    }.get(disposition, CallStatus.UNKNOWN)

    projection.ingest(
        session,
        call=call,
        source=EventSource.CDR,
        event_type="ChannelDestroyed",
        dedupe_key=f"cdr:{row.get('uniqueid') or call.id}",
        payload={
            "disposition": disposition,
            "duration": row.get("duration"),
            "billsec": row.get("billsec"),
            "source": "cdr",
        },
        occurred_at=row.get("end") or datetime.now(timezone.utc),
    )
    if status is not CallStatus.ANSWERED:
        call.status = status
    outcomes.apply_outcome(session, call)
    return True


def reconcile_call(
    session: Session, call: Call, *, live_channel_ids: set[str] | None = None
) -> bool:
    """Resolve one in-flight call. Returns True if it was closed."""
    if live_channel_ids is not None and str(call.id) in live_channel_ids:
        return False  # genuinely still up; leave it alone

    if resolve_from_cdr(session, call):
        log.info("call %s resolved from CDR as %s", call.id, call.status.value)
        return True

    # No live channel, no CDR, past the threshold. We cannot prove the phone did
    # not ring, so the attempt is spent.
    projection.ingest(
        session,
        call=call,
        source=EventSource.WORKER,
        event_type="ChannelDestroyed",
        dedupe_key=f"reconciler:{call.id}",
        payload={"reason": "no live channel and no CDR", "source": "reconciler"},
    )
    if not call.status.is_terminal:
        call.status = CallStatus.UNKNOWN
    outcomes.apply_outcome(session, call)
    log.warning("call %s reconciled to UNKNOWN", call.id)
    return True


def sweep(
    session: Session,
    *,
    now: datetime | None = None,
    live_channel_ids: set[str] | None = None,
    max_message_ms: int = 60_000,
) -> list[Call]:
    """Close every in-flight call that has outlived the threshold."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - stale_threshold(max_message_ms=max_message_ms)

    resolved = []
    for call in in_flight(session):
        started = call.originated_at or call.scheduled_at
        if started is None or started > cutoff:
            continue
        if reconcile_call(session, call, live_channel_ids=live_channel_ids):
            resolved.append(call)
    session.flush()
    return resolved


def reconnect_diff(session: Session, live_channel_ids: set[str]) -> list[Call]:
    """After a websocket reconnect: anything in flight but not live is finished.

    Runs immediately rather than waiting for the sweeper, because the gap this
    closes is exactly the one the reconnect just revealed.
    """
    resolved = []
    for call in in_flight(session):
        if str(call.id) in live_channel_ids:
            continue
        if reconcile_call(session, call, live_channel_ids=live_channel_ids):
            resolved.append(call)
    session.flush()
    return resolved
