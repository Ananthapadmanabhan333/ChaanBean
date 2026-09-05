"""A stub ARI server.

This exists because you cannot make a real carrier drop your websocket on cue,
nor ring out on demand, nor return 409 when you want to prove idempotency. Every
lifecycle test in this project runs against it, including the paths that only
occur when something goes wrong.

It is faithful where it matters:

* `POST /channels` returns **409** for a channelId that already exists
* a channel can be told to ring out, so `ChannelDestroyed` arrives with no
  preceding `StasisStart` — the never-answered path
* the event stream can be cut mid-call, leaving the call in DIALING for the
  reconciler to find

Deliberately not faithful about audio: nothing is synthesised or played. That is
what the real Asterisk in the compose stack is for.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from json import dumps, loads
from urllib.parse import parse_qs, urlparse


@dataclass
class FakeChannel:
    channel_id: str
    endpoint: str
    caller_id: str | None
    state: str = "Ring"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    playbacks: list[str] = field(default_factory=list)


class FakeAri:
    """In-process ARI. Holds channel state and records what was asked of it."""

    def __init__(self):
        self.channels: dict[str, FakeChannel] = {}
        self.originate_calls: list[dict] = []
        self.hangups: list[str] = []
        self.plays: list[tuple[str, str]] = []
        # Set to a status code to make the next originate fail that way, for
        # exercising the retry and uncertainty paths.
        self.fail_next_originate: int | None = None
        self.timeout_next_originate = False
        self._lock = threading.Lock()

    # --------------------------------------------------------------- channels

    def originate(self, payload: dict) -> tuple[int, dict]:
        with self._lock:
            self.originate_calls.append(payload)
            channel_id = payload.get("channelId")

            if self.timeout_next_originate:
                self.timeout_next_originate = False
                raise TimeoutError("simulated network timeout")
            if self.fail_next_originate is not None:
                code, self.fail_next_originate = self.fail_next_originate, None
                return code, {"message": "simulated failure"}

            if channel_id in self.channels:
                # The behaviour the whole idempotency design rests on.
                return 409, {"message": "Channel with given unique ID already exists"}

            self.channels[channel_id] = FakeChannel(
                channel_id=channel_id,
                endpoint=payload.get("endpoint", ""),
                caller_id=payload.get("callerId"),
            )
            return 200, {"id": channel_id, "state": "Ring"}

    def get_channel(self, channel_id: str) -> tuple[int, dict]:
        channel = self.channels.get(channel_id)
        if channel is None:
            return 404, {"message": "Channel not found"}
        return 200, {"id": channel.channel_id, "state": channel.state}

    def list_channels(self) -> list[dict]:
        return [{"id": c.channel_id, "state": c.state} for c in self.channels.values()]

    def play(self, channel_id: str, media: str) -> tuple[int, dict]:
        channel = self.channels.get(channel_id)
        if channel is None:
            return 404, {"message": "Channel not found"}
        playback_id = f"pb-{len(channel.playbacks) + 1}-{channel_id}"
        channel.playbacks.append(media)
        self.plays.append((channel_id, media))
        return 201, {"id": playback_id, "media_uri": media}

    def hangup(self, channel_id: str) -> tuple[int, dict]:
        self.hangups.append(channel_id)
        self.channels.pop(channel_id, None)
        return 204, {}

    # ---------------------------------------------------------------- events

    def answer(self, channel_id: str) -> None:
        if channel_id in self.channels:
            self.channels[channel_id].state = "Up"

    def ring_out(self, channel_id: str) -> None:
        """Never answered: the channel is destroyed without entering Stasis."""
        self.channels.pop(channel_id, None)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeARI/1.0"

    @property
    def ari(self) -> FakeAri:
        return self.server.ari  # type: ignore[attr-defined]

    def log_message(self, *args):  # keep the test output readable
        pass

    def _send(self, code: int, body) -> None:
        payload = dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts[-1] == "info":
            return self._send(200, {"system": {"version": "fake"}})
        if parts[-2:-1] == ["channels"] or (len(parts) >= 2 and parts[-2] == "channels"):
            return self._send(*self.ari.get_channel(parts[-1]))
        if parts[-1] == "channels":
            return self._send(200, self.ari.list_channels())
        return self._send(404, {"message": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        length = int(self.headers.get("Content-Length") or 0)
        body = loads(self.rfile.read(length) or b"{}") if length else {}

        if parts[-1] == "channels":
            try:
                code, payload = self.ari.originate(body)
            except TimeoutError:
                # Close without responding, so the client sees a transport error.
                self.close_connection = True
                return
            return self._send(code, payload)

        if parts[-1] == "play":
            media = parse_qs(parsed.query).get("media", [""])[0]
            return self._send(*self.ari.play(parts[-2], media))

        if parts[-1] == "answer":
            self.ari.answer(parts[-2])
            return self._send(204, {})

        return self._send(404, {"message": "not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[-2] == "channels":
            code, payload = self.ari.hangup(parts[-1])
            return self._send(code if code != 204 else 200, payload)
        return self._send(404, {"message": "not found"})


class FakeAriServer:
    """Runs `FakeAri` on a real socket, so the real httpx client is exercised."""

    def __init__(self, ari: FakeAri | None = None, port: int = 0):
        self.ari = ari or FakeAri()
        self._server = HTTPServer(("127.0.0.1", port), _Handler)
        self._server.ari = self.ari  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/ari"

    def __enter__(self) -> FakeAriServer:
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
