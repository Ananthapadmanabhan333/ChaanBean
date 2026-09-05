"""Seed realistic data.

    python -m app.seed

Deliberately includes cases that must be *refused*, not just cases that work:
an unscrubbed number, a stale scrub, a disputed account, a withdrawn consent, a
balance below the L3 floor, and — most importantly — an **unapproved L3
template**. A demo where everything succeeds proves nothing about a system whose
main job is knowing when not to call.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db import admin_session
from app.identity.auth import hash_password
from app.models import (
    AccountStatus,
    BlackoutDate,
    Buyer,
    BuyerPhone,
    CallerId,
    Campaign,
    CampaignStatus,
    Company,
    CreditAccount,
    DndStatus,
    EscalationLevel,
    EscalationState,
    Invoice,
    InvoiceStatus,
    MessageTemplate,
    RoleGrant,
    Seller,
    TemplateVersion,
    User,
)

NOW = datetime.now(timezone.utc)

BUYERS = [
    # (name, dpd, outstanding paise, dnd, scrub age days, status, level, delivered)
    ("Sharma Traders",        45, 4_50_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Verma Steel Works",     92, 12_00_000_00, DndStatus.CLEAR,     2, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Iyer Textiles",         28, 1_75_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Nair Electricals",     120, 28_00_000_00, DndStatus.CLEAR,     3, AccountStatus.OVERDUE, EscalationLevel.L2, 2),
    ("Patel Agro Exports",    64, 8_20_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Khan Hardware",         15, 45_000_00,   DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Reddy Constructions",  180, 55_00_000_00, DndStatus.CLEAR,     2, AccountStatus.OVERDUE, EscalationLevel.L2, 3),
    ("Bose Paper Mills",      38, 3_10_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Gupta Chemicals",       75, 15_60_000_00, DndStatus.CLEAR,     4, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Menon Logistics",       52, 6_40_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Das Packaging",         88, 9_90_000_00, DndStatus.CLEAR,      2, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Rao Instruments",       33, 2_25_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    # --- and the ones that must be refused
    ("Joshi Plastics",        60, 5_00_000_00, DndStatus.UNKNOWN,    1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Kulkarni Foods",        70, 7_00_000_00, DndStatus.REGISTERED, 1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Pillai Marine",         55, 4_00_000_00, DndStatus.CLEAR,     14, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
    ("Chatterjee Motors",     95, 18_00_000_00, DndStatus.CLEAR,     1, AccountStatus.IN_DISPUTE, EscalationLevel.L2, 1),
    ("Ahmed Garments",       110, 22_00_000_00, DndStatus.CLEAR,     1, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Singh Timber",          25, 8_000_00,    DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L2, 1),
    ("Fernandes Tiles",       48, 3_75_000_00, DndStatus.CLEAR,      1, AccountStatus.SETTLED, EscalationLevel.L1, 0),
    ("Bhatt Ceramics",        66, 6_60_000_00, DndStatus.CLEAR,      1, AccountStatus.OVERDUE, EscalationLevel.L1, 0),
]

L1_BODY = (
    "Namaste {buyer_name}. This is a payment reminder from {company_name}. "
    "Invoice {invoice_ref} for {amount_words} is now {days_past_due} days overdue. "
    "Please arrange payment at your earliest convenience."
)
L2_BODY = (
    "Namaste {buyer_name}. This is a formal reminder from {company_name}. "
    "Invoice {invoice_ref} for {amount_words} remains unpaid {days_past_due} days "
    "after its due date. Please contact us to avoid further action."
)
L3_BODY = (
    "Namaste {buyer_name}. This is a final notice from {company_name} regarding "
    "invoice {invoice_ref} for {amount_words}, now {days_past_due} days overdue. "
    "Please settle this account or contact us within seven days."
)


def seed() -> dict:
    rng = random.Random(20260101)
    created = {}

    with admin_session() as s:
        existing = s.execute(
            select(Company).where(Company.name == "Acme Steel Traders")
        ).scalar_one_or_none()
        if existing is not None:
            print("already seeded; drop and re-init to reseed")
            return {"company_id": existing.id}

        company = Company(name="Acme Steel Traders")
        s.add(company)
        s.flush()
        created["company_id"] = company.id

        owner = User(
            company_id=company.id,
            email="owner@acme.test",
            password_hash=hash_password("acme-demo-pass"),
            phone_e164="+919800000001",
        )
        approver = User(
            company_id=company.id,
            email="legal@acme.test",
            password_hash=hash_password("acme-demo-pass"),
            phone_e164="+919800000002",
        )
        s.add_all([owner, approver])
        s.flush()
        # `legal_approver` is separate from `owner` on purpose: approving legal
        # content is not an administrative privilege.
        s.add(RoleGrant(company_id=company.id, user_id=owner.id, role="owner"))
        s.add(RoleGrant(company_id=company.id, user_id=owner.id, role="admin"))
        s.add(RoleGrant(company_id=company.id, user_id=approver.id, role="legal_approver"))
        s.add(RoleGrant(company_id=company.id, user_id=approver.id, role="viewer"))

        s.add(Seller(company_id=company.id, name="Acme Steel Traders", gstin="29AABCA1234A1Z5"))
        s.add(
            CallerId(
                company_id=company.id,
                e164="+918040000000",
                carrier_approved=True,
                is_default=True,
            )
        )
        s.add(
            BlackoutDate(
                company_id=company.id, day=date(2026, 10, 20), label="Diwali"
            )
        )

        # --- templates. L1 and L2 approved; L3 deliberately NOT.
        versions = {}
        for level, body, channel, approved in (
            (EscalationLevel.L1, L1_BODY, "SMS", True),
            (EscalationLevel.L1, L1_BODY, "VOICE", True),
            (EscalationLevel.L2, L2_BODY, "VOICE", True),
            (EscalationLevel.L3, L3_BODY, "VOICE", False),
        ):
            template = MessageTemplate(
                company_id=company.id,
                key=f"{level.value.lower()}_{channel.lower()}",
                level=level,
                channel=channel,
                language="en-IN",
            )
            s.add(template)
            s.flush()
            version = TemplateVersion(
                company_id=company.id,
                template_id=template.id,
                version=1,
                body=body,
                voice_id="Kajal",
                engine="neural",
                dlt_template_id="1107xxxxxxxxxxxxx" if channel == "SMS" else None,
                dlt_approved_at=NOW if channel == "SMS" else None,
                approved_by=approver.id if approved else None,
                approved_by_label="legal@acme.test" if approved else None,
                approved_at=NOW if approved else None,
            )
            s.add(version)
            s.flush()
            template.current_version_id = version.id
            versions[(level, channel)] = version.id

        campaign = Campaign(
            company_id=company.id,
            name="Q1 overdue recovery",
            status=CampaignStatus.DRAFT,
            timezone="Asia/Kolkata",
            max_attempts_per_day=1,
            max_attempts_per_week=3,
            min_hours_between_calls=24,
            channels=["SMS", "VOICE"],
        )
        s.add(campaign)
        s.flush()
        created["campaign_id"] = campaign.id

        for index, (
            name, dpd, outstanding, dnd, scrub_age, status, level, delivered
        ) in enumerate(BUYERS):
            buyer = Buyer(
                company_id=company.id,
                name=name,
                external_ref=f"ACME-{index:03d}",
                language="en-IN",
                email=f"{name.split()[0].lower()}@example.test",
                consent_withdrawn=(name == "Bhatt Ceramics"),
                consent_withdrawn_at=NOW if name == "Bhatt Ceramics" else None,
            )
            s.add(buyer)
            s.flush()

            s.add(
                BuyerPhone(
                    company_id=company.id,
                    buyer_id=buyer.id,
                    e164=f"+9198{rng.randint(10000000, 99999999)}",
                    number_type="mobile",
                    priority=0,
                    dnd_status=dnd,
                    dnd_checked_at=NOW - timedelta(days=scrub_age),
                )
            )

            issue = (NOW - timedelta(days=dpd + 30)).date()
            due = (NOW - timedelta(days=dpd)).date()
            invoice = Invoice(
                company_id=company.id,
                buyer_id=buyer.id,
                invoice_number=f"ACME/2026/{1000 + index}",
                issue_date=issue,
                due_date=due,
                gross_paise=outstanding,
                tax_paise=0,
                net_paise=outstanding,
                outstanding_paise=0 if status is AccountStatus.SETTLED else outstanding,
                status=(
                    InvoiceStatus.PAID
                    if status is AccountStatus.SETTLED
                    else InvoiceStatus.OPEN
                ),
            )
            s.add(invoice)
            s.flush()

            account = CreditAccount(
                company_id=company.id,
                buyer_id=buyer.id,
                invoice_id=invoice.id,
                invoice_ref=invoice.invoice_number,
                outstanding_paise=invoice.outstanding_paise,
                due_date=datetime.combine(due, datetime.min.time(), tzinfo=timezone.utc),
                status=status,
                disputed_reason=(
                    "short delivery claimed" if status is AccountStatus.IN_DISPUTE else None
                ),
            )
            s.add(account)
            s.flush()

            s.add(
                EscalationState(
                    company_id=company.id,
                    account_id=account.id,
                    level=level,
                    level_entered_at=NOW - timedelta(days=min(dpd, 25)),
                    attempts_at_level=3 if level is EscalationLevel.L2 else 0,
                    delivered_at_level=delivered,
                )
            )

    print(f"seeded company {created['company_id']}")
    print(f"  campaign  {created['campaign_id']}")
    print(f"  buyers    {len(BUYERS)}")
    print()
    print("  sign in at http://localhost:8000/")
    print("    owner@acme.test / acme-demo-pass   (operate)")
    print("    legal@acme.test / acme-demo-pass   (approve L3 content)")
    print()
    print("  Deliberately refusable cases are included: an unscrubbed number, a")
    print("  stale scrub, a registered number, a dispute, a withdrawn consent, a")
    print("  balance below the L3 floor, and an UNAPPROVED L3 template.")
    return created


if __name__ == "__main__":
    seed()
