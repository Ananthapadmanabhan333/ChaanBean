"""Making a debt smaller.

The rest of the ledger suite proves that a balance goes up correctly. This one
proves it can come back down, because every adverse act this product performs —
a dunning call, a pre-legal assessment, the figure in a legal notice — is
computed from that balance, and until now nothing over HTTP could reduce it.

The four that matter most:

* over-crediting refuses, through the one guard, naming the excess;
* a written-off debt stops the ladder, so nothing keeps dialling about money
  the creditor has abandoned;
* a cleared dispute is still discoverable, because publication is gated on
  ever-disputed and an erased dispute silently unlocks it;
* on-account money lands across three invoices and the ledger still balances to
  the paise.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db import admin_session, tenant_session
from app.models import (
    AccountStatus,
    CreditAccount,
    CreditNote,
    EscalationLevel,
    EscalationState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    Return,
    Seller,
    Statement,
)
from app.registry.eligibility import ListingInput, evaluate
from app.trade import accounts, statements
from app.trade.ageing import buyer_position
from app.trade.allocation import AllocationError, apply_payment
from app.trade import reduction as reduction_module
from app.trade.reduction import (
    ReductionError,
    ReductionRefusal,
    apply_credit_note,
    apply_on_account,
    cancel_invoice,
    write_off,
)

TODAY = date(2026, 6, 1)
NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def ledger(tenants):
    """A buyer whose invoices run through the real tables.

    Teardown clears escalation states for the whole company, not only the
    invoice-backed accounts: raising or clearing a dispute now creates one where
    there was none, and a stray row fails the shared teardown on a foreign key
    inside whichever unrelated test happens to run next.
    """

    class Ledger:
        pass

    created = Ledger()
    created.company_id = tenants.a.company_id
    created.buyer_id = tenants.a.buyer_id
    yield created

    with admin_session() as s:
        cid = tenants.a.company_id
        s.execute(delete(Statement).where(Statement.company_id == cid))
        s.execute(delete(PaymentAllocation).where(PaymentAllocation.company_id == cid))
        s.execute(delete(Return).where(Return.company_id == cid))
        s.execute(delete(CreditNote).where(CreditNote.company_id == cid))
        s.execute(delete(Payment).where(Payment.company_id == cid))
        s.execute(delete(EscalationState).where(EscalationState.company_id == cid))
        s.execute(
            delete(CreditAccount).where(
                CreditAccount.company_id == cid, CreditAccount.invoice_id.isnot(None)
            )
        )
        s.execute(delete(Invoice).where(Invoice.company_id == cid))
        s.execute(delete(Seller).where(Seller.company_id == cid))


def make_invoice(session, ledger, number: str, net: int, *, due: date, issue: date | None = None):
    invoice = Invoice(
        company_id=ledger.company_id,
        buyer_id=ledger.buyer_id,
        invoice_number=f"{number}-{uuid.uuid4().hex[:6]}",
        issue_date=issue or (due - timedelta(days=30)),
        due_date=due,
        gross_paise=net,
        tax_paise=0,
        net_paise=net,
        outstanding_paise=net,
    )
    session.add(invoice)
    session.flush()
    return invoice


def make_payment(session, ledger, amount: int, *, received: date | None = None, reference=None):
    payment = Payment(
        company_id=ledger.company_id,
        buyer_id=ledger.buyer_id,
        amount_paise=amount,
        unallocated_paise=amount,
        received_date=received or TODAY,
        reference=reference,
    )
    session.add(payment)
    session.flush()
    return payment


def make_note(session, ledger, number: str, amount: int, *, invoice=None):
    return CreditNote(
        company_id=ledger.company_id,
        buyer_id=ledger.buyer_id,
        invoice_id=invoice.id if invoice is not None else None,
        note_number=f"{number}-{uuid.uuid4().hex[:6]}",
        amount_paise=amount,
        issue_date=TODAY,
    )


def events(state: EscalationState) -> list[str]:
    return [entry["event"] for entry in state.history or []]


# ----------------------------------------------------------------- credit notes


def test_crediting_beyond_the_balance_refuses_and_names_the_excess(ledger):
    """Four lakh fifty thousand credited against a four lakh invoice is a data
    error worth surfacing, not a negative balance to explain away later."""
    with pytest.raises(AllocationError, match="over-credit") as caught:
        with tenant_session(ledger.company_id) as s:
            inv = make_invoice(s, ledger, "INV-OC", 4_00_000_00, due=date(2026, 1, 1))
            apply_credit_note(s, make_note(s, ledger, "CN-OC", 4_50_000_00, invoice=inv))

    # The excess in paise, so the refusal says how far out the caller was.
    assert "5000000" in str(caught.value)


def test_the_over_credit_guard_sees_a_note_the_caller_never_added(ledger):
    """The guard sums `credit_notes` rows, so an unsaved note would be invisible
    to it and the credit would land unchecked."""
    with pytest.raises(AllocationError, match="over-credit"):
        with tenant_session(ledger.company_id) as s:
            inv = make_invoice(s, ledger, "INV-UNSAVED", 10_00_000, due=date(2026, 1, 1))
            note = make_note(s, ledger, "CN-UNSAVED", 12_00_000, invoice=inv)
            assert note not in s
            apply_credit_note(s, note)


def test_a_credit_note_that_clears_the_balance_stops_the_ladder(ledger):
    """Reducing the ledger and leaving `credit_accounts` behind is how the
    scheduler keeps dialling a debt that no longer exists."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CN", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        state = accounts.ensure_escalation_state(s, account, now=NOW)
        state.level = EscalationLevel.L2
        state.attempts_at_level = 3
        s.flush()

        apply_credit_note(s, make_note(s, ledger, "CN-FULL", 10_00_000, invoice=inv), now=NOW)

        s.refresh(inv)
        s.refresh(account)
        s.refresh(state)
        assert inv.outstanding_paise == 0
        assert inv.status is InvoiceStatus.PAID
        assert account.outstanding_paise == 0
        assert account.status is AccountStatus.SETTLED
        assert "settled" in events(state)


