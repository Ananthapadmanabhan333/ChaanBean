"""Amazon Polly.

Unused until an AWS account exists. Selecting it is a settings change:

    TTS_BACKEND=polly
    AWS_REGION=ap-south-1
    AWS_ACCESS_KEY_ID=... / AWS_SECRET_ACCESS_KEY=...

Three details that are easy to get wrong and expensive to discover late:

* `OutputFormat="pcm"` returns headerless signed 16-bit little-endian PCM. The
  only valid `SampleRate` values for pcm are 8000 and 16000, and **the default
  is 16000** — so it must be set explicitly or every asset plays at the wrong
  speed down an 8 kHz channel.
* `Kajal` is a neural-only voice. `Engine="neural"` is mandatory, not a
  preference; the standard engine returns an error for it.
* Adaptive retries matter. A campaign opening with 500 unique debtor names hits
  the synthesis rate limit within seconds.

On failure this raises. The asset is marked FAILED and the call blocks. There is
deliberately no fallback message — wrong content on a legal call is worse than
no call at all.
"""

from __future__ import annotations

from app.providers.base import SynthesisResult
from app.tts.convert import duration_ms

VALID_PCM_SAMPLE_RATES = ("8000", "16000")
NEURAL_ONLY_VOICES = frozenset({"Kajal", "Aditi"})


class PollyError(RuntimeError):
    pass


class PollyTtsBackend:
    name = "polly"

    def __init__(self, *, region: str, access_key: str | None, secret_key: str | None):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - boto3 is optional locally
            raise PollyError("boto3 is not installed") from exc

        self._client = boto3.client(
            "polly",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    def synthesize(self, text: str, *, voice_id: str, sample_rate: int) -> SynthesisResult:
        rate = str(sample_rate)
        if rate not in VALID_PCM_SAMPLE_RATES:
            raise PollyError(
                f"pcm output supports {VALID_PCM_SAMPLE_RATES}, not {rate!r}"
            )
        engine = "neural" if voice_id in NEURAL_ONLY_VOICES else "standard"

        try:
            response = self._client.synthesize_speech(
                Text=text,
                OutputFormat="pcm",
                SampleRate=rate,
                VoiceId=voice_id,
                Engine=engine,
            )
            pcm = response["AudioStream"].read()
        except Exception as exc:
            raise PollyError(f"Polly synthesis failed: {exc}") from exc

        if not pcm:
            raise PollyError("Polly returned no audio")
        if len(pcm) % 2:
            # A truncated stream must fail here rather than play as a click on a
            # real call.
            raise PollyError(f"Polly returned {len(pcm)} bytes, not a whole sample count")

        return SynthesisResult(pcm, sample_rate, duration_ms(pcm, sample_rate))
