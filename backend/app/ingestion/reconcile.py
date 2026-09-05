"""Incremental sync — where ledgers drift, and what stops them.

The single highest-value rule in this module: **a payment arriving upstream
settles here and immediately halts escalation.** It is what stops you calling
someone at L3 who paid yesterday, which is the worst business bug this product
can commit and the one that most damages trust with a customer who was
cooperating.

Second-highest: **an invoice under active recovery whose amount changes upstream
is flagged, never silently updated.** You may have already told the debtor a
figure, and quietly changing it behind them is how a recovery becomes a dispute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.normalise import NormalisationError, normalise_phone
from app.models import (
    AccountStatus,
    Buyer,
    BuyerPhone,
    DndStatus,
    EscalationLevel,
    Invoice,
    InvoiceStatus,
    Payment,
    ProviderFetch,
)
from app.trade import accounts
from app.trade.allocation import apply_payment


@dataclass
class SyncOutcome:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    flagged: int = 0
    errors: list[dict] = field(default_factory=list)

    def note_error(self, external_id: str, message: str) -> None:
        self.errors.append({"external_id": external_id, "error": message})


def _record_raw(
    session: Session,
    company_id: UUID,
    provider: str,
    resource: str,
    external_id: str | None,
    raw: dict,
    parsed: dict | None = None,
) -> None:
    """Persist the raw payload beside the parsed result (rule 10)."""
    session.add(
        ProviderFetch(
            company_id=company_id,
            provider=provider,
            resource=resource,
            external_id=external_id,
            raw=raw,
            parsed=parsed,
        )
    )


# --------------------------------------------------------------------- parties


def sync_parties(session: Session, company_id: UUID, provider: str, parties) -> SyncOutcome:
    outcome = SyncOutcome()
    for party in parties:
        outcome.fetched += 1
        _record_raw(session, company_id, provider, "party", party.external_id, party.raw)

        buyer = session.execute(
            select(Buyer).where(
                Buyer.company_id == company_id, Buyer.external_ref == party.external_id
            )
        ).scalar_one_or_none()

        if buyer is None:
            buyer = Buyer(
                company_id=company_id,
                external_ref=party.external_id,
                name=party.name,
                email=party.email,
            )
            session.add(buyer)
            session.flush()
            outcome.created += 1
        elif buyer.name != party.name or (party.email and buyer.email != party.email):
            buyer.name = party.name
            if party.email:
                buyer.email = party.email
            outcome.updated += 1
        else:
            outcome.unchanged += 1

        existing = {p.e164 for p in buyer.phones}
        for index, raw_phone in enumerate(party.phones):
            try:
                phone = normalise_phone(raw_phone)
            except NormalisationError as exc:
                outcome.note_error(party.external_id, str(exc))
                continue
            if phone.e164 in existing:
                continue
            session.add(
                BuyerPhone(
                    company_id=company_id,
                    buyer_id=buyer.id,
                    e164=phone.e164,
                    number_type=phone.number_type,
                    priority=index,
                    dnd_status=DndStatus.UNKNOWN,  # blocks calling until scrubbed
                )
            )
    session.flush()
    return outcome


# -------------------------------------------------------------------- invoices


def _buyer_for(session: Session, company_id: UUID, external_id: str) -> Buyer | None:
    return session.execute(
        select(Buyer).where(Buyer.company_id == company_id, Buyer.external_ref == external_id)
    ).scalar_one_or_none()


def _under_active_recovery(session: Session, invoice: Invoice) -> bool:
    """Has this invoice's account been escalated beyond a first courtesy contact?"""
    account = accounts.account_for_invoice(session, invoice.id)
    if account is None:
        return False
    if account.status in (AccountStatus.SETTLED, AccountStatus.WRITTEN_OFF):
        return False
    state = account.escalation
    if state is None:
        return False
    return state.level is not EscalationLevel.L1 or state.attempts_at_level > 0


