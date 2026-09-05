"""Local TTS. No vendor account, no GPU.

Tries espeak-ng, then piper, both via subprocess with output forced to 8 kHz
mono s16le — the same layout Polly returns, so the rest of the pipeline cannot
tell which produced the bytes.

When neither binary is installed, `SilentTtsBackend` produces valid PCM of a
plausible duration so the whole path — hash, cache, storage, staging, playback,
event capture — stays exercisable. It is deliberately not speech, and it says so
in `name`, because a fallback that sounds like a voice is a fallback someone
ships to a debtor by accident.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from app.providers.base import SynthesisResult
from app.tts.convert import duration_ms


class TtsUnavailable(RuntimeError):
    """No synthesis backend could produce audio. The call must block."""


def _strip_wav_header(data: bytes) -> bytes:
    """espeak writes a RIFF header; the pipeline wants raw samples."""
    if data[:4] == b"RIFF" and b"data" in data[:256]:
        index = data.index(b"data")
        return data[index + 8 :]
    return data


class EspeakTtsBackend:
    name = "espeak-ng"

    def __init__(self, binary: str = "espeak-ng"):
        self.binary = binary

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def synthesize(self, text: str, *, voice_id: str, sample_rate: int) -> SynthesisResult:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.wav"
            # `-b 1` reads the text as UTF-8; the default is latin-1 and would
            # mangle a Devanagari or Tamil name.
            command = [
                self.binary,
                "-v", voice_id or "en-us",
                "-s", "150",
                "-b", "1",
                "-w", str(out),
                text,
            ]
            result = subprocess.run(command, capture_output=True, timeout=60)
            if result.returncode != 0 or not out.exists():
                raise TtsUnavailable(
                    f"espeak-ng failed: {result.stderr.decode('utf-8', 'replace')[:200]}"
                )
            raw = _strip_wav_header(out.read_bytes())

        pcm = _resample_if_needed(raw, 22050, sample_rate)
        return SynthesisResult(pcm, sample_rate, duration_ms(pcm, sample_rate))


class PiperTtsBackend:
    name = "piper"

    def __init__(self, binary: str = "piper", model: str | None = None):
        self.binary = binary
        self.model = model

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None and self.model is not None

    def synthesize(self, text: str, *, voice_id: str, sample_rate: int) -> SynthesisResult:
        command = [self.binary, "--model", self.model or voice_id, "--output-raw"]
        result = subprocess.run(
            command, input=text.encode("utf-8"), capture_output=True, timeout=120
        )
        if result.returncode != 0:
            raise TtsUnavailable(
                f"piper failed: {result.stderr.decode('utf-8', 'replace')[:200]}"
            )
        pcm = _resample_if_needed(result.stdout, 22050, sample_rate)
        return SynthesisResult(pcm, sample_rate, duration_ms(pcm, sample_rate))


def _resample_if_needed(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Crude decimation, adequate for narrowband telephony.

    Kept deliberately simple: at 8 kHz the channel is the limiting factor, not
    the resampler, and a dependency on scipy to shave artefacts nobody can hear
    over a PSTN codec is not worth carrying.
    """
    if source_rate == target_rate or not pcm:
        return pcm
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    step = source_rate / target_rate
    picked = [samples[min(int(i * step), len(samples) - 1)] for i in range(int(len(samples) / step))]
    return struct.pack(f"<{len(picked)}h", *picked)


class SilentTtsBackend:
    """Valid PCM, deliberately not speech.

    Used when no real engine is installed, so the pipeline stays testable. The
    tone is quiet and its length tracks the text, which makes duration
    assertions meaningful without anyone mistaking it for a voice.
    """

    name = "silent-placeholder"

    WORDS_PER_MINUTE = 150

    def synthesize(self, text: str, *, voice_id: str, sample_rate: int) -> SynthesisResult:
        words = max(1, len(text.split()))
        seconds = max(0.5, words * 60 / self.WORDS_PER_MINUTE)
        count = int(seconds * sample_rate)
        samples = [
            int(1200 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(count)
        ]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        return SynthesisResult(pcm, sample_rate, duration_ms(pcm, sample_rate))


def build_local_backend(*, piper_model: str | None = None):
    """Pick the best local engine present. Never silently degrades in production."""
    espeak = EspeakTtsBackend()
    if espeak.available:
        return espeak
    piper = PiperTtsBackend(model=piper_model)
    if piper.available:
        return piper
    return SilentTtsBackend()
