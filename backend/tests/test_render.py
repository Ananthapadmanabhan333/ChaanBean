"""Rendering, Indian numbering, the TTS cache and A-law conversion.

`test_forty_two_lakh` is the one that decides whether this phase is done. If it
says "four million" the debtor hears a number they have to stop and convert, on
a call that is already adversarial.
"""

from __future__ import annotations

import struct
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db import admin_session, tenant_session
from app.models import AssetStatus, AudioAsset, EscalationLevel, MessageTemplate, TemplateVersion
from app.render.numbers import format_inr, group_indian, paise_to_words, rupees_to_words
from app.render.service import RenderFailed, ensure_audio
from app.render.template import MergeContext, RenderError, message_hash, normalise, render
from app.storage.local import LocalStorage
from app.tts.convert import duration_ms, pcm_to_alaw
from app.tts.local import SilentTtsBackend


# --------------------------------------------------------------------- numbers


def test_forty_two_lakh():
    """The assertion that decides whether this phase is done."""
    assert rupees_to_words(4_200_000) == "forty-two lakh"
    assert "million" not in rupees_to_words(4_200_000)
    assert paise_to_words(420_000_000) == "forty-two lakh rupees"
    assert format_inr(420_000_000) == "₹42,00,000"


@pytest.mark.parametrize(
    "rupees,expected",
    [
        (0, "zero"),
        (1, "one"),
        (15, "fifteen"),
        (42, "forty-two"),
        (100, "one hundred"),
        (999, "nine hundred ninety-nine"),
        (1_000, "one thousand"),
        (1_52_500, "one lakh fifty-two thousand five hundred"),
        (4_200_000, "forty-two lakh"),
        (1_00_00_000, "one crore"),
        (12_00_00_000, "twelve crore"),
        (100_00_00_000, "one hundred crore"),
    ],
)
def test_rupees_to_words_table(rupees, expected):
    assert rupees_to_words(rupees) == expected


def test_paise_and_rupees_are_spoken_together():
    assert paise_to_words(0) == "zero rupees"
    assert paise_to_words(100) == "one rupee"
    assert paise_to_words(150) == "one rupee and fifty paise"
    assert paise_to_words(1) == "one paisa"
    assert paise_to_words(-100) == "minus one rupee"


@pytest.mark.parametrize(
    "value,expected",
    [(100, "100"), (4200, "4,200"), (4_200_000, "42,00,000"), (1_00_00_000, "1,00,00,000")],
)
def test_indian_digit_grouping(value, expected):
    """2, 2, then 3 from the right — never 4,200,000."""
    assert group_indian(value) == expected


# -------------------------------------------------------------------- template


def ctx(**over) -> MergeContext:
    base = dict(
        buyer_name="Sharma Traders",
        amount_paise=420_000_000,
        invoice_ref="INV-100",
        days_past_due=45,
        company_name="Acme Steel",
        due_date=date(2026, 1, 31),
    )
    base.update(over)
    return MergeContext(**base)


def test_render_substitutes_every_field():
    body = (
        "Namaste {buyer_name}, this is a reminder from {company_name}. "
        "Invoice {invoice_ref} for {amount_words} is {days_past_due} days overdue."
    )
    text = render(body, ctx())
    assert "Sharma Traders" in text
    assert "forty-two lakh rupees" in text
    assert "{" not in text


def test_unknown_merge_field_raises():
    """A legal call saying 'your outstanding of  is overdue' is worse than no call."""
    with pytest.raises(RenderError, match="unknown merge field"):
        render("Hello {buyer_naem}", ctx())


def test_empty_merge_value_raises_rather_than_rendering_a_gap():
    with pytest.raises(RenderError, match="no value"):
        render("Due on {due_date_words}", ctx(due_date=None))


def test_normalise_is_deterministic():
    messy = "Hello​  Sharma ,  your  invoice   is due ."
    once = normalise(messy)
    assert once == normalise(messy)
    assert once == "Hello Sharma, your invoice is due."
    # Unicode-equivalent inputs must collapse to the same bytes, or the hash
    # forks and two assets are generated for one message.
    assert normalise("á") == normalise("á")


