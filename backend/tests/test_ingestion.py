"""Ingestion.

Two tests carry the weight. `test_indian_digit_grouping` is a wrong-amount bug
that ends up in a legal notice; `test_upstream_payment_halts_escalation` is the
bug that most damages trust with a customer who was already paying.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, admin_session, tenant_session
from app.ingestion import csv_import, reconcile
from app.ingestion.csv_import import CREATE, REJECT, UNCHANGED, UPDATE
from app.ingestion.erp.base import RawInvoice, RawParty, RawPayment
from app.ingestion.erp.tally import TallyConnector, TallyFixtureTransport
from app.ingestion.normalise import (
    NormalisationError,
    normalise_amount,
    normalise_date,
    normalise_name,
    normalise_phone,
    split_phone_cell,
)
from app.models import (
    AccountStatus,
    Buyer,
    BuyerPhone,
    CreditAccount,
    DndStatus,
    EscalationLevel,
    EscalationState,
    ImportBatch,
    ImportRow,
    Invoice,
    Payment,
    PaymentAllocation,
    ProviderFetch,
)
from app.trade import accounts


# ------------------------------------------------------------------ normalise


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("098765-43210", "+919876543210"),
        ("91-9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("044-28123456", "+914428123456"),
    ],
)
def test_phone_formats_normalise(raw, expected):
    assert normalise_phone(raw).e164 == expected


@pytest.mark.parametrize("raw", ["12345", "", "abcdef", "+91 98765 4321", "99"])
def test_invalid_phones_are_rejected_with_a_reason(raw):
    with pytest.raises(NormalisationError) as exc:
        normalise_phone(raw)
    assert str(exc.value)


def test_landline_and_mobile_are_distinguished():
    """A landline is far more likely to be a shared office phone, which is the
    third-party disclosure risk that gates L3 behind a keypress."""
    assert normalise_phone("9876543210").number_type == "mobile"
    assert normalise_phone("044-28123456").number_type == "fixed_line"


def test_two_numbers_in_one_cell_split():
    assert split_phone_cell("9876543210 / 044-28123456") == ["9876543210", "044-28123456"]
    assert len(split_phone_cell("9876543210, 9812345678")) == 2


def test_indian_digit_grouping():
    """4,50,000 is four lakh fifty thousand, not forty-five thousand.

    A parser assuming Western grouping is wrong by an order of magnitude, in a
    number that ends up in a legal notice.
    """
    assert normalise_amount("4,50,000") == 45000000
    assert normalise_amount("Rs. 4,50,000/-") == 45000000
    assert normalise_amount("450,000") == 45000000  # Western grouping, same value
    assert normalise_amount("1,23,45,678") == 1234567800


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("450000.00", 45000000),
        ("4.5 lakhs", 45000000),
        ("1 crore", 1000000000),
        ("(45000)", -4500000),
        ("45000.50", 4500050),
        ("4,50,000 Dr", 45000000),
    ],
)
def test_amount_formats(raw, expected):
    assert normalise_amount(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", "1,2345", "45.123", "Rs. --", None])
def test_unparseable_amounts_reject(raw):
    with pytest.raises(NormalisationError):
        normalise_amount(raw)


def test_dates_need_an_explicit_format():
    """01/02/2026 is ambiguous and no cleverness resolves it."""
    assert normalise_date("01/02/2026", fmt="%d/%m/%Y") == date(2026, 2, 1)
    assert normalise_date("01/02/2026", fmt="%m/%d/%Y") == date(2026, 1, 2)
    with pytest.raises(NormalisationError):
        normalise_date("not a date")


def test_names_keep_their_spelling():
    result = normalise_name("  M/s.   Sharma   Traders ")
    assert result.name == "Sharma Traders"
    assert result.honorific == "M/s"
    # Not "corrected" — a debtor's name is not ours to improve.
    assert normalise_name("Anantha Padmanabhan").name == "Anantha Padmanabhan"


# ----------------------------------------------------------------- csv import


HEADER = "party_code,name,mobile,email,invoice_no,amount,invoice_date,due_date\n"


def _csv(*rows: str) -> str:
    return HEADER + "".join(rows)


@pytest.fixture
def importer(tenants):
    company_id = tenants.a.company_id
    yield company_id
    with admin_session() as s:
        s.execute(delete(ProviderFetch).where(ProviderFetch.company_id == company_id))
        s.execute(delete(PaymentAllocation).where(PaymentAllocation.company_id == company_id))
        s.execute(delete(Payment).where(Payment.company_id == company_id))
        s.execute(delete(ImportRow).where(ImportRow.company_id == company_id))
        s.execute(delete(ImportBatch).where(ImportBatch.company_id == company_id))
        accs = select(CreditAccount.id).where(
            CreditAccount.company_id == company_id, CreditAccount.invoice_id.isnot(None)
        )
        s.execute(delete(EscalationState).where(EscalationState.account_id.in_(accs)))
        s.execute(
            delete(CreditAccount).where(
                CreditAccount.company_id == company_id, CreditAccount.invoice_id.isnot(None)
            )
        )
        s.execute(delete(Invoice).where(Invoice.company_id == company_id))
        # leave the fixture's own buyer alone; drop anything the import created
        s.execute(
            delete(BuyerPhone).where(
                BuyerPhone.company_id == company_id,
                BuyerPhone.buyer_id.in_(
                    select(Buyer.id).where(
                        Buyer.company_id == company_id, Buyer.external_ref.like("IMP-%")
                    )
                ),
            )
        )
        s.execute(
            delete(Buyer).where(
                Buyer.company_id == company_id, Buyer.external_ref.like("IMP-%")
            )
        )


def test_dry_run_classifies_and_commits_nothing(importer):
    content = _csv(
        "IMP-1,Sharma Traders,9876543210,a@x.test,INV-100,4550000,01/01/2026,31/01/2026\n",
        "IMP-2,Verma Steel,9812345678,,INV-101,Rs. 4|50|000,01/01/2026,31/01/2026\n",
    )
    with tenant_session(importer) as s:
        batch = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        result = csv_import.preview(s, batch)
        buyers_after_preview = s.execute(
            select(Buyer).where(Buyer.external_ref.like("IMP-%"))
        ).scalars().all()

    assert result.counts[CREATE] == 1
    assert result.counts[REJECT] == 1
    assert result.rejections[0]["reason"]
    assert buyers_after_preview == [], "preview must not touch the ledger"


def test_commit_matches_the_preview(importer):
    content = _csv(
        "IMP-1,Sharma Traders,9876543210,a@x.test,INV-100,4550000,01/01/2026,31/01/2026\n",
        "IMP-2,Verma Steel,9812345678,,INV-101,4,01/01/2026,31/01/2026\n",
    )
    with tenant_session(importer) as s:
        batch = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        result = csv_import.preview(s, batch)
        applied = csv_import.commit(s, batch)

    assert applied["buyers_created"] == result.counts[CREATE]
    assert applied["invoices"] == 2


def test_imported_numbers_start_unknown_and_block_calling(importer):
    """Correct, surprising, and not to be mistaken for a bug later."""
    content = _csv("IMP-1,Sharma Traders,9876543210,,INV-100,450000,01/01/2026,31/01/2026\n")
    with tenant_session(importer) as s:
        batch = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        csv_import.commit(s, batch)
        phone = s.execute(
            select(BuyerPhone).join(Buyer).where(Buyer.external_ref == "IMP-1")
        ).scalar_one()
        assert phone.dnd_status is DndStatus.UNKNOWN


def test_reimporting_the_same_file_is_all_unchanged(importer):
    content = _csv("IMP-1,Sharma Traders,9876543210,a@x.test,INV-100,450000,01/01/2026,31/01/2026\n")
    with tenant_session(importer) as s:
        first = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        csv_import.commit(s, first)

    with tenant_session(importer) as s:
        second = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        result = csv_import.preview(s, second)

    assert result.counts[UNCHANGED] == 1
    assert result.counts[CREATE] == 0


def test_duplicate_external_ref_updates_rather_than_duplicating(importer):
    with tenant_session(importer) as s:
        first = csv_import.stage(
            s,
            company_id=importer,
            filename="t.csv",
            content=_csv("IMP-1,Sharma Traders,9876543210,,INV-100,450000,01/01/2026,31/01/2026\n"),
        )
        csv_import.commit(s, first)

    with tenant_session(importer) as s:
        second = csv_import.stage(
            s,
            company_id=importer,
            filename="t.csv",
            content=_csv("IMP-1,Sharma Trading Co,9876543210,,INV-100,450000,01/01/2026,31/01/2026\n"),
        )
        result = csv_import.preview(s, second)
        csv_import.commit(s, second)
        buyers = s.execute(select(Buyer).where(Buyer.external_ref == "IMP-1")).scalars().all()

    assert result.counts[UPDATE] == 1
    assert len(buyers) == 1
    assert buyers[0].name == "Sharma Trading Co"


def test_no_partial_row_is_written_for_a_bad_amount(importer):
    content = _csv("IMP-9,Broken Co,9876543210,,INV-999,not-a-number,01/01/2026,31/01/2026\n")
    with tenant_session(importer) as s:
        batch = csv_import.stage(s, company_id=importer, filename="t.csv", content=content)
        csv_import.commit(s, batch)
        assert s.execute(select(Buyer).where(Buyer.external_ref == "IMP-9")).scalar_one_or_none() is None
        row = s.execute(select(ImportRow).where(ImportRow.batch_id == batch.id)).scalar_one()
        assert row.action == REJECT
        assert row.raw, "the raw row is kept even when rejected"


def test_messy_500_row_file_previews_accurately(importer):
    """Mixed phone formats, Indian grouping, duplicates, missing fields."""
    rows = []
    expected_ok = 0
    for n in range(500):
        variant = n % 5
        if variant == 0:
            rows.append(f"IMP-B{n},Buyer {n},98765{n:05d},,INV-B{n},\"4,50,000\",01/01/2026,31/01/2026\n")
            expected_ok += 1
        elif variant == 1:
            rows.append(f"IMP-B{n},Buyer {n},+91 98765 {n:05d},,INV-B{n},Rs. 12500/-,01/01/2026,31/01/2026\n")
            expected_ok += 1
        elif variant == 2:
            rows.append(f"IMP-B{n},M/s. Buyer {n},098765-{n:05d},,INV-B{n},4.5 lakhs,01/01/2026,31/01/2026\n")
            expected_ok += 1
        elif variant == 3:
            rows.append(f"IMP-B{n},Buyer {n},12345,,INV-B{n},450000,01/01/2026,31/01/2026\n")  # bad phone
        else:
            rows.append(f"IMP-B{n},Buyer {n},98765{n:05d},,INV-B{n},garbage,01/01/2026,31/01/2026\n")  # bad amount

    with tenant_session(importer) as s:
        batch = csv_import.stage(
            s, company_id=importer, filename="messy.csv", content=_csv(*rows)
        )
        result = csv_import.preview(s, batch)
        applied = csv_import.commit(s, batch)

    assert result.total == 500
    assert result.counts[CREATE] == expected_ok == 300
    assert result.counts[REJECT] == 200
    assert applied["buyers_created"] == result.counts[CREATE], "commit must match the preview"


# ------------------------------------------------------------------ erp sync


TALLY_PARTIES = """<ENVELOPE><BODY><DATA><TALLYMESSAGE>
<LEDGER NAME="Sharma Traders" GUID="ldg-1"><LEDGERMOBILE>9876543210</LEDGERMOBILE>
<EMAIL>sharma@x.test</EMAIL></LEDGER>
<LEDGER NAME="Verma Steel" GUID="ldg-2"><LEDGERMOBILE>9812345678</LEDGERMOBILE></LEDGER>
</TALLYMESSAGE></DATA></BODY></ENVELOPE>"""

TALLY_SALES = """<ENVELOPE><BODY><DATA><TALLYMESSAGE>
<VOUCHER GUID="vch-1"><VOUCHERNUMBER>INV-T1</VOUCHERNUMBER>
<PARTYLEDGERNAME>ldg-1</PARTYLEDGERNAME><AMOUNT>-450000</AMOUNT>
<DATE>20260101</DATE><DUEDATE>20260131</DUEDATE></VOUCHER>
</TALLYMESSAGE></DATA></BODY></ENVELOPE>"""

TALLY_RECEIPTS = """<ENVELOPE><BODY><DATA><TALLYMESSAGE>
<VOUCHER GUID="rcp-1"><VOUCHERNUMBER>RCP-1</VOUCHERNUMBER>
<PARTYLEDGERNAME>ldg-1</PARTYLEDGERNAME><AMOUNT>450000</AMOUNT>
<DATE>20260215</DATE><REFERENCE>NEFT-99</REFERENCE></VOUCHER>
</TALLYMESSAGE></DATA></BODY></ENVELOPE>"""


def _tally() -> TallyConnector:
    return TallyConnector(
        TallyFixtureTransport(
            {
                "List of Accounts": TALLY_PARTIES,
                "Sales Register": TALLY_SALES,
                "Receipts Register": TALLY_RECEIPTS,
                "List of Companies": "<ENVELOPE><OK/></ENVELOPE>",
            }
        )
    )


def test_tally_connector_parses_a_full_sync(importer):
    connector = _tally()
    assert connector.test_connection().ok

    parties = list(connector.fetch_parties(None))
    invoices = list(connector.fetch_invoices(None))
    payments = list(connector.fetch_payments(None))

    assert [p.name for p in parties] == ["Sharma Traders", "Verma Steel"]
    assert invoices[0].amount_paise == 45000000  # Tally signs sales negative
    assert invoices[0].due_date == date(2026, 1, 31)
    assert payments[0].reference == "NEFT-99"


def test_tally_xml_is_parsed_defensively():
    """External XML from a machine we do not control."""
    connector = TallyConnector(TallyFixtureTransport({"List of Accounts": "<not xml"}))
    with pytest.raises(NormalisationError):
        list(connector.fetch_parties(None))


def test_raw_payloads_are_persisted_and_reparse_identically(importer):
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        fetches = s.execute(
            select(ProviderFetch).where(ProviderFetch.resource == "party")
        ).scalars().all()

    assert len(fetches) == 2
    # Re-parsing from the stored raw reproduces the same normalised value.
    stored = {f.raw["GUID"]: f.raw for f in fetches}
    assert normalise_phone(stored["ldg-1"]["LEDGERMOBILE"]).e164 == "+919876543210"


def test_sync_is_idempotent(importer):
    connector = _tally()
    with tenant_session(importer) as s:
        first = reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
    with tenant_session(importer) as s:
        second = reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))

    assert first.created == 2
    assert second.created == 0
    assert second.unchanged == 2


def test_concurrent_syncs_do_not_double_create(importer):
    """Two overlapping sync runs is the normal case, not an exotic race."""
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def run():
        session = SessionLocal()
        session.info["company_id"] = str(importer)
        try:
            connector = _tally()
            parties = list(connector.fetch_parties(None))
            barrier.wait(timeout=10)
            reconcile.sync_parties(session, importer, "tally", parties)
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    with tenant_session(importer) as s:
        buyers = s.execute(
            select(Buyer).where(Buyer.external_ref.in_(["ldg-1", "ldg-2"]))
        ).scalars().all()

    # The unique constraint on (company_id, external_ref) is what makes this safe:
    # one run wins, the other fails loudly rather than duplicating a debtor.
    assert len(buyers) == 2, f"double-created buyers: {[b.external_ref for b in buyers]}"


def test_upstream_payment_halts_escalation(importer):
    """The single highest-value line in this phase.

    A debtor who paid yesterday must not be called at L3 today.
    """
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))

        invoice = s.execute(select(Invoice).where(Invoice.invoice_number == "INV-T1")).scalar_one()
        account = accounts.account_for_invoice(s, invoice.id)
        state = accounts.ensure_escalation_state(s, account)
        state.level = EscalationLevel.L2
        state.attempts_at_level = 2
        state.delivered_at_level = 1
        s.flush()

        # The payment lands upstream.
        reconcile.sync_payments(s, importer, "tally", connector.fetch_payments(None))

        s.refresh(invoice)
        account = accounts.account_for_invoice(s, invoice.id)

    assert invoice.outstanding_paise == 0
    assert account.status is AccountStatus.SETTLED, (
        "a settled account is what stops the scheduler placing the call"
    )


def test_amount_change_under_active_recovery_flags_rather_than_updates(importer):
    """You may already have told the debtor a figure."""
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))

        invoice = s.execute(select(Invoice).where(Invoice.invoice_number == "INV-T1")).scalar_one()
        account = accounts.account_for_invoice(s, invoice.id)
        state = accounts.ensure_escalation_state(s, account)
        state.level = EscalationLevel.L2
        state.attempts_at_level = 1
        s.flush()

        changed = RawInvoice(
            external_id="vch-1",
            invoice_number="INV-T1",
            party_external_id="ldg-1",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
            amount_paise=99900000,
            raw={"AMOUNT": "-999000"},
        )
        outcome = reconcile.sync_invoices(s, importer, "tally", [changed])

        s.refresh(invoice)
        s.refresh(state)

    assert outcome.flagged == 1
    assert outcome.updated == 0
    assert invoice.net_paise == 45000000, "the amount must not have moved under recovery"
    assert state.needs_human_review is True


def test_amount_change_before_recovery_applies_cleanly(importer):
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))

        changed = RawInvoice(
            external_id="vch-1",
            invoice_number="INV-T1",
            party_external_id="ldg-1",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
            amount_paise=50000000,
            raw={},
        )
        outcome = reconcile.sync_invoices(s, importer, "tally", [changed])
        invoice = s.execute(select(Invoice).where(Invoice.invoice_number == "INV-T1")).scalar_one()

    assert outcome.updated == 1
    assert invoice.net_paise == 50000000


def test_voided_invoice_is_marked_not_deleted(importer):
    """There are call recordings referencing it."""
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))

        voided = RawInvoice(
            external_id="vch-1",
            invoice_number="INV-T1",
            party_external_id="ldg-1",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
            amount_paise=45000000,
            voided=True,
            raw={},
        )
        reconcile.sync_invoices(s, importer, "tally", [voided])
        invoice = s.execute(select(Invoice).where(Invoice.invoice_number == "INV-T1")).scalar_one()

    assert invoice is not None
    assert invoice.status.value == "CANCELLED"
    assert invoice.outstanding_paise == 0


def test_duplicate_payment_is_not_banked_twice(importer):
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))
        first = reconcile.sync_payments(s, importer, "tally", connector.fetch_payments(None))
        second = reconcile.sync_payments(s, importer, "tally", connector.fetch_payments(None))
        payments = s.execute(select(Payment).where(Payment.external_ref == "rcp-1")).scalars().all()

    assert first.created == 1
    assert second.created == 0
    assert second.unchanged == 1
    assert len(payments) == 1


def test_drift_is_reported_not_silently_corrected(importer):
    connector = _tally()
    with tenant_session(importer) as s:
        reconcile.sync_parties(s, importer, "tally", connector.fetch_parties(None))
        reconcile.sync_invoices(s, importer, "tally", connector.fetch_invoices(None))
        report = reconcile.compare_totals(s, importer, source_total_paise=44000000)
        after = s.execute(select(Invoice).where(Invoice.invoice_number == "INV-T1")).scalar_one()

    assert report["in_agreement"] is False
    assert report["drift_paise"] == 1000000
    assert after.outstanding_paise == 45000000, "reconciliation must not edit the ledger"


# ------------------------------------------------------------------ zoho books


def _zoho():
    from app.ingestion.erp.zoho import ZohoConnector, ZohoFixtureTransport

    return ZohoConnector(
        ZohoFixtureTransport(
            {
                "/contacts": [
                    {
                        "contact_id": 4001,
                        "contact_name": "Sharma Traders",
                        "mobile": "9876543210",
                        "email": "sharma@x.test",
                    }
                ],
                "/invoices": [
                    {
                        "invoice_id": 5001,
                        "invoice_number": "INV-Z1",
                        "customer_id": 4001,
                        "date": "2026-01-01",
                        "due_date": "2026-01-31",
                        "total": 4500.55,
                        "status": "sent",
                    }
                ],
                "/customerpayments": [
                    {
                        "payment_id": 6001,
                        "customer_id": 4001,
                        "date": "2026-02-15",
                        "amount": 4500.55,
                        "reference_number": "NEFT-Z",
                        "invoices": [{"invoice_number": "INV-Z1"}],
                    }
                ],
            }
        )
    )


def test_zoho_connector_parses_a_sync():
    connector = _zoho()
    assert connector.test_connection().ok

    parties = list(connector.fetch_parties(None))
    invoices = list(connector.fetch_invoices(None))
    payments = list(connector.fetch_payments(None))

    assert parties[0].name == "Sharma Traders"
    assert invoices[0].invoice_number == "INV-Z1"
    assert payments[0].against_invoice_number == "INV-Z1"


def test_zoho_float_rupees_become_integer_paise():
    """A float rupee value that reaches the ledger is a rounding error in a
    legal notice. It is converted once, at the boundary."""
    from app.ingestion.erp.zoho import rupees_to_paise

    assert rupees_to_paise(4500.55) == 450055
    assert rupees_to_paise(0.1 + 0.2) == 30  # float noise rounded, not truncated
    assert rupees_to_paise(None) == 0
    assert isinstance(list(_zoho().fetch_invoices(None))[0].amount_paise, int)


def test_zoho_pages_where_tally_does_not():
    """The interface yields rather than returning a list because of this."""
    from app.ingestion.erp.zoho import PAGE_SIZE, ZohoConnector, ZohoFixtureTransport

    rows = [
        {
            "invoice_id": n,
            "invoice_number": f"INV-P{n}",
            "customer_id": 1,
            "date": "2026-01-01",
            "due_date": "2026-01-31",
            "total": 100.0,
        }
        for n in range(PAGE_SIZE + 30)
    ]
    transport = ZohoFixtureTransport({"/invoices": rows})
    fetched = list(ZohoConnector(transport).fetch_invoices(None))

    assert len(fetched) == PAGE_SIZE + 30
    assert len(transport.requests) == 2, "a second page must actually be requested"


def test_zoho_and_tally_satisfy_the_same_interface():
    """The point of building two: an interface designed against one integration
    is a guess."""
    from app.ingestion.erp.base import ErpConnector

    assert isinstance(_zoho(), ErpConnector)
    assert isinstance(_tally(), ErpConnector)


def test_voided_zoho_invoice_is_flagged():
    from app.ingestion.erp.zoho import ZohoConnector, ZohoFixtureTransport

    connector = ZohoConnector(
        ZohoFixtureTransport(
            {
                "/invoices": [
                    {
                        "invoice_id": 1,
                        "invoice_number": "INV-V",
                        "customer_id": 1,
                        "date": "2026-01-01",
                        "due_date": "2026-01-31",
                        "total": 100.0,
                        "status": "void",
                    }
                ]
            }
        )
    )
    assert list(connector.fetch_invoices(None))[0].voided is True
