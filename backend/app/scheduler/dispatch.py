"""Deciding and acting on one buyer.

Two things in here are load-bearing far beyond their size.

**The advisory lock.** Two campaigns targeting one buyer both count today's
attempts, both see zero, and both dial. With `SKIP LOCKED` workers this is not
theoretical — it is the concurrency this architecture creates on purpose. The
lock is taken before the count and held through the insert; without it the cap
is advisory rather than enforced.

**Re-reading account status at dispatch.** Policy is evaluated against state read
now, not at schedule time. Calling someone at L3 who paid yesterday is the
highest-severity business bug this product can commit, and the gap between
scheduling and dialling is exactly where that payment lands.

Blocks are recorded, never silent. The block log is how you prove compliance,
and how you debug a campaign that "isn't calling anyone".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.calls import projection
from app.comms import service as comms_service
from app.config import settings
from app.models import (
    AccountStatus,
    AudioAsset,
    BlackoutDate,
    Buyer,
    BuyerPhone,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    Channel,
    ChannelOptOut,
    CreditAccount,
    EscalationState,
    EventSource,
    Message,
    MessageStatus,
    MessageTemplate,
    TemplateVersion,
)
from app.policy import engine as policy
from app.providers.base import ContactBlocked, assert_contactable
from app.render.service import RenderFailed, ensure_audio
from app.render.template import MergeContext
from app.telephony.flow import CallBlocked, derive_idempotency_key, resolve_caller_id
from app.trade.accounts import ensure_escalation_state
from app.trade.ageing import days_past_due

log = logging.getLogger(__name__)

RETRY_AFTER_BLOCK = timedelta(hours=4)

# Refusals that no retry, no scrub and no passage of time will clear. The ladder
# deliberately holds an unreached debtor at L2 rather than escalating them to
# legal content, and a debtor whose every valid number is on the registry can
# never be reached by automation at all. Both are designed dead ends whose only
# exit is a person, and until this set existed nothing put them in front of one.
#
# DND_UNKNOWN and DND_CHECK_STALE are deliberately absent: a scrub clears both,
# and queueing them for a human would bury the two refusals that need one.
_NEEDS_HUMAN_REVIEW = frozenset(
    {
        policy.BlockReason.L3_REQUIRES_PRIOR_CONTACT.value,
        policy.BlockReason.DND_REGISTERED.value,
    }
)

# Refusals that only a person lifts, so a retry timer is the wrong answer.
# Consent is the whole of the set: restoring it is a deliberate admin act with
# its own permission, and nothing else here reverses. DND_REGISTERED is
# deliberately absent — a scrub can change that answer, and parking on it would
# leave the buyer waiting on a review queue whose resolution does not re-arm the
# scheduler.
_PARKS_THE_BUYER = frozenset({policy.BlockReason.CONSENT_WITHDRAWN.value})


@dataclass
class DispatchResult:
    buyer_id: UUID
    allowed: bool
    channel: Channel | None = None
    reason: str | None = None
    call_id: UUID | None = None
    message_id: UUID | None = None
    detail: str = ""


def lock_buyer(session: Session, buyer_id: UUID) -> None:
    """Serialise everything that touches one buyer's frequency budget."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": str(buyer_id)}
    )


# ------------------------------------------------------------------- context


def _attempts(session: Session, buyer_id: UUID, since: datetime) -> int:
    """Only attempts that count. A carrier fault is our failure, not the
    debtor's ring."""
    calls = session.execute(
        select(func.count())
        .select_from(Call)
        .where(
            Call.buyer_id == buyer_id,
            Call.created_at >= since,
            Call.counts_against_cap.is_(True),
            Call.status.notin_([CallStatus.BLOCKED, CallStatus.CANCELLED]),
        )
    ).scalar_one()
    messages = session.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.buyer_id == buyer_id,
            Message.created_at >= since,
            Message.counts_against_cap.is_(True),
            Message.status != MessageStatus.BLOCKED,
        )
    ).scalar_one()
    return int(calls) + int(messages)


