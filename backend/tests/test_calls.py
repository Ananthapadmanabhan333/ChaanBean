"""Telephony and call lifecycle.

Everything here runs against the stub ARI server rather than a carrier, because
the paths that matter are the ones that only happen when something goes wrong —
a websocket that dies mid-call, a channel that rings out, a POST that times out
after Asterisk already created the channel. You cannot ask a carrier for those
on cue.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.calls import outcomes, projection, reconciler
from app.db import admin_session, tenant_session
from app.models import (
    AssetStatus,
    AttemptClass,
    AudioAsset,
    Buyer,
    BuyerPhone,
    Call,
    CallEvent,
    CallerId,
    CallStatus,
    Campaign,
    CampaignStatus,
    CreditAccount,
    EscalationLevel,
    EscalationState,
    EventSource,
)
from app.policy import classify_attempt
from app.telephony.ari import AriClient, AriError
from app.telephony.fake import FakeAri, FakeAriServer
from app.telephony.flow import (
    CallBlocked,
    derive_idempotency_key,
    originate,
    preflight,
    resolve_caller_id,
)
from app.telephony.staging import LocalStager

NOW = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def telephony(tenants):
    """A campaign, an account, a phone and a READY asset for one company."""

    class Ctx:
        pass

    ctx = Ctx()
    ctx.company_id = tenants.a.company_id
    ctx.buyer_id = tenants.a.buyer_id
    ctx.account_id = tenants.a.account_id

    with admin_session() as s:
        campaign = Campaign(
            company_id=ctx.company_id, name="test", status=CampaignStatus.ACTIVE
        )
        s.add(campaign)
        caller = CallerId(
            company_id=ctx.company_id,
            e164=f"+91804{uuid.uuid4().int % 1000000:06d}",
            carrier_approved=True,
            is_default=True,
        )
        s.add(caller)
        asset = AudioAsset(
            company_id=ctx.company_id,
            message_hash=uuid.uuid4().hex,
            final_text="Namaste",
            voice_id="Kajal",
            engine="neural",
            sample_rate=8000,
            status=AssetStatus.READY,
            storage_key="k",
            byte_size=16000,
            duration_ms=1000,
        )
        s.add(asset)
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == ctx.buyer_id)
        ).scalar_one()
        s.flush()
        ctx.campaign_id = campaign.id
        ctx.caller_id_id = caller.id
        ctx.caller_e164 = caller.e164
        ctx.asset_id = asset.id
        ctx.asset_hash = asset.message_hash
        ctx.phone_id = phone.id
        ctx.phone_e164 = phone.e164

        state = EscalationState(
            company_id=ctx.company_id,
            account_id=ctx.account_id,
            level=EscalationLevel.L1,
            level_entered_at=NOW - timedelta(days=1),
        )
        s.add(state)
        s.flush()
        ctx.state_id = state.id

    yield ctx

    with admin_session() as s:
        s.execute(delete(CallEvent).where(CallEvent.company_id == ctx.company_id))
        s.execute(delete(Call).where(Call.company_id == ctx.company_id))
        s.execute(delete(EscalationState).where(EscalationState.id == ctx.state_id))
        s.execute(delete(AudioAsset).where(AudioAsset.company_id == ctx.company_id))
        s.execute(delete(Campaign).where(Campaign.company_id == ctx.company_id))
        s.execute(delete(CallerId).where(CallerId.company_id == ctx.company_id))
        # undo any consent/phone damage the outcome tests did
        buyer = s.get(Buyer, ctx.buyer_id)
        buyer.consent_withdrawn = False
        buyer.consent_withdrawn_at = None
        buyer.next_action_at = None
        phone = s.get(BuyerPhone, ctx.phone_id)
        phone.is_valid = True
        phone.last_outcome = None


def make_call(session, ctx, *, level=EscalationLevel.L1, status=CallStatus.DIALING, **over):
    call = Call(
        company_id=ctx.company_id,
        campaign_id=ctx.campaign_id,
        buyer_id=ctx.buyer_id,
        account_id=ctx.account_id,
        phone_id=ctx.phone_id,
        level=level,
        to_e164=ctx.phone_e164,
        scheduled_at=over.pop("scheduled_at", NOW),
        originated_at=over.pop("originated_at", NOW),
        status=status,
        idempotency_key=over.pop("idempotency_key", uuid.uuid4().hex),
        **over,
    )
    session.add(call)
    session.flush()
    return call


def ingest(session, call, event_type, *, at=NOW, key=None, **payload):
    return projection.ingest(
        session,
        call=call,
        source=EventSource.ARI,
        event_type=event_type,
        dedupe_key=key or f"{event_type}:{at.isoformat()}",
        payload=payload,
        occurred_at=at,
    )


# ---------------------------------------------------------------- idempotency


def test_idempotency_key_is_derived_not_random(telephony):
    args = dict(
        company_id=telephony.company_id,
        buyer_id=telephony.buyer_id,
        account_id=telephony.account_id,
        level=EscalationLevel.L2,
        attempt_number=2,
        scheduled_at=NOW,
    )
    assert derive_idempotency_key(**args) == derive_idempotency_key(**args)
    assert derive_idempotency_key(**{**args, "attempt_number": 3}) != derive_idempotency_key(
        **args
    )


def test_second_originate_returns_409_and_places_no_second_call(telephony):
    """The failure this prevents is dialling a debtor twice."""
    fake = FakeAri()
    with FakeAriServer(fake) as server:
        client = AriClient(base_url=server.base_url, app_name="test")
        with tenant_session(telephony.company_id) as s:
            call = make_call(s, telephony, status=CallStatus.SCHEDULED)
            first = originate(s, client, call, caller_id=telephony.caller_e164)
            assert first.status is CallStatus.DIALING

            second_outcome = client.originate(
                call_id=call.id, to_e164=call.to_e164, caller_id=telephony.caller_e164
            )

    assert second_outcome.created is False
    assert "409" in second_outcome.detail
    assert len(fake.channels) == 1
    assert len(fake.originate_calls) == 2, "both POSTs were made"


def test_originate_retries_a_timeout_without_double_dialling(telephony):
    """The POST is idempotent by channelId, so retrying it is safe."""
    fake = FakeAri()
    fake.timeout_next_originate = True
    with FakeAriServer(fake) as server:
        client = AriClient(base_url=server.base_url, app_name="test", timeout=2.0)
        with tenant_session(telephony.company_id) as s:
            call = make_call(s, telephony, status=CallStatus.SCHEDULED)
            originate(s, client, call, caller_id=telephony.caller_e164)

    assert len(fake.channels) == 1


def test_play_refuses_a_media_name_with_an_extension(telephony):
    """Passing .sln makes Asterisk look for <hash>.sln.sln and find nothing."""
    with FakeAriServer(FakeAri()) as server:
        client = AriClient(base_url=server.base_url, app_name="test")
        with pytest.raises(AriError, match="extension"):
            client.play("abc", "/sounds/deadbeef.sln")


# --------------------------------------------------------------- caller id


def test_caller_id_must_be_carrier_approved(telephony):
    with admin_session() as s:
        caller = s.get(CallerId, telephony.caller_id_id)
        caller.carrier_approved = False
    with tenant_session(telephony.company_id) as s:
        with pytest.raises(CallBlocked) as exc:
            resolve_caller_id(s, telephony.company_id, telephony.caller_id_id)
    assert exc.value.reason == "CALLER_ID_NOT_APPROVED"


def test_a_tenant_cannot_present_another_tenants_cli(telephony, tenants):
    with tenant_session(telephony.company_id) as s:
        # Another company's caller id resolves to nothing for this company.
        assert resolve_caller_id(s, tenants.b.company_id, telephony.caller_id_id) is None


# ------------------------------------------------------------------- staging


def test_unstaged_audio_blocks_the_call(telephony, tmp_path):
    stager = LocalStager(str(tmp_path))
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, status=CallStatus.SCHEDULED)
        asset = s.get(AudioAsset, telephony.asset_id)
        with pytest.raises(CallBlocked) as exc:
            preflight(s, call=call, asset=asset, stager=stager)
    assert exc.value.reason == "AUDIO_NOT_STAGED"


def test_truncated_staged_file_is_not_considered_staged(telephony, tmp_path):
    """Existence alone misses the failure that actually happens."""
    stager = LocalStager(str(tmp_path))
    with tenant_session(telephony.company_id) as s:
        asset = s.get(AudioAsset, telephony.asset_id)
        stager.stage(asset, b"\x00" * 100)  # asset.byte_size is 16000
        assert stager.is_staged(asset) is False
        stager.stage(asset, b"\x00" * asset.byte_size)
        assert stager.is_staged(asset) is True


def test_preflight_passes_with_correctly_staged_audio(telephony, tmp_path):
    stager = LocalStager(str(tmp_path))
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, status=CallStatus.SCHEDULED)
        asset = s.get(AudioAsset, telephony.asset_id)
        stager.stage(asset, b"\x00" * asset.byte_size)
        result = preflight(s, call=call, asset=asset, stager=stager)

    assert result.call.message_hash == telephony.asset_hash
    assert not result.media_name.endswith(".sln")


def test_not_ready_asset_blocks(telephony, tmp_path):
    stager = LocalStager(str(tmp_path))
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, status=CallStatus.SCHEDULED)
        asset = s.get(AudioAsset, telephony.asset_id)
        asset.status = AssetStatus.FAILED
        s.flush()
        with pytest.raises(CallBlocked) as exc:
            preflight(s, call=call, asset=asset, stager=stager)
    assert exc.value.reason == "AUDIO_NOT_READY"


# ---------------------------------------------------------------- projection


def test_events_out_of_order_produce_the_right_state(telephony):
    """StasisEnd can and does land before PlaybackFinished."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "StasisEnd", at=NOW + timedelta(seconds=20), cause=16)
        ingest(s, call, "PlaybackFinished", at=NOW + timedelta(seconds=18))
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=21), cause=16)

        assert call.status is CallStatus.ANSWERED
        assert call.playback_completed is True
        assert call.delivered is True


