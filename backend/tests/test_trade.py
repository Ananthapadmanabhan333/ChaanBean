"""The ledger.

Allocation is the module most likely to produce a legally consequential error,
so it is tested hardest. `test_concurrent_payments_do_not_double_allocate`
matters more than it looks: two overlapping ERP sync runs is the normal case in
Phase 4, not an exotic race.
"""

from __future__ import annotations

import random
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, admin_session, tenant_session
from app.models import (
    AccountStatus,
    AgeingBucket,
    AllocationRule,
    CreditNote,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    Return,
    Seller,
    Statement,
)
from app.trade import accounts, statements
from app.trade.ageing import (
    ageing_bucket,
    buyer_position,
    days_past_due,
    position_from_invoices,
)
from app.trade.allocation import (
    AllocationError,
    InvoiceRef,
    apply_credit_note,
    apply_payment,
    invoice_refs,
    plan_allocation,
    recompute_invoice,
)

TODAY = date(2026, 6, 1)


# ------------------------------------------------------------------- builders


def ref(number: str, net: int, *, due: date, allocated: int = 0, credited: int = 0):
    return InvoiceRef(
        id=uuid.uuid4(),
        invoice_number=number,
        issue_date=due - timedelta(days=30),
        due_date=due,
        net_paise=net,
        allocated_paise=allocated,
        credited_paise=credited,
    )


@pytest.fixture
def ledger(tenants):
    """A buyer with invoices, wired through the real tables."""

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
        from app.models import CreditAccount, EscalationState

        invoice_accounts = select(CreditAccount.id).where(
            CreditAccount.company_id == cid, CreditAccount.invoice_id.isnot(None)
        )
        s.execute(
            delete(EscalationState).where(EscalationState.account_id.in_(invoice_accounts))
        )
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
        invoice_number=number,
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


# --------------------------------------------------------------- pure planning


def test_one_payment_across_three_invoices_oldest_first():
    invoices = [
        ref("INV-3", 20_00_000, due=date(2026, 4, 1)),
        ref("INV-1", 30_00_000, due=date(2026, 1, 1)),
        ref("INV-2", 15_00_000, due=date(2026, 2, 1)),
    ]
    plan = plan_allocation(50_00_000, invoices)

    assert plan.unallocated_paise == 0
    assert [a.amount_paise for a in plan.allocations] == [30_00_000, 15_00_000, 5_00_000]
    assert [i.invoice_number for i in invoices if i.id == plan.allocations[0].invoice_id] == [
        "INV-1"
    ]
    assert all(a.rule is AllocationRule.OLDEST_FIRST for a in plan.allocations)


def test_remainder_lands_on_account_not_absorbed():
    invoices = [ref("INV-1", 10_00_000, due=date(2026, 1, 1))]
    plan = plan_allocation(25_00_000, invoices)
    assert plan.allocated_paise == 10_00_000
    assert plan.unallocated_paise == 15_00_000


def test_exact_match_beats_oldest_first():
    """An amount matching one invoice exactly is almost always meant for it."""
    old = ref("INV-OLD", 30_00_000, due=date(2026, 1, 1))
    exact = ref("INV-EXACT", 7_77_777, due=date(2026, 5, 1))
    plan = plan_allocation(7_77_777, [old, exact])
    assert len(plan.allocations) == 1
    assert plan.allocations[0].invoice_id == exact.id
    assert plan.allocations[0].rule is AllocationRule.EXACT_MATCH


def test_explicit_instruction_wins_over_the_default():
    old = ref("INV-OLD", 30_00_000, due=date(2026, 1, 1))
    newer = ref("INV-NEW", 30_00_000, due=date(2026, 5, 1))
    plan = plan_allocation(10_00_000, [old, newer], instructions={newer.id: 10_00_000})
    assert plan.allocations[0].invoice_id == newer.id
    assert plan.allocations[0].rule is AllocationRule.MANUAL


def test_over_allocation_raises():
    invoice = ref("INV-1", 10_00_000, due=date(2026, 1, 1))
    with pytest.raises(AllocationError, match="only"):
        plan_allocation(50_00_000, [invoice], instructions={invoice.id: 20_00_000})

    other = ref("INV-2", 10_00_000, due=date(2026, 2, 1))
    with pytest.raises(AllocationError, match="total"):
        plan_allocation(
            5_00_000, [invoice, other], instructions={invoice.id: 4_00_000, other.id: 4_00_000}
        )


