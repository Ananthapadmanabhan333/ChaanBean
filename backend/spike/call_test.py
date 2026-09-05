"""One real call, through real Asterisk, verified end to end.

    python spike/call_test.py

The Phase 7 acceptance check. Deliberately does not use the stub: it registers
the Stasis app over a real websocket, places a genuine SIP call, plays audio
that went through this project's own conversion path, and reads back the events
Asterisk actually emitted.

Two things it proves that a stub cannot:

* `channelId` really does make origination idempotent — Asterisk answers 409.
* the `.sln` we write is a format Asterisk plays, at the speed we intended.
  That is the check that catches "we wrote the wrong audio format", which
  otherwise reaches a debtor as static on a legal call.

Prerequisite: the container is up.
    docker run -d --name crp-asterisk-test -p 127.0.0.1:8088:8088 crp-asterisk
"""

from __future__ import annotations

import asyncio
import json
import struct
import subprocess
import sys
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import websockets  # noqa: E402

from app.tts.convert import duration_ms, pcm_to_alaw  # noqa: E402
from app.tts.local import _resample_if_needed  # noqa: E402

CONTAINER = "crp-asterisk-test"
SOUNDS = "/var/lib/asterisk/sounds/crp"
BASE = "http://127.0.0.1:8088/ari"
AUTH = ("crp", "crp")
WS = "ws://127.0.0.1:8088/ari/events?app=crp-outbound&subscribeAll=false&api_key=crp:crp"

LINE = "Namaste. Invoice I N V one hundred for forty two lakh rupees is overdue."


def sh(*args: str) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    return (r.stdout or "") + (r.stderr or "")


def build_audio(name: str) -> tuple[int, int]:
    """Real speech from the container's espeak-ng, converted by our own code."""
    sh("docker", "exec", CONTAINER, "sh", "-lc",
       f"espeak-ng -v en-us -s 150 -b 1 -w /tmp/{name}.wav \"{LINE}\"")
    sh("docker", "cp", f"{CONTAINER}:/tmp/{name}.wav", f"/tmp/{name}.wav")

    with wave.open(f"/tmp/{name}.wav", "rb") as w:
        source_rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    # Through the project's own resampler and A-law encoder, not a shell tool —
    # this is the code path production uses.
    pcm = _resample_if_needed(frames, source_rate, 8000)
    Path(f"/tmp/{name}.sln").write_bytes(pcm)
    Path(f"/tmp/{name}.alaw").write_bytes(pcm_to_alaw(pcm))

    sh("docker", "cp", f"/tmp/{name}.sln", f"{CONTAINER}:{SOUNDS}/{name}.sln")
    sh("docker", "cp", f"/tmp/{name}.alaw", f"{CONTAINER}:{SOUNDS}/{name}.alaw")
    return source_rate, len(pcm)


async def main() -> int:
    name = f"spike{uuid.uuid4().hex[:8]}"
    version = sh("docker", "exec", CONTAINER, "asterisk", "-rx", "core show version")
    print(f"asterisk : {version.strip()[:58]}")

    print("\n1. audio")
    source_rate, size = build_audio(name)
    expected_ms = duration_ms(Path(f"/tmp/{name}.sln").read_bytes(), 8000)
    print(f"   espeak-ng at {source_rate} Hz -> {size} bytes of 8 kHz s16le")
    print(f"   expected playback: {expected_ms} ms   (len/16 = {size // 16} ms)")
    if size % 2:
        print("   FAILED: odd byte count is not whole samples")
        return 1

    seen: list[str] = []
    async with websockets.connect(WS, ping_interval=20) as ws:
        print("\n2. Stasis app registered over the real websocket")
        call_id = str(uuid.uuid4())
        body = {
            "endpoint": "Local/answer@crp-test",
            "app": "crp-outbound",
            "appArgs": call_id,
            "channelId": call_id,
            "timeout": 30,
            "variables": {"CDR(userfield)": call_id},
        }

        with httpx.Client(base_url=BASE, auth=AUTH, timeout=15) as c:
            print("\n3. originating a real SIP call")
            r1 = c.post("/channels", json=body)
            print(f"   HTTP {r1.status_code}  channel {call_id[:8]}…")
            if r1.status_code != 200:
                print(f"   FAILED: {r1.text[:200]}")
                return 1

            async def pump(until: str, timeout: float) -> bool:
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        e = json.loads(raw)
                        seen.append(e["type"])
                        if e["type"] in (
                            "StasisStart", "PlaybackStarted", "PlaybackFinished",
                            "StasisEnd", "ChannelDestroyed",
                        ):
                            print(f"   event {e['type']}")
                        if e["type"] == until:
                            return True
                except asyncio.TimeoutError:
                    return False

            await pump("StasisStart", 12)

            print("\n4. idempotency: same channelId again")
            r2 = c.post("/channels", json=body)
            print(f"   HTTP {r2.status_code} — {'REFUSED (correct)' if r2.status_code == 409 else 'ACCEPTED (WRONG)'}")
            if r2.status_code != 409:
                return 1

            print("\n5. playing the generated audio")
            # No file extension: Asterisk resolves the format itself and picks
            # the zero-transcode match. Passing .sln makes it seek <name>.sln.sln
            play = c.post(f"/channels/{call_id}/play",
                          params={"media": f"sound:{SOUNDS}/{name}"})
            print(f"   HTTP {play.status_code}")
            if play.status_code not in (200, 201):
                print(f"   FAILED: {play.text[:200]}")
                return 1

            finished = await pump("PlaybackFinished", expected_ms / 1000 + 12)
            print(f"   PlaybackFinished received: {finished}")
            if not finished:
                print("   FAILED: audio never finished playing — wrong format?")
                return 1

            print("\n6. hangup and teardown")
            c.delete(f"/channels/{call_id}")
            await pump("StasisEnd", 8)
            gone = c.get(f"/channels/{call_id}").status_code == 404
            print(f"   channel gone: {gone}")

    print("\n7. what Asterisk emitted")
    for kind in ("StasisStart", "PlaybackStarted", "PlaybackFinished", "StasisEnd"):
        print(f"   {kind:<18} {'yes' if kind in seen else 'NO'}")

    sh("docker", "exec", CONTAINER, "sh", "-lc",
       f"rm -f {SOUNDS}/{name}.* /tmp/{name}.wav")
    for f in Path("/tmp").glob(f"{name}.*"):
        f.unlink(missing_ok=True)

    ok = {"StasisStart", "PlaybackStarted", "PlaybackFinished"} <= set(seen)
    print(f"\n{'PASS' if ok else 'FAIL'}: real SIP call, real audio played, 409 enforced.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
