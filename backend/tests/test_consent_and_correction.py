"""Correction and cessation.

The bar here is the same as the Policy Engine's, and for the same reason: this
is the half of the system a debtor exercises against us, so "it worked when I
tried it" is not enough. Every refusal pins its exact enum member, and the two
rules that protect a real person — an opt-out must never move money, and a
channel opt-out must never become a silence — are asserted directly rather than
inferred from a happy path.

Two of these tests are wiring tests on purpose. `ChannelOptOut` had no writer
anywhere in `app/`, which meant the engine's CHANNEL_OPTED_OUT refusal could not
fire, and `BuyerPhone.is_valid` had no writer outside a carrier hangup cause. A
test that only checks the row was written would not have noticed either gap, so
those two run the written rows back through `evaluate`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.db import admin_session, tenant_session
from app.models import (
    AccountStatus,
    AuditLog,
    Buyer,
    BuyerPhone,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    CampaignTarget,
    Channel,
    ChannelOptOut,
    CreditAccount,
    EscalationLevel,
)
from app.policy.engine import (
    AccountRef,
    BlockReason,
    DecisionContext,
    EscalationPolicy,
    PhoneRef,
    TemplateRef,
    evaluate,
)
from app.trade import consent
from app.trade.consent import (
    CORRECTABLE_FIELDS,
    ConsentError,
    ConsentRefusal,
    ConsentSource,
    correct_buyer,
    invalidate_phone,
    record_channel_optout,
    record_consent_withdrawal,
    restore_consent,
    suppress_until,
)

NOW = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST


@pytest.fixture(autouse=True)
def _clean_up_after(tenants):
    """`tenants` tears down a hand-maintained list of tables and knows nothing
    about these three.

    A left-over `channel_opt_outs` row does not fail this test — it fails the
    *next* test's teardown, on a foreign key, in a file that never mentioned
    opt-outs.
    """
    yield
    with admin_session() as s:
        for cid in (tenants.a.company_id, tenants.b.company_id):
            s.execute(delete(Call).where(Call.company_id == cid))
            # Targets reference campaigns, so they go first. The order here is
            # the foreign keys, not the order the rows were written.
            s.execute(delete(CampaignTarget).where(CampaignTarget.company_id == cid))
            s.execute(delete(Campaign).where(Campaign.company_id == cid))
            s.execute(delete(ChannelOptOut).where(ChannelOptOut.company_id == cid))


# ------------------------------------------------------------------- helpers


def buyer_of(session, tenant) -> Buyer:
    return session.get(Buyer, tenant.buyer_id)


def phone_of(session, tenant) -> BuyerPhone:
    return session.execute(
        select(BuyerPhone).where(BuyerPhone.buyer_id == tenant.buyer_id)
    ).scalar_one()


def context_for(session, tenant, *, now: datetime, templates) -> DecisionContext:
    """A decision context assembled from the rows, the way the dispatcher does.

    Everything unrelated to the gate under test is opened up — any weekday, any
    hour, no blackouts, caps far away — so a block observed here came from the
    thing the test is about.
    """
    buyer = buyer_of(session, tenant)
    account = session.get(CreditAccount, tenant.account_id)
    phones = tuple(
        PhoneRef(
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
    )
    opted_out = frozenset(
        o.channel
        for o in session.execute(
            select(ChannelOptOut).where(ChannelOptOut.buyer_id == buyer.id)
        ).scalars()
    )
    return DecisionContext(
        now=now,
        campaign_active=True,
        timezone="Asia/Kolkata",
        window_start=time(0, 0),
        window_end=time(23, 59, 59),
        call_on_weekends=True,
        blackout_days=frozenset(),
        max_attempts_per_day=10,
        max_attempts_per_week=10,
        min_hours_between_calls=0,
        consent_withdrawn=buyer.consent_withdrawn,
        suppressed_until=buyer.suppressed_until,
        phones=phones,
        accounts=(
            AccountRef(
                id=account.id,
                status=account.status,
                outstanding_paise=account.outstanding_paise,
                due_date=account.due_date,
                level=EscalationLevel.L1,
                level_entered_at=now - timedelta(days=1),
                attempts_at_level=0,
                delivered_at_level=0,
            ),
        ),
        attempts_today=0,
        attempts_this_week=0,
        templates=templates,
        policy=EscalationPolicy(),
        language=buyer.language,
        opted_out_channels=opted_out,
        email=buyer.email,
    )


def sms_template() -> tuple[TemplateRef, ...]:
    """One SMS template and nothing else, so SMS is the only usable rung."""
    return (
        TemplateRef(
            key="l1_sms",
            level=EscalationLevel.L1,
            language="en-IN",
            version_id=uuid.uuid4(),
            is_approved=True,
            channel=Channel.SMS,
            dlt_registered=True,
        ),
    )


# ------------------------------------------------------- withdrawal by any route


def test_a_withdrawal_that_arrived_by_letter_can_be_recorded(tenants):
    """The DPDP-shaped hole. Until this existed, consent could only be withdrawn
    by a debtor pressing 9 during a live call — a withdrawal posted to the
    office could not be entered at all."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        change = record_consent_withdrawal(
            s,
            buyer,
            reason="signed letter received at the registered office",
            source=ConsentSource.LETTER,
            now=NOW,
        )

        assert buyer.consent_withdrawn is True
        assert buyer.consent_withdrawn_at == NOW
        assert change.changed is True
        assert change.before["consent_withdrawn"] is False
        assert change.after["consent_withdrawn_at"] == NOW.isoformat()
        assert "letter" in change.detail


