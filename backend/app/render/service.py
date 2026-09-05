"""From an approved template version to a stored, playable audio asset.

Content generation never happens on the delivery path. This runs at schedule
time, and a call may not leave SCHEDULED until its asset is READY — so a slow or
failing TTS vendor delays a call rather than producing silence on a live one.

The cache does not deduplicate genuinely unique messages: 100 different debtor
names means up to 100 generations. What it prevents is *re*-generation, which is
where the money is over a campaign's life — the same reminder to the same debtor
on attempts two and three costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AssetStatus, AudioAsset, TemplateVersion
from app.render.template import MergeContext, message_hash, normalise, render
from app.storage import build_storage
from app.tts import build_tts
from app.tts.convert import duration_ms, pcm_to_alaw


class RenderFailed(RuntimeError):
    """Synthesis or storage failed. The asset is FAILED and the call must block."""


@dataclass(frozen=True)
class RenderResult:
    asset: AudioAsset
    cache_hit: bool
    final_text: str


def asset_keys(company_id: UUID | str, message_hash_hex: str) -> tuple[str, str]:
    """Tenant-scoped storage keys (rule 11).

    `company_id` is in the path as well as in the hash, so a misconfiguration
    cannot make one tenant's evidence point at another tenant's file.
    """
    return (
        f"{company_id}/{message_hash_hex}.sln",
        f"{company_id}/{message_hash_hex}.alaw",
    )


def ensure_audio(
    session: Session,
    *,
    company_id: UUID,
    template_version: TemplateVersion,
    merge_ctx: MergeContext,
    tts=None,
    storage=None,
    sample_rate: int | None = None,
) -> RenderResult:
    """Return a READY asset for this message, generating it only if needed.

    On failure the asset is marked FAILED **in the caller's transaction** and
    `RenderFailed` is raised. The exception is an application error, not a
    database one, so the session stays usable — but a caller that simply lets
    the exception escape a rollback-on-error scope loses the diagnosis along
    with the PENDING row.

    Callers that want the failure recorded must therefore catch and commit:

        try:
            ensure_audio(...)
        except RenderFailed:
            session.commit()   # keeps the FAILED asset and its error message
            ...                # then block the call

    `app.scheduler.tick` does exactly this. Writing the marker on a separate
    connection instead would deadlock against this transaction's own uncommitted
    insert on `(company_id, message_hash)`.
    """
    sample_rate = sample_rate or settings.audio_sample_rate
    tts = tts or build_tts()
    storage = storage or build_storage()

    final_text = normalise(render(template_version.body, merge_ctx))
    digest = message_hash(
        company_id=company_id,
        template_version_id=template_version.id,
        final_text=final_text,
        voice_id=template_version.voice_id,
        engine=template_version.engine,
        sample_rate=sample_rate,
    )

    existing = session.execute(
        select(AudioAsset).where(
            AudioAsset.company_id == company_id, AudioAsset.message_hash == digest
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status is AssetStatus.READY:
        return RenderResult(existing, cache_hit=True, final_text=final_text)

    asset = existing or AudioAsset(
        company_id=company_id,
        message_hash=digest,
        final_text=final_text,
        voice_id=template_version.voice_id,
        engine=template_version.engine,
        sample_rate=sample_rate,
        status=AssetStatus.PENDING,
    )
    if existing is None:
        session.add(asset)
    else:
        asset.status = AssetStatus.PENDING
        asset.error = None
    session.flush()

    try:
        result = tts.synthesize(
            final_text, voice_id=template_version.voice_id, sample_rate=sample_rate
        )
        pcm = result.audio
        if not pcm:
            raise RenderFailed("synthesis returned no audio")
        if len(pcm) % 2:
            # A truncated synthesis fails loudly here rather than playing as a
            # click on a real call.
            raise RenderFailed(f"synthesis returned {len(pcm)} bytes, not whole samples")

        sln_key, alaw_key = asset_keys(company_id, digest)
        storage.put(sln_key, pcm)
        storage.put(alaw_key, pcm_to_alaw(pcm))

        asset.storage_key = sln_key
        asset.content_sha256 = storage.checksum(pcm)
        asset.byte_size = len(pcm)
        asset.duration_ms = duration_ms(pcm, sample_rate)
        asset.status = AssetStatus.READY
        asset.error = None
        session.flush()
    except Exception as exc:
        asset.status = AssetStatus.FAILED
        asset.error = str(exc)[:2000]
        asset.storage_key = None
        session.flush()
        # No fallback message, ever. Wrong content on a legal call is worse than
        # no call.
        raise RenderFailed(str(exc)) from exc

    return RenderResult(asset, cache_hit=False, final_text=final_text)


def mark_staged(session: Session, asset: AudioAsset) -> AudioAsset:
    """Record that the file is present on the box that will play it."""
    asset.staged_at = datetime.now(timezone.utc)
    session.flush()
    return asset