def test_duplicate_events_change_nothing(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", key="dup")
        answered_first = call.answered_at
        ingest(s, call, "StasisStart", key="dup")
        events = s.execute(select(CallEvent).where(CallEvent.call_id == call.id)).scalars().all()

    assert len(events) == 1
    assert call.answered_at == answered_first


def test_status_never_moves_backwards(telephony):
    """A late StasisStart must not reopen a finished call."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=30), cause=19)
        assert call.status is CallStatus.NO_ANSWER
        ingest(s, call, "StasisStart", at=NOW + timedelta(seconds=1), key="late")
        assert call.status is CallStatus.NO_ANSWER


def test_channel_destroyed_without_stasis_start_is_no_answer(telephony):
    """The never-answered path. A naive implementation hangs here forever."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=30), cause=19)

        assert call.status is CallStatus.NO_ANSWER
        assert call.answered_at is None
        assert call.connected is False


def test_answered_then_dropped_early_is_connected_but_not_delivered(telephony):
    """The distinction rule 15 exists for.

    A call answered at second 0 and dropped at second 2 of a 22-second message
    connected and delivered nothing.
    """
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "PlaybackStarted", at=NOW + timedelta(seconds=1))
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=2), cause=16)

        assert call.connected is True
        assert call.delivered is False
        assert call.playback_completed is False
        assert call.duration_sec == 2