@pytest.mark.parametrize(
    "source",
    [ConsentSource.EMAIL, ConsentSource.IN_PERSON, ConsentSource.PORTAL, "letter"],
)
def test_every_route_a_withdrawal_actually_arrives_by_is_recordable(tenants, source):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=source, now=NOW
        )
        assert buyer.consent_withdrawn is True


def test_an_unrecognised_source_is_refused(tenants):
    """Who told us, and how, is the first question asked when a withdrawal is
    disputed. A free-text answer is not an answer."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            record_consent_withdrawal(
                s, buyer, reason="he said so", source="a colleague mentioned it", now=NOW
            )
        assert exc.value.refusal is ConsentRefusal.SOURCE_NOT_RECOGNISED
        assert buyer.consent_withdrawn is False


def test_a_withdrawal_without_a_reason_is_refused(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            record_consent_withdrawal(
                s, buyer, reason="   ", source=ConsentSource.EMAIL, now=NOW
            )
        assert exc.value.refusal is ConsentRefusal.REASON_REQUIRED


def test_a_second_letter_does_not_move_the_withdrawal_date(tenants):
    """The date that matters is the first one: it is the date from which any
    later contact was contact they had already refused."""
    later = NOW + timedelta(days=30)
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="first letter", source=ConsentSource.LETTER, now=NOW
        )
        change = record_consent_withdrawal(
            s, buyer, reason="second letter", source=ConsentSource.LETTER, now=later
        )
        assert buyer.consent_withdrawn_at == NOW
        assert change.changed is False


def test_a_withdrawal_stops_the_scheduler_waking_on_this_buyer(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        buyer.next_action_at = NOW + timedelta(hours=1)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.PHONE, now=NOW
        )
        assert buyer.next_action_at is None


def test_a_withdrawal_reaches_the_policy_engine(tenants):
    with tenant_session(tenants.a.company_id) as s:
        now = datetime.now(timezone.utc)
        assert evaluate(context_for(s, tenants.a, now=now, templates=sms_template())).allowed

        record_consent_withdrawal(
            s,
            buyer_of(s, tenants.a),
            reason="asked us to stop",
            source=ConsentSource.LETTER,
            now=now,
        )
        decision = evaluate(
            context_for(s, tenants.a, now=now, templates=sms_template())
        )
        assert decision.allowed is False
        assert decision.reason is BlockReason.CONSENT_WITHDRAWN


# ---------------------------------------------------- consent must not move money


def test_withdrawing_consent_does_not_touch_the_debt(tenants):
    """The one that matters most in this file.

    Withdrawing consent stops contact. If it could also write to the account,
    a debtor's opt-out would silently write off their balance — and the debtor
    would be told, later and by someone else, that they owe it after all.
    """
    with tenant_session(tenants.a.company_id) as s:
        account = s.get(CreditAccount, tenants.a.account_id)
        owed, status, due = account.outstanding_paise, account.status, account.due_date

        record_consent_withdrawal(
            s,
            buyer_of(s, tenants.a),
            reason="asked us to stop",
            source=ConsentSource.LETTER,
            now=NOW,
        )
        suppress_until(
            s,
            buyer_of(s, tenants.a),
            until=NOW + timedelta(days=14),
            reason="in hospital",
            now=NOW,
        )
        record_channel_optout(
            s,
            buyer_of(s, tenants.a),
            channel=Channel.SMS,
            source=ConsentSource.SMS_STOP,
            now=NOW,
        )

        s.expire(account)
        assert account.outstanding_paise == owed
        assert account.status is status
        assert account.due_date == due
        assert account.status is AccountStatus.OVERDUE


def test_the_consent_module_has_no_way_to_reach_the_ledger():
    """The behavioural test above passes for as long as nobody adds a line. This
    one fails the moment somebody can."""
    ledger = {
        "CreditAccount",
        "Invoice",
        "InvoiceLine",
        "Payment",
        "PaymentAllocation",
        "CreditNote",
        "recompute_invoice",
        "sync_account_from_invoice",
    }
    assert not ledger & set(vars(consent))

    source = Path(consent.__file__).read_text(encoding="utf-8")
    assert "outstanding_paise" not in source
    assert "amount_paise" not in source


def test_none_of_these_writes_its_own_audit_row(tenants):
    """The actor lives in the request. A service function that wrote the audit
    itself could only record that "the system" changed a debtor's name."""
    with admin_session() as s:
        before = s.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.LETTER, now=NOW
        )
        invalidate_phone(s, phone_of(s, tenants.a), reason="wrong number")
        correct_buyer(s, buyer, fields={"name": "Corrected Name"})

    with admin_session() as s:
        assert s.execute(select(func.count()).select_from(AuditLog)).scalar_one() == before


