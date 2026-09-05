"""Placing one call.

The ordering here is the whole point:

1. Pre-flight in a **committed** transaction — policy passed, asset READY and
   staged with the right byte size, `Call` written as DIALING with its derived
   idempotency key.
2. **Commit. Then originate.** Never originate from inside an open transaction
   that might roll back: if it does, Asterisk has dialled a debtor and you have
   no record that it happened.
3. On `StasisStart`, gate L3 behind a keypress, then play.

The idempotency key is derived, never random, so a retrying worker computes the
same value and Asterisk answers 409 instead of dialling twice.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calls import projection
from app.models import (
    AssetStatus,
    AudioAsset,
    Call,
    CallerId,
    CallStatus,
    EscalationLevel,
    EventSource,
)
from app.telephony.staging import StagingError

log = logging.getLogger(__name__)

# The prompt that proves a human is present before any account detail is spoken.
# Says who it is for and nothing about the debt — whoever picks up learns nothing
# they should not, which is the entire point of the gate.
DTMF_GATE_PROMPT = "gate-prompt"
DTMF_GATE_TIMEOUT_SEC = 8


class CallBlocked(RuntimeError):
    """Pre-flight refused. Carries the reason for the audit trail."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PreflightResult:
    call: Call
    asset: AudioAsset
    media_name: str


def derive_idempotency_key(
    *, company_id: UUID, buyer_id: UUID, account_id: UUID, level: EscalationLevel,
    attempt_number: int, scheduled_at: datetime,
) -> str:
    """Derived, never random.

    A worker that crashes after originating and retries must compute the same
    value, or the retry becomes a second call to the same debtor.
    """
    payload = "|".join(
        [
            str(company_id),
            str(buyer_id),
            str(account_id),
            level.value,
            str(attempt_number),
            scheduled_at.replace(microsecond=0).isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:64]


def resolve_caller_id(session: Session, company_id: UUID, campaign_caller_id) -> str | None:
    """A carrier-approved number belonging to *this* company.

    Checked against the company rather than trusted from the campaign row, so a
    misconfiguration cannot make one tenant present another tenant's CLI.
    """
    if campaign_caller_id is None:
        chosen = session.execute(
            select(CallerId).where(
                CallerId.company_id == company_id,
                CallerId.carrier_approved.is_(True),
                CallerId.is_default.is_(True),
            )
        ).scalar_one_or_none()
    else:
        chosen = session.execute(
            select(CallerId).where(
                CallerId.id == campaign_caller_id, CallerId.company_id == company_id
            )
        ).scalar_one_or_none()

    if chosen is None:
        return None
    if not chosen.carrier_approved:
        raise CallBlocked(
            "CALLER_ID_NOT_APPROVED",
            f"{chosen.e164} is not carrier-approved and would be rejected",
        )
    return chosen.e164


def preflight(
    session: Session,
    *,
    call: Call,
    asset: AudioAsset,
    stager,
) -> PreflightResult:
    """Refuse rather than dial into a known-bad state."""
    if asset.status is not AssetStatus.READY:
        raise CallBlocked("AUDIO_NOT_READY", f"asset is {asset.status.value}")
    if not asset.storage_key:
        raise CallBlocked("AUDIO_NOT_READY", "asset has no stored object")

    try:
        stager.assert_staged(asset)
    except StagingError as exc:
        # A policy gate, not a best-effort optimisation. Dialling a debtor and
        # playing silence is worse than not calling.
        raise CallBlocked("AUDIO_NOT_STAGED", str(exc)) from exc

    call.audio_asset_id = asset.id
    call.message_hash = asset.message_hash
    session.flush()
    return PreflightResult(call=call, asset=asset, media_name=stager.media_name(asset))


def originate(session: Session, client, call: Call, *, caller_id: str | None) -> Call:
    """Dial. Must be called on a committed `Call` row.

    A 409 means we already dialled: record that and reconcile, never re-dial.
    """
    outcome = client.originate(
        call_id=call.id, to_e164=call.to_e164, caller_id=caller_id
    )

    call.provider_call_id = outcome.channel_id
    if call.originated_at is None:
        call.originated_at = datetime.now(timezone.utc)
    if call.status is CallStatus.SCHEDULED:
        call.status = CallStatus.DIALING

    projection.ingest(
        session,
        call=call,
        source=EventSource.WORKER,
        event_type="Originated",
        dedupe_key=f"originate:{outcome.channel_id}",
        payload={"created": outcome.created, "detail": outcome.detail},
    )
    if not outcome.created:
        log.info("call %s was already dialled (%s)", call.id, outcome.detail)
    session.flush()
    return call


def on_stasis_start(session: Session, client, call: Call, media_name: str) -> None:
    """Channel entered the app. Gate L3, then play."""
    if call.level is EscalationLevel.L3 and not call.dtmf_ack:
        # Play only the gate prompt. Nothing about the account is spoken until a
        # keypress proves a human is on the line — whoever answered a shared
        # office line learns nothing they should not.
        client.play(str(call.id), DTMF_GATE_PROMPT)
        return
    client.play(str(call.id), media_name)


def on_dtmf(session: Session, client, call: Call, digit: str, media_name: str) -> None:
    if digit == "1" and call.level is EscalationLevel.L3:
        client.play(str(call.id), media_name)
    elif digit == "9":
        client.hangup(str(call.id))


def on_playback_finished(session: Session, client, call: Call) -> None:
    """The message is done. Nothing is gained by holding the channel open."""
    if call.level is EscalationLevel.L3 and not call.dtmf_ack:
        # That was the gate prompt, not the message. Wait for the keypress; the
        # sweeper will close this if nobody presses anything.
        return
    client.hangup(str(call.id))
