"""Payment behaviour, promises, and the human review queue.

Three primitives that the rest of the system already reads and, until now, got
None from. The tests that carry the weight here are
`test_a_slowing_payer_shows_a_positive_trend` — the earliest reliable distress
signal, and worthless if it lags — and
`test_the_ladder_refusing_to_escalate_puts_the_account_in_front_of_a_person`,
which is the ladder's designed escape hatch. Without a producer for
`needs_human_review` that hatch opens onto nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.db import admin_session, tenant_session
from app.intelligence.behaviour import (
    RollupRefusal,
    RollupRefused,
    Settlement,
    days_to_pay_trend,
    part_payment_rate,
    rollup_payment_behaviour,
)
from app.intelligence.promises import (
    PROMISE_TIMEZONE,
    DEFAULT_GRACE_DAYS,
    MAX_PROMISE_HORIZON_DAYS,
    PromiseRefusal,
    PromiseRefused,
    PromiseStatus,
    chase_resumes_at,
    record_promise,
    resolve_due_promises,
    settle_promise,
)
from app.models import (
    AccountStatus,
    AudioAsset,
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
    DndStatus,
    EscalationLevel,
    EscalationState,
    Invoice,
    Message,
    MessageStatus,
    MessageTemplate,
    Payment,
    PaymentAllocation,
    PaymentBehaviour,
    Promise,
    TemplateVersion,
)
from app.policy import BlockReason
from app.scheduler.dispatch import dispatch_buyer
from app.storage.local import LocalStorage
from app.telephony.staging import LocalStager
from app.trade import accounts
from app.trade.allocation import apply_payment
from app.tts.local import SilentTtsBackend

AS_OF = date(2026, 7, 1)
NOW = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST, in-window


# ------------------------------------------------------------------ pure maths


def settlement(issue: str, settled: str, *, payments: int = 1) -> Settlement:
    return Settlement(
        issue_date=date.fromisoformat(issue),
        settled_on=date.fromisoformat(settled),
        payment_count=payments,
    )


def test_days_to_pay_runs_from_issue_not_from_the_due_date():
    """A net-30 invoice paid on time is 30 days, which `scoring` reads as
    unremarkable. Measured from the due date it would read as 0."""
    assert settlement("2026-01-01", "2026-01-31").days_to_pay == 30


def test_a_slowing_payer_shows_a_positive_trend():
    """30, 30, 60, 74 days: the mean still looks survivable, the trend does not."""
    slowing = [
        settlement("2026-01-01", "2026-01-31"),
        settlement("2026-02-01", "2026-03-03"),
        settlement("2026-03-01", "2026-04-30"),
        settlement("2026-04-01", "2026-06-14"),
    ]
    assert days_to_pay_trend(slowing) == 37.0


def test_a_steady_payer_shows_no_drift():
    steady = [
        settlement("2026-01-01", "2026-01-31"),
        settlement("2026-02-01", "2026-03-03"),
        settlement("2026-03-01", "2026-03-31"),
        settlement("2026-04-01", "2026-05-01"),
    ]
    assert days_to_pay_trend(steady) == 0.0


def test_three_settlements_are_not_a_trend():
    """Refusing to guess is the whole point: a slope through three points would
    be read as evidence."""
    assert days_to_pay_trend([settlement("2026-01-01", "2026-01-31")] * 3) is None


def test_part_payment_rate_counts_settlements_that_took_more_than_one_payment():
    mixed = [
        settlement("2026-01-01", "2026-01-31"),
        settlement("2026-02-01", "2026-03-03"),
        settlement("2026-03-01", "2026-04-30"),
        settlement("2026-04-01", "2026-06-14", payments=2),
    ]
    assert part_payment_rate(mixed) == 0.25
    assert part_payment_rate([]) is None


def test_a_promise_pause_ends_at_local_midnight_not_utc_midnight():
    """Resuming at UTC midnight would ring the debtor at 05:30 their time on the
    morning their grace ran out."""
    resumes = chase_resumes_at(date(2026, 3, 10), grace_days=3)
    assert resumes == datetime(2026, 3, 13, 18, 30, tzinfo=timezone.utc)


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def ledger(tenants):
    """A buyer whose invoices and payments are wired through the real tables."""

    class Ledger:
        pass

    created = Ledger()
    created.company_id = tenants.a.company_id
    created.buyer_id = tenants.a.buyer_id
    created.account_id = tenants.a.account_id
    yield created

    with admin_session() as s:
        cid = created.company_id
        s.execute(delete(PaymentBehaviour).where(PaymentBehaviour.company_id == cid))
        s.execute(delete(Promise).where(Promise.company_id == cid))
        s.execute(delete(PaymentAllocation).where(PaymentAllocation.company_id == cid))
        s.execute(delete(Payment).where(Payment.company_id == cid))
        s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
        s.execute(
            delete(CreditAccount).where(
                CreditAccount.company_id == cid, CreditAccount.invoice_id.isnot(None)
            )
        )
        s.execute(delete(Invoice).where(Invoice.company_id == cid))
        buyer = s.get(Buyer, created.buyer_id)
        buyer.suppressed_until = None


def make_invoice(session, ledger, number: str, net_paise: int, *, issue: date) -> Invoice:
    invoice = Invoice(
        company_id=ledger.company_id,
        buyer_id=ledger.buyer_id,
        invoice_number=number,
        issue_date=issue,
        due_date=issue + timedelta(days=30),
        gross_paise=net_paise,
        tax_paise=0,
        net_paise=net_paise,
        outstanding_paise=net_paise,
    )
    session.add(invoice)
    session.flush()
    return invoice


def pay(session, ledger, invoice, amount_paise: int, *, on: date) -> Payment:
    payment = Payment(
        company_id=ledger.company_id,
        buyer_id=ledger.buyer_id,
        amount_paise=amount_paise,
        unallocated_paise=amount_paise,
        received_date=on,
    )
    session.add(payment)
    session.flush()
    apply_payment(session, payment, instructions={invoice.id: amount_paise})
    return payment


def build_slowing_ledger(session, ledger) -> None:
    """Four settled invoices at 30, 30, 60 and 74 days, the last in two parts."""
    first = make_invoice(session, ledger, "INV-1", 100_000, issue=date(2026, 1, 1))
    pay(session, ledger, first, 100_000, on=date(2026, 1, 31))

    second = make_invoice(session, ledger, "INV-2", 200_000, issue=date(2026, 2, 1))
    pay(session, ledger, second, 200_000, on=date(2026, 3, 3))

    third = make_invoice(session, ledger, "INV-3", 300_000, issue=date(2026, 3, 1))
    pay(session, ledger, third, 300_000, on=date(2026, 4, 30))

    fourth = make_invoice(session, ledger, "INV-4", 400_000, issue=date(2026, 4, 1))
    pay(session, ledger, fourth, 150_000, on=date(2026, 5, 20))
    pay(session, ledger, fourth, 250_000, on=date(2026, 6, 14))


# --------------------------------------------------------------------- rollup


def test_a_rollup_over_a_known_ledger_produces_the_expected_figures(ledger):
    with tenant_session(ledger.company_id) as s:
        build_slowing_ledger(s, ledger)
        behaviour = rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)

        assert behaviour.invoices_settled == 4
        assert float(behaviour.median_days_to_pay) == 45.0
        assert float(behaviour.mean_days_to_pay) == 48.5
        assert float(behaviour.days_to_pay_trend) == 37.0
        assert float(behaviour.part_payment_rate) == 0.25


def test_an_invoice_paid_after_the_as_of_date_is_not_yet_a_settlement(ledger):
    """A rollup for March must reproduce March, not read today's ledger back."""
    with tenant_session(ledger.company_id) as s:
        build_slowing_ledger(s, ledger)
        march = rollup_payment_behaviour(s, ledger.buyer_id, as_of=date(2026, 3, 31))

        assert march.invoices_settled == 2
        assert float(march.median_days_to_pay) == 30.0
        assert march.days_to_pay_trend is None