# ------------------------------------------------------------------- restoring


def test_consent_can_be_given_again(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.LETTER, now=NOW
        )
        change = restore_consent(
            s,
            buyer,
            reason="fresh written mandate signed at the branch",
            source=ConsentSource.IN_PERSON,
            now=NOW + timedelta(days=60),
        )

        assert buyer.consent_withdrawn is False
        # Cleared, not left standing: `consent_withdrawn=False` beside a
        # withdrawal date reads as "withdrawn" to the next person here.
        assert buyer.consent_withdrawn_at is None
        # What happened survives in what the caller is handed to audit.
        assert change.before["consent_withdrawn"] is True
        assert change.before["consent_withdrawn_at"] == NOW.isoformat()


def test_restoring_consent_does_not_enrol_an_untargeted_buyer(tenants):
    """A null wake-up on a buyer no campaign is working means nobody is chasing
    them. Inventing one here would start that by side effect."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.LETTER, now=NOW
        )
        restore_consent(
            s, buyer, reason="mandate signed", source=ConsentSource.IN_PERSON, now=NOW
        )
        assert buyer.next_action_at is None


def test_restoring_consent_resumes_a_campaign_that_was_still_running(tenants):
    """`CONSENT_RESTORE` is documented as the one act that turns contact back
    on, and it has to actually turn it back on.

    The withdrawal parked the buyer, and the scheduler claims on a non-null
    `next_action_at`; every other writer of it now refuses to touch a withdrawn
    buyer. Without this the permission restores a flag and nothing dials.
    """
    with admin_session() as s:
        campaign = Campaign(
            company_id=tenants.a.company_id,
            name="restore-resumes",
            status=CampaignStatus.ACTIVE,
        )
        s.add(campaign)
        s.flush()
        s.add(
            CampaignTarget(
                company_id=tenants.a.company_id,
                campaign_id=campaign.id,
                buyer_id=tenants.a.buyer_id,
                is_active=True,
            )
        )

    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.LETTER, now=NOW
        )
        assert buyer.next_action_at is None

        restore_consent(
            s, buyer, reason="mandate signed", source=ConsentSource.IN_PERSON, now=NOW
        )
        assert buyer.next_action_at == NOW


def test_restoring_consent_without_a_reason_is_refused(tenants):
    """Restoring is the direction that needs the evidence, not the one that
    stops contact."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_consent_withdrawal(
            s, buyer, reason="asked us to stop", source=ConsentSource.LETTER, now=NOW
        )
        with pytest.raises(ConsentError) as exc:
            restore_consent(s, buyer, reason="", source=ConsentSource.PORTAL, now=NOW)
        assert exc.value.refusal is ConsentRefusal.REASON_REQUIRED
        assert buyer.consent_withdrawn is True


# ----------------------------------------------------------------- suppression


