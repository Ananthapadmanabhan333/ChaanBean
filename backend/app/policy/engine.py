"""The Policy Engine — whether a call happens, at what level, about which account.

This module is **pure**. No database, no network, no clock of its own. Everything
it needs arrives in a `DecisionContext`, and the only thing it returns is a
`Decision`. That constraint is not tidiness: it means every production decision
is reproducible from stored context. When someone asks "why did you call this
person at 6pm on a Saturday", you replay the exact input and get the exact
answer — which is the difference between an auditable system and an opinion.

Two properties matter more than anything else here:

* **Fail closed.** Any gate that cannot be evaluated blocks. An unknown DND
  status blocks. A scrub older than `DND_MAX_AGE` blocks. An unapproved L3
  template blocks. There is no default-allow path in this file.
* **Every refusal names itself.** `BlockReason` is a closed enum defined here and
  nowhere else. "The system decided not to call" is not an auditable answer.

The ladder thresholds in `EscalationPolicy` are placeholders. They are a business
and legal decision, not an engineering one, and they must be confirmed with
counsel against the current DND/TRAI regime before any of this contacts a real
debtor.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.models import (
    AccountStatus,
    AttemptClass,
    CallStatus,
    Channel,
    DndStatus,
    EscalationLevel,
)

# A DND scrub older than this is treated as no scrub at all. Mirrors
# `settings.dnd_max_age_days`, but the engine takes no configuration of its own —
# a pure function that reads global settings is no longer reproducible.
DND_MAX_AGE = timedelta(days=7)

# Returned by `next_retry_delay` for a number the carrier says is dead. The caller
# should retire the number rather than actually wait this long; the value exists
# so the return type stays a timedelta instead of an Optional someone forgets to
# check.
RETIRE_NUMBER_DELAY = timedelta(days=3650)


class BlockReason(str, enum.Enum):
    """Why a call did not happen. Closed set, defined once, stored on the call row."""

    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    BUYER_SUPPRESSED = "BUYER_SUPPRESSED"
    ACCOUNT_IN_DISPUTE = "ACCOUNT_IN_DISPUTE"
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    ACCOUNT_NOT_OVERDUE = "ACCOUNT_NOT_OVERDUE"
    NO_VALID_PHONE = "NO_VALID_PHONE"
    DND_REGISTERED = "DND_REGISTERED"
    DND_UNKNOWN = "DND_UNKNOWN"
    DND_CHECK_STALE = "DND_CHECK_STALE"
    BLACKOUT_DATE = "BLACKOUT_DATE"
    WEEKEND_NOT_PERMITTED = "WEEKEND_NOT_PERMITTED"
    OUTSIDE_CALLING_WINDOW = "OUTSIDE_CALLING_WINDOW"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    WEEKLY_CAP_REACHED = "WEEKLY_CAP_REACHED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    NO_TEMPLATE_FOR_LEVEL = "NO_TEMPLATE_FOR_LEVEL"
    L3_TEMPLATE_NOT_APPROVED = "L3_TEMPLATE_NOT_APPROVED"
    L3_REQUIRES_PRIOR_CONTACT = "L3_REQUIRES_PRIOR_CONTACT"
    # messaging
    CHANNEL_OPTED_OUT = "CHANNEL_OPTED_OUT"
    CHANNEL_CAP_REACHED = "CHANNEL_CAP_REACHED"
    NO_CONTACT_ADDRESS = "NO_CONTACT_ADDRESS"
    SMS_TEMPLATE_NOT_REGISTERED = "SMS_TEMPLATE_NOT_REGISTERED"


# ------------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class PhoneRef:
    id: UUID
    e164: str
    priority: int
    is_valid: bool
    dnd_status: DndStatus
    dnd_checked_at: datetime | None
    number_type: str | None = None


@dataclass(frozen=True)
class AccountRef:
    id: UUID
    status: AccountStatus
    outstanding_paise: int
    due_date: datetime
    level: EscalationLevel
    level_entered_at: datetime
    attempts_at_level: int
    delivered_at_level: int
    last_contact_at: datetime | None = None


@dataclass(frozen=True)
class TemplateRef:
    key: str
    level: EscalationLevel
    language: str
    version_id: UUID
    is_approved: bool
    channel: Channel = Channel.VOICE
    # TRAI DLT registration. An SMS template without one is unsendable, and that
    # is a refusal here rather than a provider error discovered at send time.
    dlt_registered: bool = True


@dataclass(frozen=True)
class EscalationPolicy:
    """Ladder thresholds. Placeholders — see the module docstring."""

    l1_after_days: int = 7  # days past due before any automated contact
    l2_after_days_at_l1: int = 7
    l3_after_days_at_l2: int = 14
    max_attempts_per_level: int = 3
    # Below this, hold at L2 permanently. Legal action on a small balance costs
    # more than the debt. 25,000 rupees.
    l3_min_outstanding_paise: int = 2_500_000


@dataclass(frozen=True)
class DecisionContext:
    now: datetime  # timezone-aware UTC
    campaign_active: bool
    timezone: str
    window_start: time
    window_end: time
    call_on_weekends: bool
    blackout_days: frozenset[date]
    max_attempts_per_day: int
    max_attempts_per_week: int
    min_hours_between_calls: int
    consent_withdrawn: bool
    suppressed_until: datetime | None
    phones: tuple[PhoneRef, ...]
    accounts: tuple[AccountRef, ...]
    attempts_today: int  # per BUYER, counting only attempts that count against the cap
    attempts_this_week: int
    templates: tuple[TemplateRef, ...]
    policy: EscalationPolicy
    language: str = "en-IN"
    # --- messaging
    channels_enabled: tuple[Channel, ...] = (
        Channel.SMS,
        Channel.WHATSAPP,
        Channel.EMAIL,
        Channel.VOICE,
    )
    opted_out_channels: frozenset = frozenset()
    email: str | None = None
    attempts_today_by_channel: dict = field(default_factory=dict)
    max_attempts_per_day_by_channel: dict = field(default_factory=dict)


# ------------------------------------------------------------------------ output


@dataclass(frozen=True)
class Decision:
    allowed: bool
    account_id: UUID | None = None
    level: EscalationLevel | None = None
    template_version_id: UUID | None = None
    phone_id: UUID | None = None
    to_e164: str | None = None
    channel: Channel | None = None
    to_address: str | None = None
    # L3 plays only after a keypress proves a human is present. Playing a debt
    # amount to whoever picked up — a spouse, a colleague, a shared office line —
    # is a third-party disclosure problem, not a UX preference.
    requires_dtmf_ack: bool = False
    reason: BlockReason | None = None
    detail: str = ""


def _block(
    reason: BlockReason, detail: str = "", *, channel: Channel | None = None
) -> Decision:
    """A refusal, carrying the channel wherever one had already been chosen.

    `app.scheduler.dispatch` files the block in the table for the channel it
    refused, and a blocked SMS written as a Call with an empty number makes
    every block report count it as a call that never happened — so an operator
    debugging a silent messaging campaign reads a dialler failure. Refusals
    raised before any channel is in view genuinely have none, and a Call row is
    the right home for those.
    """
    return Decision(allowed=False, reason=reason, detail=detail, channel=channel)


# ------------------------------------------------------------------- level ladder

_LEVEL_RANK = {EscalationLevel.L1: 1, EscalationLevel.L2: 2, EscalationLevel.L3: 3}

_CLOSED_STATUSES = frozenset({AccountStatus.SETTLED, AccountStatus.WRITTEN_OFF})


def determine_level(
    ctx: DecisionContext, account: AccountRef
) -> tuple[EscalationLevel, bool]:
    """Return the level to contact at, and whether a human should look first.

    Never descends. A debtor who has already received L3 legal content does not
    go back to a courtesy reminder — the relationship has changed, and pretending
    otherwise reads as incompetence.
    """
    policy = ctx.policy
    days_at_level = (ctx.now - account.level_entered_at).days
    attempts_exhausted = account.attempts_at_level >= policy.max_attempts_per_level

    if account.level is EscalationLevel.L1:
        if days_at_level >= policy.l2_after_days_at_l1 and attempts_exhausted:
            return EscalationLevel.L2, False
        return EscalationLevel.L1, False

    if account.level is EscalationLevel.L2:
        time_and_attempts_met = (
            days_at_level >= policy.l3_after_days_at_l2 and attempts_exhausted
        )
        if not time_and_attempts_met:
            return EscalationLevel.L2, False

        # A balance too small to be worth a lawyer holds at L2 forever. This is a
        # deliberate resting state, not something for a human to resolve.
        if account.outstanding_paise < policy.l3_min_outstanding_paise:
            return EscalationLevel.L2, False

        # Thresholds met, but this person has never actually been reached.
        # Escalating to legal content off the back of unanswered calls is unfair
        # and evidentially weak — route it to a human instead.
        if account.delivered_at_level < 1:
            return EscalationLevel.L2, True

        return EscalationLevel.L3, False

    return EscalationLevel.L3, False


# --------------------------------------------------------------------- selection


def _select_account(ctx: DecisionContext) -> tuple[AccountRef | None, Decision | None]:
    eligible = [
        a
        for a in ctx.accounts
        if a.status is AccountStatus.OVERDUE
        and (ctx.now - a.due_date).days >= ctx.policy.l1_after_days
    ]
    if eligible:
        # Highest severity first: current level, then age, then size.
        eligible.sort(
            key=lambda a: (
                _LEVEL_RANK[a.level],
                (ctx.now - a.due_date).days,
                a.outstanding_paise,
            ),
            reverse=True,
        )
        return eligible[0], None

    if not ctx.accounts:
        return None, _block(BlockReason.ACCOUNT_NOT_OVERDUE, "buyer has no accounts")

    statuses = {a.status for a in ctx.accounts}
    # A dispute is the strongest signal present, so it wins the reason even in a
    # mixed set — it is the one a human needs to see.
    if AccountStatus.IN_DISPUTE in statuses and statuses <= (
        _CLOSED_STATUSES | {AccountStatus.IN_DISPUTE}
    ):
        return None, _block(
            BlockReason.ACCOUNT_IN_DISPUTE, "all accounts disputed or closed"
        )
    if statuses <= _CLOSED_STATUSES:
        return None, _block(
            BlockReason.ACCOUNT_CLOSED, "all accounts settled or written off"
        )
    return None, _block(
        BlockReason.ACCOUNT_NOT_OVERDUE,
        f"no account is OVERDUE by >= {ctx.policy.l1_after_days} days",
    )


def _select_phone(ctx: DecisionContext) -> tuple[PhoneRef | None, Decision | None]:
    valid = [p for p in ctx.phones if p.is_valid]
    if not valid:
        return None, _block(BlockReason.NO_VALID_PHONE, "no valid number on file")

    callable_now = [
        p
        for p in valid
        if p.dnd_status is DndStatus.CLEAR
        and p.dnd_checked_at is not None
        and (ctx.now - p.dnd_checked_at) <= DND_MAX_AGE
    ]
    if callable_now:
        # Lower `priority` first — 0 is the number to try before any other.
        callable_now.sort(key=lambda p: p.priority)
        return callable_now[0], None

    # Nothing callable. Report the most specific true cause, hardest first.
    statuses = {p.dnd_status for p in valid}
    if statuses == {DndStatus.REGISTERED}:
        return None, _block(BlockReason.DND_REGISTERED, "every valid number is on DND")
    if DndStatus.UNKNOWN in statuses:
        return None, _block(
            BlockReason.DND_UNKNOWN, "DND status unknown — fail closed until scrubbed"
        )
    return None, _block(
        BlockReason.DND_CHECK_STALE,
        f"DND scrub older than {DND_MAX_AGE.days} days",
    )


# Cheapest and least intrusive first. Most debts are recovered by a message, and
# a system that calls first burns money and goodwill.
_CHANNEL_PREFERENCE = {
    EscalationLevel.L1: (Channel.SMS, Channel.WHATSAPP, Channel.EMAIL, Channel.VOICE),
    EscalationLevel.L2: (Channel.VOICE, Channel.WHATSAPP, Channel.SMS, Channel.EMAIL),
    # L3 is voice only. Legal content is gated behind a keypress, and that
    # keypress is the only evidence a human rather than a shared inbox received it.
    EscalationLevel.L3: (Channel.VOICE,),
}

# DND is applied to WhatsApp as well as SMS and voice. TRAI's registry does not
# formally cover it, which is exactly why the conservative reading is the right
# one until counsel says otherwise.
_PHONE_CHANNELS = frozenset({Channel.VOICE, Channel.SMS, Channel.WHATSAPP})


def _templates_for(ctx: DecisionContext, level: EscalationLevel, channel: Channel):
    return [t for t in ctx.templates if t.level is level and t.channel is channel]


def _select_channel(
    ctx: DecisionContext, level: EscalationLevel, *, has_phone: bool
) -> tuple[Channel | None, Decision | None]:
    """Pick the cheapest usable rung for this level.

    A channel is usable only when it is enabled, not opted out of, has a
    template for this level, and has somewhere to send to. Skipping one that
    fails any of those is not a refusal — it is why the ladder has rungs.
    """
    considered = [c for c in _CHANNEL_PREFERENCE[level] if c in ctx.channels_enabled]

    # The first channel each cause was seen on, so the refusal can be filed in
    # the table it belongs to. On a single-channel campaign — the case an
    # operator is usually debugging — it is the only channel there was.
    saw_opt_out = saw_missing_address = saw_cap = None

    for channel in considered:
        if not _templates_for(ctx, level, channel):
            continue
        if channel in ctx.opted_out_channels:
            saw_opt_out = saw_opt_out or channel
            continue
        if channel in _PHONE_CHANNELS and not has_phone:
            saw_missing_address = saw_missing_address or channel
            continue
        if channel is Channel.EMAIL and not ctx.email:
            saw_missing_address = saw_missing_address or channel
            continue
        cap = ctx.max_attempts_per_day_by_channel.get(channel)
        if cap is not None and ctx.attempts_today_by_channel.get(channel, 0) >= cap:
            # Three SMS, one WhatsApp and a call in a day is harassment even when
            # each channel's own cap passed — hence a per-channel cap *and* the
            # shared per-buyer one below.
            saw_cap = saw_cap or channel
            continue
        return channel, None

    if saw_opt_out:
        return None, _block(
            BlockReason.CHANNEL_OPTED_OUT,
            "every usable channel was opted out of",
            channel=saw_opt_out,
        )
    if saw_cap:
        return None, _block(
            BlockReason.CHANNEL_CAP_REACHED,
            "every usable channel is at its daily cap",
            channel=saw_cap,
        )
    if saw_missing_address:
        return None, _block(
            BlockReason.NO_CONTACT_ADDRESS,
            "no address on file for any usable channel",
            channel=saw_missing_address,
        )
    return None, _block(
        BlockReason.NO_TEMPLATE_FOR_LEVEL, f"no template configured for {level.value}"
    )


def _select_template(
    ctx: DecisionContext, level: EscalationLevel, channel: Channel = Channel.VOICE
) -> tuple[TemplateRef | None, Decision | None]:
    for_level = _templates_for(ctx, level, channel)
    if not for_level:
        return None, _block(
            BlockReason.NO_TEMPLATE_FOR_LEVEL,
            f"no {channel.value} template configured for {level.value}",
            channel=channel,
        )

    exact = [t for t in for_level if t.language == ctx.language]
    candidates = exact or for_level
    # Prefer approved content where a choice exists, so the approval gate below
    # fires only when nothing approved is configured at all.
    chosen = sorted(candidates, key=lambda t: not t.is_approved)[0]

    # The engine may select approved legal content. It may never improvise it.
    if level is EscalationLevel.L3 and not chosen.is_approved:
        return None, _block(
            BlockReason.L3_TEMPLATE_NOT_APPROVED,
            f"L3 template '{chosen.key}' has no recorded approval",
            channel=channel,
        )

    # An unregistered SMS template is unsendable. Refusing here keeps it a policy
    # decision with a recorded reason, rather than a provider error at send time.
    if channel is Channel.SMS and not chosen.dlt_registered:
        return None, _block(
            BlockReason.SMS_TEMPLATE_NOT_REGISTERED,
            f"SMS template '{chosen.key}' has no TRAI DLT registration",
            channel=channel,
        )
    return chosen, None


# ---------------------------------------------------------------------- evaluate


def evaluate(ctx: DecisionContext) -> Decision:
    """Decide whether to place a call, and refuse with a reason when not.

    Gate order is deliberate: the recorded reason should be the most meaningful
    true one. A buyer who withdrew consent shows CONSENT_WITHDRAWN in the audit
    log, not OUTSIDE_CALLING_WINDOW, even when both are true at that moment.
    """
    if not ctx.campaign_active:
        return _block(BlockReason.CAMPAIGN_NOT_ACTIVE, "campaign is not ACTIVE")

    # Consent is absolute and beats every other consideration.
    if ctx.consent_withdrawn:
        return _block(BlockReason.CONSENT_WITHDRAWN, "buyer withdrew consent to contact")

    if ctx.suppressed_until is not None and ctx.now < ctx.suppressed_until:
        return _block(
            BlockReason.BUYER_SUPPRESSED,
            f"suppressed until {ctx.suppressed_until.isoformat()}",
        )

    account, blocked = _select_account(ctx)
    if blocked is not None:
        return blocked
    assert account is not None

    phone, phone_block = _select_phone(ctx)

    local = ctx.now.astimezone(ZoneInfo(ctx.timezone))

    if local.date() in ctx.blackout_days:
        return _block(
            BlockReason.BLACKOUT_DATE, f"{local.date().isoformat()} is a blackout date"
        )

    if not ctx.call_on_weekends and local.weekday() >= 5:
        return _block(BlockReason.WEEKEND_NOT_PERMITTED, f"local day is {local:%A}")

    # A window that wraps past midnight is treated as empty rather than valid.
    # Collections at 01:00 is not a configuration worth supporting, and reading a
    # reversed window as "overnight" would make it reachable by a typo.
    if ctx.window_end <= ctx.window_start or not (
        ctx.window_start <= local.time() < ctx.window_end
    ):
        return _block(
            BlockReason.OUTSIDE_CALLING_WINDOW,
            f"local time {local:%H:%M} outside "
            f"{ctx.window_start:%H:%M}-{ctx.window_end:%H:%M}",
        )

    if ctx.attempts_today >= ctx.max_attempts_per_day:
        return _block(
            BlockReason.DAILY_CAP_REACHED,
            f"{ctx.attempts_today}/{ctx.max_attempts_per_day} attempts today",
        )
    if ctx.attempts_this_week >= ctx.max_attempts_per_week:
        return _block(
            BlockReason.WEEKLY_CAP_REACHED,
            f"{ctx.attempts_this_week}/{ctx.max_attempts_per_week} attempts this week",
        )

    # Cooldown is a property of the person, not the account. Calling the same
    # debtor ten minutes later about a different invoice is exactly what this
    # prevents.
    contacts = [a.last_contact_at for a in ctx.accounts if a.last_contact_at is not None]
    if contacts:
        since = ctx.now - max(contacts)
        if since < timedelta(hours=ctx.min_hours_between_calls):
            return _block(
                BlockReason.COOLDOWN_ACTIVE,
                f"last contact {since.total_seconds() / 3600:.1f}h ago, "
                f"minimum {ctx.min_hours_between_calls}h",
            )

    level, needs_human_review = determine_level(ctx, account)
    if needs_human_review:
        return _block(
            BlockReason.L3_REQUIRES_PRIOR_CONTACT,
            "L2 thresholds met but no delivered contact at this level",
        )

    channel, blocked = _select_channel(ctx, level, has_phone=phone is not None)
    if blocked is not None:
        # A phone problem is the more meaningful reason when the ladder wanted a
        # phone channel and could not have one — DND_REGISTERED tells an operator
        # what to fix, NO_CONTACT_ADDRESS does not.
        if phone_block is not None and blocked.reason is BlockReason.NO_CONTACT_ADDRESS:
            # The phone refusal is the more useful sentence, but it was raised
            # before any channel was in view; the channel comes from the refusal
            # it replaces so the row is still filed in the right table.
            return replace(phone_block, channel=blocked.channel)
        return blocked
    assert channel is not None

    if channel in _PHONE_CHANNELS and phone_block is not None:
        return replace(phone_block, channel=channel)

    template, blocked = _select_template(ctx, level, channel)
    if blocked is not None:
        return blocked
    assert template is not None

    to_address = phone.e164 if channel in _PHONE_CHANNELS else ctx.email
    return Decision(
        allowed=True,
        account_id=account.id,
        level=level,
        template_version_id=template.version_id,
        phone_id=phone.id if channel in _PHONE_CHANNELS else None,
        to_e164=phone.e164 if channel in _PHONE_CHANNELS else None,
        channel=channel,
        to_address=to_address,
        # Only voice can carry a keypress, and only L3 needs one.
        requires_dtmf_ack=level is EscalationLevel.L3 and channel is Channel.VOICE,
        detail=f"{level.value} via {channel.value} template {template.key} to {to_address}",
    )


# ---------------------------------------------------------------------- outcomes

# Q.850 causes. Only the ones we act on differently are listed; anything else
# falls through to the status-based default, which counts.
_CAUSE_COUNTS = frozenset({16, 17, 18, 19, 21})
_CAUSE_TERMINAL_BAD_NUMBER = frozenset({1, 22})
_CAUSE_CARRIER_FAULT = frozenset({34, 38, 41})

_NOT_A_CARRIER_OUTCOME = frozenset(
    {CallStatus.SCHEDULED, CallStatus.DIALING, CallStatus.BLOCKED, CallStatus.CANCELLED}
)


def classify_attempt(status: CallStatus, hangup_cause: int | None) -> AttemptClass:
    """Whether an outcome consumes the debtor's frequency budget.

    Congestion is *our carrier* failing, not the debtor's phone ringing. Charging
    it against a per-buyer cap lets one bad carrier hour silently cancel a day of
    collections.

    Raises for statuses that never reached the carrier: a blocked or cancelled
    call is not an attempt, and asking is a bug worth hearing about rather than
    silently spending someone's cap.
    """
    if status in _NOT_A_CARRIER_OUTCOME:
        raise ValueError(f"{status.value} is not a carrier outcome")

    # The phone was picked up. Whatever cause the channel reported on teardown
    # cannot turn that into a carrier fault.
    if status in (CallStatus.ANSWERED, CallStatus.ANSWERED_MACHINE):
        return AttemptClass.COUNTS

    if hangup_cause is not None:
        if hangup_cause in _CAUSE_CARRIER_FAULT:
            return AttemptClass.CARRIER_FAULT
        if hangup_cause in _CAUSE_TERMINAL_BAD_NUMBER:
            return AttemptClass.TERMINAL_BAD_NUMBER
        if hangup_cause in _CAUSE_COUNTS:
            return AttemptClass.COUNTS

    # Includes CallStatus.UNKNOWN and a FAILED carrying no cause: there is no
    # proof the phone did not ring, so fail safe toward the debtor and spend the
    # attempt.
    return AttemptClass.COUNTS


def next_retry_delay(
    status: CallStatus, hangup_cause: int | None, attempt_number: int
) -> timedelta:
    """How long before trying this number again.

    Busy means a human is on the line right now, so it retries soonest. Carrier
    faults back off exponentially and do not consume the cap, so backing off hard
    costs the debtor nothing.
    """
    attempt = max(1, attempt_number)
    klass = classify_attempt(status, hangup_cause)

    if klass is AttemptClass.TERMINAL_BAD_NUMBER:
        return RETIRE_NUMBER_DELAY
    if klass is AttemptClass.CARRIER_FAULT:
        return min(timedelta(minutes=2**attempt), timedelta(hours=1))
    if status is CallStatus.BUSY:
        return min(timedelta(minutes=15 * attempt), timedelta(hours=1))
    if status in (CallStatus.NO_ANSWER, CallStatus.ANSWERED_MACHINE):
        return min(timedelta(hours=4 * attempt), timedelta(hours=24))
    return timedelta(hours=24)


def record_attempt(
    delivered: bool, attempts: int, delivered_count: int
) -> tuple[int, int]:
    """New `(attempts_at_level, delivered_at_level)` after one attempt.

    Reaching a human resets the attempt counter — the ladder measures *failure to
    make contact*, and a delivered call is not that.
    """
    if delivered:
        return 0, delivered_count + 1
    return attempts + 1, delivered_count