def _attempts_by_channel(session: Session, buyer_id: UUID, since: datetime) -> dict:
    rows = session.execute(
        select(Message.channel, func.count())
        .where(
            Message.buyer_id == buyer_id,
            Message.created_at >= since,
            Message.status != MessageStatus.BLOCKED,
        )
        .group_by(Message.channel)
    ).all()
    counts = {channel: int(n) for channel, n in rows}
    counts[Channel.VOICE] = int(
        session.execute(
            select(func.count())
            .select_from(Call)
            .where(
                Call.buyer_id == buyer_id,
                Call.created_at >= since,
                Call.status.notin_([CallStatus.BLOCKED, CallStatus.CANCELLED]),
            )
        ).scalar_one()
    )
    return counts


def build_context(
    session: Session, buyer: Buyer, campaign: Campaign, *, now: datetime
) -> policy.DecisionContext:
    """Assemble everything the engine needs, read *now*."""
    day_start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)

    accounts = list(
        session.execute(
            select(CreditAccount).where(CreditAccount.buyer_id == buyer.id)
        ).scalars()
    )
    states = {
        s.account_id: s
        for s in session.execute(
            select(EscalationState).where(
                EscalationState.account_id.in_([a.id for a in accounts] or [None])
            )
        ).scalars()
    }

    account_refs = []
    for account in accounts:
        state = states.get(account.id)
        account_refs.append(
            policy.AccountRef(
                id=account.id,
                status=account.status,
                outstanding_paise=account.outstanding_paise,
                due_date=account.due_date,
                level=state.level if state else policy.EscalationLevel.L1,
                level_entered_at=state.level_entered_at if state else now,
                attempts_at_level=state.attempts_at_level if state else 0,
                delivered_at_level=state.delivered_at_level if state else 0,
                last_contact_at=state.last_contact_at if state else None,
            )
        )

    phones = [
        policy.PhoneRef(
            id=p.id,
            e164=p.e164,
            priority=p.priority,
            is_valid=p.is_valid,
            dnd_status=p.dnd_status,
            dnd_checked_at=p.dnd_checked_at,
            number_type=p.number_type,
        )
        for p in session.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == buyer.id)
        ).scalars()
    ]

    templates = []
    rows = session.execute(
        select(TemplateVersion, MessageTemplate)
        .join(MessageTemplate, MessageTemplate.id == TemplateVersion.template_id)
        .where(
            MessageTemplate.company_id == campaign.company_id,
            MessageTemplate.is_active.is_(True),
        )
    ).all()
    for version, tmpl in rows:
        templates.append(
            policy.TemplateRef(
                key=tmpl.key,
                level=tmpl.level,
                language=tmpl.language,
                version_id=version.id,
                is_approved=version.is_approved,
                channel=Channel(tmpl.channel),
                dlt_registered=version.dlt_template_id is not None,
            )
        )

    blackouts = frozenset(
        d.day
        for d in session.execute(
            select(BlackoutDate).where(BlackoutDate.company_id == campaign.company_id)
        ).scalars()
    )
    opted_out = frozenset(
        o.channel
        for o in session.execute(
            select(ChannelOptOut).where(ChannelOptOut.buyer_id == buyer.id)
        ).scalars()
    )

    return policy.DecisionContext(
        now=now,
        campaign_active=campaign.status is CampaignStatus.ACTIVE,
        timezone=campaign.timezone,
        window_start=campaign.window_start,
        window_end=campaign.window_end,
        call_on_weekends=campaign.call_on_weekends,
        blackout_days=blackouts,
        max_attempts_per_day=campaign.max_attempts_per_day,
        max_attempts_per_week=campaign.max_attempts_per_week,
        min_hours_between_calls=campaign.min_hours_between_calls,
        consent_withdrawn=buyer.consent_withdrawn,
        suppressed_until=buyer.suppressed_until,
        phones=tuple(phones),
        accounts=tuple(account_refs),
        attempts_today=_attempts(session, buyer.id, day_start),
        attempts_this_week=_attempts(session, buyer.id, week_start),
        templates=tuple(templates),
        policy=policy.EscalationPolicy(),
        language=buyer.language,
        channels_enabled=tuple(Channel(c) for c in (campaign.channels or [])),
        opted_out_channels=opted_out,
        email=buyer.email,
        attempts_today_by_channel=_attempts_by_channel(session, buyer.id, day_start),
        max_attempts_per_day_by_channel={},
    )


# ------------------------------------------------------------------- blocking


