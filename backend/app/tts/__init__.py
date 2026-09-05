"""TTS adapter selection."""

from __future__ import annotations

from app.config import settings
from app.tts.local import SilentTtsBackend, TtsUnavailable, build_local_backend


def build_tts():
    if settings.tts_backend == "polly":
        from app.tts.polly import PollyTtsBackend

        return PollyTtsBackend(
            region=settings.aws_region,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
    return build_local_backend()


__all__ = ["SilentTtsBackend", "TtsUnavailable", "build_local_backend", "build_tts"]