def test_machine_answer_is_never_a_delivery(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "AmdResult", at=NOW + timedelta(seconds=1), status="MACHINE")
        ingest(s, call, "PlaybackFinished", at=NOW + timedelta(seconds=20))
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=21), cause=16)

        assert call.status is CallStatus.ANSWERED_MACHINE
        assert call.delivered is False, "three voicemails must not escalate anyone to L3"


def test_state_is_derivable_purely_by_replaying_events(telephony):
    """If this fails, the event log is not the source of truth and rule 14 is
    decorative."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "ChannelDtmfReceived", at=NOW + timedelta(seconds=3), digit="1")
        ingest(s, call, "PlaybackStarted", at=NOW + timedelta(seconds=4))
        ingest(s, call, "PlaybackFinished", at=NOW + timedelta(seconds=25))
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=26), cause=16)

        before = {
            "status": call.status,
            "answered_at": call.answered_at,
            "playback_completed": call.playback_completed,
            "dtmf_ack": call.dtmf_ack,
            "ended_at": call.ended_at,
            "duration_sec": call.duration_sec,
            "hangup_cause": call.hangup_cause,
        }

        projection.rebuild(s, call)
        after = {
            "status": call.status,
            "answered_at": call.answered_at,
            "playback_completed": call.playback_completed,
            "dtmf_ack": call.dtmf_ack,
            "ended_at": call.ended_at,
            "duration_sec": call.duration_sec,
            "hangup_cause": call.hangup_cause,
        }

    assert before == after


# ------------------------------------------------------------------- the gate


def test_l3_keypress_sets_acknowledged(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, level=EscalationLevel.L3)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "ChannelDtmfReceived", at=NOW + timedelta(seconds=3), digit="1")
        ingest(s, call, "PlaybackFinished", at=NOW + timedelta(seconds=25))
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=26), cause=16)

        assert call.dtmf_ack is True
        assert call.acknowledged is True
        assert call.delivered is True


def test_l3_without_a_keypress_is_not_acknowledged(telephony):
    """No keypress, no account detail spoken, and no L3 delivery recorded."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, level=EscalationLevel.L3)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=15), cause=16)

        assert call.status is CallStatus.ANSWERED
        assert call.dtmf_ack is False
        assert call.playback_completed is False
        assert call.delivered is False