def record_block(
    session: Session,
    *,
    buyer: Buyer,
    campaign: Campaign,
    reason: str,
    detail: str,
    account_id: UUID | None,
    level,
    now: datetime,
    channel: Channel | None = None,
    retry_after: timedelta = RETRY_AFTER_BLOCK,
) -> Call | Message:
    """A refusal is a row, never a silence.

    The row goes in the table for the channel that was refused. A blocked SMS
    written as a Call with an empty `to_e164` is not merely untidy: every block
    report counts it as a call that never happened, so an operator debugging a
    silent messaging campaign reads a dialler failure instead.

    Refusals raised before the policy engine has chosen a channel have none, and
    a Call row remains the right home for those.
    """
    account_id = account_id or _any_account(session, buyer.id)
    level = level or policy.EscalationLevel.L1

    if channel is not None and channel is not Channel.VOICE:
        row: Call | Message = Message(
            company_id=campaign.company_id,
            campaign_id=campaign.id,
            buyer_id=buyer.id,
            account_id=account_id,
            channel=channel,
            level=level,
            to_address="",
            rendered_body="",
            status=MessageStatus.BLOCKED,
            block_reason=reason,
            counts_against_cap=False,
            idempotency_key=f"blocked:{buyer.id}:{now.isoformat()}:{reason}",
        )
    else:
        row = Call(
            company_id=campaign.company_id,
            campaign_id=campaign.id,
            buyer_id=buyer.id,
            account_id=account_id,
            level=level,
            to_e164="",
            scheduled_at=now,
            status=CallStatus.BLOCKED,
            block_reason=reason,
            counts_against_cap=False,
            idempotency_key=f"blocked:{buyer.id}:{now.isoformat()}:{reason}",
        )
    session.add(row)

    if reason in _NEEDS_HUMAN_REVIEW and account_id is not None:
        _flag_for_review(session, account_id, reason=reason, detail=detail, now=now)

    if reason in _PARKS_THE_BUYER:
        # `None` is how this codebase says "nothing further is due for this
        # buyer". `record_consent_withdrawal` sets it for exactly this reason,
        # and a retry timer here would undo it — waking every four hours to
        # write the same refusal about a person who asked to be left alone, and
        # keeping their campaign target alive because the sweep reads a
        # non-null wake-up as unfinished work.
        buyer.next_action_at = None
    else:
        buyer.next_action_at = now + retry_after
    session.flush()
    log.info("buyer %s blocked: %s (%s)", buyer.id, reason, detail)
    return row


def _flag_for_review(
    session: Session, account_id: UUID, *, reason: str, detail: str, now: datetime
) -> None:
    """Put one account in front of a person, with why written down.

    The ladder state is created if it is missing. An account can hit
    DND_REGISTERED on its very first dispatch, before anything has written a
    ladder position, and a review queue that silently drops those is the queue
    not existing.
    """
    account = session.get(CreditAccount, account_id)
    if account is None:
        return
    state = ensure_escalation_state(session, account, now=now)
    if state.needs_human_review:
        # A transition, like the ladder stop. The refusals in this set do not
        # clear on their own, so the buyer comes back every four hours; writing
        # the flag and the history entry again on each pass buries the queue in
        # duplicates of one unresolved fact and leaves a reviewer unable to tell
        # a new problem from an old one.
        return
    state.needs_human_review = True
    state.history = list(state.history or []) + [
        {
            "at": now.isoformat(),
            "event": "needs_human_review",
            "reason": reason,
            "detail": detail,
        }
    ]


def _blocked_result(
    row: Call | Message, *, buyer_id: UUID, reason: str, detail: str = ""
) -> DispatchResult:
    is_message = isinstance(row, Message)
    return DispatchResult(
        buyer_id,
        False,
        channel=row.channel if is_message else None,
        reason=reason,
        call_id=None if is_message else row.id,
        message_id=row.id if is_message else None,
        detail=detail,
    )


def _any_account(session: Session, buyer_id: UUID) -> UUID | None:
    account = session.execute(
        # Ordered, so a refusal that names no account lands on the same one every
        # time rather than on whichever row the planner happened to return.
        select(CreditAccount)
        .where(CreditAccount.buyer_id == buyer_id)
        .order_by(CreditAccount.due_date, CreditAccount.id)
        .limit(1)
    ).scalar_one_or_none()
    return account.id if account else None


# ------------------------------------------------------------------- dispatch