def sync_invoices(session: Session, company_id: UUID, provider: str, invoices) -> SyncOutcome:
    outcome = SyncOutcome()
    for raw_invoice in invoices:
        outcome.fetched += 1
        _record_raw(
            session,
            company_id,
            provider,
            "invoice",
            raw_invoice.external_id,
            raw_invoice.raw,
            {
                "invoice_number": raw_invoice.invoice_number,
                "amount_paise": raw_invoice.amount_paise,
                "due_date": raw_invoice.due_date.isoformat(),
            },
        )

        buyer = _buyer_for(session, company_id, raw_invoice.party_external_id)
        if buyer is None:
            outcome.note_error(raw_invoice.external_id, "no matching buyer; sync parties first")
            continue

        # Idempotent on the source's own identity plus the tenant, so the same
        # record arriving twice produces one row.
        invoice = session.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.invoice_number == raw_invoice.invoice_number,
            )
        ).scalar_one_or_none()

        if invoice is None:
            invoice = Invoice(
                company_id=company_id,
                buyer_id=buyer.id,
                invoice_number=raw_invoice.invoice_number,
                external_ref=raw_invoice.external_id,
                issue_date=raw_invoice.issue_date,
                due_date=raw_invoice.due_date,
                gross_paise=raw_invoice.amount_paise,
                tax_paise=0,
                net_paise=raw_invoice.amount_paise,
                outstanding_paise=raw_invoice.amount_paise,
            )
            session.add(invoice)
            session.flush()
            accounts.sync_account_from_invoice(session, invoice)
            outcome.created += 1
            continue

        if raw_invoice.voided:
            # Marked, never hard-deleted: there are call recordings referencing it.
            invoice.status = InvoiceStatus.CANCELLED
            invoice.outstanding_paise = 0
            account = accounts.account_for_invoice(session, invoice.id)
            if account is not None:
                account.status = AccountStatus.WRITTEN_OFF
            outcome.updated += 1
            continue

        if invoice.net_paise != raw_invoice.amount_paise:
            if _under_active_recovery(session, invoice):
                # We may already have told this debtor a figure. A human decides.
                account = accounts.account_for_invoice(session, invoice.id)
                if account is not None and account.escalation is not None:
                    account.escalation.needs_human_review = True
                    account.escalation.history = list(account.escalation.history or []) + [
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "event": "upstream_amount_changed",
                            "from_paise": invoice.net_paise,
                            "to_paise": raw_invoice.amount_paise,
                        }
                    ]
                outcome.flagged += 1
                continue
            invoice.gross_paise = raw_invoice.amount_paise
            invoice.net_paise = raw_invoice.amount_paise
            invoice.due_date = raw_invoice.due_date
            from app.trade.allocation import recompute_invoice

            recompute_invoice(session, invoice)
            accounts.sync_account_from_invoice(session, invoice)
            outcome.updated += 1
        else:
            outcome.unchanged += 1

    session.flush()
    return outcome


# -------------------------------------------------------------------- payments


def sync_payments(session: Session, company_id: UUID, provider: str, payments) -> SyncOutcome:
    """Apply upstream payments and stop escalation on anything they settle.

    This is the function that prevents the L3-call-to-someone-who-paid bug.
    """
    outcome = SyncOutcome()
    for raw_payment in payments:
        outcome.fetched += 1
        _record_raw(
            session,
            company_id,
            provider,
            "payment",
            raw_payment.external_id,
            raw_payment.raw,
            {"amount_paise": raw_payment.amount_paise},
        )

        buyer = _buyer_for(session, company_id, raw_payment.party_external_id)
        if buyer is None:
            outcome.note_error(raw_payment.external_id, "no matching buyer")
            continue

        # Idempotent: the same receipt fetched twice must not be banked twice.
        existing = session.execute(
            select(Payment).where(
                Payment.company_id == company_id,
                Payment.external_ref == raw_payment.external_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            outcome.unchanged += 1
            continue

        payment = Payment(
            company_id=company_id,
            buyer_id=buyer.id,
            amount_paise=raw_payment.amount_paise,
            unallocated_paise=raw_payment.amount_paise,
            received_date=raw_payment.received_date,
            reference=raw_payment.reference,
            external_ref=raw_payment.external_id,
        )
        session.add(payment)
        session.flush()

        instructions = None
        if raw_payment.against_invoice_number:
            target = session.execute(
                select(Invoice).where(
                    Invoice.company_id == company_id,
                    Invoice.invoice_number == raw_payment.against_invoice_number,
                )
            ).scalar_one_or_none()
            if target is not None:
                instructions = {
                    target.id: min(raw_payment.amount_paise, target.outstanding_paise)
                }

        plan = apply_payment(session, payment, instructions=instructions)

        # Bring the recovery projection into line straight away. Waiting for a
        # nightly job is waiting long enough to place the call.
        for allocation in plan.allocations:
            invoice = session.get(Invoice, allocation.invoice_id)
            accounts.sync_account_from_invoice(session, invoice)

        outcome.created += 1

    session.flush()
    return outcome


# ------------------------------------------------------------- full comparison


def compare_totals(session: Session, company_id: UUID, source_total_paise: int) -> dict:
    """Report drift against the source. Never silently correct it.

    A reconciliation that quietly fixes differences hides the bug that caused
    them, and the next difference is the one that matters.
    """
    ours = (
        session.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF]),
            )
        )
        .scalars()
        .all()
    )
    our_total = sum(i.outstanding_paise for i in ours)
    return {
        "ours_paise": our_total,
        "source_paise": source_total_paise,
        "drift_paise": our_total - source_total_paise,
        "in_agreement": our_total == source_total_paise,
        "invoice_count": len(ours),
    }