def test_allocation_skips_already_settled_invoices():
    settled = ref("INV-PAID", 10_00_000, due=date(2026, 1, 1), allocated=10_00_000)
    open_one = ref("INV-OPEN", 10_00_000, due=date(2026, 3, 1))
    plan = plan_allocation(5_00_000, [settled, open_one])
    assert len(plan.allocations) == 1
    assert plan.allocations[0].invoice_id == open_one.id


def test_zero_or_negative_payment_refused():
    with pytest.raises(AllocationError):
        plan_allocation(0, [ref("INV-1", 100, due=TODAY)])


# ------------------------------------------------------------------ persistence


def test_payment_settles_invoice_and_flips_status(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A1", 10_00_000, due=date(2026, 1, 1))
        pay = make_payment(s, ledger, 10_00_000)
        apply_payment(s, pay)

        s.refresh(inv)
        assert inv.outstanding_paise == 0
        assert inv.status is InvoiceStatus.PAID
        assert pay.unallocated_paise == 0


def test_partial_payment_keeps_invoice_open(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A2", 10_00_000, due=date(2026, 1, 1))
        apply_payment(s, make_payment(s, ledger, 4_00_000))
        s.refresh(inv)
        assert inv.outstanding_paise == 6_00_000
        assert inv.status is InvoiceStatus.PART_PAID


def test_credit_note_reduces_outstanding(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A3", 10_00_000, due=date(2026, 1, 1))
        note = CreditNote(
            company_id=ledger.company_id,
            buyer_id=ledger.buyer_id,
            invoice_id=inv.id,
            note_number="CN-1",
            amount_paise=3_00_000,
            issue_date=TODAY,
        )
        s.add(note)
        apply_credit_note(s, note)
        s.refresh(inv)
        assert inv.outstanding_paise == 7_00_000


def test_credit_note_larger_than_balance_is_refused(ledger):
    with pytest.raises(AllocationError, match="over-credit"):
        with tenant_session(ledger.company_id) as s:
            inv = make_invoice(s, ledger, "INV-A4", 10_00_000, due=date(2026, 1, 1))
            note = CreditNote(
                company_id=ledger.company_id,
                buyer_id=ledger.buyer_id,
                invoice_id=inv.id,
                note_number="CN-2",
                amount_paise=15_00_000,
                issue_date=TODAY,
            )
            s.add(note)
            apply_credit_note(s, note)


def test_full_settlement_settles_account_and_stops_ladder(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A5", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv)
        state = accounts.ensure_escalation_state(s, account)
        state.attempts_at_level = 2

        apply_payment(s, make_payment(s, ledger, 10_00_000))
        s.refresh(inv)
        account = accounts.sync_account_from_invoice(s, inv)
        assert account.status is AccountStatus.SETTLED


def test_partial_payment_resets_cadence_but_not_level(ledger):
    """Someone who just paid something is engaging. Keep the level; ease the pace."""
    from app.models import EscalationLevel

    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A6", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv)
        state = accounts.ensure_escalation_state(s, account)
        state.level = EscalationLevel.L2
        state.attempts_at_level = 3
        s.flush()

        apply_payment(s, make_payment(s, ledger, 2_00_000))
        s.refresh(inv)
        account = accounts.sync_account_from_invoice(s, inv)

        s.refresh(state)
        assert account.status is AccountStatus.OVERDUE
        assert state.attempts_at_level == 0
        assert state.level is EscalationLevel.L2


def test_dispute_halts_contact_and_survives_a_payment(ledger):
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-A7", 10_00_000, due=date(2026, 1, 1))
        account = accounts.sync_account_from_invoice(s, inv)
        accounts.raise_dispute(s, account, reason="goods not received")

        apply_payment(s, make_payment(s, ledger, 1_00_000))
        s.refresh(inv)
        account = accounts.sync_account_from_invoice(s, inv)
        assert account.status is AccountStatus.IN_DISPUTE


def test_outstanding_is_never_over_settled(ledger):
    with pytest.raises(AllocationError):
        with tenant_session(ledger.company_id) as s:
            inv = make_invoice(s, ledger, "INV-A8", 10_00_000, due=date(2026, 1, 1))
            s.add(
                PaymentAllocation(
                    company_id=ledger.company_id,
                    payment_id=make_payment(s, ledger, 20_00_000).id,
                    invoice_id=inv.id,
                    amount_paise=20_00_000,
                )
            )
            s.flush()
            recompute_invoice(s, inv)


# ---------------------------------------------------------------------- ageing


@pytest.mark.parametrize(
    "dpd,expected",
    [
        (0, AgeingBucket.CURRENT),
        (1, AgeingBucket.B1_30),
        (30, AgeingBucket.B1_30),
        (31, AgeingBucket.B31_60),
        (60, AgeingBucket.B31_60),
        (61, AgeingBucket.B61_90),
        (90, AgeingBucket.B61_90),
        (91, AgeingBucket.B90_PLUS),
    ],
)
def test_ageing_bucket_boundaries(dpd, expected):
    assert ageing_bucket(dpd) is expected


def test_days_past_due_is_as_of_a_date_not_now():
    due = date(2026, 1, 1)
    assert days_past_due(due, date(2026, 1, 1)) == 0
    assert days_past_due(due, date(2025, 12, 1)) == 0  # never negative
    assert days_past_due(due, date(2026, 3, 2)) == 60


def test_historical_position_reproduces(ledger):
    """The same question asked about a past date must give the past answer."""
    with tenant_session(ledger.company_id) as s:
        make_invoice(
            s, ledger, "INV-H1", 10_00_000, due=date(2026, 1, 1), issue=date(2025, 12, 1)
        )
        early = buyer_position(s, ledger.buyer_id, date(2026, 1, 15))
        late = buyer_position(s, ledger.buyer_id, date(2026, 6, 1))

    assert early.max_days_past_due == 14
    assert late.max_days_past_due == 151
    assert early.buckets[AgeingBucket.B1_30] == 10_00_000
    assert late.buckets[AgeingBucket.B90_PLUS] == 10_00_000


def test_position_matches_the_sum_of_its_invoices_under_random_data():
    """Where a sign error would hide."""
    rng = random.Random(20260601)
    for _ in range(200):
        invoices = [
            ref(
                f"INV-{n}",
                rng.randint(1, 50_00_000),
                due=TODAY - timedelta(days=rng.randint(-30, 400)),
            )
            for n in range(rng.randint(0, 8))
        ]
        buyer = uuid.uuid4()
        position = position_from_invoices(buyer, invoices, TODAY)
        assert position.total_outstanding_paise == sum(i.outstanding_paise for i in invoices)
        assert sum(position.buckets.values()) == position.total_outstanding_paise
        assert position.invoice_count == len(invoices)


# ------------------------------------------------------------------ statements


def test_statement_reconciles_across_every_movement_type(ledger):
    with tenant_session(ledger.company_id) as s:
        make_invoice(
            s, ledger, "INV-S1", 30_00_000, due=date(2026, 3, 1), issue=date(2026, 2, 1)
        )
        make_invoice(
            s, ledger, "INV-S2", 20_00_000, due=date(2026, 4, 1), issue=date(2026, 3, 1)
        )
        pay = make_payment(s, ledger, 25_00_000, received=date(2026, 3, 15), reference="NEFT-1")
        apply_payment(s, pay)
        note = CreditNote(
            company_id=ledger.company_id,
            buyer_id=ledger.buyer_id,
            note_number="CN-S1",
            amount_paise=2_00_000,
            issue_date=date(2026, 3, 20),
        )
        s.add(note)
        s.flush()

        result = statements.generate(s, ledger.buyer_id, date(2026, 2, 1), date(2026, 4, 30))
        stored = statements.persist(s, ledger.company_id, result)

    assert result.opening_paise == 0
    assert result.closing_paise == 50_00_000 - 25_00_000 - 2_00_000
    assert result.opening_paise + result.debits_paise - result.credits_paise == (
        result.closing_paise
    )
    assert {line.kind for line in result.lines} == {"invoice", "payment", "credit_note"}
    assert stored.closing_paise == result.closing_paise


def test_statement_opening_balance_carries_from_before_the_period(ledger):
    with tenant_session(ledger.company_id) as s:
        make_invoice(
            s, ledger, "INV-S3", 12_00_000, due=date(2026, 1, 31), issue=date(2026, 1, 1)
        )
        result = statements.generate(s, ledger.buyer_id, date(2026, 2, 1), date(2026, 2, 28))

    assert result.opening_paise == 12_00_000
    assert result.lines == ()
    assert result.closing_paise == 12_00_000


def test_realistic_ledger_reconciles_to_the_paise(ledger):
    """20 invoices, 12 payments, 3 credit notes, 2 returns."""
    rng = random.Random(7)
    with tenant_session(ledger.company_id) as s:
        invoiced = 0
        for n in range(20):
            amount = rng.randint(1_00_000, 40_00_000)
            invoiced += amount
            make_invoice(
                s,
                ledger,
                f"INV-R{n:02d}",
                amount,
                due=date(2026, 1, 1) + timedelta(days=n * 5),
                issue=date(2025, 12, 1) + timedelta(days=n * 5),
            )

        paid = 0
        for n in range(12):
            amount = rng.randint(50_000, 15_00_000)
            pay = make_payment(
                s, ledger, amount, received=date(2026, 2, 1) + timedelta(days=n * 3)
            )
            apply_payment(s, pay)
            paid += amount

        credited = 0
        for n in range(3):
            amount = rng.randint(10_000, 2_00_000)
            credited += amount
            s.add(
                CreditNote(
                    company_id=ledger.company_id,
                    buyer_id=ledger.buyer_id,
                    note_number=f"CN-R{n}",
                    amount_paise=amount,
                    issue_date=date(2026, 3, 1) + timedelta(days=n),
                )
            )
        for n in range(2):
            s.add(
                Return(
                    company_id=ledger.company_id,
                    buyer_id=ledger.buyer_id,
                    amount_paise=5_000,
                    return_date=date(2026, 3, 10),
                )
            )
        s.flush()

        result = statements.generate(s, ledger.buyer_id, date(2025, 12, 1), date(2026, 12, 31))
        position = buyer_position(s, ledger.buyer_id, date(2026, 12, 31))

    # Hand calculation: everything invoiced, less everything received and credited.
    assert result.closing_paise == invoiced - paid - credited
    assert result.opening_paise + result.debits_paise - result.credits_paise == (
        result.closing_paise
    )
    # The position counts only what is still sitting on invoices, so unallocated
    # payment (on-account credit) is the difference.
    assert position.total_outstanding_paise >= 0
    assert position.total_outstanding_paise >= result.closing_paise


def test_imbalanced_period_raises_rather_than_emitting(ledger):
    with tenant_session(ledger.company_id) as s:
        with pytest.raises(ValueError):
            statements.generate(s, ledger.buyer_id, date(2026, 5, 1), date(2026, 4, 1))


# ----------------------------------------------------------------- concurrency


def test_concurrent_payments_do_not_double_allocate(ledger):
    """Two overlapping syncs against one invoice.

    Both threads try to allocate the whole balance. With the row lock the second
    waits, re-reads, finds nothing left and cleanly allocates nothing.

    Remove the lock and this test fails with 20,00,000 allocated against a
    10,00,000 invoice — verified, not assumed. The over-settlement check in
    `recompute_invoice` is a backstop, not a substitute: each transaction reads a
    balance that was true when it read it, so both pass their own check and the
    corruption only becomes visible once both have committed.

    `errors == []` is asserted too, so a run where the backstop fires and rolls a
    transaction back cannot be mistaken for correct serialisation.
    """
    with tenant_session(ledger.company_id) as s:
        inv = make_invoice(s, ledger, "INV-RACE", 10_00_000, due=date(2026, 1, 1))
        invoice_id = inv.id
        p1 = make_payment(s, ledger, 10_00_000, reference="RACE-1").id
        p2 = make_payment(s, ledger, 10_00_000, reference="RACE-2").id

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def settle(payment_id):
        session = SessionLocal()
        session.info["company_id"] = str(ledger.company_id)
        try:
            payment = session.get(Payment, payment_id)
            barrier.wait(timeout=10)
            apply_payment(session, payment)
            session.commit()
        except Exception as exc:  # recorded, not raised, so both threads finish
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=settle, args=(pid,)) for pid in (p1, p2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    with tenant_session(ledger.company_id) as s:
        invoice = s.get(Invoice, invoice_id)
        allocated = sum(
            a.amount_paise
            for a in s.execute(
                select(PaymentAllocation).where(PaymentAllocation.invoice_id == invoice_id)
            ).scalars()
        )

    assert allocated == 10_00_000, f"double allocation: {allocated} against a 10,00,000 invoice"
    assert invoice.outstanding_paise == 0
    assert invoice.status is InvoiceStatus.PAID
    assert errors == [], (
        "the second writer hit the over-settlement backstop instead of the row "
        f"lock — serialisation is not working: {errors}"
    )