def dispatch_buyer(
    session: Session,
    *,
    buyer: Buyer,
    campaign: Campaign,
    now: datetime | None = None,
    ari_client=None,
    stager=None,
    providers: dict | None = None,
    throttle=None,
    redis_client=None,
    tts=None,
    storage=None,
) -> DispatchResult:
    """Evaluate one buyer and act. Returns what happened, for the tick log."""
    now = now or datetime.now(timezone.utc)
    lock_buyer(session, buyer.id)

    ctx = build_context(session, buyer, campaign, now=now)
    decision = policy.evaluate(ctx)

    if not decision.allowed:
        row = record_block(
            session,
            buyer=buyer,
            campaign=campaign,
            reason=decision.reason.value,
            detail=decision.detail,
            account_id=decision.account_id,
            level=decision.level,
            now=now,
            channel=decision.channel,
        )
        return _blocked_result(
            row, buyer_id=buyer.id, reason=decision.reason.value, detail=decision.detail
        )

    # Re-read the account immediately before contact. A payment or a dispute
    # that landed since scheduling must stop this, and this is the only place
    # that check is worth anything.
    account = session.get(CreditAccount, decision.account_id)
    if account is None or account.status in (
        AccountStatus.SETTLED,
        AccountStatus.IN_DISPUTE,
        AccountStatus.WRITTEN_OFF,
    ):
        reason = (
            policy.BlockReason.ACCOUNT_IN_DISPUTE.value
            if account and account.status is AccountStatus.IN_DISPUTE
            else policy.BlockReason.ACCOUNT_CLOSED.value
        )
        row = record_block(
            session,
            buyer=buyer,
            campaign=campaign,
            reason=reason,
            detail="account changed between scheduling and dispatch",
            account_id=decision.account_id,
            level=decision.level,
            now=now,
            channel=decision.channel,
        )
        return _blocked_result(row, buyer_id=buyer.id, reason=reason)

    # Non-production must not contact a real person (rule 4).
    try:
        assert_contactable(decision.to_address or "")
    except ContactBlocked as exc:
        row = record_block(
            session,
            buyer=buyer,
            campaign=campaign,
            reason="CONTACT_NOT_ALLOWLISTED",
            detail=str(exc),
            account_id=decision.account_id,
            level=decision.level,
            now=now,
            channel=decision.channel,
            retry_after=timedelta(days=3650),
        )
        return _blocked_result(
            row, buyer_id=buyer.id, reason="CONTACT_NOT_ALLOWLISTED"
        )

    version = session.get(TemplateVersion, decision.template_version_id)
    merge_ctx = MergeContext(
        buyer_name=buyer.name,
        amount_paise=account.outstanding_paise,
        invoice_ref=account.invoice_ref or "your account",
        days_past_due=days_past_due(account.due_date.date(), now.date()),
        company_name=campaign.company.name if campaign.company else "your supplier",
        due_date=account.due_date.date(),
    )

    if decision.channel is Channel.VOICE:
        return _dispatch_voice(
            session,
            buyer=buyer,
            campaign=campaign,
            account=account,
            decision=decision,
            version=version,
            merge_ctx=merge_ctx,
            now=now,
            ari_client=ari_client,
            stager=stager,
            throttle=throttle,
            tts=tts,
            storage=storage,
        )
    return _dispatch_message(
        session,
        buyer=buyer,
        campaign=campaign,
        account=account,
        decision=decision,
        version=version,
        merge_ctx=merge_ctx,
        now=now,
        providers=providers or {},
        redis_client=redis_client,
    )


def _dispatch_message(
    session, *, buyer, campaign, account, decision, version, merge_ctx, now,
    providers, redis_client,
) -> DispatchResult:
    from app.render.template import normalise, render

    body = normalise(render(version.body, merge_ctx))
    message = comms_service.queue_message(
        session,
        company_id=campaign.company_id,
        buyer_id=buyer.id,
        account_id=account.id,
        campaign_id=campaign.id,
        channel=decision.channel,
        level=decision.level,
        to_address=decision.to_address,
        body=body,
        template_version_id=version.id,
        attempt_number=1,
        scheduled_at=now,
        phone_id=decision.phone_id,
    )
    # Commit before handing anything to a provider: if the transaction rolled
    # back afterwards, the debtor would have a message we have no record of.
    session.commit()

    provider = providers.get(decision.channel)
    if provider is None:
        message.status = MessageStatus.FAILED
        message.failed_reason = f"no provider configured for {decision.channel.value}"
        session.commit()
        return DispatchResult(
            buyer.id, False, channel=decision.channel, reason="NO_PROVIDER",
            message_id=message.id,
        )

    comms_service.send(session, message, provider, redis_client=redis_client)
    buyer.next_action_at = now + timedelta(hours=campaign.min_hours_between_calls)
    session.commit()
    return DispatchResult(
        buyer.id, True, channel=decision.channel, message_id=message.id,
        detail=decision.detail,
    )