def test_a_buyer_with_no_settled_invoices_gets_a_row_of_nothing(ledger):
    """Silence is a fact about this buyer and has to be recorded as one."""
    with tenant_session(ledger.company_id) as s:
        behaviour = rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)

        assert behaviour.invoices_settled == 0
        assert behaviour.mean_days_to_pay is None
        assert behaviour.promise_kept_rate is None


def test_a_rerun_appends_a_row_and_never_overwrites(ledger):
    """"What did we know on 12 March" cannot be answered by an updated row."""
    with tenant_session(ledger.company_id) as s:
        build_slowing_ledger(s, ledger)
        rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)
        rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)

    with tenant_session(ledger.company_id) as s:
        rows = s.execute(
            select(func.count())
            .select_from(PaymentBehaviour)
            .where(PaymentBehaviour.buyer_id == ledger.buyer_id)
        ).scalar_one()
    assert rows == 2


def test_a_rollup_for_an_unknown_buyer_is_a_named_refusal(ledger, tenants):
    """Under RLS another tenant's buyer and a missing one look identical, and
    both must fail closed rather than write a row against a guessed company."""
    with tenant_session(ledger.company_id) as s:
        with pytest.raises(RollupRefused) as exc:
            rollup_payment_behaviour(s, tenants.b.buyer_id, as_of=AS_OF)
    assert exc.value.reason is RollupRefusal.UNKNOWN_BUYER