def test_a_partial_credit_note_leaves_the_account_open(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CNP", 10_00_000, due=date(2026, 1, 1))
        accounts.sync_account_from_invoice(s, inv, now=NOW)
        apply_credit_note(s, make_note(s, ledger, "CN-PART", 3_00_000, invoice=inv), now=NOW)

        s.refresh(inv)
        account = accounts.account_for_invoice(s, inv.id)
        assert inv.outstanding_paise == 7_00_000
        assert account.outstanding_paise == 7_00_000
        assert account.status is AccountStatus.OVERDUE


def test_an_on_account_credit_note_touches_no_invoice(ledger):
    with tenant_session(ledger.company_id) as s:
        note = make_note(s, ledger, "CN-ACCT", 5_00_000)
        assert apply_credit_note(s, note) is note


def test_crediting_a_cancelled_invoice_refuses(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CXL-CN", 10_00_000, due=date(2026, 1, 1))
        cancel_invoice(s, inv, reason="raised against the wrong buyer", now=NOW)

        with pytest.raises(ReductionError) as caught:
            apply_credit_note(s, make_note(s, ledger, "CN-VOID", 1_00_000, invoice=inv))
        assert caught.value.refusal is ReductionRefusal.INVOICE_ALREADY_CANCELLED


# ------------------------------------------------------------------- write-offs


def test_a_write_off_stops_the_ladder(ledger):
    """The bug the docstring promised was fixed and was not: `sync_account_from_
    invoice` stopped the ladder for SETTLED only, so a written-off debt kept
    dialling."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WO", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        state = accounts.ensure_escalation_state(s, account, now=NOW)
        state.level = EscalationLevel.L3
        state.attempts_at_level = 4
        state.needs_human_review = True
        s.flush()

        write_off(s, inv, reason="debtor insolvent; recovery abandoned", now=NOW)

        s.refresh(inv)
        s.refresh(account)
        s.refresh(state)

    assert inv.status is InvoiceStatus.WRITTEN_OFF
    assert account.status is AccountStatus.WRITTEN_OFF
    assert state.needs_human_review is False

    stop = [e for e in state.history if e["event"] == "written_off"]
    assert len(stop) == 1, "the ladder recorded no stop for a written-off debt"
    assert stop[0]["reason"] == "debtor insolvent; recovery abandoned"
    assert stop[0]["level"] == EscalationLevel.L3.value


def test_a_write_off_leaves_the_debt_standing_but_stops_pursuing_it(ledger):
    """Writing off is an accounting decision on this side. It does not extinguish
    what the buyer owes, so the balance stays; it only ends the chase."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WO2", 10_00_000, due=date(2026, 1, 1))
        accounts.sync_account_from_invoice(s, inv, now=NOW)
        write_off(s, inv, reason="uneconomic to pursue", now=NOW)
        s.refresh(inv)
        position = buyer_position(s, ledger.buyer_id, TODAY)

    assert inv.outstanding_paise == 10_00_000
    assert position.total_outstanding_paise == 0, "still counted as collectable"


def test_a_write_off_overrides_a_standing_dispute(ledger):
    """A dispute outranks the ledger, but a write-off is the more terminal human
    decision — otherwise the account sits in the review queue for good."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WO3", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        accounts.ensure_escalation_state(s, account, now=NOW)
        accounts.raise_dispute(s, account, reason="quality claim", now=NOW)

        write_off(s, inv, reason="settled commercially, balance abandoned", now=NOW)
        s.refresh(account)
        assert account.status is AccountStatus.WRITTEN_OFF


def test_a_write_off_needs_a_recorded_reason(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WO4", 10_00_000, due=date(2026, 1, 1))
        with pytest.raises(ReductionError) as caught:
            write_off(s, inv, reason="   ", now=NOW)
        assert caught.value.refusal is ReductionRefusal.REASON_REQUIRED
        assert inv.status is not InvoiceStatus.WRITTEN_OFF


def test_nothing_assigns_outstanding_paise_directly(ledger):
    """Both closing paths land on `recompute_invoice`, so the balance still
    agrees with the movements afterwards.

    The write-off half is behavioural: a direct `= 0` there fails it. The
    cancellation half cannot be — `recompute_invoice` short-circuits a cancelled
    invoice to zero, so the same number arrives whether or not the single writer
    was used — so that half is asserted structurally instead.
    """
    with tenant_session(ledger.company_id) as s:
        written = make_invoice(s, ledger, "INV-DER1", 10_00_000, due=date(2026, 1, 1))
        apply_payment(s, make_payment(s, ledger, 4_00_000))
        write_off(s, written, reason="remainder abandoned", now=NOW)

        cancelled = make_invoice(s, ledger, "INV-DER2", 7_00_000, due=date(2026, 2, 1))
        cancel_invoice(s, cancelled, reason="duplicate of INV-DER1", now=NOW)

        s.refresh(written)
        s.refresh(cancelled)

    assert written.outstanding_paise == 6_00_000  # net less what was actually paid
    assert cancelled.outstanding_paise == 0

    source = Path(reduction_module.__file__).read_text(encoding="utf-8")
    assert "outstanding_paise =" not in source, (
        "app.trade.reduction assigns the derived balance; recompute_invoice is "
        "its only writer"
    )


def test_writing_off_a_cancelled_invoice_is_refused(ledger):
    """A voided invoice is not a debt to abandon; there is nothing left to write
    off, and recording one would claim a decision nobody made."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WO5", 10_00_000, due=date(2026, 1, 1))
        cancel_invoice(s, inv, reason="raised against the wrong buyer", now=NOW)

        with pytest.raises(ReductionError) as caught:
            write_off(s, inv, reason="debtor insolvent", now=NOW)
        assert caught.value.refusal is ReductionRefusal.INVOICE_ALREADY_CANCELLED
        assert inv.status is InvoiceStatus.CANCELLED


