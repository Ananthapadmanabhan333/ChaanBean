"""The debt-reduction surface, over HTTP.

What `app.trade.reduction` decides is covered where it lives. What matters here
is that the decision survives the trip out to a client: that the permission
holds, that a refusal arrives as its named enum member rather than a 500, that
the recovery projection moves with the ledger, and that one tenant cannot reach
another's invoice.

Two of these are about the shape of a mistake rather than the shape of a
request. A payment recorded twice used to allocate twice, driving a debtor's
balance below what they had paid; an invoice cancelled with money against it
reads on a statement as though the buyer had overpaid. Both are here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import admin_session
from app.identity.auth import create_access_token
from app.main import app
from app.models import (
    AccountStatus,
    AuditLog,
    CreditAccount,
    EscalationState,
    Invoice,
    InvoiceStatus,
    Payment,
)
from app.trade import accounts as account_ops

# ₹20,00,000. Large enough that the credit notes below stay inside it and that
# the display strings exercise Indian grouping rather than a three-digit number
# that looks the same either way.
INVOICE_PAISE = 200_000_000


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    """The checked-in .env may select Supabase, which rejects our own tokens."""
    monkeypatch.setattr(settings, "auth_backend", "local")


@pytest.fixture
def client():
    return TestClient(app)


def _auth(t, roles=("admin",)):
    token = create_access_token(
        user_id=t.user_id, company_id=t.company_id, roles=list(roles)
    )
    return {"Authorization": f"Bearer {token}"}


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def ledger(tenants):
    """One overdue invoice per tenant, with the recovery projection derived from it.

    Derived rather than written alongside, for the same reason `create_buyer`
    does it that way: there is one route by which an account comes to exist, so
    the ledger and the projection cannot start out disagreeing.
    """
    today = datetime.now(timezone.utc).date()
    with admin_session() as s:
        for label in ("a", "b"):
            t = getattr(tenants, label)
            invoice = Invoice(
                company_id=t.company_id,
                buyer_id=t.buyer_id,
                invoice_number=_uniq("INV"),
                issue_date=today - timedelta(days=60),
                due_date=today - timedelta(days=30),
                gross_paise=INVOICE_PAISE,
                tax_paise=0,
                net_paise=INVOICE_PAISE,
                outstanding_paise=INVOICE_PAISE,
                status=InvoiceStatus.OPEN,
            )
            s.add(invoice)
            s.flush()
            account_ops.sync_account_from_invoice(s, invoice)
            t.invoice_id = invoice.id
            t.invoice_number = invoice.invoice_number
    return tenants


def _invoice(invoice_id):
    with admin_session() as s:
        return s.execute(
            select(Invoice).where(Invoice.id == invoice_id)
        ).scalar_one()


def _account_for(invoice_id):
    with admin_session() as s:
        return s.execute(
            select(CreditAccount).where(CreditAccount.invoice_id == invoice_id)
        ).scalar_one()


# ------------------------------------------------------------------ credit notes


def test_a_credit_note_reduces_the_invoice_and_the_projection(client, ledger):
    res = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": _uniq("CN"),
            # Indian grouping, typed the way a person types it. Twelve lakh
            # thirty-four thousand five hundred and sixty-seven rupees.
            "amount": "12,34,567",
            "issue_date": date.today().isoformat(),
            "reason": "short delivery on two cartons",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["amount_paise"] == 123_456_700
    assert body["amount_display"] == "₹12,34,567"

    remaining = INVOICE_PAISE - 123_456_700
    assert body["invoice_outstanding_paise"] == remaining
    assert body["invoice_outstanding_display"] == "₹7,65,433"

    # The projection is the point of the route, not a tidy-up after it: a credit
    # note that left `credit_accounts` alone would keep the scheduler dialling.
    assert _account_for(ledger.a.invoice_id).outstanding_paise == remaining


def test_a_credit_note_clearing_the_balance_settles_the_account(client, ledger):
    res = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": _uniq("CN"),
            # The lakh spelling reaches `normalise_amount` unchanged: ₹20 lakh.
            "amount": "20 lakh",
            "issue_date": date.today().isoformat(),
            "reason": "order cancelled after invoicing",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["invoice_outstanding_paise"] == 0
    assert _account_for(ledger.a.invoice_id).status is AccountStatus.SETTLED


def test_a_credit_note_beyond_the_balance_is_refused_by_the_excess(client, ledger):
    res = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": _uniq("CN"),
            "amount": "25,00,000",
            "issue_date": date.today().isoformat(),
        },
    )
    assert res.status_code == 409
    # `app.trade.allocation`'s sentence, quoted rather than rephrased, naming the
    # excess in paise.
    assert "50000000" in res.json()["detail"]
    assert _invoice(ledger.a.invoice_id).outstanding_paise == INVOICE_PAISE


def test_a_refused_credit_note_leaves_no_row_behind(client, ledger):
    number = _uniq("CN")
    client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": number,
            "amount": "25,00,000",
            "issue_date": date.today().isoformat(),
        },
    )
    # The same number is free again, which is only true if the rejected note was
    # rolled back rather than left for the next reader to puzzle over.
    ok = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": number,
            "amount": "1,000",
            "issue_date": date.today().isoformat(),
        },
    )
    assert ok.status_code == 201, ok.text


def test_a_credit_note_cannot_reach_another_tenants_invoice(client, ledger):
    """Cross-tenant isolation on the write side of the ledger.

    RLS makes the other tenant's invoice indistinguishable from one that does
    not exist, which is the right answer: a 403 would confirm it exists.
    """
    res = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.b.invoice_id),
            "note_number": _uniq("CN"),
            "amount": "1,000",
            "issue_date": date.today().isoformat(),
        },
    )
    assert res.status_code == 404
    assert _invoice(ledger.b.invoice_id).outstanding_paise == INVOICE_PAISE


def test_issuing_a_credit_note_is_audited(client, ledger):
    number = _uniq("CN")
    client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": number,
            "amount": "1,000",
            "issue_date": date.today().isoformat(),
            "reason": "goodwill",
        },
    )
    with admin_session() as s:
        rows = list(
            s.execute(
                select(AuditLog).where(
                    AuditLog.company_id == ledger.a.company_id,
                    AuditLog.action == "ledger.credit_note_issued",
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].after["note_number"] == number
    assert rows[0].after["amount_paise"] == 100_000


def test_an_operator_cannot_issue_a_credit_note(client, ledger):
    """The permission this route exists to separate.

    An operator may record a payment all day — money that arrived and can be
    checked against a bank statement. A credit note asserts nothing arrived and
    the debt shrank anyway.
    """
    res = client.post(
        "/api/trade/credit-notes",
        headers=_auth(ledger.a, roles=("operator",)),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_id": str(ledger.a.invoice_id),
            "note_number": _uniq("CN"),
            "amount": "1,000",
            "issue_date": date.today().isoformat(),
        },
    )
    assert res.status_code == 403
    assert "ledger:credit" in res.json()["detail"]


def test_an_api_key_cannot_write_off_an_invoice(client, ledger):
    """A permission gate answers whether the act is allowed. It cannot answer
    who performed it, and for the admin-only decisions that second answer is the
    point: `invoices.closed_by` exists to record who abandoned a debt, and a key
    scoped `ledger:write_off` would write NULL into it and nothing would catch
    that."""
    from app.identity.api_keys import create_api_key
    from app.models import ApiKey

    with admin_session() as s:
        key, token = create_api_key(
            s,
            company_id=ledger.a.company_id,
            name="erp-integration",
            scopes=["ledger:write_off"],
            created_by=None,
        )
        key_id = key.id

    try:
        res = client.post(
            f"/api/trade/invoices/{ledger.a.invoice_id}/write-off",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "recovery abandoned"},
        )
        assert res.status_code == 403
        assert "named user" in res.json()["detail"]

        with admin_session() as s:
            invoice = s.get(Invoice, ledger.a.invoice_id)
            assert invoice.status is not InvoiceStatus.WRITTEN_OFF
            assert invoice.closed_by is None
    finally:
        with admin_session() as s:
            s.delete(s.get(ApiKey, key_id))


# ------------------------------------------------------------ payments on account


def _record_payment(client, t, *, amount_paise, reference=None):
    return client.post(
        "/api/trade/payments",
        headers=_auth(t),
        json={
            "buyer_id": str(t.buyer_id),
            "amount_paise": amount_paise,
            "received_date": date.today().isoformat(),
            "reference": reference,
        },
    )


def test_the_same_payment_reference_cannot_be_recorded_twice(client, ledger):
    """The double-submit that used to allocate twice.

    Both `invoices` and `credit_notes` have always carried a uniqueness rule on
    the number they were issued under; `payments` carried none, so a retried
    request settled the same invoice a second time and the debtor was told they
    owed less than they do.
    """
    reference = _uniq("UTR")
    first = _record_payment(client, ledger.a, amount_paise=50_000, reference=reference)
    assert first.status_code == 201, first.text

    second = _record_payment(client, ledger.a, amount_paise=50_000, reference=reference)
    assert second.status_code == 409
    assert reference in second.json()["detail"]

    with admin_session() as s:
        rows = list(
            s.execute(
                select(Payment).where(
                    Payment.buyer_id == ledger.a.buyer_id, Payment.reference == reference
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert _invoice(ledger.a.invoice_id).outstanding_paise == INVOICE_PAISE - 50_000


def test_cash_with_no_reference_can_be_recorded_repeatedly(client, ledger):
    """The partial index has to leave this alone: money over a counter has no
    transaction id, and two ₹500 receipts in a week are two receipts."""
    for _ in range(2):
        res = _record_payment(client, ledger.a, amount_paise=50_000, reference=None)
        assert res.status_code == 201, res.text
    assert _invoice(ledger.a.invoice_id).outstanding_paise == INVOICE_PAISE - 100_000


def test_money_left_on_account_allocates_once_and_then_refuses(client, ledger):
    """The bug this route exists to avoid re-introducing.

    Re-running `apply_payment` would plan against the payment's full amount and
    write a second set of allocations — the invoice over-settled, or settled
    twice across different invoices and the buyer credited with money that was
    never received. Allocating on account plans against what is genuinely left.
    """
    overpaid = INVOICE_PAISE + 100_000
    paid = _record_payment(client, ledger.a, amount_paise=overpaid, reference=_uniq("UTR"))
    assert paid.status_code == 201, paid.text
    payment_id = paid.json()["payment_id"]
    assert paid.json()["on_account_paise"] == 100_000

    # A second invoice for exactly what is sitting on account.
    second = client.post(
        "/api/trade/invoices",
        headers=_auth(ledger.a),
        json={
            "buyer_id": str(ledger.a.buyer_id),
            "invoice_number": _uniq("INV"),
            "issue_date": date.today().isoformat(),
            "due_date": date.today().isoformat(),
            "amount_paise": 100_000,
        },
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]

    applied = client.post(
        f"/api/trade/payments/{payment_id}/allocate", headers=_auth(ledger.a), json={}
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["allocated_paise"] == 100_000
    assert body["on_account_paise"] == 0
    assert _invoice(second_id).outstanding_paise == 0

    again = client.post(
        f"/api/trade/payments/{payment_id}/allocate", headers=_auth(ledger.a), json={}
    )
    assert again.status_code == 409
    assert "NO_UNALLOCATED_BALANCE" in again.json()["detail"]
    # The decisive assertion: the first invoice was not settled a second time.
    assert _invoice(ledger.a.invoice_id).outstanding_paise == 0
    assert _invoice(second_id).outstanding_paise == 0


# ------------------------------------------------------------- closing an invoice


def test_writing_off_stops_the_chase_without_erasing_the_debt(client, ledger):
    """A write-off is this side deciding to stop expecting the money.

    The balance stays, because the sale happened and the buyer's books still
    carry the payable. What changes is the recovery projection.
    """
    res = client.post(
        f"/api/trade/invoices/{ledger.a.invoice_id}/write-off",
        headers=_auth(ledger.a),
        json={"reason": "debtor untraceable since March; two notices returned"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == InvoiceStatus.WRITTEN_OFF.value
    assert body["outstanding_paise"] == INVOICE_PAISE
    assert body["account_status"] == AccountStatus.WRITTEN_OFF.value

    invoice = _invoice(ledger.a.invoice_id)
    # The columns the ladder history could not be trusted to hold: this account
    # has no escalation row at all, so `_stop_ladder` wrote nothing, and without
    # these the reason would have vanished entirely.
    assert invoice.closure_reason.startswith("debtor untraceable")
    assert invoice.closed_by == ledger.a.user_id
    assert invoice.closed_at is not None


def test_a_write_off_reason_of_only_whitespace_is_refused_by_name(client, ledger):
    res = client.post(
        f"/api/trade/invoices/{ledger.a.invoice_id}/write-off",
        headers=_auth(ledger.a),
        json={"reason": "   "},
    )
    assert res.status_code == 409
    assert "REASON_REQUIRED" in res.json()["detail"]
    assert _invoice(ledger.a.invoice_id).status is InvoiceStatus.OPEN


def test_cancelling_a_clean_invoice_voids_it(client, ledger):
    res = client.post(
        f"/api/trade/invoices/{ledger.a.invoice_id}/cancel",
        headers=_auth(ledger.a),
        json={"reason": "raised against the wrong buyer's account code"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == InvoiceStatus.CANCELLED.value
    assert body["outstanding_paise"] == 0
    # WRITTEN_OFF rather than SETTLED: `AccountStatus` has no CANCELLED, and
    # SETTLED would claim a voided invoice had been paid.
    assert body["account_status"] == AccountStatus.WRITTEN_OFF.value


def test_cancelling_an_invoice_with_money_against_it_is_refused(client, ledger):
    """Dropping the debit while the payment stays as a credit reads, on a
    statement already sent, as though the buyer had overpaid."""
    paid = _record_payment(client, ledger.a, amount_paise=100, reference=_uniq("UTR"))
    assert paid.status_code == 201, paid.text

    res = client.post(
        f"/api/trade/invoices/{ledger.a.invoice_id}/cancel",
        headers=_auth(ledger.a),
        json={"reason": "duplicate of last month's invoice"},
    )
    assert res.status_code == 409
    assert "INVOICE_HAS_SETTLEMENTS" in res.json()["detail"]
    assert _invoice(ledger.a.invoice_id).status is not InvoiceStatus.CANCELLED


def test_an_operator_cannot_write_off_or_cancel(client, ledger):
    for action in ("write-off", "cancel"):
        res = client.post(
            f"/api/trade/invoices/{ledger.a.invoice_id}/{action}",
            headers=_auth(ledger.a, roles=("operator",)),
            json={"reason": "no longer worth chasing"},
        )
        assert res.status_code == 403, action
        assert "ledger:write_off" in res.json()["detail"]


def test_one_tenant_cannot_write_off_anothers_invoice(client, ledger):
    """RLS makes another tenant's invoice indistinguishable from a missing one,
    which is the right answer — a 403 would confirm it exists."""
    res = client.post(
        f"/api/trade/invoices/{ledger.a.invoice_id}/write-off",
        headers=_auth(ledger.b),
        json={"reason": "not ours to write off"},
    )
    assert res.status_code == 404
    assert _invoice(ledger.a.invoice_id).status is InvoiceStatus.OPEN


# ------------------------------------------------- disputes and the review queue


def _raise_dispute(client, t, account_id, reason="buyer says goods never arrived"):
    return client.post(
        f"/api/trade/accounts/{account_id}/dispute",
        headers=_auth(t),
        params={"reason": reason},
    )


def test_clearing_a_dispute_is_admin_only_and_leaves_a_permanent_trace(client, ledger):
    """The trace is the point.

    `disputed_reason` is nulled when the dispute ends, so the ladder history
    becomes the only surviving record that the debt was ever contested — and
    `app.registry.eligibility` gates publication on ever-disputed rather than
    currently-disputed. Clear a dispute without a trace and the account
    silently becomes publishable.
    """
    account_id = _account_for(ledger.a.invoice_id).id
    assert _raise_dispute(client, ledger.a, account_id).status_code == 200

    refused = client.post(
        f"/api/trade/accounts/{account_id}/dispute/clear",
        headers=_auth(ledger.a, roles=("operator",)),
        json={"resolution": "buyer withdrew the complaint"},
    )
    assert refused.status_code == 403
    assert "dispute:clear" in refused.json()["detail"]

    res = client.post(
        f"/api/trade/accounts/{account_id}/dispute/clear",
        headers=_auth(ledger.a),
        json={"resolution": "proof of delivery produced; buyer withdrew"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == AccountStatus.OVERDUE.value
    assert res.json()["ever_disputed"] is True

    with admin_session() as s:
        account = s.execute(
            select(CreditAccount).where(CreditAccount.id == account_id)
        ).scalar_one()
        assert account.disputed_reason is None
        assert account_ops.ever_disputed(s, account) is True


def test_clearing_a_dispute_that_is_not_there_is_a_conflict(client, ledger):
    account_id = _account_for(ledger.a.invoice_id).id
    res = client.post(
        f"/api/trade/accounts/{account_id}/dispute/clear",
        headers=_auth(ledger.a),
        json={},
    )
    assert res.status_code == 409


def test_the_review_queue_shows_only_this_tenants_accounts(client, ledger):
    """Cross-tenant isolation on one of the new reads.

    The queue is a join across three tables, which is exactly the shape of query
    where a hand-written company_id predicate gets forgotten on one of them.
    """
    account_id = _account_for(ledger.a.invoice_id).id
    assert _raise_dispute(client, ledger.a, account_id).status_code == 200

    mine = client.get("/api/trade/review-queue", headers=_auth(ledger.a))
    assert mine.status_code == 200, mine.text
    rows = mine.json()
    assert [r["account_id"] for r in rows] == [str(account_id)]
    assert rows[0]["ever_disputed"] is True
    assert rows[0]["last_event"]["event"] == account_ops.DISPUTE_RAISED
    assert rows[0]["outstanding_display"] == "₹20,00,000"

    theirs = client.get("/api/trade/review-queue", headers=_auth(ledger.b))
    assert theirs.status_code == 200
    assert theirs.json() == []


def test_resolving_a_review_is_admin_only_and_refuses_while_disputed(client, ledger):
    account_id = _account_for(ledger.a.invoice_id).id
    assert _raise_dispute(client, ledger.a, account_id).status_code == 200

    # Still disputed: the dispute is the more specific fact and has its own
    # route. Clearing the queue flag underneath it would leave the account
    # contactable according to the queue and blocked according to the ledger.
    blocked = client.post(
        f"/api/trade/review-queue/{account_id}/resolve",
        headers=_auth(ledger.a),
        json={"note": "looked at it"},
    )
    assert blocked.status_code == 409

    client.post(
        f"/api/trade/accounts/{account_id}/dispute/clear",
        headers=_auth(ledger.a),
        json={"resolution": "withdrawn"},
    )

    refused = client.post(
        f"/api/trade/review-queue/{account_id}/resolve",
        headers=_auth(ledger.a, roles=("operator",)),
        json={"note": "clearing the queue"},
    )
    assert refused.status_code == 403
    assert "review:resolve" in refused.json()["detail"]

    res = client.post(
        f"/api/trade/review-queue/{account_id}/resolve",
        headers=_auth(ledger.a),
        json={"note": "spoke to the buyer; safe to resume at L2"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["needs_human_review"] is False

    with admin_session() as s:
        state = s.execute(
            select(EscalationState).where(EscalationState.account_id == account_id)
        ).scalar_one()
    assert state.needs_human_review is False
    # Appended, never replaced: the dispute entries are still there underneath.
    events = [e["event"] for e in state.history]
    assert events == [
        account_ops.DISPUTE_RAISED,
        account_ops.DISPUTE_CLEARED,
        "review_resolved",
    ]
    assert state.history[-1]["note"].startswith("spoke to the buyer")


def test_an_empty_queue_cannot_be_resolved(client, ledger):
    account_id = _account_for(ledger.a.invoice_id).id
    res = client.post(
        f"/api/trade/review-queue/{account_id}/resolve",
        headers=_auth(ledger.a),
        json={"note": "nothing was waiting"},
    )
    assert res.status_code == 409