def test_the_dispute_rate_counts_current_disputes_and_says_nothing_about_contact(ledger):
    """Two figures on the row that nothing asserted.

    `dispute_rate` is deliberately current rather than historical — a cleared
    dispute stops counting, which under-reports rather than over-reports, the
    right direction for a factor that raises risk. `contact_response_rate` is
    deliberately None: whether a delivered SMS counts as a debtor responding is
    a decision about call outcomes, not about the ledger.
    """
    with tenant_session(ledger.company_id) as s:
        # A second account beside the fixture's, so the share is a half rather
        # than a number that reads the same whether or not it was computed.
        invoice = make_invoice(s, ledger, "INV-DR1", 100_000, issue=date(2026, 1, 1))
        accounts.sync_account_from_invoice(s, invoice, now=NOW)
        accounts.raise_dispute(
            s,
            s.get(CreditAccount, ledger.account_id),
            reason="short delivery claimed",
            now=NOW,
        )

        behaviour = rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)
        assert float(behaviour.dispute_rate) == 0.5
        assert behaviour.contact_response_rate is None


# -------------------------------------------------------------------- promises


def test_a_promise_pauses_chasing_until_the_date_plus_grace(ledger):
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=500_000,
            promised_on=date(2026, 3, 1),
            promised_by_date=date(2026, 3, 10),
        )
        assert buyer.suppressed_until == chase_resumes_at(date(2026, 3, 10))


def test_a_promise_never_shortens_an_existing_suppression(ledger):
    """A promise is a reason to wait longer, never a reason to chase sooner."""
    far_off = datetime(2026, 12, 1, tzinfo=timezone.utc)
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        buyer.suppressed_until = far_off
        record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=500_000,
            promised_on=date(2026, 3, 1),
            promised_by_date=date(2026, 3, 10),
        )
        assert buyer.suppressed_until == far_off


def test_a_promise_beyond_the_horizon_is_refused(ledger):
    """Otherwise "I will pay next year" is a way to switch the ladder off.

    Measured from today rather than from the promise's own anchor date, so the
    dates here are relative to the clock the cap is read against.
    """
    # date.today() is the machine's local date, which on a UTC host is a day
    # behind Asia/Kolkata for five and a half hours out of every twenty-four.
    # The cap is measured in IST, so anchoring the test in UTC made it pass for
    # most of the day and fail after 18:30 UTC — a test that is only wrong at
    # night is worse than one that is always wrong.
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(PROMISE_TIMEZONE)).date()
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        with pytest.raises(PromiseRefused) as exc:
            record_promise(
                s,
                buyer=buyer,
                promised_amount_paise=500_000,
                promised_on=today,
                promised_by_date=today + timedelta(days=MAX_PROMISE_HORIZON_DAYS + 1),
            )
    assert exc.value.reason is PromiseRefusal.HORIZON_TOO_FAR


def test_a_forward_dated_promise_cannot_buy_a_longer_pause(ledger):
    """The hole the cap had. Both dates are supplied by the caller, so a pair
    ninety days apart in 2029 passed every check and suppressed the buyer until
    2029 — the ladder switched off from the debtor's side, by an operator
    permission, which is exactly what the cap exists to prevent."""
    far = date.today() + timedelta(days=365)
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        with pytest.raises(PromiseRefused) as exc:
            record_promise(
                s,
                buyer=buyer,
                promised_amount_paise=500_000,
                promised_on=far,
                promised_by_date=far + timedelta(days=MAX_PROMISE_HORIZON_DAYS),
            )
        assert exc.value.reason is PromiseRefusal.PROMISED_ON_IN_FUTURE
        assert buyer.suppressed_until is None, "a refused promise still paused chasing"


