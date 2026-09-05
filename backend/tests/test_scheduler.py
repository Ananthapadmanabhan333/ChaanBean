"""Scheduler, dispatch and campaign execution.

Two tests carry the weight here.

`test_two_workers_one_buyer_at_cap_place_exactly_one_call` is the race that
silently breaks compliance under load, and it is run genuinely concurrently —
a sequential test proves nothing about it.

`test_account_settled_between_scheduling_and_dispatch_is_not_called` is the
business bug that most damages trust with a customer who was already paying.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import redis as redis_lib
from sqlalchemy import delete, func, select

from app.comms.base import FakeProvider
from app.config import settings
from app.db import SessionLocal, admin_session, tenant_session
from app.models import (
    AccountStatus,
    AudioAsset,
    Buyer,
    BuyerPhone,
    Call,
    CallEvent,
    CallStatus,
    Campaign,
    CampaignStatus,
    CampaignTarget,
    Channel,
    ChannelOptOut,
    CreditAccount,
    DndStatus,
    EscalationLevel,
    EscalationState,
    Message,
    MessageEvent,
    MessageStatus,
    MessageTemplate,
    TemplateVersion,
)
from app.policy import BlockReason
from app.scheduler import campaign as campaign_ops
from app.scheduler.dispatch import dispatch_buyer
from app.scheduler.throttle import Throttle
from app.scheduler.tick import run_once
from app.storage.local import LocalStorage
from app.telephony.fake import FakeAri, FakeAriServer
from app.telephony.staging import LocalStager
from app.tts.local import SilentTtsBackend

NOW = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST, in-window


@pytest.fixture
def redis_client():
    try:
        client = redis_lib.from_url(settings.redis_url)
        client.ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"redis unavailable: {exc}")
    return client


@pytest.fixture
def world(tenants, tmp_path):
    """A company with an active campaign, one enrolled buyer, and templates."""

    class World:
        pass

    w = World()
    w.company_id = tenants.a.company_id
    w.buyer_id = tenants.a.buyer_id
    w.account_id = tenants.a.account_id
    w.storage = LocalStorage(str(tmp_path / "audio"))
    w.stager = LocalStager(str(tmp_path / "sounds"))
    w.tts = SilentTtsBackend()

    with admin_session() as s:
        campaign = Campaign(
            company_id=w.company_id,
            name="recovery",
            status=CampaignStatus.ACTIVE,
            max_attempts_per_day=1,
            max_attempts_per_week=3,
            min_hours_between_calls=24,
            channels=["VOICE"],
        )
        s.add(campaign)
        s.flush()
        w.campaign_id = campaign.id

        for level, channel in (
            (EscalationLevel.L1, "VOICE"),
            (EscalationLevel.L2, "VOICE"),
            (EscalationLevel.L1, "SMS"),
        ):
            tmpl = MessageTemplate(
                company_id=w.company_id,
                key=f"{level.value.lower()}_{channel.lower()}",
                level=level,
                channel=channel,
                language="en-IN",
            )
            s.add(tmpl)
            s.flush()
            s.add(
                TemplateVersion(
                    company_id=w.company_id,
                    template_id=tmpl.id,
                    version=1,
                    body="Namaste {buyer_name}, invoice {invoice_ref} for {amount_words} is overdue.",
                    dlt_template_id="DLT-1" if channel == "SMS" else None,
                )
            )

        s.add(
            CampaignTarget(
                company_id=w.company_id,
                campaign_id=campaign.id,
                buyer_id=w.buyer_id,
                is_active=True,
            )
        )
        s.add(
            EscalationState(
                company_id=w.company_id,
                account_id=w.account_id,
                level=EscalationLevel.L1,
                level_entered_at=NOW - timedelta(days=1),
            )
        )
        buyer = s.get(Buyer, w.buyer_id)
        buyer.next_action_at = NOW - timedelta(minutes=1)
        buyer.email = "buyer@example.test"
        account = s.get(CreditAccount, w.account_id)
        account.status = AccountStatus.OVERDUE
        account.invoice_ref = "INV-1"
        # Dated relative to the injected clock, not the wall clock. The shared
        # fixture builds it against `now()`, which is months away from NOW and
        # would leave it not yet overdue.
        account.due_date = NOW - timedelta(days=40)
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == w.buyer_id)
        ).scalar_one()
        phone.dnd_checked_at = NOW - timedelta(days=1)
        s.flush()

    yield w

    with admin_session() as s:
        cid = w.company_id
        s.execute(delete(MessageEvent).where(MessageEvent.company_id == cid))
        s.execute(delete(Message).where(Message.company_id == cid))
        s.execute(delete(CallEvent).where(CallEvent.company_id == cid))
        s.execute(delete(Call).where(Call.company_id == cid))
        s.execute(delete(ChannelOptOut).where(ChannelOptOut.company_id == cid))
        s.execute(delete(CampaignTarget).where(CampaignTarget.company_id == cid))
        s.execute(delete(Campaign).where(Campaign.company_id == cid))
        s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
        s.execute(delete(AudioAsset).where(AudioAsset.company_id == cid))
        s.execute(delete(TemplateVersion).where(TemplateVersion.company_id == cid))
        s.execute(delete(MessageTemplate).where(MessageTemplate.company_id == cid))
        buyer = s.get(Buyer, w.buyer_id)
        buyer.next_action_at = None
        buyer.consent_withdrawn = False
        for extra in s.execute(
            select(Buyer).where(Buyer.company_id == cid, Buyer.external_ref.like("BULK-%"))
        ).scalars():
            s.execute(delete(BuyerPhone).where(BuyerPhone.buyer_id == extra.id))
            s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
            s.execute(delete(CreditAccount).where(CreditAccount.buyer_id == extra.id))
            s.delete(extra)


def dispatch_kwargs(w, **over):
    base = dict(stager=w.stager, tts=w.tts, storage=w.storage, now=NOW)
    base.update(over)
    return base


# ------------------------------------------------------------------ the races


def test_two_workers_one_buyer_at_cap_place_exactly_one_call(world):
    """The race that silently breaks compliance under load.

    Both workers claim the same buyer at the same moment, both evaluate a cap of
    one, and without `pg_advisory_xact_lock` both see zero attempts and dial.
    """
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        session = SessionLocal()
        session.info["company_id"] = str(world.company_id)
        try:
            buyer = session.get(Buyer, world.buyer_id)
            campaign = session.get(Campaign, world.campaign_id)
            barrier.wait(timeout=15)
            dispatch_buyer(session, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    with tenant_session(world.company_id) as s:
        placed = s.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.buyer_id == world.buyer_id, Call.status != CallStatus.BLOCKED)
        ).scalar_one()
        blocked = s.execute(
            select(Call).where(
                Call.buyer_id == world.buyer_id, Call.status == CallStatus.BLOCKED
            )
        ).scalars().all()

    assert placed == 1, f"cap of 1 produced {placed} calls"
    assert len(blocked) == 1
    assert blocked[0].block_reason == BlockReason.DAILY_CAP_REACHED.value


def test_skip_locked_lets_two_workers_share_a_batch(world):
    """Twenty buyers, two workers, no double-processing and none missed."""
    buyer_ids = []
    with admin_session() as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.max_attempts_per_day = 5
        for n in range(20):
            buyer = Buyer(
                company_id=world.company_id,
                name=f"Bulk Buyer {n}",
                external_ref=f"BULK-{n}",
                next_action_at=NOW - timedelta(minutes=1),
            )
            s.add(buyer)
            s.flush()
            s.add(
                BuyerPhone(
                    company_id=world.company_id,
                    buyer_id=buyer.id,
                    e164=f"+9199{n:08d}",
                    dnd_status=DndStatus.CLEAR,
                    dnd_checked_at=NOW - timedelta(days=1),
                )
            )
            account = CreditAccount(
                company_id=world.company_id,
                buyer_id=buyer.id,
                invoice_ref=f"INV-B{n}",
                outstanding_paise=5_000_000,
                due_date=NOW - timedelta(days=40),
                status=AccountStatus.OVERDUE,
            )
            s.add(account)
            s.flush()
            s.add(
                EscalationState(
                    company_id=world.company_id,
                    account_id=account.id,
                    level=EscalationLevel.L1,
                    level_entered_at=NOW - timedelta(days=1),
                )
            )
            s.add(
                CampaignTarget(
                    company_id=world.company_id,
                    campaign_id=world.campaign_id,
                    buyer_id=buyer.id,
                    is_active=True,
                )
            )
            buyer_ids.append(buyer.id)

    processed: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker():
        session = SessionLocal()
        session.info["company_id"] = str(world.company_id)
        try:
            results = run_once(session, limit=25, **dispatch_kwargs(world))
            with lock:
                processed.extend(r.buyer_id for r in results)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(processed) == len(set(processed)), "a buyer was processed twice"
    assert set(buyer_ids) <= set(processed), "a buyer was skipped entirely"


# ------------------------------------------------------- the dispatch recheck


def test_account_settled_between_scheduling_and_dispatch_is_not_called(world):
    """The worst business bug this product can commit.

    Policy is evaluated, then the payment lands, then we dial. Re-reading the
    account immediately before contact is the only thing that catches it.
    """
    with tenant_session(world.company_id) as s:
        account = s.get(CreditAccount, world.account_id)
        account.status = AccountStatus.SETTLED
        account.outstanding_paise = 0
        s.flush()

        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))

    assert result.allowed is False
    assert result.reason in (
        BlockReason.ACCOUNT_CLOSED.value,
        BlockReason.ACCOUNT_NOT_OVERDUE.value,
    )

    with tenant_session(world.company_id) as s:
        placed = s.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.buyer_id == world.buyer_id, Call.status != CallStatus.BLOCKED)
        ).scalar_one()
    assert placed == 0


def test_dispute_raised_after_scheduling_blocks_dispatch(world):
    with tenant_session(world.company_id) as s:
        account = s.get(CreditAccount, world.account_id)
        account.status = AccountStatus.IN_DISPUTE
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))

    assert result.allowed is False
    assert result.reason == BlockReason.ACCOUNT_IN_DISPUTE.value


# ----------------------------------------------------------------- blocking


def test_blocks_are_recorded_with_a_reason_and_a_retry_time(world):
    """How you prove compliance, and how you debug a campaign that is not
    calling anyone."""
    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        buyer.consent_withdrawn = True
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))
        call = s.get(Call, result.call_id)

        assert call.status is CallStatus.BLOCKED
        assert call.block_reason == BlockReason.CONSENT_WITHDRAWN.value
        assert call.counts_against_cap is False
        assert buyer.next_action_at > NOW


def test_a_blocked_call_does_not_consume_the_cap(world):
    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        # Weekend: blocked, but the debtor was never contacted.
        saturday = datetime(2026, 3, 14, 6, 0, tzinfo=timezone.utc)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world, now=saturday)
        )
        assert result.reason == BlockReason.WEEKEND_NOT_PERMITTED.value

        # Now on a weekday the cap must still be intact.
        buyer.next_action_at = NOW
        second = dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))

    assert second.allowed is True


# ------------------------------------------------------------------ channels


def test_sms_is_used_when_the_campaign_enables_it(world):
    providers = {Channel.SMS: FakeProvider(Channel.SMS)}
    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS", "VOICE"]
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, providers=providers, **dispatch_kwargs(world)
        )

    assert result.allowed is True
    assert result.channel is Channel.SMS
    assert len(providers[Channel.SMS].sent) == 1
    # 5,000,000 paise is 50,000 rupees, spoken in the Indian system.
    assert "fifty thousand rupees" in providers[Channel.SMS].sent[0]["body"]


def test_a_repeated_send_does_not_reach_the_debtor_twice(world):
    """A duplicate SMS is a compliance incident, not a duplicate row."""
    provider = FakeProvider(Channel.SMS)
    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS"]
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        dispatch_buyer(
            s, buyer=buyer, campaign=campaign,
            providers={Channel.SMS: provider}, **dispatch_kwargs(world),
        )
        message = s.execute(select(Message)).scalars().one()
        from app.comms import service as comms_service

        comms_service.send(s, message, provider)
        comms_service.send(s, message, provider)

    assert len(provider.sent) == 1, "the provider was asked to send more than once"


def test_channel_opt_out_blocks_that_channel_only(world):
    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS"]
        s.add(
            ChannelOptOut(
                company_id=world.company_id, buyer_id=world.buyer_id, channel=Channel.SMS
            )
        )
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign,
            providers={Channel.SMS: FakeProvider(Channel.SMS)}, **dispatch_kwargs(world),
        )

    assert result.allowed is False
    assert result.reason == BlockReason.CHANNEL_OPTED_OUT.value


def test_message_delivery_events_project_separately(world):
    from app.comms import service as comms_service

    provider = FakeProvider(Channel.SMS)
    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS"]
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        dispatch_buyer(
            s, buyer=buyer, campaign=campaign,
            providers={Channel.SMS: provider}, **dispatch_kwargs(world),
        )
        message = s.execute(select(Message)).scalars().one()
        assert message.status is MessageStatus.SENT
        assert message.delivered is False

        comms_service.ingest_event(
            s, message=message, source="provider", event_type="delivered",
            dedupe_key="d1", payload={},
        )
        assert message.delivered is True
        assert message.read is False

        # Duplicated webhook: nothing changes.
        first_delivered = message.delivered_at
        comms_service.ingest_event(
            s, message=message, source="provider", event_type="delivered",
            dedupe_key="d1", payload={},
        )
        assert message.delivered_at == first_delivered


# ------------------------------------------------------------------ throttle


def test_cps_limiter_holds_a_burst(redis_client):
    throttle = Throttle(redis_client, calls_per_second=5, max_concurrent=100)
    throttle.reset()
    granted = sum(1 for _ in range(100) if throttle.acquire_rate())
    assert granted <= 5, f"{granted} originations allowed in one second at a cap of 5"
    assert granted >= 1


def test_per_tenant_concurrency_cap_holds_under_competition(redis_client):
    """One company's 5,000-row campaign must not consume every channel."""
    throttle = Throttle(
        redis_client, calls_per_second=1000, max_concurrent=100, max_concurrent_per_tenant=3
    )
    throttle.reset()
    company_a, company_b = uuid.uuid4(), uuid.uuid4()

    a_granted = sum(1 for _ in range(10) if throttle.acquire_channel(company_a))
    b_granted = sum(1 for _ in range(10) if throttle.acquire_channel(company_b))

    assert a_granted == 3
    assert b_granted == 3, "one tenant starved another"
    throttle.reset()