def _dispatch_voice(
    session, *, buyer, campaign, account, decision, version, merge_ctx, now,
    ari_client, stager, throttle, tts, storage,
) -> DispatchResult:
    try:
        result = ensure_audio(
            session,
            company_id=campaign.company_id,
            template_version=version,
            merge_ctx=merge_ctx,
            tts=tts,
            storage=storage,
        )
    except RenderFailed as exc:
        session.commit()  # keep the FAILED asset and its reason
        call = record_block(
            session, buyer=buyer, campaign=campaign, reason="AUDIO_NOT_READY",
            detail=str(exc), account_id=account.id, level=decision.level, now=now,
        )
        session.commit()
        return DispatchResult(buyer.id, False, reason="AUDIO_NOT_READY", call_id=call.id)

    asset = result.asset
    if stager is not None:
        pcm = storage.get(asset.storage_key) if storage else None
        if pcm is not None and not stager.is_staged(asset):
            from app.tts.convert import pcm_to_alaw

            stager.stage(asset, pcm, pcm_to_alaw(pcm))

    try:
        caller_id = resolve_caller_id(session, campaign.company_id, campaign.caller_id_id)
    except CallBlocked as exc:
        call = record_block(
            session, buyer=buyer, campaign=campaign, reason=exc.reason, detail=exc.detail,
            account_id=account.id, level=decision.level, now=now,
        )
        return DispatchResult(buyer.id, False, reason=exc.reason, call_id=call.id)

    if throttle is not None:
        if not throttle.acquire_rate() or not throttle.acquire_channel(campaign.company_id):
            # Not a refusal about the debtor — try again shortly.
            buyer.next_action_at = now + timedelta(minutes=2)
            session.flush()
            return DispatchResult(buyer.id, False, reason="THROTTLED", detail="rate or channel limit")

    call = Call(
        company_id=campaign.company_id,
        campaign_id=campaign.id,
        buyer_id=buyer.id,
        account_id=account.id,
        phone_id=decision.phone_id,
        level=decision.level,
        to_e164=decision.to_e164,
        from_e164=caller_id,
        scheduled_at=now,
        status=CallStatus.SCHEDULED,
        audio_asset_id=asset.id,
        message_hash=asset.message_hash,
        template_version_id=version.id,
        idempotency_key=derive_idempotency_key(
            company_id=campaign.company_id,
            buyer_id=buyer.id,
            account_id=account.id,
            level=decision.level,
            attempt_number=1,
            scheduled_at=now,
        ),
    )
    session.add(call)
    session.flush()

    if stager is not None:
        try:
            from app.telephony.flow import preflight

            preflight(session, call=call, asset=asset, stager=stager)
        except CallBlocked as exc:
            call.status = CallStatus.BLOCKED
            call.block_reason = exc.reason
            call.counts_against_cap = False
            buyer.next_action_at = now + RETRY_AFTER_BLOCK
            session.commit()
            return DispatchResult(buyer.id, False, reason=exc.reason, call_id=call.id)

    # Commit *before* originating. Never dial from inside a transaction that
    # might roll back and leave a dialled debtor with no record.
    session.commit()

    if ari_client is None:
        return DispatchResult(
            buyer.id, True, channel=Channel.VOICE, call_id=call.id,
            detail="no ARI client; call left SCHEDULED",
        )

    from app.telephony.flow import originate

    originate(session, ari_client, call, caller_id=caller_id)
    buyer.next_action_at = now + timedelta(hours=campaign.min_hours_between_calls)
    session.commit()
    return DispatchResult(
        buyer.id, True, channel=Channel.VOICE, call_id=call.id, detail=decision.detail
    )
