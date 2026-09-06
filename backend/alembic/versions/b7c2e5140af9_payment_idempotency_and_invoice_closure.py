"""payment idempotency, and why an invoice was closed

Revision ID: b7c2e5140af9
Revises: e4b1c07d9a53
Create Date: 2026-09-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'b7c2e5140af9'
down_revision = 'e4b1c07d9a53'
branch_labels = None
depends_on = None

PAYMENT_REFERENCE_INDEX = 'uq_payment_reference_per_buyer'


def upgrade() -> None:
    # `invoices` and `credit_notes` both carry a uniqueness rule on the number
    # the document was issued under; `payments` carried none at all. So a
    # double-submitted form, a retried request or an ERP replaying a webhook
    # wrote a second payment row and allocated it a second time — and the debtor
    # was then told they owed less than they do, or, once the surplus landed
    # on-account, that they were in credit. The scale of the error is exactly the
    # payment, which in this ledger is the largest single number in play.
    #
    # Partial rather than plain: `reference` is nullable and legitimately so. Cash
    # over a counter has no transaction id, and NULLs are distinct in a unique
    # index anyway, so spelling the exclusion out is documentation rather than
    # behaviour. What it prevents is a future NOT NULL default of '' colliding
    # every cash receipt in the tenant.
    #
    # Scoped to the buyer as well as the tenant: two customers' banks may issue
    # the same UTR-shaped string, and refusing the second one would reject a real
    # payment. It is the same reference *from the same buyer* that is a replay.
    #
    # This will fail loudly on a database that already holds duplicates, which is
    # correct. A duplicate is the bug this index exists to stop, and which of the
    # two rows is the real payment is a question about somebody's money — a
    # person answers it, not a migration.
    op.create_index(
        PAYMENT_REFERENCE_INDEX,
        'payments',
        ['company_id', 'buyer_id', 'reference'],
        unique=True,
        postgresql_where=sa.text('reference IS NOT NULL'),
    )

    # Where a write-off or a cancellation records its reason.
    #
    # Until now the only home for it was the escalation ladder's JSONB history —
    # durable, but on the recovery projection rather than on the ledger, and
    # skipped entirely when the account had no ladder row yet. That made "why did
    # we stop chasing this" unanswerable by query and absent from any export,
    # which is the one question an audit of an abandoned debt begins with.
    #
    # Nullable, with no default and no backfill: invoices closed before this
    # revision genuinely have no recorded reason, and inventing one — even the
    # empty string — would claim a decision was documented when it was not.
    op.add_column('invoices', sa.Column('closure_reason', sa.Text(), nullable=True))
    op.add_column('invoices', sa.Column('closed_by', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_invoices_closed_by_users', 'invoices', 'users', ['closed_by'], ['id']
    )
    op.add_column(
        'invoices', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True)
    )

    # No RLS block, for the reason spelled out in e4b1c07d9a53: this revision
    # creates no table. `payments` and `invoices` already carry ENABLE + FORCE
    # ROW LEVEL SECURITY, the `tenant_isolation` policy and the role grants from
    # d1a4f7c2e8b6. A policy governs rows and grants govern tables, so neither a
    # new column nor a new index changes what either covers — and a second CREATE
    # POLICY here would leave two definitions with no answer to which is
    # authoritative.


def downgrade() -> None:
    op.drop_column('invoices', 'closed_at')
    op.drop_constraint('fk_invoices_closed_by_users', 'invoices', type_='foreignkey')
    op.drop_column('invoices', 'closed_by')
    op.drop_column('invoices', 'closure_reason')
    op.drop_index(PAYMENT_REFERENCE_INDEX, table_name='payments')