def test_a_late_written_up_promise_is_judged_by_how_far_out_it_actually_is(ledger):
    """The other side of anchoring on the clock. A promise taken on the phone
    six weeks ago and written up today is ordinary; measured from its own
    anchor it would be refused for a horizon that has mostly already passed."""
    today = date.today()
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        promise = record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=500_000,
            promised_on=today - timedelta(days=60),
            promised_by_date=today + timedelta(days=40),
        )
        assert promise.status == PromiseStatus.OPEN.value


def test_a_promise_to_have_paid_yesterday_is_refused(ledger):
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        with pytest.raises(PromiseRefused) as exc:
            record_promise(
                s,
                buyer=buyer,
                promised_amount_paise=500_000,
                promised_on=date(2026, 3, 10),
                promised_by_date=date(2026, 3, 9),
            )
    assert exc.value.reason is PromiseRefusal.DATE_IN_PAST


def test_a_promise_for_nothing_is_refused(ledger):
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        with pytest.raises(PromiseRefused) as exc:
            record_promise(
                s,
                buyer=buyer,
                promised_amount_paise=0,
                promised_on=date(2026, 3, 1),
                promised_by_date=date(2026, 3, 10),
            )
    assert exc.value.reason is PromiseRefusal.AMOUNT_NOT_POSITIVE


def test_a_promise_against_another_buyers_account_is_refused(ledger, tenants):
    """RLS would normally hide the account entirely, so this runs on the
    cross-tenant session to prove the check itself holds."""
    with admin_session() as s:
        other = s.get(CreditAccount, tenants.b.account_id)
        buyer = s.get(Buyer, ledger.buyer_id)
        with pytest.raises(PromiseRefused) as exc:
            record_promise(
                s,
                buyer=buyer,
                promised_amount_paise=500_000,
                promised_on=date(2026, 3, 1),
                promised_by_date=date(2026, 3, 10),
                account=other,
            )
    assert exc.value.reason is PromiseRefusal.ACCOUNT_NOT_THIS_BUYERS


def test_settling_a_promise_twice_is_refused(ledger):
    """The outcome of a promise is evidence. It is written once."""
    when = datetime(2026, 3, 20, tzinfo=timezone.utc)
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        promise = record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=500_000,
            promised_on=date(2026, 3, 1),
            promised_by_date=date(2026, 3, 10),
        )
        settle_promise(s, promise, kept=False, settled_at=when)
        with pytest.raises(PromiseRefused) as exc:
            settle_promise(s, promise, kept=True, settled_at=when)
    assert exc.value.reason is PromiseRefusal.ALREADY_SETTLED


def test_a_broken_promise_is_recorded_as_broken_and_moves_the_kept_rate(ledger):
    """Deleting the failures would flatter the debtor at exactly the moment the
    evidence should be getting harsher."""
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        kept = record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=100_000,
            promised_on=date(2026, 1, 5),
            promised_by_date=date(2026, 1, 20),
        )
        broken = record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=100_000,
            promised_on=date(2026, 2, 5),
            promised_by_date=date(2026, 2, 20),
        )
        # Money against the first promise only.
        invoice = make_invoice(s, ledger, "INV-P", 100_000, issue=date(2026, 1, 1))
        pay(s, ledger, invoice, 100_000, on=date(2026, 1, 18))

        settled = resolve_due_promises(
            s, as_of=datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)
        )
        assert {p.id for p in settled} == {kept.id, broken.id}
        assert kept.status == PromiseStatus.KEPT.value
        assert broken.status == PromiseStatus.BROKEN.value

        behaviour = rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)
        assert float(behaviour.promise_kept_rate) == 0.5

    with tenant_session(ledger.company_id) as s:
        surviving = s.execute(
            select(func.count())
            .select_from(Promise)
            .where(Promise.buyer_id == ledger.buyer_id)
        ).scalar_one()
    assert surviving == 2, "a broken promise was removed rather than recorded"