def test_a_temporary_hold_blocks_contact_until_it_expires(tenants):
    with tenant_session(tenants.a.company_id) as s:
        now = datetime.now(timezone.utc)
        # Three days rather than a fortnight so the fixture's DND scrub is still
        # inside DND_MAX_AGE when the hold lifts — otherwise the second half of
        # this test would be asserting a stale-scrub refusal instead.
        until = now + timedelta(days=3)
        buyer = buyer_of(s, tenants.a)
        change = suppress_until(
            s, buyer, until=until, reason="bereavement, family asked for time", now=now
        )
        assert buyer.suppressed_until == until
        assert change.before["suppressed_until"] is None

        decision = evaluate(context_for(s, tenants.a, now=now, templates=sms_template()))
        assert decision.reason is BlockReason.BUYER_SUPPRESSED

        # Temporary means temporary: nothing has to lift the hold for contact to
        # resume once it has run out.
        after = context_for(
            s, tenants.a, now=until + timedelta(minutes=1), templates=sms_template()
        )
        assert evaluate(after).allowed is True


def test_a_shorter_hold_never_cancels_a_longer_one(tenants):
    """Two people work the same file. The one recording Friday's promise to pay
    must not resume calling in the middle of the fortnight the other recorded."""
    fortnight = NOW + timedelta(days=14)
    friday = NOW + timedelta(days=2)
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        suppress_until(s, buyer, until=fortnight, reason="hospitalised", now=NOW)
        change = suppress_until(
            s, buyer, until=friday, reason="promised to pay on Friday", now=NOW
        )
        assert buyer.suppressed_until == fortnight
        assert change.after["suppressed_until"] == fortnight.isoformat()
        assert friday.isoformat() in change.detail