def test_a_cancellation_needs_a_recorded_reason(ledger):
    """The same rule as a write-off, and the same reason: the sentence is the
    only account of why this debt stopped being pursued."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CXL9", 10_00_000, due=date(2026, 1, 1))
        with pytest.raises(ReductionError) as caught:
            cancel_invoice(s, inv, reason="   ", now=NOW)
        assert caught.value.refusal is ReductionRefusal.REASON_REQUIRED
        assert inv.status is not InvoiceStatus.CANCELLED


def test_a_second_sync_of_a_closed_account_records_no_second_stop(ledger):
    """An ERP re-sync of a settled invoice is routine, and appending "settled"
    on a day nothing settled turns the one durable trace into noise."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-RESYNC", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        state = accounts.ensure_escalation_state(s, account, now=NOW)

        write_off(s, inv, reason="uneconomic to pursue", now=NOW)
        accounts.sync_account_from_invoice(s, inv, now=NOW)
        accounts.sync_account_from_invoice(s, inv, now=NOW)

        s.refresh(state)
        stops = [e for e in state.history if e["event"] == "written_off"]
        assert len(stops) == 1, "a re-sync appended a second ladder stop"


def test_crediting_a_written_off_invoice_does_not_rewind_the_ladder(ledger):
    """Nothing was part-paid. Resetting the cadence would record a payment that
    did not happen, and start the ladder from zero if the account ever came
    back."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-WOCN", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        state = accounts.ensure_escalation_state(s, account, now=NOW)
        state.attempts_at_level = 3
        s.flush()

        write_off(s, inv, reason="recovery abandoned", now=NOW)
        apply_credit_note(
            s, make_note(s, ledger, "CN-WO", 2_00_000, invoice=inv), now=NOW
        )

        s.refresh(state)
        assert state.attempts_at_level == 3
        assert "part_payment_cadence_reset" not in events(state)


# ---------------------------------------------------------------- cancellations


def test_cancelling_an_invoice_with_money_against_it_refuses(ledger):
    """Removing the debit while the payment stays as a credit reads, on a
    statement already sent, as though the buyer had overpaid."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CXL1", 10_00_000, due=date(2026, 1, 1))
        apply_payment(s, make_payment(s, ledger, 3_00_000))

        with pytest.raises(ReductionError) as caught:
            cancel_invoice(s, inv, reason="raised in error", now=NOW)

        assert caught.value.refusal is ReductionRefusal.INVOICE_HAS_SETTLEMENTS
        assert "300000" in str(caught.value)
        s.refresh(inv)
        assert inv.status is not InvoiceStatus.CANCELLED