def test_a_promise_still_inside_its_grace_is_not_judged_yet(ledger):
    with tenant_session(ledger.company_id) as s:
        buyer = s.get(Buyer, ledger.buyer_id)
        promise = record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=100_000,
            promised_on=date(2026, 3, 1),
            promised_by_date=date(2026, 3, 10),
        )
        last_day = datetime(
            2026, 3, 10 + DEFAULT_GRACE_DAYS, 6, 0, tzinfo=timezone.utc
        )
        assert resolve_due_promises(s, as_of=last_day) == []
        assert promise.status == PromiseStatus.OPEN.value

        # An OPEN promise past its date is not evidence either way, so it must
        # not drag the kept-rate down before the sweep has run.
        behaviour = rollup_payment_behaviour(s, ledger.buyer_id, as_of=AS_OF)
        assert behaviour.promise_kept_rate is None


# --------------------------------------------------- the human review queue


@pytest.fixture
def world(tenants, tmp_path):
    """One active campaign with the tenants buyer enrolled, ready to dispatch."""

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
            name="review-queue",
            status=CampaignStatus.ACTIVE,
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
                    body="Namaste {buyer_name}, invoice {invoice_ref} is overdue.",
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
        account = s.get(CreditAccount, w.account_id)
        account.status = AccountStatus.OVERDUE
        account.invoice_ref = "INV-1"
        # Dated against the injected clock; the shared fixture builds this
        # against `now()`, which is months away from NOW.
        account.due_date = NOW - timedelta(days=40)
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == w.buyer_id)
        ).scalar_one()
        phone.dnd_checked_at = NOW - timedelta(days=1)
        s.flush()

    yield w

    with admin_session() as s:
        cid = w.company_id
        s.execute(delete(PaymentBehaviour).where(PaymentBehaviour.company_id == cid))
        s.execute(delete(Promise).where(Promise.company_id == cid))
        s.execute(delete(Message).where(Message.company_id == cid))
        s.execute(delete(Call).where(Call.company_id == cid))
        s.execute(delete(CampaignTarget).where(CampaignTarget.company_id == cid))
        s.execute(delete(Campaign).where(Campaign.company_id == cid))
        s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
        s.execute(delete(AudioAsset).where(AudioAsset.company_id == cid))
        s.execute(delete(TemplateVersion).where(TemplateVersion.company_id == cid))
        s.execute(delete(MessageTemplate).where(MessageTemplate.company_id == cid))
        buyer = s.get(Buyer, w.buyer_id)
        buyer.next_action_at = None
        buyer.suppressed_until = None


def dispatch_kwargs(w, **over):
    base = dict(stager=w.stager, tts=w.tts, storage=w.storage, now=NOW)
    base.update(over)
    return base


def test_the_ladder_refusing_to_escalate_puts_the_account_in_front_of_a_person(world):
    """L2 thresholds met, but this debtor has never actually been reached.

    Policy refuses to escalate them to legal content — correctly — and that
    refusal is the one place the design says a person must look. Before the
    dispatch path set the flag, the queue could never contain this case.
    """
    with tenant_session(world.company_id) as s:
        state = s.execute(
            select(EscalationState).where(
                EscalationState.account_id == world.account_id
            )
        ).scalar_one()
        state.level = EscalationLevel.L2
        state.level_entered_at = NOW - timedelta(days=20)
        state.attempts_at_level = 3
        state.delivered_at_level = 0
        s.flush()

        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.reason == BlockReason.L3_REQUIRES_PRIOR_CONTACT.value

    with tenant_session(world.company_id) as s:
        state = s.execute(
            select(EscalationState).where(
                EscalationState.account_id == world.account_id
            )
        ).scalar_one()
        assert state.needs_human_review is True
        assert state.history[-1]["reason"] == (
            BlockReason.L3_REQUIRES_PRIOR_CONTACT.value
        )


def test_a_scrubbable_refusal_does_not_reach_the_review_queue(world):
    """An unscrubbed number is fixed by running a scrub. Queueing it for a human
    would bury the refusals that genuinely need one."""
    with tenant_session(world.company_id) as s:
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == world.buyer_id)
        ).scalar_one()
        phone.dnd_status = DndStatus.UNKNOWN
        s.flush()

        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.reason == BlockReason.DND_UNKNOWN.value

    with tenant_session(world.company_id) as s:
        state = s.execute(
            select(EscalationState).where(
                EscalationState.account_id == world.account_id
            )
        ).scalar_one()
        assert state.needs_human_review is False


