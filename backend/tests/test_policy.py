"""Policy Engine tests.

The engine is the piece a regulator would audit, so the bar here is higher than
"the happy path works": every refusal reason has a test that pins the exact enum
value, and the two rules that protect a real person from a wrong legal call —
L3 needs prior delivered contact, and L3 content must be approved — are asserted
directly rather than inferred.

Time is injected, never read from the system clock. `NOW` is a Wednesday at 11:30
IST: a weekday, inside the calling window, so any block a test observes comes
from the thing that test is about.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import (
    AccountStatus,
    AttemptClass,
    CallStatus,
    Channel,
    DndStatus,
    EscalationLevel,
)
from app.policy import engine
from app.policy.engine import (
    AccountRef,
    BlockReason,
    DecisionContext,
    EscalationPolicy,
    PhoneRef,
    TemplateRef,
    classify_attempt,
    determine_level,
    evaluate,
    next_retry_delay,
    record_attempt,
)

NOW = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST
BEFORE_WINDOW = datetime(2026, 3, 11, 3, 0, tzinfo=timezone.utc)  # Wed 08:30 IST
SATURDAY = datetime(2026, 3, 14, 6, 0, tzinfo=timezone.utc)  # Sat 11:30 IST


# ------------------------------------------------------------------- builders


def phone(
    *,
    dnd=DndStatus.CLEAR,
    checked_days_ago: float | None = 1,
    valid: bool = True,
    priority: int = 0,
    e164: str = "+919000000001",
    number_type: str = "mobile",
) -> PhoneRef:
    checked = None if checked_days_ago is None else NOW - timedelta(days=checked_days_ago)
    return PhoneRef(
        id=uuid4(),
        e164=e164,
        priority=priority,
        is_valid=valid,
        dnd_status=dnd,
        dnd_checked_at=checked,
        number_type=number_type,
    )


def account(
    *,
    status=AccountStatus.OVERDUE,
    dpd: int = 30,
    level=EscalationLevel.L1,
    days_at_level: int = 1,
    attempts: int = 0,
    delivered: int = 0,
    outstanding: int = 5_000_000,
    last_contact_at: datetime | None = None,
    now: datetime = NOW,
) -> AccountRef:
    return AccountRef(
        id=uuid4(),
        status=status,
        outstanding_paise=outstanding,
        due_date=now - timedelta(days=dpd),
        level=level,
        level_entered_at=now - timedelta(days=days_at_level),
        attempts_at_level=attempts,
        delivered_at_level=delivered,
        last_contact_at=last_contact_at,
    )


def l2_ready(*, delivered: int = 1, outstanding: int = 5_000_000) -> AccountRef:
    """An L2 account that has met every threshold for advancing to L3."""
    return account(
        level=EscalationLevel.L2,
        days_at_level=20,
        attempts=3,
        delivered=delivered,
        outstanding=outstanding,
        last_contact_at=NOW - timedelta(days=5),
    )


def template(
    level=EscalationLevel.L1,
    *,
    approved: bool = True,
    language: str = "en-IN",
    channel=Channel.VOICE,
    dlt_registered: bool = True,
):
    return TemplateRef(
        key=f"{level.value.lower()}_{channel.value.lower()}",
        level=level,
        language=language,
        version_id=uuid4(),
        is_approved=approved,
        channel=channel,
        dlt_registered=dlt_registered,
    )


ALL_TEMPLATES = (
    template(EscalationLevel.L1),
    template(EscalationLevel.L2),
    template(EscalationLevel.L3),
)


def make_ctx(**over) -> DecisionContext:
    now = over.pop("now", NOW)
    base = dict(
        now=now,
        campaign_active=True,
        timezone="Asia/Kolkata",
        window_start=time(10, 0),
        window_end=time(19, 0),
        call_on_weekends=False,
        blackout_days=frozenset(),
        max_attempts_per_day=1,
        max_attempts_per_week=3,
        min_hours_between_calls=24,
        consent_withdrawn=False,
        suppressed_until=None,
        phones=(phone(),),
        accounts=(account(now=now),),
        attempts_today=0,
        attempts_this_week=0,
        templates=ALL_TEMPLATES,
        policy=EscalationPolicy(),
        language="en-IN",
    )
    base.update(over)
    return DecisionContext(**base)


# ------------------------------------------------------------------ happy path


def test_l1_prefers_the_cheapest_channel_with_a_template():
    """Most debts are recovered by a message; a system that calls first burns
    money and goodwill."""
    d = evaluate(
        make_ctx(
            templates=(
                template(EscalationLevel.L1, channel=Channel.SMS),
                template(EscalationLevel.L1, channel=Channel.VOICE),
            )
        )
    )
    assert d.allowed is True
    assert d.channel is Channel.SMS
    assert d.requires_dtmf_ack is False


def test_l3_is_voice_only_and_always_gated():
    d = evaluate(
        make_ctx(
            accounts=(l2_ready(delivered=1),),
            templates=(
                template(EscalationLevel.L3, channel=Channel.SMS),
                template(EscalationLevel.L3, channel=Channel.VOICE),
            ),
        )
    )
    assert d.channel is Channel.VOICE
    assert d.requires_dtmf_ack is True


def test_email_is_used_when_no_phone_channel_has_a_template():
    d = evaluate(
        make_ctx(
            templates=(template(EscalationLevel.L1, channel=Channel.EMAIL),),
            email="buyer@example.test",
        )
    )
    assert d.allowed is True
    assert d.channel is Channel.EMAIL
    assert d.to_address == "buyer@example.test"
    assert d.phone_id is None


def test_dnd_still_blocks_a_phone_channel_even_when_email_exists():
    """A scrubbed number does not become callable because an inbox exists."""
    d = evaluate(
        make_ctx(
            phones=(phone(dnd=DndStatus.REGISTERED),),
            templates=(template(EscalationLevel.L1, channel=Channel.SMS),),
            email="buyer@example.test",
        )
    )
    assert d.allowed is False
    assert d.reason is BlockReason.DND_REGISTERED


def test_default_context_is_allowed_at_l1():
    d = evaluate(make_ctx())
    assert d.allowed is True
    assert d.level is EscalationLevel.L1
    assert d.reason is None
    assert d.requires_dtmf_ack is False
    assert d.to_e164 == "+919000000001"


# --------------------------------------------------- one case per BlockReason

BLOCK_CASES = [
    (BlockReason.CAMPAIGN_NOT_ACTIVE, lambda: make_ctx(campaign_active=False)),
    (BlockReason.CONSENT_WITHDRAWN, lambda: make_ctx(consent_withdrawn=True)),
    (
        BlockReason.BUYER_SUPPRESSED,
        lambda: make_ctx(suppressed_until=NOW + timedelta(days=3)),
    ),
    (
        BlockReason.ACCOUNT_IN_DISPUTE,
        lambda: make_ctx(accounts=(account(status=AccountStatus.IN_DISPUTE),)),
    ),
    (
        BlockReason.ACCOUNT_CLOSED,
        lambda: make_ctx(
            accounts=(
                account(status=AccountStatus.SETTLED),
                account(status=AccountStatus.WRITTEN_OFF),
            )
        ),
    ),
    (
        BlockReason.ACCOUNT_NOT_OVERDUE,
        lambda: make_ctx(accounts=(account(status=AccountStatus.CURRENT, dpd=0),)),
    ),
    (BlockReason.NO_VALID_PHONE, lambda: make_ctx(phones=(phone(valid=False),))),
    (
        BlockReason.DND_REGISTERED,
        lambda: make_ctx(phones=(phone(dnd=DndStatus.REGISTERED),)),
    ),
    (BlockReason.DND_UNKNOWN, lambda: make_ctx(phones=(phone(dnd=DndStatus.UNKNOWN),))),
    (BlockReason.DND_CHECK_STALE, lambda: make_ctx(phones=(phone(checked_days_ago=8),))),
    (
        BlockReason.BLACKOUT_DATE,
        lambda: make_ctx(blackout_days=frozenset({NOW.date()})),
    ),
    (BlockReason.WEEKEND_NOT_PERMITTED, lambda: make_ctx(now=SATURDAY)),
    (BlockReason.OUTSIDE_CALLING_WINDOW, lambda: make_ctx(now=BEFORE_WINDOW)),
    (
        BlockReason.DAILY_CAP_REACHED,
        lambda: make_ctx(max_attempts_per_day=1, attempts_today=1),
    ),
    (
        BlockReason.WEEKLY_CAP_REACHED,
        lambda: make_ctx(
            max_attempts_per_day=5, attempts_today=0, max_attempts_per_week=3,
            attempts_this_week=3,
        ),
    ),
    (
        BlockReason.COOLDOWN_ACTIVE,
        lambda: make_ctx(
            min_hours_between_calls=24,
            accounts=(account(last_contact_at=NOW - timedelta(hours=2)),),
        ),
    ),
    (BlockReason.NO_TEMPLATE_FOR_LEVEL, lambda: make_ctx(templates=())),
    (
        BlockReason.L3_TEMPLATE_NOT_APPROVED,
        lambda: make_ctx(
            accounts=(l2_ready(delivered=1),),
            templates=(
                template(EscalationLevel.L2),
                template(EscalationLevel.L3, approved=False),
            ),
        ),
    ),
    (
        BlockReason.L3_REQUIRES_PRIOR_CONTACT,
        lambda: make_ctx(accounts=(l2_ready(delivered=0),)),
    ),
    (
        BlockReason.CHANNEL_OPTED_OUT,
        lambda: make_ctx(
            templates=(template(EscalationLevel.L1, channel=Channel.SMS),),
            opted_out_channels=frozenset({Channel.SMS}),
        ),
    ),
    (
        BlockReason.CHANNEL_CAP_REACHED,
        lambda: make_ctx(
            templates=(template(EscalationLevel.L1, channel=Channel.SMS),),
            max_attempts_per_day_by_channel={Channel.SMS: 1},
            attempts_today_by_channel={Channel.SMS: 1},
        ),
    ),
    (
        BlockReason.NO_CONTACT_ADDRESS,
        lambda: make_ctx(
            templates=(template(EscalationLevel.L1, channel=Channel.EMAIL),),
            email=None,
        ),
    ),
    (
        BlockReason.SMS_TEMPLATE_NOT_REGISTERED,
        lambda: make_ctx(
            templates=(
                template(EscalationLevel.L1, channel=Channel.SMS, dlt_registered=False),
            )
        ),
    ),
]


@pytest.mark.parametrize(
    "reason,build", BLOCK_CASES, ids=[r.value for r, _ in BLOCK_CASES]
)
def test_block_reason(reason, build):
    d = evaluate(build())
    assert d.allowed is False
    assert d.reason is reason
    assert d.detail, "a refusal must explain itself"


def test_every_block_reason_has_a_test():
    """The enum is closed; a new reason without a test should fail the suite."""
    assert {r for r, _ in BLOCK_CASES} == set(BlockReason)


# ----------------------------------------------------------- gate precedence


def test_consent_beats_calling_window():
    """Both are true; the audit log should show the one that actually matters."""
    d = evaluate(make_ctx(consent_withdrawn=True, now=BEFORE_WINDOW))
    assert d.reason is BlockReason.CONSENT_WITHDRAWN


# ---------------------------------------------------------- account selection


def test_account_selection_picks_highest_severity():
    older_bigger_l1 = account(dpd=90, outstanding=9_999_999, level=EscalationLevel.L1)
    target_l2 = account(dpd=40, outstanding=900_000, level=EscalationLevel.L2)
    accounts = (
        account(dpd=10, outstanding=100_000),
        account(status=AccountStatus.IN_DISPUTE),
        target_l2,
        account(status=AccountStatus.SETTLED),
        older_bigger_l1,
    )
    d = evaluate(make_ctx(accounts=accounts))
    assert d.allowed is True
    # Level outranks both age and size — an L2 account is further down the ladder.
    assert d.account_id == target_l2.id
    assert d.level is EscalationLevel.L2


# ------------------------------------------------------------ phone selection


def test_skips_dnd_mobile_and_picks_clear_landline():
    phones = (
        phone(dnd=DndStatus.REGISTERED, priority=0, e164="+919111111111"),
        phone(dnd=DndStatus.CLEAR, priority=1, e164="+912222222222", number_type="fixed_line"),
    )
    d = evaluate(make_ctx(phones=phones))
    assert d.allowed is True
    assert d.to_e164 == "+912222222222"


def test_skips_dnd_landline_and_picks_clear_mobile():
    """The inverse of the case above — the rule is about the number, not its type."""
    phones = (
        phone(
            dnd=DndStatus.REGISTERED,
            priority=0,
            e164="+912222222222",
            number_type="fixed_line",
        ),
        phone(dnd=DndStatus.CLEAR, priority=1, e164="+919111111111"),
    )
    d = evaluate(make_ctx(phones=phones))
    assert d.allowed is True
    assert d.to_e164 == "+919111111111"


@pytest.mark.parametrize(
    "age_days,expected_allowed", [(6, True), (8, False)]
)
def test_dnd_scrub_staleness_boundary(age_days, expected_allowed):
    d = evaluate(make_ctx(phones=(phone(checked_days_ago=age_days),)))
    assert d.allowed is expected_allowed
    if not expected_allowed:
        assert d.reason is BlockReason.DND_CHECK_STALE


def test_clear_status_without_a_scrub_timestamp_blocks():
    """CLEAR with no recorded check is an unverified claim. Fail closed."""
    d = evaluate(make_ctx(phones=(phone(checked_days_ago=None),)))
    assert d.allowed is False
    assert d.reason is BlockReason.DND_CHECK_STALE


# -------------------------------------------------------------------- ladder


def test_ladder_never_descends():
    ctx = make_ctx()
    fresh_l3 = account(level=EscalationLevel.L3, days_at_level=0, attempts=0)
    level, review = determine_level(ctx, fresh_l3)
    assert level is EscalationLevel.L3
    assert review is False


def test_l1_advances_to_l2_only_when_time_and_attempts_are_both_met():
    ctx = make_ctx()
    assert determine_level(ctx, account(days_at_level=30, attempts=0))[0] is EscalationLevel.L1
    assert determine_level(ctx, account(days_at_level=1, attempts=5))[0] is EscalationLevel.L1
    assert determine_level(ctx, account(days_at_level=30, attempts=3))[0] is EscalationLevel.L2


def test_l3_blocked_without_delivered_contact_and_allowed_with_one():
    blocked = evaluate(make_ctx(accounts=(l2_ready(delivered=0),)))
    assert blocked.allowed is False
    assert blocked.reason is BlockReason.L3_REQUIRES_PRIOR_CONTACT

    allowed = evaluate(make_ctx(accounts=(l2_ready(delivered=1),)))
    assert allowed.allowed is True
    assert allowed.level is EscalationLevel.L3


def test_l3_decision_requires_dtmf_ack():
    d = evaluate(make_ctx(accounts=(l2_ready(delivered=1),)))
    assert d.level is EscalationLevel.L3
    assert d.requires_dtmf_ack is True


def test_small_balance_never_reaches_l3():
    """Legal action on a small balance costs more than the debt — hold at L2."""
    tiny = account(
        level=EscalationLevel.L2,
        days_at_level=3650,
        attempts=99,
        delivered=5,
        outstanding=1_000,
        last_contact_at=NOW - timedelta(days=30),
    )
    d = evaluate(make_ctx(accounts=(tiny,)))
    assert d.allowed is True
    assert d.level is EscalationLevel.L2
    assert d.requires_dtmf_ack is False


def test_unapproved_l3_template_is_refused():
    """The single most important assertion in this suite.

    The engine may select legal content a named human approved. It may never
    improvise it.
    """
    d = evaluate(
        make_ctx(
            accounts=(l2_ready(delivered=1),),
            templates=(
                template(EscalationLevel.L2),
                template(EscalationLevel.L3, approved=False),
            ),
        )
    )
    assert d.allowed is False
    assert d.reason is BlockReason.L3_TEMPLATE_NOT_APPROVED
    assert d.template_version_id is None


# ------------------------------------------------------------------ outcomes


@pytest.mark.parametrize(
    "status,cause,expected",
    [
        (CallStatus.FAILED, 34, AttemptClass.CARRIER_FAULT),
        (CallStatus.FAILED, 38, AttemptClass.CARRIER_FAULT),
        (CallStatus.FAILED, 41, AttemptClass.CARRIER_FAULT),
        (CallStatus.BUSY, 17, AttemptClass.COUNTS),
        (CallStatus.NO_ANSWER, 19, AttemptClass.COUNTS),
        (CallStatus.FAILED, 1, AttemptClass.TERMINAL_BAD_NUMBER),
        (CallStatus.FAILED, 22, AttemptClass.TERMINAL_BAD_NUMBER),
        (CallStatus.ANSWERED, 16, AttemptClass.COUNTS),
        (CallStatus.ANSWERED_MACHINE, 16, AttemptClass.COUNTS),
        # No proof the phone did not ring: fail safe toward the debtor.
        (CallStatus.UNKNOWN, None, AttemptClass.COUNTS),
        (CallStatus.FAILED, None, AttemptClass.COUNTS),
    ],
)
def test_classify_attempt(status, cause, expected):
    assert classify_attempt(status, cause) is expected


def test_an_answered_call_counts_whatever_the_teardown_cause_says():
    assert classify_attempt(CallStatus.ANSWERED, 34) is AttemptClass.COUNTS


@pytest.mark.parametrize(
    "status", [CallStatus.BLOCKED, CallStatus.CANCELLED, CallStatus.SCHEDULED]
)
def test_non_carrier_outcomes_are_not_attempts(status):
    with pytest.raises(ValueError):
        classify_attempt(status, None)


def test_busy_retries_sooner_than_no_answer():
    assert next_retry_delay(CallStatus.BUSY, 17, 1) < next_retry_delay(
        CallStatus.NO_ANSWER, 19, 1
    )


def test_carrier_fault_backs_off_and_bad_number_is_retired():
    first = next_retry_delay(CallStatus.FAILED, 34, 1)
    third = next_retry_delay(CallStatus.FAILED, 34, 3)
    assert third > first
    assert next_retry_delay(CallStatus.FAILED, 1, 1) == engine.RETIRE_NUMBER_DELAY


def test_record_attempt_resets_on_delivery():
    assert record_attempt(delivered=True, attempts=2, delivered_count=0) == (0, 1)
    assert record_attempt(delivered=False, attempts=2, delivered_count=0) == (3, 0)


# --------------------------------------------------------------------- purity


def test_engine_module_is_pure():
    """Purity degrades silently the first time someone adds a convenience query."""
    src = Path(engine.__file__).read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "app.db", "httpx", "boto3", "redis"):
        assert forbidden not in src, f"engine.py must not reference {forbidden}"
