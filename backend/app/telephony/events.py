"""The ARI event consumer.

**Run exactly one of these per Stasis application.** Multiple ARI clients
subscribed to the same app get duplicate or split delivery depending on the
Asterisk version, and neither failure is obvious from the outside. Restart
safety comes from reconciliation, not from clustering — which is why
`app/calls/reconciler.py` exists and why this module does not try to be clever
about resuming.

Every event is **appended to `call_events` first, then projected**. ARI has no
replay and no cursor: if the projection ran first and the process died, the fact
would be gone. Append-then-project means a crash costs at most a re-projection.

On reconnect the consumer asks Asterisk which channels are actually live and
hands that set to the reconciler, which closes every in-flight call that is not
among them. That diff is the only thing that recovers a hangup lost while the
socket was down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import websockets

from app.calls import outcomes, projection, reconciler
from app.config import settings
from app.db import admin_session
from app.models import EventSource
from app.telephony.ari import AriClient
from app.telephony.flow import on_dtmf, on_playback_finished, on_stasis_start

log = logging.getLogger(__name__)

# Events we act on. Anything else is still logged — an event we ignore today is
# evidence tomorrow — but does not drive the flow.
HANDLED = frozenset(
    {
        "StasisStart",
        "StasisEnd",
        "ChannelStateChange",
        "ChannelDtmfReceived",
        "PlaybackStarted",
        "PlaybackFinished",
        "ChannelDestroyed",
        "ChannelHangupRequest",
    }
)


def _ws_url(base_url: str, app_name: str, username: str, password: str) -> str:
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    query = urlencode(
        {
            "app": app_name,
            # Only our own app's events. `subscribeAll` would deliver every
            # channel on the box, including ones we did not create.
            "subscribeAll": "false",
            "api_key": f"{username}:{password}",
        },
        quote_via=quote,
    )
    return f"{ws_base}/events?{query}"


def _dedupe_key(event: dict) -> str:
    """ARI has no event id, so one is derived from what identifies the event.

    Timestamp plus type plus channel is stable across a redelivery of the same
    event and distinct between two real ones.
    """
    channel = (event.get("channel") or {}).get("id", "")
    playback = (event.get("playback") or {}).get("id", "")
    return f"{event.get('type')}:{event.get('timestamp')}:{channel}:{playback}"[:128]


def _occurred_at(event: dict) -> datetime:
    stamp = event.get("timestamp")
    if not stamp:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _payload(event: dict) -> dict:
    """Keep what matters for the projection and the audit trail, not the lot."""
    channel = event.get("channel") or {}
    cause = event.get("cause")
    if cause is None:
        cause = (channel.get("dialplan") or {}).get("cause")
    payload = {
        "type": event.get("type"),
        "channel_state": channel.get("state"),
        "channel_name": channel.get("name"),
    }
    if cause is not None:
        payload["cause"] = cause
    if event.get("digit") is not None:
        payload["digit"] = event["digit"]
    if event.get("playback"):
        payload["playback_id"] = event["playback"].get("id")
        payload["media_uri"] = event["playback"].get("media_uri")
    return payload


class AriEventConsumer:
    def __init__(self, client: AriClient | None = None, *, stager=None):
        self.client = client or AriClient()
        self.stager = stager
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    # ------------------------------------------------------------------ core

    def handle(self, event: dict) -> None:
        """Append, project, then act. Synchronous and short by design."""
        kind = event.get("type")
        channel_id = (event.get("channel") or {}).get("id")
        if not channel_id:
            return

        # Cross-tenant on purpose: a channel id arrives with no tenant attached,
        # and resolving it is exactly what the worker role exists for.
        with admin_session() as session:
            call = projection.call_for_channel(session, channel_id)
            if call is None:
                log.debug("event %s for unknown channel %s", kind, channel_id)
                return

            projection.ingest(
                session,
                call=call,
                source=EventSource.ARI,
                event_type=kind,
                dedupe_key=_dedupe_key(event),
                payload=_payload(event),
                occurred_at=_occurred_at(event),
            )

            media_name = None
            if self.stager is not None and call.audio_asset_id:
                from app.models import AudioAsset

                asset = session.get(AudioAsset, call.audio_asset_id)
                if asset is not None:
                    media_name = self.stager.media_name(asset)

            try:
                if kind == "StasisStart" and media_name:
                    on_stasis_start(session, self.client, call, media_name)
                elif kind == "ChannelDtmfReceived":
                    on_dtmf(
                        session, self.client, call, str(event.get("digit", "")), media_name or ""
                    )
                elif kind == "PlaybackFinished":
                    on_playback_finished(session, self.client, call)
            except Exception:
                # A control failure must not stop the consumer. The call will be
                # closed by the reconciler rather than left mid-flight forever.
                log.exception("control action failed for call %s", call.id)

            if call.status.is_terminal:
                outcomes.apply_outcome(session, call)

    def on_reconnect(self) -> None:
        """Close everything that finished while we were not listening."""
        try:
            live = {c.get("id") for c in self.client.list_channels()}
        except Exception:
            log.warning("could not list channels on reconnect; leaving the sweeper to it")
            return
        with admin_session() as session:
            resolved = reconciler.reconnect_diff(session, live)
        if resolved:
            log.info("reconnect diff closed %s call(s)", len(resolved))

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        url = _ws_url(
            self.client.base_url,
            self.client.app_name,
            self.client.username,
            self.client.password,
        )
        backoff = 1
        first = True

        while not self._stopping:
            try:
                async with websockets.connect(url, ping_interval=20) as socket:
                    log.info("ARI websocket connected to app %r", self.client.app_name)
                    if not first:
                        self.on_reconnect()
                    first = False
                    backoff = 1

                    async for raw in socket:
                        if self._stopping:
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("non-JSON frame from ARI")
                            continue
                        # Off the event loop: the handler talks to PostgreSQL,
                        # and blocking here stalls delivery for every channel.
                        await asyncio.to_thread(self.handle, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    break
                log.warning("ARI websocket dropped (%s); reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

        log.info("ARI consumer stopped")


def run_consumer(stager=None) -> None:
    """Blocking entry point for a worker process."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    consumer = AriEventConsumer(stager=stager)
    try:
        asyncio.run(consumer.run())
    except KeyboardInterrupt:
        consumer.stop()


if __name__ == "__main__":
    from app.telephony.staging import LocalStager

    run_consumer(LocalStager(settings.asterisk_sounds_dir))