def test_a_hold_that_has_already_expired_is_refused(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            suppress_until(
                s, buyer, until=NOW - timedelta(days=1), reason="backdated", now=NOW
            )
        assert exc.value.refusal is ConsentRefusal.SUPPRESSION_NOT_IN_FUTURE
        assert buyer.suppressed_until is None


def test_a_hold_with_no_timezone_is_refused(tenants):
    """Naive here is five and a half hours of contact in one direction or the
    other, and no way to tell which was meant."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            suppress_until(
                s, buyer, until=datetime(2026, 3, 25, 9, 0), reason="hold", now=NOW
            )
        assert exc.value.refusal is ConsentRefusal.NAIVE_TIMESTAMP


def test_a_hold_pushes_a_pending_wake_up_past_it(tenants):
    until = NOW + timedelta(days=14)
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        buyer.next_action_at = NOW + timedelta(hours=4)
        suppress_until(s, buyer, until=until, reason="hospitalised", now=NOW)
        assert buyer.next_action_at == until


def test_a_hold_never_wakes_a_buyer_a_human_has_parked(tenants):
    """`next_action_at is None` is how this codebase says "nothing further is
    due" — the campaign sweep reads it as exhausted. A hold must not undo that."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        buyer.next_action_at = None
        suppress_until(
            s, buyer, until=NOW + timedelta(days=14), reason="hospitalised", now=NOW
        )
        assert buyer.next_action_at is None


# -------------------------------------------------------------- channel opt-out


def test_stop_on_sms_suppresses_sms_only(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        change = record_channel_optout(
            s, buyer, channel=Channel.SMS, source=ConsentSource.SMS_STOP, now=NOW
        )

        rows = list(
            s.execute(
                select(ChannelOptOut).where(ChannelOptOut.buyer_id == buyer.id)
            ).scalars()
        )
        assert [r.channel for r in rows] == [Channel.SMS]
        assert rows[0].source == "sms_stop"
        assert rows[0].opted_out_at == NOW
        assert change.changed is True


def test_a_channel_opt_out_is_not_a_withdrawal_of_consent(tenants):
    """Conflating the two silences every channel over one text — and lets a real
    withdrawal be filed as a channel preference while the calls keep coming."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        record_channel_optout(
            s, buyer, channel=Channel.SMS, source=ConsentSource.SMS_STOP, now=NOW
        )
        assert buyer.consent_withdrawn is False
        assert buyer.consent_withdrawn_at is None
        assert buyer.suppressed_until is None


def test_a_repeated_stop_does_not_re_record_the_opt_out(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        first = record_channel_optout(
            s, buyer, channel=Channel.SMS, source=ConsentSource.SMS_STOP, now=NOW
        )
        again = record_channel_optout(
            s,
            buyer,
            channel=Channel.SMS,
            source=ConsentSource.SMS_STOP,
            now=NOW + timedelta(days=7),
        )
        assert again.entity_id == first.entity_id
        assert again.changed is False
        # The first refusal is the one every later message was sent in defiance of.
        assert again.after["opted_out_at"] == NOW.isoformat()


def test_the_opt_out_makes_the_engines_refusal_reachable(tenants):
    """`ChannelOptOut` was constructed nowhere in `app/`, so CHANNEL_OPTED_OUT
    was a refusal the engine could never actually return."""
    with tenant_session(tenants.a.company_id) as s:
        now = datetime.now(timezone.utc)
        allowed = evaluate(context_for(s, tenants.a, now=now, templates=sms_template()))
        assert allowed.allowed is True
        assert allowed.channel is Channel.SMS

        record_channel_optout(
            s,
            buyer_of(s, tenants.a),
            channel=Channel.SMS,
            source=ConsentSource.SMS_STOP,
            now=now,
        )
        decision = evaluate(context_for(s, tenants.a, now=now, templates=sms_template()))
        assert decision.allowed is False
        assert decision.reason is BlockReason.CHANNEL_OPTED_OUT


def test_an_unknown_channel_is_refused(tenants):
    with tenant_session(tenants.a.company_id) as s:
        with pytest.raises(ConsentError) as exc:
            record_channel_optout(
                s,
                buyer_of(s, tenants.a),
                channel="TELEX",
                source=ConsentSource.LETTER,
                now=NOW,
            )
        assert exc.value.refusal is ConsentRefusal.CHANNEL_NOT_RECOGNISED


def test_an_opt_out_belongs_to_one_tenant(tenants):
    """Written through the tenant path, not an admin session — which is also
    what proves the row satisfies the RLS check on insert."""
    with tenant_session(tenants.a.company_id) as s:
        record_channel_optout(
            s,
            buyer_of(s, tenants.a),
            channel=Channel.SMS,
            source=ConsentSource.SMS_STOP,
            now=NOW,
        )
    with tenant_session(tenants.b.company_id) as s:
        assert s.execute(select(ChannelOptOut)).scalars().all() == []


# ------------------------------------------------------------- retiring a number


def test_a_wrong_number_can_be_retired(tenants):
    with tenant_session(tenants.a.company_id) as s:
        phone = phone_of(s, tenants.a)
        change = invalidate_phone(
            s, phone, reason="answered by an unrelated business three times"
        )
        assert phone.is_valid is False
        assert change.before["is_valid"] is True
        assert change.after["e164"] == phone.e164
        assert "unrelated business" in change.detail


def test_retiring_a_number_leaves_the_calls_made_to_it_visible(tenants):
    """Flagged, never deleted: "who did we ring on 12 March, and on what number"
    has to stay answerable after the number is retired."""
    with admin_session() as s:
        campaign = Campaign(
            company_id=tenants.a.company_id, name="retire", status=CampaignStatus.ACTIVE
        )
        s.add(campaign)
        s.flush()
        phone = phone_of(s, tenants.a)
        s.add(
            Call(
                company_id=tenants.a.company_id,
                campaign_id=campaign.id,
                buyer_id=tenants.a.buyer_id,
                account_id=tenants.a.account_id,
                phone_id=phone.id,
                level=EscalationLevel.L1,
                to_e164=phone.e164,
                scheduled_at=NOW,
                status=CallStatus.NO_ANSWER,
                idempotency_key=uuid.uuid4().hex,
            )
        )

    with tenant_session(tenants.a.company_id) as s:
        phone = phone_of(s, tenants.a)
        invalidate_phone(s, phone, reason="wrong number")

    with tenant_session(tenants.a.company_id) as s:
        call = s.execute(select(Call)).scalars().one()
        assert call.phone_id is not None
        retired = s.get(BuyerPhone, call.phone_id)
        assert retired is not None
        assert retired.is_valid is False
        assert retired.e164 == call.to_e164


def test_a_retired_number_is_not_dialled_again(tenants):
    """`is_valid` had no writer outside a carrier hangup cause, so the dialler
    spent the debtor's daily cap on a wrong number until a human noticed."""
    with tenant_session(tenants.a.company_id) as s:
        now = datetime.now(timezone.utc)
        assert evaluate(context_for(s, tenants.a, now=now, templates=sms_template())).allowed

        invalidate_phone(s, phone_of(s, tenants.a), reason="wrong number")
        decision = evaluate(context_for(s, tenants.a, now=now, templates=sms_template()))
        assert decision.allowed is False
        assert decision.reason is BlockReason.NO_VALID_PHONE


def test_retiring_a_number_without_a_reason_is_refused(tenants):
    with tenant_session(tenants.a.company_id) as s:
        phone = phone_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            invalidate_phone(s, phone, reason=" ")
        assert exc.value.refusal is ConsentRefusal.REASON_REQUIRED
        assert phone.is_valid is True


# ------------------------------------------------------------------- correction


def test_a_misspelled_name_can_be_corrected(tenants):
    """That name is what a legal notice says."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        was = buyer.name
        before, after = correct_buyer(
            s, buyer, fields={"name": "  Kumar Traders (Madurai)  "}
        )
        assert buyer.name == "Kumar Traders (Madurai)"
        assert before == {"name": was}
        assert after == {"name": "Kumar Traders (Madurai)"}


def test_a_correction_hands_back_what_it_replaced(tenants):
    """The audit matters more than the edit: the answer to "who was the notice
    addressed to before" has to survive the correction."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        buyer.email = "wrong@example.test"
        s.flush()
        before, after = correct_buyer(
            s, buyer, fields={"email": "accounts@kumartraders.test", "language": "ta-IN"}
        )
        assert before == {"email": "wrong@example.test", "language": "en-IN"}
        assert after == {"email": "accounts@kumartraders.test", "language": "ta-IN"}


def test_resubmitting_the_same_values_records_nothing(tenants):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        assert correct_buyer(s, buyer, fields={"name": buyer.name}) == ({}, {})


def test_only_whitelisted_fields_are_correctable(tenants):
    assert CORRECTABLE_FIELDS == {"name", "email", "language"}


@pytest.mark.parametrize(
    "field, value",
    [
        ("outstanding_paise", 0),
        ("consent_withdrawn", True),
        ("company_id", uuid.uuid4()),
        ("gstin", "27AAPFU0939F1ZV"),
        ("id", uuid.uuid4()),
    ],
)
def test_a_correction_cannot_reach_past_the_whitelist(tenants, field, value):
    """Money, consent, tenancy and the declared identifiers each have their own
    path with their own evidence. Reaching them through a contact-details edit
    is how one of them changes without anybody deciding it should."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            correct_buyer(s, buyer, fields={field: value})
        assert exc.value.refusal is ConsentRefusal.FIELD_NOT_CORRECTABLE


def test_one_bad_value_rejects_the_whole_correction(tenants):
    """A half-applied correction leaves the caller holding an exception and the
    debtor holding a new name nobody recorded.

    The good field is the one that validates *first*. Pairing a good name with a
    bad email proves nothing — fields are validated in sorted order, so `email`
    would raise before `name` was ever reached, and a loop that validated and
    wrote each field in turn would pass. This pairing fails the moment
    validation and writing are interleaved.
    """
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        was = buyer.email
        with pytest.raises(ConsentError) as exc:
            correct_buyer(
                s, buyer, fields={"email": "accounts@kumartraders.test", "name": ""}
            )
        assert exc.value.refusal is ConsentRefusal.VALUE_REJECTED
        assert buyer.email == was


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_a_buyer_cannot_be_corrected_into_having_no_name(tenants, bad):
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            correct_buyer(s, buyer, fields={"name": bad})
        assert exc.value.refusal is ConsentRefusal.VALUE_REJECTED


def test_an_address_can_be_cleared_but_not_mangled(tenants):
    """No address is better than one that delivers a demand to a stranger."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        buyer.email = "accounts@kumartraders.test"
        s.flush()

        correct_buyer(s, buyer, fields={"email": None})
        assert buyer.email is None

        with pytest.raises(ConsentError) as exc:
            correct_buyer(s, buyer, fields={"email": "accounts at kumartraders.test"})
        assert exc.value.refusal is ConsentRefusal.VALUE_REJECTED


def test_a_language_typo_is_refused(tenants):
    """It does not fail closed downstream: with no template in the buyer's
    language the engine falls back to any template at that level, so `hindi`
    instead of `hi-IN` plays the wrong language at the debtor rather than
    stopping."""
    with tenant_session(tenants.a.company_id) as s:
        buyer = buyer_of(s, tenants.a)
        with pytest.raises(ConsentError) as exc:
            correct_buyer(s, buyer, fields={"language": "hindi"})
        assert exc.value.refusal is ConsentRefusal.VALUE_REJECTED
        assert buyer.language == "en-IN"