def test_a_cancelled_account_is_not_reported_as_settled(ledger):
    """AccountStatus has no CANCELLED, and SETTLED would claim it was paid."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CXL2", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        cancel_invoice(s, inv, reason="raised against the wrong buyer", now=NOW)
        s.refresh(account)

        assert account.status is AccountStatus.WRITTEN_OFF
        assert account.outstanding_paise == 0


def test_cancelling_twice_refuses(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-CXL3", 10_00_000, due=date(2026, 1, 1))
        cancel_invoice(s, inv, reason="raised in error", now=NOW)
        with pytest.raises(ReductionError) as caught:
            cancel_invoice(s, inv, reason="raised in error again", now=NOW)
        assert caught.value.refusal is ReductionRefusal.INVOICE_ALREADY_CANCELLED


# ------------------------------------------------------- the WRITTEN_OFF meaning


def test_a_written_off_invoice_leaves_collections_but_stays_on_the_statement(ledger):
    """The divergence, stated as a test.

    Ageing and allocation answer "what are we chasing"; the statement answers
    "what passed between these two parties". A statement is rebuilt for a past
    period out of today's rows, so dropping written-off invoices would make a
    February statement stop showing an invoice that was plainly live in
    February.
    """
    with tenant_session(ledger.company_id) as s:
        live = make_invoice(
            s, ledger, "INV-LIVE", 6_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        dead = make_invoice(
            s, ledger, "INV-DEAD", 4_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        void = make_invoice(
            s, ledger, "INV-VOID", 9_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        accounts.sync_account_from_invoice(s, dead, now=NOW)
        write_off(s, dead, reason="uneconomic to pursue", now=NOW)
        cancel_invoice(s, void, reason="duplicate", now=NOW)

        result = statements.generate(s, ledger.buyer_id, date(2026, 2, 1), date(2026, 4, 30))
        position = buyer_position(s, ledger.buyer_id, date(2026, 4, 30))
        refs = {r.reference for r in result.lines}

    assert live.invoice_number in refs
    assert dead.invoice_number in refs, "a written-off invoice vanished from the statement"
    assert void.invoice_number not in refs, "a cancelled invoice is not a movement"

    assert result.closing_paise == 10_00_000  # live plus written off, not the void
    assert position.total_outstanding_paise == 6_00_000  # only what is still chased


# -------------------------------------------------------------------- disputes


def test_a_cleared_dispute_is_still_discoverable(ledger):
    """`clear_dispute` nulls `disputed_reason`, so the ladder history is the only
    surviving evidence that this debt was ever contested."""
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-D1", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)

        accounts.raise_dispute(s, account, reason="short delivery claimed", now=NOW)
        assert accounts.ever_disputed(s, account) is True

        accounts.clear_dispute(s, account, resolution="credit note CN-77 issued", now=NOW)
        state = accounts.ensure_escalation_state(s, account, now=NOW)

        assert account.status is AccountStatus.OVERDUE
        assert account.disputed_reason is None
        assert accounts.ever_disputed(s, account) is True

        cleared = [e for e in state.history if e["event"] == accounts.DISPUTE_CLEARED]
        assert len(cleared) == 1
        assert cleared[0]["disputed_reason"] == "short delivery claimed"
        assert cleared[0]["resolution"] == "credit note CN-77 issued"
        assert accounts.DISPUTE_RAISED in events(state)


def test_a_cleared_dispute_still_blocks_publication(ledger):
    """The consequence the trace exists for. `app.registry.eligibility` gates on
    ever-disputed, so an erased dispute would silently unlock a public statement
    of fact about a named business.

    The `ListingInput` is assembled here, because nothing in `app/` assembles
    one yet: this proves the two pieces compose, not that anything in the
    running system composes them. Wiring the gate is still to do.
    """
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-D2", 20_00_000_00, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        accounts.raise_dispute(s, account, reason="quantity disputed", now=NOW)
        accounts.clear_dispute(s, account, resolution="withdrawn by the buyer", now=NOW)

        listing = ListingInput(
            as_of=TODAY,
            account_status=account.status.value,
            ever_disputed=accounts.ever_disputed(s, account),
            outstanding_paise=account.outstanding_paise,
            notice_dispatched=True,
            notice_delivery_proof=True,
            notice_response_deadline=date(2026, 5, 1),
            notice_response_received=False,
            entity_confidence_publishable=True,
            ledger_reconciled_on=TODAY,
            last_payment_on=None,
            human_signed_off_by="ops@example.test",
        )

    result = evaluate(listing)
    assert result.eligible is False
    assert any("disputed" in blocker for blocker in result.blockers)


def test_an_account_never_disputed_says_so(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-D3", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv, now=NOW)
        assert accounts.ever_disputed(s, account) is False

        accounts.ensure_escalation_state(s, account, now=NOW)
        assert accounts.ever_disputed(s, account) is False


# ------------------------------------------------------------ money on account


def test_on_account_allocation_across_three_invoices_balances_to_the_paise(ledger):
    """Fifty lakh received, ten lakh applied on the day, forty lakh sitting on
    account until the buyer says where it goes."""
    with tenant_session(ledger.company_id) as s:
        one = make_invoice(
            s, ledger, "INV-OA1", 30_00_000, due=date(2026, 1, 1), issue=date(2025, 12, 1)
        )
        two = make_invoice(
            s, ledger, "INV-OA2", 15_00_000, due=date(2026, 2, 1), issue=date(2026, 1, 1)
        )
        three = make_invoice(
            s, ledger, "INV-OA3", 20_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        for invoice in (one, two, three):
            accounts.sync_account_from_invoice(s, invoice, now=NOW)

        pay = make_payment(s, ledger, 50_00_000, received=date(2026, 3, 15), reference="NEFT-OA")
        apply_payment(s, pay, instructions={one.id: 10_00_000})
        assert pay.unallocated_paise == 40_00_000

        plan = apply_on_account(
            s,
            pay,
            instructions={one.id: 20_00_000, two.id: 15_00_000, three.id: 5_00_000},
            now=NOW,
        )

        s.refresh(one)
        s.refresh(two)
        s.refresh(three)
        allocated = sum(
            row.amount_paise
            for row in s.execute(
                select(PaymentAllocation).where(PaymentAllocation.payment_id == pay.id)
            ).scalars()
        )
        result = statements.generate(s, ledger.buyer_id, date(2025, 12, 1), date(2026, 12, 31))
        position = buyer_position(s, ledger.buyer_id, date(2026, 12, 31))
        states = {
            invoice.invoice_number: accounts.account_for_invoice(s, invoice.id).status
            for invoice in (one, two, three)
        }

    assert plan.allocated_paise == 40_00_000
    assert plan.unallocated_paise == 0

    # Never more than the money that actually arrived.
    assert allocated == 50_00_000

    assert one.outstanding_paise == 0
    assert two.outstanding_paise == 0
    assert three.outstanding_paise == 15_00_000

    # 65,00,000 invoiced, 50,00,000 received, to the paise on both sides.
    assert result.closing_paise == 15_00_000
    assert result.opening_paise + result.debits_paise - result.credits_paise == (
        result.closing_paise
    )
    assert position.total_outstanding_paise == 15_00_000

    assert states[one.invoice_number] is AccountStatus.SETTLED
    assert states[two.invoice_number] is AccountStatus.SETTLED
    assert states[three.invoice_number] is AccountStatus.OVERDUE


def test_on_account_does_not_replan_the_whole_payment(ledger):
    """`apply_payment` plans against `amount_paise`. Calling it a second time
    would plan the full fifty lakh again; this plans the forty that is left."""
    with tenant_session(ledger.company_id) as s:
        one = make_invoice(
            s, ledger, "INV-RP1", 30_00_000, due=date(2026, 1, 1), issue=date(2025, 12, 1)
        )
        two = make_invoice(
            s, ledger, "INV-RP2", 15_00_000, due=date(2026, 2, 1), issue=date(2026, 1, 1)
        )
        three = make_invoice(
            s, ledger, "INV-RP3", 20_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        pay = make_payment(s, ledger, 50_00_000, reference="NEFT-RP")
        apply_payment(s, pay, instructions={one.id: 10_00_000})

        plan = apply_on_account(s, pay, now=NOW)  # oldest-first with what is left

        s.refresh(one)
        s.refresh(two)
        s.refresh(three)
        allocated = sum(
            row.amount_paise
            for row in s.execute(
                select(PaymentAllocation).where(PaymentAllocation.payment_id == pay.id)
            ).scalars()
        )

    assert plan.allocated_paise == 40_00_000
    assert allocated == 50_00_000, f"{allocated} allocated from a 50,00,000 payment"
    assert one.outstanding_paise == 0  # topped up in place, not a second row
    assert two.outstanding_paise == 0
    assert three.outstanding_paise == 15_00_000


def test_on_account_with_nothing_on_account_refuses(ledger):
    with tenant_session(ledger.company_id) as s:
        make_invoice(s, ledger, "INV-OA-EMPTY", 10_00_000, due=date(2026, 1, 1))
        pay = make_payment(s, ledger, 10_00_000, reference="NEFT-EMPTY")
        apply_payment(s, pay)
        assert pay.unallocated_paise == 0

        with pytest.raises(ReductionError) as caught:
            apply_on_account(s, pay, now=NOW)
        assert caught.value.refusal is ReductionRefusal.NO_UNALLOCATED_BALANCE


def test_on_account_cannot_over_allocate_an_invoice(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-OA-OVER", 10_00_000, due=date(2026, 1, 1))
        pay = make_payment(s, ledger, 25_00_000, reference="NEFT-OVER")
        apply_payment(s, pay, instructions={inv.id: 4_00_000})

        with pytest.raises(AllocationError, match="only"):
            apply_on_account(s, pay, instructions={inv.id: 9_00_000}, now=NOW)


def test_on_account_skips_written_off_invoices(ledger):
    """Money must not land on a debt the creditor has stopped pursuing — it would
    reduce a balance nobody is chasing while a live invoice goes unpaid."""
    with tenant_session(ledger.company_id) as s:
        dead = make_invoice(
            s, ledger, "INV-OA-DEAD", 30_00_000, due=date(2026, 1, 1), issue=date(2025, 12, 1)
        )
        live = make_invoice(
            s, ledger, "INV-OA-LIVE", 20_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        accounts.sync_account_from_invoice(s, dead, now=NOW)
        write_off(s, dead, reason="debtor untraceable", now=NOW)

        pay = make_payment(s, ledger, 25_00_000, reference="NEFT-DEAD")
        plan = apply_on_account(s, pay, now=NOW)

        s.refresh(dead)
        s.refresh(live)

    assert [entry.invoice_id for entry in plan.allocations] == [live.id]
    assert plan.unallocated_paise == 5_00_000
    assert dead.outstanding_paise == 30_00_000
    assert live.outstanding_paise == 0
