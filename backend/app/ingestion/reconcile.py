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

from app.ingestion import csv_import
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
from app.trade import CLOSED_ACCOUNT_STATUSES, UNCOLLECTABLE_STATUSES, accounts, reduction
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
            # The same overwrite the CSV path performs, and audited for the same
            # reason: this is the second writer of the fields a data principal
            # corrects through `PATCH /trade/buyers/{id}`, and an unrecorded
            # revert of a correction is the one thing that route's evidence
            # cannot survive.
            csv_import.overwrite_contact(
                session,
                buyer,
                name=party.name,
                email=party.email,
                company_id=company_id,
                actor_id=None,
                actor_label=f"erp sync ({provider})",
            )
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
    if account.status in CLOSED_ACCOUNT_STATUSES:
        return False
    state = account.escalation
    if state is None:
        return False
    return state.level is not EscalationLevel.L1 or state.attempts_at_level > 0


def _flag_for_review(session: Session, invoice: Invoice, entry: dict) -> None:
    """Put one invoice's account in front of a person, with why written down.

    The ladder row is created if it is missing: an invoice can arrive already
    voided upstream, before anything has written a ladder position, and a review
    queue that silently drops those is the queue not existing.
    """
    account = accounts.account_for_invoice(session, invoice.id)
    if account is None:
        return
    state = accounts.ensure_escalation_state(session, account)
    state.needs_human_review = True
    state.history = list(state.history or []) + [
        {"at": datetime.now(timezone.utc).isoformat(), **entry}
    ]


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
            #
            # Through `reduction.cancel_invoice` rather than by hand, because the
            # rules for voiding a debt live there: the balance goes through its
            # single writer, the recovery projection follows, and the ladder
            # records that it stopped. Voiding an invoice that has money against
            # it is refused there for a reason a sync cannot overrule — dropping
            # the debit while the payment stays as a credit leaves the buyer's
            # balance short by the invoice, reading on a statement already sent
            # as though they had overpaid. Upstream saying otherwise is exactly
            # the case a person has to resolve.
            if invoice.status is InvoiceStatus.CANCELLED:
                outcome.unchanged += 1
                continue
            try:
                reduction.cancel_invoice(
                    session, invoice, reason=f"voided upstream in {provider}"
                )
            except reduction.ReductionError as exc:
                _flag_for_review(
                    session,
                    invoice,
                    {
                        "event": "upstream_void_refused",
                        "refusal": exc.refusal.value,
                        "detail": exc.detail,
                    },
                )
                outcome.note_error(raw_invoice.external_id, str(exc))
                outcome.flagged += 1
                continue
            outcome.updated += 1
            continue

        if invoice.net_paise != raw_invoice.amount_paise:
            if _under_active_recovery(session, invoice):
                # We may already have told this debtor a figure. A human decides.
                _flag_for_review(
                    session,
                    invoice,
                    {
                        "event": "upstream_amount_changed",
                        "from_paise": invoice.net_paise,
                        "to_paise": raw_invoice.amount_paise,
                    },
                )
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
                Invoice.status.notin_(UNCOLLECTABLE_STATUSES),
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