def test_hash_covers_tenant_and_voice():
    common = dict(
        template_version_id=uuid.uuid4(),
        final_text="Namaste",
        engine="neural",
        sample_rate=8000,
    )
    a = uuid.uuid4()
    b = uuid.uuid4()
    assert message_hash(company_id=a, voice_id="Kajal", **common) != message_hash(
        company_id=b, voice_id="Kajal", **common
    )
    assert message_hash(company_id=a, voice_id="Kajal", **common) != message_hash(
        company_id=a, voice_id="Aditi", **common
    )


# ---------------------------------------------------------------------- a-law


def test_alaw_is_half_the_bytes_and_round_numbers():
    pcm = struct.pack("<8h", 0, 100, -100, 32767, -32768, 5000, -5000, 1)
    alaw = pcm_to_alaw(pcm)
    assert len(alaw) == len(pcm) // 2


def test_truncated_pcm_is_rejected():
    with pytest.raises(ValueError):
        pcm_to_alaw(b"\x00\x01\x02")


def test_duration_matches_the_sample_count():
    pcm = b"\x00\x00" * 8000  # exactly one second at 8 kHz
    assert duration_ms(pcm, 8000) == 1000
    assert duration_ms(pcm, 8000) == len(pcm) // 16


# ------------------------------------------------------------------- the cache


class CountingTts:
    """Wraps a real backend and counts calls, so the cache is asserted by
    call-count rather than by inspection."""

    name = "counting"

    def __init__(self, inner=None):
        self.inner = inner or SilentTtsBackend()
        self.calls = 0

    def synthesize(self, text, *, voice_id, sample_rate):
        self.calls += 1
        return self.inner.synthesize(text, voice_id=voice_id, sample_rate=sample_rate)


class ExplodingTts:
    name = "exploding"

    def synthesize(self, text, *, voice_id, sample_rate):
        raise RuntimeError("vendor is down")


@pytest.fixture
def template(tenants):
    ids = {}
    with admin_session() as s:
        tmpl = MessageTemplate(
            company_id=tenants.a.company_id,
            key="l1_reminder",
            level=EscalationLevel.L1,
            language="en-IN",
        )
        s.add(tmpl)
        s.flush()
        version = TemplateVersion(
            company_id=tenants.a.company_id,
            template_id=tmpl.id,
            version=1,
            body="Namaste {buyer_name}, invoice {invoice_ref} for {amount_words} is overdue.",
            voice_id="Kajal",
            engine="neural",
        )
        s.add(version)
        s.flush()
        ids["template_id"] = tmpl.id
        ids["version_id"] = version.id

    yield ids

    with admin_session() as s:
        for cid in (tenants.a.company_id, tenants.b.company_id):
            s.execute(delete(AudioAsset).where(AudioAsset.company_id == cid))
        s.execute(delete(TemplateVersion).where(TemplateVersion.id == ids["version_id"]))
        s.execute(delete(MessageTemplate).where(MessageTemplate.id == ids["template_id"]))


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(str(tmp_path))


def test_second_render_is_a_cache_hit(tenants, template, storage):
    """Asserted by call-count on the backend, not by inspection."""
    tts = CountingTts()
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        first = ensure_audio(
            s,
            company_id=tenants.a.company_id,
            template_version=version,
            merge_ctx=ctx(),
            tts=tts,
            storage=storage,
        )
        second = ensure_audio(
            s,
            company_id=tenants.a.company_id,
            template_version=version,
            merge_ctx=ctx(),
            tts=tts,
            storage=storage,
        )
        assets = s.execute(select(AudioAsset)).scalars().all()

    assert tts.calls == 1, "the same message must synthesize once"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(assets) == 1
    assert first.asset.id == second.asset.id


def test_changing_voice_misses_the_cache(tenants, template, storage):
    """Otherwise you serve last month's voice from a stale asset."""
    tts = CountingTts()
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        first = ensure_audio(
            s, company_id=tenants.a.company_id, template_version=version,
            merge_ctx=ctx(), tts=tts, storage=storage,
        )
        version.voice_id = "Aditi"
        s.flush()
        second = ensure_audio(
            s, company_id=tenants.a.company_id, template_version=version,
            merge_ctx=ctx(), tts=tts, storage=storage,
        )

    assert tts.calls == 2
    assert first.asset.message_hash != second.asset.message_hash