def test_throttle_fails_closed_without_redis():
    """A dialler that keeps dialling when it has lost track of its own rate is
    the one that gets a trunk suspended."""
    throttle = Throttle(None, calls_per_second=100)
    assert throttle.acquire_rate() is False
    assert throttle.acquire_channel(uuid.uuid4()) is False


# ----------------------------------------------------------------- allowlist


def test_allowlist_blocks_a_non_allowlisted_number(world, monkeypatch):
    """Twenty lines that prevent the worst incident available to this product."""
    monkeypatch.setattr(settings, "env", "staging")
    monkeypatch.setattr(settings, "contact_allowlist_enforced", True)
    monkeypatch.setattr(settings, "contact_allowlist", ["+919999999999"])

    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))

    assert result.allowed is False
    assert result.reason == "CONTACT_NOT_ALLOWLISTED"


# ---------------------------------------------------------------- end to end


def test_a_campaign_runs_to_completion_unattended(world):
    """Every buyer ends with either an outcome or a block reason, and nobody
    exceeds their cap."""
    with admin_session() as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.max_attempts_per_day = 1
        for n in range(19):
            buyer = Buyer(
                company_id=world.company_id,
                name=f"Bulk Buyer {n}",
                external_ref=f"BULK-{n}",
                next_action_at=NOW - timedelta(minutes=1),
            )
            s.add(buyer)
            s.flush()
            s.add(
                BuyerPhone(
                    company_id=world.company_id,
                    buyer_id=buyer.id,
                    e164=f"+9188{n:08d}",
                    # A third of them are unscrubbed, which must block.
                    dnd_status=DndStatus.CLEAR if n % 3 else DndStatus.UNKNOWN,
                    dnd_checked_at=NOW - timedelta(days=1),
                )
            )
            account = CreditAccount(
                company_id=world.company_id,
                buyer_id=buyer.id,
                invoice_ref=f"INV-C{n}",
                outstanding_paise=5_000_000 + n,
                due_date=NOW - timedelta(days=40),
                status=AccountStatus.OVERDUE,
            )
            s.add(account)
            s.flush()
            s.add(
                EscalationState(
                    company_id=world.company_id,
                    account_id=account.id,
                    level=EscalationLevel.L1,
                    level_entered_at=NOW - timedelta(days=1),
                )
            )
            s.add(
                CampaignTarget(
                    company_id=world.company_id,
                    campaign_id=world.campaign_id,
                    buyer_id=buyer.id,
                    is_active=True,
                )
            )

    fake = FakeAri()
    with FakeAriServer(fake) as server:
        from app.telephony.ari import AriClient

        client = AriClient(base_url=server.base_url, app_name="test")
        with tenant_session(world.company_id) as s:
            results = run_once(
                s, limit=50, ari_client=client, **dispatch_kwargs(world)
            )

    assert len(results) == 20
    assert all(r.allowed or r.reason for r in results), "a buyer ended with neither"

    with tenant_session(world.company_id) as s:
        rows = s.execute(
            select(Call.buyer_id, func.count())
            .where(Call.campaign_id == world.campaign_id, Call.status != CallStatus.BLOCKED)
            .group_by(Call.buyer_id)
        ).all()
        blocked = s.execute(
            select(Call.block_reason, func.count())
            .where(Call.campaign_id == world.campaign_id, Call.status == CallStatus.BLOCKED)
            .group_by(Call.block_reason)
        ).all()

    for _, count in rows:
        assert count <= 1, "a buyer exceeded a daily cap of 1"
    reasons = {r: n for r, n in blocked}
    assert BlockReason.DND_UNKNOWN.value in reasons, (
        "unscrubbed numbers must block, and be visible as such"
    )


def test_campaign_progress_reports_the_block_breakdown(world):
    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        buyer.consent_withdrawn = True
        campaign = s.get(Campaign, world.campaign_id)
        dispatch_buyer(s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world))
        report = campaign_ops.progress(s, campaign)

    assert report["targets"] == 1
    assert report["block_reasons"][BlockReason.CONSENT_WITHDRAWN.value] == 1
