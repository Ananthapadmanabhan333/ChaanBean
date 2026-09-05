"""CSV import: parse, classify, preview, then commit.

The preview is the whole point. An import that mangles 500 debtor records is
discovered three days later, on a call to a stranger — so nothing reaches the
ledger until a human has seen the counts, a sample of each class, and every
rejection with its reason.

Each row keeps its `raw` payload beside the `parsed` result, so a parser fix can
be replayed over history without asking the customer for the file again.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion.normalise import (
    NormalisationError,
    normalise_amount,
    normalise_date,
    normalise_name,
    normalise_phone,
    split_phone_cell,
)
from app.models import (
    Buyer,
    BuyerPhone,
    DndStatus,
    ImportBatch,
    ImportRow,
    Invoice,
    InvoiceStatus,
)

CREATE = "CREATE"
UPDATE = "UPDATE"
UNCHANGED = "UNCHANGED"
REJECT = "REJECT"

# Accepted header spellings. Real files are not tidy.
FIELD_ALIASES = {
    "external_ref": ("external_ref", "party_code", "ledger_code", "customer_id", "code"),
    "name": ("name", "party", "party_name", "customer", "customer_name", "buyer"),
    "phone": ("phone", "mobile", "contact", "phone_number", "contact_number"),
    "email": ("email", "email_id", "e-mail"),
    "language": ("language", "lang"),
    "invoice_number": ("invoice_number", "invoice_no", "bill_no", "voucher_no"),
    "amount": ("amount", "outstanding", "balance", "amount_due", "invoice_amount"),
    "issue_date": ("issue_date", "invoice_date", "bill_date", "date"),
    "due_date": ("due_date", "due", "payable_by"),
}


def _canonical(header: str) -> str | None:
    key = header.strip().lower().replace(" ", "_").replace("-", "_")
    for canonical, aliases in FIELD_ALIASES.items():
        if key in aliases:
            return canonical
    return None


@dataclass
class RowOutcome:
    row_number: int
    raw: dict
    parsed: dict | None
    action: str
    reason: str | None = None
    matched_buyer_id: UUID | None = None
    needs_review: bool = False


@dataclass
class Preview:
    batch_id: UUID
    total: int
    counts: dict[str, int]
    samples: dict[str, list[dict]] = field(default_factory=dict)
    rejections: list[dict] = field(default_factory=list)
    review_rows: list[dict] = field(default_factory=list)
    new_phone_count: int = 0

    @property
    def will_change(self) -> int:
        return self.counts.get(CREATE, 0) + self.counts.get(UPDATE, 0)


# ------------------------------------------------------------------- parsing


def parse_row(raw: dict, *, date_format: str) -> dict:
    """Normalise one row, or raise. No partial rows are ever written."""
    parsed: dict = {}

    name = normalise_name(raw.get("name"))
    parsed["name"] = name.name
    parsed["honorific"] = name.honorific

    phones = []
    for piece in split_phone_cell(raw.get("phone") or ""):
        phone = normalise_phone(piece)
        phones.append({"e164": phone.e164, "number_type": phone.number_type})
    if not phones:
        raise NormalisationError("no usable phone number")
    parsed["phones"] = phones

    parsed["external_ref"] = (raw.get("external_ref") or "").strip() or None
    parsed["email"] = (raw.get("email") or "").strip() or None
    parsed["language"] = (raw.get("language") or "").strip() or "en-IN"

    if raw.get("amount"):
        amount = normalise_amount(raw["amount"])
        if amount < 0:
            raise NormalisationError(f"negative outstanding {raw['amount']!r}")
        parsed["amount_paise"] = amount
        parsed["invoice_number"] = (raw.get("invoice_number") or "").strip() or None
        if not parsed["invoice_number"]:
            raise NormalisationError("an amount was given without an invoice number")
        parsed["due_date"] = normalise_date(raw["due_date"], fmt=date_format).isoformat()
        issue = raw.get("issue_date") or raw.get("due_date")
        parsed["issue_date"] = normalise_date(issue, fmt=date_format).isoformat()

    return parsed


def _classify(session: Session, company_id: UUID, parsed: dict) -> RowOutcome:
    """Decide what this row would do, without doing it."""
    external_ref = parsed.get("external_ref")

    if external_ref:
        buyer = session.execute(
            select(Buyer).where(
                Buyer.company_id == company_id, Buyer.external_ref == external_ref
            )
        ).scalar_one_or_none()
        if buyer is not None:
            changed = buyer.name != parsed["name"] or (
                parsed.get("email") and buyer.email != parsed["email"]
            )
            return RowOutcome(
                0, {}, parsed, UPDATE if changed else UNCHANGED, matched_buyer_id=buyer.id
            )
        return RowOutcome(0, {}, parsed, CREATE)

    # No stable identifier: fall back to name plus phone, and flag rather than
    # merge when that is not decisive. Merging two real debtors is far worse than
    # asking someone to look.
    numbers = [p["e164"] for p in parsed["phones"]]
    matches = list(
        session.execute(
            select(Buyer)
            .join(BuyerPhone, BuyerPhone.buyer_id == Buyer.id)
            .where(
                Buyer.company_id == company_id,
                func.lower(Buyer.name) == parsed["name"].lower(),
                BuyerPhone.e164.in_(numbers),
            )
            .distinct()
        ).scalars()
    )
    if len(matches) == 1:
        buyer = matches[0]
        changed = parsed.get("email") and buyer.email != parsed["email"]
        return RowOutcome(
            0, {}, parsed, UPDATE if changed else UNCHANGED, matched_buyer_id=buyer.id
        )
    if len(matches) > 1:
        return RowOutcome(
            0,
            {},
            parsed,
            REJECT,
            reason=f"ambiguous: {len(matches)} buyers share this name and number",
            needs_review=True,
        )
    return RowOutcome(0, {}, parsed, CREATE)


def stage(
    session: Session,
    *,
    company_id: UUID,
    filename: str,
    content: str,
    date_format: str = "%d/%m/%Y",
    uploaded_by: UUID | None = None,
) -> ImportBatch:
    """Parse and classify every row. Writes `import_rows`, never the ledger."""
    batch = ImportBatch(
        company_id=company_id,
        filename=filename,
        date_format=date_format,
        uploaded_by=uploaded_by,
        status="PENDING",
    )
    session.add(batch)
    session.flush()

    reader = csv.DictReader(io.StringIO(content))
    header_map = {h: _canonical(h) for h in (reader.fieldnames or [])}

    counts = {CREATE: 0, UPDATE: 0, UNCHANGED: 0, REJECT: 0}
    for index, raw_row in enumerate(reader, start=2):  # row 1 is the header
        raw = {k: (v or "").strip() for k, v in raw_row.items() if k}
        mapped = {
            header_map[k]: v for k, v in raw.items() if header_map.get(k) is not None
        }

        try:
            parsed = parse_row(mapped, date_format=date_format)
            outcome = _classify(session, company_id, parsed)
        except NormalisationError as exc:
            outcome = RowOutcome(index, raw, None, REJECT, reason=str(exc))

        counts[outcome.action] += 1
        session.add(
            ImportRow(
                company_id=company_id,
                batch_id=batch.id,
                row_number=index,
                raw=raw,
                parsed=outcome.parsed,
                action=outcome.action,
                reason=outcome.reason,
                matched_buyer_id=outcome.matched_buyer_id,
                needs_review=outcome.needs_review,
            )
        )

    batch.total_rows = sum(counts.values())
    batch.create_count = counts[CREATE]
    batch.update_count = counts[UPDATE]
    batch.unchanged_count = counts[UNCHANGED]
    batch.reject_count = counts[REJECT]
    batch.status = "PREVIEWED"
    session.flush()
    return batch


def preview(session: Session, batch: ImportBatch, *, sample_size: int = 5) -> Preview:
    rows = list(
        session.execute(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id)
            .order_by(ImportRow.row_number)
        ).scalars()
    )

    samples: dict[str, list[dict]] = {}
    rejections: list[dict] = []
    review_rows: list[dict] = []
    new_phones = 0

    for row in rows:
        bucket = samples.setdefault(row.action, [])
        if len(bucket) < sample_size:
            bucket.append({"row": row.row_number, "raw": row.raw, "parsed": row.parsed})
        if row.action == REJECT:
            rejections.append({"row": row.row_number, "reason": row.reason, "raw": row.raw})
        if row.needs_review:
            review_rows.append({"row": row.row_number, "reason": row.reason})
        if row.action in (CREATE, UPDATE) and row.parsed:
            new_phones += len(row.parsed.get("phones", []))

    return Preview(
        batch_id=batch.id,
        total=batch.total_rows,
        counts={
            CREATE: batch.create_count,
            UPDATE: batch.update_count,
            UNCHANGED: batch.unchanged_count,
            REJECT: batch.reject_count,
        },
        samples=samples,
        rejections=rejections,
        review_rows=review_rows,
        # Every imported number starts UNKNOWN, which blocks calling until it is
        # scrubbed. Correct, surprising, and not a bug — so it is said out loud.
        new_phone_count=new_phones,
    )


def commit(session: Session, batch: ImportBatch, *, committed_by: UUID | None = None) -> dict:
    """Apply a previewed batch in one transaction."""
    if batch.status != "PREVIEWED":
        raise ValueError(f"batch is {batch.status}, expected PREVIEWED")

    rows = list(
        session.execute(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id, ImportRow.action.in_([CREATE, UPDATE]))
            .order_by(ImportRow.row_number)
        ).scalars()
    )

    applied = {"buyers_created": 0, "buyers_updated": 0, "phones_added": 0, "invoices": 0}

    for row in rows:
        parsed = row.parsed or {}
        buyer = session.get(Buyer, row.matched_buyer_id) if row.matched_buyer_id else None

        if buyer is None:
            buyer = Buyer(
                company_id=batch.company_id,
                external_ref=parsed.get("external_ref"),
                name=parsed["name"],
                language=parsed.get("language", "en-IN"),
                email=parsed.get("email"),
            )
            session.add(buyer)
            session.flush()
            applied["buyers_created"] += 1
        else:
            buyer.name = parsed["name"]
            if parsed.get("email"):
                buyer.email = parsed["email"]
            applied["buyers_updated"] += 1

        existing = {p.e164 for p in buyer.phones}
        for index, phone in enumerate(parsed.get("phones", [])):
            if phone["e164"] in existing:
                continue
            session.add(
                BuyerPhone(
                    company_id=batch.company_id,
                    buyer_id=buyer.id,
                    e164=phone["e164"],
                    number_type=phone["number_type"],
                    priority=index,
                    # UNKNOWN blocks calling until a scrub clears it. Fail closed.
                    dnd_status=DndStatus.UNKNOWN,
                )
            )
            applied["phones_added"] += 1

        if parsed.get("amount_paise") is not None:
            number = parsed["invoice_number"]
            invoice = session.execute(
                select(Invoice).where(
                    Invoice.company_id == batch.company_id, Invoice.invoice_number == number
                )
            ).scalar_one_or_none()
            if invoice is None:
                session.add(
                    Invoice(
                        company_id=batch.company_id,
                        buyer_id=buyer.id,
                        invoice_number=number,
                        issue_date=normalise_date(parsed["issue_date"], fmt="%Y-%m-%d"),
                        due_date=normalise_date(parsed["due_date"], fmt="%Y-%m-%d"),
                        gross_paise=parsed["amount_paise"],
                        tax_paise=0,
                        net_paise=parsed["amount_paise"],
                        outstanding_paise=parsed["amount_paise"],
                        status=InvoiceStatus.OPEN,
                    )
                )
                applied["invoices"] += 1

    batch.status = "COMMITTED"
    batch.committed_by = committed_by
    batch.committed_at = datetime.now(timezone.utc)
    session.flush()
    return applied
