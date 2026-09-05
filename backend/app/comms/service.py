"""Sending a message, and projecting what came back.

Deliberately the same shape as the voice path: policy decides, an approved
template version is rendered, a derived idempotency key is computed, the row is
written and committed, and only then does anything leave the building.

Idempotency is enforced twice — once in Redis before the provider call and once
by the unique constraint on the row. That is not belt-and-braces for its own
sake: a duplicate SMS to a debtor is a compliance incident rather than a
duplicate row, and messaging retries happen far more often than call retries.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, Message, MessageEvent, MessageStatus
from app.providers.base import ContactBlocked

log = logging.getLogger(__name__)

_EVENT_STATUS = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}

# Sent, delivered and read are three separate facts and are never collapsed.
_RANK = {
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
    MessageStatus.FAILED: 4,
    MessageStatus.BLOCKED: 4,
}


class DuplicateSend(RuntimeError):
    """This exact message was already sent. Never sent again."""


def derive_idempotency_key(
    *,
    company_id: UUID,
    buyer_id: UUID,
    account_id: UUID | None,
    channel: Channel,
    level: str,
    attempt_number: int,
    scheduled_at: datetime,
) -> str:
    """Derived, never random — a retrying worker must compute the same value."""
    payload = "|".join(
        [
            str(company_id),
            str(buyer_id),
            str(account_id or ""),
            channel.value,
            level,
            str(attempt_number),
            scheduled_at.replace(microsecond=0).isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:64]


def _redis_guard(redis_client, key: str, ttl_seconds: int = 86400) -> bool:
    """Claim the key. False means somebody already has it.

    Checked before the provider call rather than only in the database, because
    the window that matters is between our commit and the provider's accept.
    """
    if redis_client is None:
        return True
    try:
        return bool(redis_client.set(f"msg:idem:{key}", "1", nx=True, ex=ttl_seconds))
    except Exception:
        # Redis being down must not become a silent licence to double-send; the
        # database constraint still holds, so proceed and let it decide.
        log.warning("redis idempotency guard unavailable; relying on the DB constraint")
        return True


def queue_message(
    session: Session,
    *,
    company_id: UUID,
    buyer_id: UUID,
    account_id: UUID | None,
    campaign_id: UUID | None,
    channel: Channel,
    level,
    to_address: str,
    body: str,
    template_version_id: UUID | None,
    attempt_number: int,
    scheduled_at: datetime,
    phone_id: UUID | None = None,
) -> Message:
    """Write the QUEUED row. The caller commits before sending."""
    key = derive_idempotency_key(
        company_id=company_id,
        buyer_id=buyer_id,
        account_id=account_id,
        channel=channel,
        level=level.value if hasattr(level, "value") else str(level),
        attempt_number=attempt_number,
        scheduled_at=scheduled_at,
    )
    existing = session.execute(
        select(Message).where(Message.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateSend(f"message {key} already exists as {existing.status.value}")

    message = Message(
        company_id=company_id,
        campaign_id=campaign_id,
        buyer_id=buyer_id,
        account_id=account_id,
        phone_id=phone_id,
        channel=channel,
        level=level,
        to_address=to_address,
        rendered_body=body,
        template_version_id=template_version_id,
        attempt_number=attempt_number,
        idempotency_key=key,
        status=MessageStatus.QUEUED,
    )
    session.add(message)
    session.flush()
    return message


def send(
    session: Session,
    message: Message,
    provider,
    *,
    redis_client=None,
    template_ref: str | None = None,
) -> Message:
    """Hand a committed message to its provider."""
    if not _redis_guard(redis_client, message.idempotency_key):
        raise DuplicateSend(f"{message.idempotency_key} is already in flight")

    try:
        result = provider.send(
            to=message.to_address,
            body=message.rendered_body,
            template_ref=template_ref,
            idempotency_key=message.idempotency_key,
        )
    except ContactBlocked as exc:
        # Non-production tried to message someone outside the allowlist.
        message.status = MessageStatus.BLOCKED
        message.block_reason = "CONTACT_NOT_ALLOWLISTED"
        message.failed_reason = str(exc)
        session.flush()
        raise

    if not result.accepted:
        message.status = MessageStatus.FAILED
        message.failed_reason = result.detail
        session.flush()
        return message

    message.provider_message_id = result.provider_message_id
    message.cost_paise = result.cost_paise
    message.status = MessageStatus.SENT
    message.sent_at = datetime.now(timezone.utc)
    session.flush()
    return message


def ingest_event(
    session: Session,
    *,
    message: Message,
    source: str,
    event_type: str,
    dedupe_key: str,
    payload: dict,
    occurred_at: datetime | None = None,
) -> Message:
    """Append the webhook, then project. Late, duplicated and out of order are
    all normal."""
    occurred_at = occurred_at or datetime.now(timezone.utc)
    already = session.execute(
        select(MessageEvent).where(
            MessageEvent.message_id == message.id,
            MessageEvent.source == source,
            MessageEvent.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if already is not None:
        return message

    session.add(
        MessageEvent(
            company_id=message.company_id,
            message_id=message.id,
            source=source,
            event_type=event_type,
            dedupe_key=dedupe_key,
            payload=payload,
            occurred_at=occurred_at,
        )
    )

    if event_type == "sent":
        message.sent_at = message.sent_at or occurred_at
    elif event_type == "delivered":
        message.delivered_at = message.delivered_at or occurred_at
    elif event_type == "read":
        message.read_at = message.read_at or occurred_at
    elif event_type == "failed":
        message.failed_reason = payload.get("reason", "provider reported failure")

    status = _EVENT_STATUS.get(event_type)
    if status is not None and _RANK[status] >= _RANK[message.status]:
        message.status = status

    session.flush()
    return message


def find_by_provider_id(session: Session, provider_message_id: str) -> Message | None:
    return session.execute(
        select(Message).where(Message.provider_message_id == provider_message_id)
    ).scalar_one_or_none()
