"""PCM to G.711 A-law.

Indian PSTN negotiates G.711 A-law. Storing an A-law copy beside the PCM lets
Asterisk pick the zero-transcode match, and A-law is half the bytes — on a call
path where transcoding costs CPU per concurrent channel, both matter.

Implemented directly rather than via `audioop`, for two reasons: `audioop` was
removed in Python 3.13 (PEP 594), and on 3.11 it raises a DeprecationWarning
that this project's `filterwarnings = error` turns into a failure. A 256-entry
segment table is less code than carrying a compatibility shim.

The encoder is the standard CCITT G.711 routine, verified sample-for-sample
against `audioop.lin2alaw` across all 65,536 inputs — see `tests/test_render.py`.
"""

from __future__ import annotations

import struct

_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _segment(value: int) -> int:
    for index, end in enumerate(_SEG_END):
        if value <= end:
            return index
    return len(_SEG_END)


def _linear_to_alaw(sample: int) -> int:
    """One signed 16-bit sample to one A-law byte."""
    sample >>= 3  # G.711 A-law works on 13 significant bits
    if sample >= 0:
        mask = 0xD5  # sign bit set, plus the standard alternate-bit inversion
    else:
        mask = 0x55
        sample = -sample - 1

    segment = _segment(sample)
    if segment >= 8:  # saturate
        return 0x7F ^ mask

    value = segment << 4
    value |= (sample >> 1) & 0x0F if segment < 2 else (sample >> segment) & 0x0F
    return value ^ mask


# Built once at import: 65,536 entries is well under a millisecond and turns the
# per-sample encode into an index.
_TABLE = bytes(_linear_to_alaw(s if s < 32768 else s - 65536) for s in range(65536))


def pcm_to_alaw(pcm: bytes) -> bytes:
    """Signed 16-bit little-endian mono PCM to A-law, one byte per sample."""
    if len(pcm) % 2:
        raise ValueError(f"PCM length {len(pcm)} is not a whole number of 16-bit samples")
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return bytes(_TABLE[s & 0xFFFF] for s in samples)


def duration_ms(pcm: bytes, sample_rate: int) -> int:
    """At 8 kHz 16-bit mono this is len(pcm) / 16."""
    if len(pcm) % 2:
        raise ValueError("truncated PCM: odd byte count")
    return (len(pcm) // 2) * 1000 // sample_rate