def test_dtmf_nine_opts_the_buyer_out(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        ingest(s, call, "StasisStart", at=NOW)
        ingest(s, call, "ChannelDtmfReceived", at=NOW + timedelta(seconds=4), digit="9")
        ingest(s, call, "ChannelDestroyed", at=NOW + timedelta(seconds=5), cause=16)
        outcomes.apply_outcome(s, call, now=NOW)
        buyer = s.get(Buyer, telephony.buyer_id)

        assert call.opted_out is True
        assert buyer.consent_withdrawn is True
        assert buyer.consent_withdrawn_at is not None


# ------------------------------------------------------------------ outcomes


@pytest.mark.parametrize(
    "cause,counts", [(34, False), (17, True), (19, True), (38, False), (16, True)]
)
def test_carrier_faults_do_not_consume_the_cap(telephony, cause, counts):
    with tenant_session(telephony.company_id) as s:
        status = CallStatus.BUSY if cause == 17 else CallStatus.FAILED
        if cause == 16:
            status = CallStatus.ANSWERED
        call = make_call(s, telephony, status=status, hangup_cause=cause)
        outcomes.apply_outcome(s, call, now=NOW)
        assert call.counts_against_cap is counts


def test_dead_number_is_retired(telephony):
    """buyer_phones.last_outcome exists precisely for this and is easy to leave
    unread."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, status=CallStatus.FAILED, hangup_cause=1)
        outcomes.apply_outcome(s, call, now=NOW)
        phone = s.get(BuyerPhone, telephony.phone_id)

        assert classify_attempt(CallStatus.FAILED, 1) is AttemptClass.TERMINAL_BAD_NUMBER
        assert phone.is_valid is False
        assert phone.last_outcome == "FAILED"


def test_delivered_call_resets_attempts_and_counts_a_delivery(telephony):
    with tenant_session(telephony.company_id) as s:
        state = s.get(EscalationState, telephony.state_id)
        state.attempts_at_level = 2
        s.flush()

        call = make_call(s, telephony, status=CallStatus.ANSWERED, playback_completed=True)
        ingest(s, call, "StasisStart", at=NOW)
        outcomes.apply_outcome(s, call, now=NOW)
        s.refresh(state)

        assert call.delivered is True
        assert state.attempts_at_level == 0
        assert state.delivered_at_level == 1


def test_undelivered_call_increments_attempts_only(telephony):
    with tenant_session(telephony.company_id) as s:
        state = s.get(EscalationState, telephony.state_id)
        state.attempts_at_level = 1
        s.flush()

        call = make_call(s, telephony, status=CallStatus.NO_ANSWER, hangup_cause=19)
        outcomes.apply_outcome(s, call, now=NOW)
        s.refresh(state)

        assert state.attempts_at_level == 2
        assert state.delivered_at_level == 0


# --------------------------------------------------------------- reconciler


def test_sweeper_resolves_a_call_the_websocket_lost_after_it_was_answered(telephony):
    """The websocket died mid-call, after the answer arrived.

    The honest outcome is ANSWERED and *not* delivered: the StasisStart is
    evidence the call connected, and the absent PlaybackFinished is the absence
    of evidence that anything was heard. Downgrading this to UNKNOWN would throw
    away a fact we actually hold.
    """
    with tenant_session(telephony.company_id) as s:
        call = make_call(
            s,
            telephony,
            status=CallStatus.DIALING,
            originated_at=NOW - timedelta(hours=1),
        )
        ingest(s, call, "StasisStart", at=NOW - timedelta(hours=1))
        call_id = call.id

        resolved = reconciler.sweep(s, now=NOW)
        s.refresh(call)

    assert call_id in [c.id for c in resolved]
    assert call.status is CallStatus.ANSWERED
    assert call.connected is True
    assert call.delivered is False
    assert call.ended_at is not None, "it must not sit in DIALING forever"


def test_unknown_counts_against_the_cap(telephony):
    """You cannot prove the phone did not ring, so fail safe toward the debtor."""
    with tenant_session(telephony.company_id) as s:
        call = make_call(
            s, telephony, status=CallStatus.DIALING, originated_at=NOW - timedelta(hours=1)
        )
        reconciler.sweep(s, now=NOW)
        s.refresh(call)

    assert call.status is CallStatus.UNKNOWN
    assert call.counts_against_cap is True


def test_sweeper_leaves_a_recent_call_alone(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony, status=CallStatus.DIALING, originated_at=NOW)
        resolved = reconciler.sweep(s, now=NOW + timedelta(seconds=5))
        assert resolved == []
        assert call.status is CallStatus.DIALING


def test_reconnect_diff_keeps_live_channels_and_closes_the_rest(telephony):
    with tenant_session(telephony.company_id) as s:
        live = make_call(s, telephony, status=CallStatus.DIALING)
        dead = make_call(s, telephony, status=CallStatus.DIALING)

        resolved = reconciler.reconnect_diff(s, live_channel_ids={str(live.id)})

        assert [c.id for c in resolved] == [dead.id]
        assert live.status is CallStatus.DIALING
        assert dead.status is CallStatus.UNKNOWN


def test_every_arrived_event_is_in_the_log(telephony):
    with tenant_session(telephony.company_id) as s:
        call = make_call(s, telephony)
        for kind in ("StasisStart", "PlaybackStarted", "PlaybackFinished", "StasisEnd"):
            ingest(s, call, kind, key=kind)
        events = s.execute(
            select(CallEvent).where(CallEvent.call_id == call.id)
        ).scalars().all()

    assert {e.event_type for e in events} == {
        "StasisStart",
        "PlaybackStarted",
        "PlaybackFinished",
        "StasisEnd",
    }
