"""declared gstin/cin on buyers, provenance on registry records

Revision ID: e4b1c07d9a53
Revises: d1a4f7c2e8b6
Create Date: 2026-09-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'e4b1c07d9a53'
down_revision = 'd1a4f7c2e8b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # What the creditor typed off an invoice, and nothing more. Nullable and
    # unconstrained on purpose: most buyers arrive from a CSV with neither, and a
    # NOT NULL here would push people into typing *something* to get past the
    # form — the worst possible outcome for a pair of columns whose only job is
    # to record honestly what was declared.
    #
    # No unique index either. Two buyer rows in one tenant may legitimately carry
    # the same GSTIN — a head office and a branch keyed separately in the
    # creditor's ERP — and uniqueness would reject the truth.
    op.add_column('buyers', sa.Column('gstin', sa.String(length=15), nullable=True))
    op.add_column('buyers', sa.Column('cin', sa.String(length=21), nullable=True))

    # No RLS block here, unlike 965d9af3e0c7 which created a table. `buyers`
    # already has ENABLE + FORCE ROW LEVEL SECURITY and the `tenant_isolation`
    # policy from d1a4f7c2e8b6, and a policy governs rows, not columns — adding a
    # column to a protected table leaves it protected. The grants are likewise
    # table-level and crp_app/crp_worker already hold them. A second CREATE
    # POLICY here would at best be a no-op and at worst drift from the one in
    # d1a4f7c2e8b6, leaving two definitions and no answer to which is authoritative.

    # gst_records and mca_records are append-only evidence: a re-fetch writes a
    # new row with a new fetched_at rather than overwriting. Until now those rows
    # could not say where they came from, so a record fetched from the register
    # and a record a person read off a portal screen and typed in were
    # indistinguishable a year later — which is how self-declared data ends up
    # carrying the weight of a verified fact.
    #
    # Both the column default and the backfill are USER_PROVIDED rather than
    # REGISTRY. Every existing row was written before anyone recorded a source,
    # so the honest label is the weaker one; understating provenance costs a
    # re-verification, while overstating it publishes a claim about a company
    # nobody checked.
    for table in ('gst_records', 'mca_records'):
        op.add_column(
            table,
            sa.Column(
                'provenance',
                sa.String(length=16),
                nullable=False,
                server_default='USER_PROVIDED',
            ),
        )


def downgrade() -> None:
    for table in ('gst_records', 'mca_records'):
        op.drop_column(table, 'provenance')
    op.drop_column('buyers', 'cin')
    op.drop_column('buyers', 'gstin')