def test_two_tenants_never_share_an_asset(tenants, template, storage):
    """Rule 11 — one tenant's evidence must not point at another tenant's file."""
    tts = CountingTts()
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        a = ensure_audio(
            s, company_id=tenants.a.company_id, template_version=version,
            merge_ctx=ctx(), tts=tts, storage=storage,
        )
        hash_a, key_a = a.asset.message_hash, a.asset.storage_key

    with admin_session() as s:
        version = s.get(TemplateVersion, template["version_id"])
        b = ensure_audio(
            s, company_id=tenants.b.company_id, template_version=version,
            merge_ctx=ctx(), tts=tts, storage=storage,
        )
        hash_b, key_b = b.asset.message_hash, b.asset.storage_key

    assert hash_a != hash_b
    assert key_a != key_b
    assert str(tenants.a.company_id) in key_a
    assert str(tenants.b.company_id) in key_b


def test_generated_audio_satisfies_the_duration_invariant(tenants, template, storage):
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        result = ensure_audio(
            s, company_id=tenants.a.company_id, template_version=version,
            merge_ctx=ctx(), tts=CountingTts(), storage=storage,
        )
        asset = result.asset
        pcm = storage.get(asset.storage_key)

    assert asset.status is AssetStatus.READY
    assert len(pcm) % 2 == 0
    assert asset.byte_size == len(pcm)
    assert asset.duration_ms == len(pcm) // 16
    assert asset.content_sha256 and len(asset.content_sha256) == 64


def test_an_alaw_copy_is_written_beside_the_pcm(tenants, template, storage):
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        result = ensure_audio(
            s, company_id=tenants.a.company_id, template_version=version,
            merge_ctx=ctx(), tts=CountingTts(), storage=storage,
        )
        sln = result.asset.storage_key

    alaw = sln.replace(".sln", ".alaw")
    assert storage.exists(alaw)
    assert len(storage.get(alaw)) == len(storage.get(sln)) // 2


def test_failed_synthesis_marks_the_asset_and_leaves_no_partial_file(
    tenants, template, storage, tmp_path
):
    """No fallback message, ever, and no .tmp remnant for Asterisk to find.

    Written the way the scheduler uses it: catch, commit the FAILED marker, then
    block the call. Letting the exception escape a rollback-on-error scope would
    discard the asset row and the reason with it.
    """
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        with pytest.raises(RenderFailed):
            ensure_audio(
                s, company_id=tenants.a.company_id, template_version=version,
                merge_ctx=ctx(), tts=ExplodingTts(), storage=storage,
            )
        s.commit()

    with tenant_session(tenants.a.company_id) as s:
        asset = s.execute(select(AudioAsset)).scalar_one()

    assert asset.status is AssetStatus.FAILED
    assert "vendor is down" in asset.error
    assert asset.storage_key is None
    assert list(Path(tmp_path).rglob("*.tmp")) == []
    assert list(Path(tmp_path).rglob("*.sln")) == []


def test_unrenderable_message_creates_no_asset(tenants, template, storage):
    with tenant_session(tenants.a.company_id) as s:
        version = s.get(TemplateVersion, template["version_id"])
        version.body = "Hello {not_a_field}"
        s.flush()
        with pytest.raises(RenderError):
            ensure_audio(
                s, company_id=tenants.a.company_id, template_version=version,
                merge_ctx=ctx(), tts=CountingTts(), storage=storage,
            )
        assert s.execute(select(AudioAsset)).scalars().all() == []


def test_local_storage_writes_atomically(storage):
    """Asterisk must never open a half-written file."""
    storage.put("a/b.sln", b"\x01\x02" * 100)
    assert storage.exists("a/b.sln")
    assert len(storage.get("a/b.sln")) == 200
    assert list(Path(storage.root).rglob("*.tmp")) == []


def test_storage_key_cannot_escape_the_root(storage):
    with pytest.raises(ValueError):
        storage.put("../../escaped.sln", b"\x00\x00")
