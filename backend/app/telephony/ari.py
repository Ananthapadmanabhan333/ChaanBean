"""Asterisk REST Interface client.

Hand-rolled over httpx rather than a wrapper: the surface this product needs is
about five endpoints, and `ari-py` is effectively unmaintained.

ARI is chosen over the alternatives because it is the only one that gives all
three things this product depends on:

* a **client-supplied `channelId`**, which is the idempotency mechanism
* `PlaybackFinished`, which is the only honest source of the "played" fact
* `ChannelDtmfReceived`, which is the L3 human-present gate

The most important behaviour in this file is the 409 path. The failure it
prevents: the HTTP request times out, but Asterisk created the channel anyway.
Retry blindly and you have dialled a debtor twice — which is a complaint, and at
L3 a regulatory one.

**Verified against Asterisk 20.9.3, including its limit.** A second POST with a
live channel's id is refused with 409. But once that channel is destroyed the id
is free again and a re-POST creates a *new* call — so ARI's 409 is a
concurrent-duplicate guard, not a durable record of "we already called this
person". Durability comes from the unique `calls.idempotency_key` and the call's
terminal status; the 409 only closes the window while the call is in flight.

Also verified: origination into a Stasis app whose websocket is not connected
produces a channel that is torn down immediately. The event consumer must be
running before anything dials, and `spike/call_test.py` asserts the whole path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class AriError(RuntimeError):
    pass


@dataclass(frozen=True)
class OriginateOutcome:
    channel_id: str
    created: bool  # False means it already existed — we had already dialled
    detail: str = ""


class AriClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        app_name: str | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = (base_url or settings.ari_base_url).rstrip("/")
        self.username = username or settings.ari_username
        self.password = password or settings.ari_password
        self.app_name = app_name or settings.ari_app_name
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(self.username, self.password),
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- originate

    def originate(
        self,
        *,
        call_id: UUID | str,
        to_e164: str,
        caller_id: str | None,
        endpoint_template: str | None = None,
        timeout_sec: int | None = None,
        variables: dict | None = None,
        max_retries: int = 2,
    ) -> OriginateOutcome:
        """Place one call, exactly once.

        `channelId` is the call's own id, so a retried POST cannot produce a
        second channel: Asterisk answers 409 instead of dialling again.
        """
        channel_id = str(call_id)
        template = endpoint_template or settings.ari_endpoint_template
        payload = {
            "endpoint": template.format(number=to_e164),
            "app": self.app_name,
            "appArgs": channel_id,
            "channelId": channel_id,
            "timeout": timeout_sec or settings.ari_originate_timeout_sec,
        }
        if caller_id:
            payload["callerId"] = caller_id

        # `CDR(userfield)` is how a CDR row joins back to this call later. It is
        # the out-of-band truth when the websocket drops, so it is set here and
        # never optional.
        variables = dict(variables or {})
        variables.setdefault("CDR(userfield)", channel_id)

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.post(
                    "/channels", json={**payload, "variables": variables}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # The channel may or may not exist. The POST is idempotent by
                # channelId, so retrying is safe; after that, ask Asterisk.
                if attempt > max_retries:
                    return self._resolve_after_uncertainty(channel_id, str(exc))
                log.warning("originate attempt %s failed (%s); retrying", attempt, exc)
                continue

            if response.status_code in (200, 201):
                return OriginateOutcome(channel_id, created=True)
            if response.status_code == 409:
                # Already dialled. Never re-dial; reconcile instead.
                return OriginateOutcome(
                    channel_id, created=False, detail="channel already exists (409)"
                )
            if 500 <= response.status_code < 600:
                if attempt > max_retries:
                    return self._resolve_after_uncertainty(
                        channel_id, f"HTTP {response.status_code}"
                    )
                continue
            raise AriError(
                f"originate failed: HTTP {response.status_code} {response.text[:300]}"
            )

    def _resolve_after_uncertainty(self, channel_id: str, detail: str) -> OriginateOutcome:
        """Retries exhausted. Ask Asterisk what actually happened."""
        channel = self.get_channel(channel_id)
        if channel is not None:
            return OriginateOutcome(
                channel_id, created=False, detail=f"{detail}; channel exists"
            )
        raise AriError(f"originate outcome unknown and no channel exists: {detail}")

    # --------------------------------------------------------------- channels

    def get_channel(self, channel_id: str) -> dict | None:
        try:
            response = self._client.get(f"/channels/{channel_id}")
        except (httpx.TimeoutException, httpx.TransportError):
            return None
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return response.json()
        return None

    def list_channels(self) -> list[dict]:
        response = self._client.get("/channels")
        if response.status_code != 200:
            raise AriError(f"list channels failed: HTTP {response.status_code}")
        return response.json()

    def answer(self, channel_id: str) -> None:
        self._client.post(f"/channels/{channel_id}/answer")

    def play(self, channel_id: str, media_name: str, *, playback_id: str | None = None) -> str:
        """Play a staged sound.

        `media_name` carries **no file extension**. Asterisk resolves the format
        itself and picks the zero-transcode match; passing `.sln` makes it look
        for `<hash>.sln.sln` and find nothing.
        """
        if media_name.endswith((".sln", ".alaw", ".wav", ".gsm")):
            raise AriError(
                f"media name must not carry an extension: {media_name!r} — "
                f"Asterisk appends the format itself"
            )
        params = {"media": f"sound:{media_name}"}
        if playback_id:
            params["playbackId"] = playback_id
        response = self._client.post(f"/channels/{channel_id}/play", params=params)
        if response.status_code not in (200, 201):
            raise AriError(f"play failed: HTTP {response.status_code} {response.text[:200]}")
        return response.json().get("id", playback_id or "")

    def hangup(self, channel_id: str, *, reason: str | None = None) -> None:
        params = {"reason": reason} if reason else None
        try:
            self._client.delete(f"/channels/{channel_id}", params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("hangup of %s failed: %s", channel_id, exc)

    def ping(self) -> bool:
        try:
            response = self._client.get("/asterisk/info")
            return response.status_code == 200
        except Exception:
            return False
