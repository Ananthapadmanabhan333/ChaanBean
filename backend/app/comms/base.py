"""Message providers.

Everything below works fully against `FakeProvider`, which is the default. The
real providers need accounts, and two of them need approvals with lead times
measured in weeks:

* **SMS** requires TRAI DLT registration of the header and of every template.
  Templates need re-approval whenever the copy changes, which makes template
  management a workflow rather than a settings field.
* **WhatsApp** — confirm with Meta that this collections use case is permitted
  *before* designing around it. Their messaging policies restrict debt
  collection, and products have been caught by this after building on it.

Neither approval blocks the build, and that is the point of the seam.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from app.models import Channel
from app.providers.base import assert_contactable


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    accepted: bool
    detail: str = ""
    cost_paise: int | None = None


@dataclass(frozen=True)
class ProviderEvent:
    """One delivery-state change from a provider webhook."""

    provider_message_id: str
    event_type: str  # sent | delivered | read | failed
    occurred_at: datetime
    dedupe_key: str
    payload: dict = field(default_factory=dict)


@runtime_checkable
class MessageProvider(Protocol):
    channel: Channel
    name: str

    def send(
        self, *, to: str, body: str, template_ref: str | None, idempotency_key: str
    ) -> SendResult: ...

    def parse_webhook(self, payload: dict) -> list[ProviderEvent]: ...


class FakeProvider:
    """Accepts everything, remembers everything, sends nothing.

    Honours the contact allowlist like a real provider would, so a test that
    would have messaged a real number fails here rather than in production.
    """

    def __init__(self, channel: Channel):
        self.channel = channel
        self.name = f"fake-{channel.value.lower()}"
        self.sent: list[dict] = []
        self.fail_next = False
        # Keyed by idempotency key, so a repeated send returns the first result
        # instead of producing a second message — the same guarantee a good
        # provider offers, asserted here so the tests can rely on it.
        self._by_key: dict[str, SendResult] = {}

    def send(
        self, *, to: str, body: str, template_ref: str | None, idempotency_key: str
    ) -> SendResult:
        assert_contactable(to)
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        if self.fail_next:
            self.fail_next = False
            return SendResult("", accepted=False, detail="simulated provider rejection")

        provider_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        self.sent.append(
            {
                "to": to,
                "body": body,
                "template_ref": template_ref,
                "idempotency_key": idempotency_key,
                "provider_message_id": provider_id,
            }
        )
        result = SendResult(provider_id, accepted=True, cost_paise=15)
        self._by_key[idempotency_key] = result
        return result

    def parse_webhook(self, payload: dict) -> list[ProviderEvent]:
        return [
            ProviderEvent(
                provider_message_id=payload["provider_message_id"],
                event_type=payload["event"],
                occurred_at=payload.get("at") or datetime.now(timezone.utc),
                dedupe_key=payload.get(
                    "dedupe_key", f"{payload['provider_message_id']}:{payload['event']}"
                ),
                payload=payload,
            )
        ]

    def deliver(self, provider_message_id: str, event: str = "delivered") -> dict:
        """Build the webhook a provider would have sent. Test convenience."""
        return {
            "provider_message_id": provider_message_id,
            "event": event,
            "at": datetime.now(timezone.utc),
        }


def build_providers() -> dict:
    """Adapter selection. Everything defaults to the fake."""
    from app.config import settings

    providers: dict = {}
    for channel, backend in (
        (Channel.SMS, settings.sms_backend),
        (Channel.WHATSAPP, settings.whatsapp_backend),
        (Channel.EMAIL, settings.email_backend),
    ):
        if backend == "fake":
            providers[channel] = FakeProvider(channel)
        else:
            # Real providers land here. Deliberately not stubbed with something
            # that silently succeeds — an unconfigured live backend must fail
            # loudly rather than pretend to message a debtor.
            raise NotImplementedError(
                f"{channel.value} backend {backend!r} is configured but not implemented; "
                f"set it to 'fake' or supply an implementation"
            )
    return providers