def test_every_number_on_dnd_reaches_the_review_queue(world):
    """No scrub and no retry will ever make this debtor callable again."""
    with tenant_session(world.company_id) as s:
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.buyer_id == world.buyer_id)
        ).scalar_one()
        phone.dnd_status = DndStatus.REGISTERED
        s.flush()

        buyer = s.get(Buyer, world.buyer_id)
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.reason == BlockReason.DND_REGISTERED.value

    with tenant_session(world.company_id) as s:
        state = s.execute(
            select(EscalationState).where(
                EscalationState.account_id == world.account_id
            )
        ).scalar_one()
        assert state.needs_human_review is True


def test_a_promise_recorded_mid_campaign_stops_the_next_dispatch(world):
    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        record_promise(
            s,
            buyer=buyer,
            promised_amount_paise=5_000_000,
            promised_on=NOW.date(),
            promised_by_date=NOW.date() + timedelta(days=14),
        )
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.allowed is False
    assert result.reason == BlockReason.BUYER_SUPPRESSED.value


# ------------------------------------------------- a refusal in the right table


def test_a_refused_sms_is_recorded_as_a_message_not_a_call(world, monkeypatch):
    """A blocked SMS filed as a Call makes an operator debugging a silent
    messaging campaign read a dialler failure instead."""
    monkeypatch.setattr(settings, "env", "staging")
    monkeypatch.setattr(settings, "contact_allowlist_enforced", True)
    monkeypatch.setattr(settings, "contact_allowlist", ["+919999999999"])

    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS"]
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.reason == "CONTACT_NOT_ALLOWLISTED"
    assert result.message_id is not None
    assert result.call_id is None

    with tenant_session(world.company_id) as s:
        message = s.execute(
            select(Message).where(Message.buyer_id == world.buyer_id)
        ).scalars().one()
        calls = s.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.buyer_id == world.buyer_id)
        ).scalar_one()

    assert message.channel is Channel.SMS
    assert message.status is MessageStatus.BLOCKED
    assert message.block_reason == "CONTACT_NOT_ALLOWLISTED"
    assert message.counts_against_cap is False
    assert calls == 0, "a message refusal was filed as a call"


def test_an_engine_refusal_on_a_messaging_campaign_is_a_message(world):
    """The general case the fix is for, and the one that was still broken.

    `CONTACT_NOT_ALLOWLISTED` is raised after the engine has chosen a channel;
    every refusal the engine itself returns is raised through `_block`, which
    carried no channel — so CHANNEL_OPTED_OUT on an SMS-only campaign, the
    refusal most likely to be behind "why is this campaign silent", was filed as
    a call that never happened.
    """
    with tenant_session(world.company_id) as s:
        campaign = s.get(Campaign, world.campaign_id)
        campaign.channels = ["SMS"]
        s.add(
            ChannelOptOut(
                company_id=world.company_id,
                buyer_id=world.buyer_id,
                channel=Channel.SMS,
                source="sms_stop",
                opted_out_at=NOW,
            )
        )
        s.flush()
        buyer = s.get(Buyer, world.buyer_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )

    assert result.reason == BlockReason.CHANNEL_OPTED_OUT.value

    with tenant_session(world.company_id) as s:
        message = s.execute(
            select(Message).where(Message.buyer_id == world.buyer_id)
        ).scalars().one()
        calls = s.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.buyer_id == world.buyer_id)
        ).scalar_one()

    assert message.channel is Channel.SMS
    assert message.block_reason == BlockReason.CHANNEL_OPTED_OUT.value
    assert calls == 0, "an SMS refusal was filed as a call"


def test_a_refused_voice_call_is_still_recorded_as_a_call(world):
    """The other half of the same fix: nothing moved that should not have."""
    with tenant_session(world.company_id) as s:
        buyer = s.get(Buyer, world.buyer_id)
        buyer.consent_withdrawn = True
        campaign = s.get(Campaign, world.campaign_id)
        result = dispatch_buyer(
            s, buyer=buyer, campaign=campaign, **dispatch_kwargs(world)
        )
        buyer.consent_withdrawn = False

    assert result.call_id is not None
    assert result.message_id is None

    with tenant_session(world.company_id) as s:
        call = s.execute(
            select(Call).where(Call.buyer_id == world.buyer_id)
        ).scalars().one()
    assert call.status is CallStatus.BLOCKED
    assert call.block_reason == BlockReason.CONSENT_WITHDRAWN.value
