"""rls on every tenant table, append-only audit trail

Revision ID: d1a4f7c2e8b6
Revises: 965d9af3e0c7
Create Date: 2026-09-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'd1a4f7c2e8b6'
down_revision = '965d9af3e0c7'
branch_labels = None
depends_on = None

# Every tenant table, frozen from app/models as of this revision rather than
# imported from it: a migration must mean the same thing next year, after the
# models have moved on. `companies` joins the loop below keyed on `id` — without
# a policy one tenant could enumerate every other tenant on the platform.
TENANT_TABLES = (
    'api_keys',
    'audio_assets',
    'audit_log',
    'blackout_dates',
    'buyer_phones',
    'buyers',
    'call_events',
    'caller_ids',
    'calls',
    'campaign_targets',
    'campaigns',
    'case_parties',
    'channel_opt_outs',
    'company_profiles',
    'court_cases',
    'credit_accounts',
    'credit_assessments',
    'credit_notes',
    'entity_candidates',
    'erp_connections',
    'escalation_states',
    'gst_records',
    'import_batches',
    'import_rows',
    'invoice_lines',
    'invoices',
    'legal_histories',
    'legal_links',
    'legal_matters',
    'legal_notices',
    'listing_audit',
    'listing_disputes',
    'listing_evidence',
    'mca_records',
    'message_events',
    'message_templates',
    'messages',
    'payment_allocations',
    'payment_behaviours',
    'payments',
    'prelegal_assessments',
    'promises',
    'provider_fetches',
    'registry_listings',
    'returns',
    'risk_scores',
    'roles',
    'sellers',
    'statements',
    'sync_runs',
    'template_versions',
    'trace_audit',
    'trace_requests',
    'trace_results',
    'users',
    'verification_reports',
)

ROLES = ('crp_app', 'crp_worker')


def _if_role(role: str, sql: str) -> None:
    """Guarded on the role existing: on a managed database a DBA provisions
    roles out of band, and a grant to a missing role would fail the upgrade."""
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
        f"{sql}; "
        "END IF; END $$"
    )


def upgrade() -> None:
    # Until this revision only credit_assessments carried RLS inside a
    # migration; the other 56 tables relied on somebody running
    # `python -m app.db rls` by hand, and "somebody remembers" is not a
    # security control. FORCE matters: without it the owning role bypasses the
    # policy entirely. NULLIF guards the empty string: an unset variable yields
    # NULL, and `company_id = NULL` is NULL rather than true — no rows. Fail
    # closed. Everything here is idempotent, so tables already covered by hand
    # (or by 965d9af3e0c7) simply converge.
    for table in ('companies',) + TENANT_TABLES:
        col = 'id' if table == 'companies' else 'company_id'
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({col} = NULLIF(current_setting('app.company_id', true), '')::uuid) "
            f"WITH CHECK ({col} = NULLIF(current_setting('app.company_id', true), '')::uuid)"
        )
        for role in ROLES:
            _if_role(role, f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")

    for role in ROLES:
        _if_role(role, f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")

    # The audit trail is the defence in a DPDP or defamation complaint, and a
    # trail the application role can rewrite proves nothing. The worker keeps
    # DELETE — deliberately: `admin_session` runs on the worker role (see
    # app/db.py), and the test suite's teardown deletes its own audit rows
    # through it; revoking that DELETE would fail teardown at the ACL check
    # before any trigger runs. UPDATE goes for both: nothing rewrites a row.
    _if_role('crp_app', "REVOKE UPDATE, DELETE ON audit_log FROM crp_app")
    _if_role('crp_worker', "REVOKE UPDATE ON audit_log FROM crp_worker")

    # Grants stop the application roles; the trigger stops everyone else short
    # of the two paths trusted for maintenance — superusers (the DDL role) and
    # BYPASSRLS roles (the worker, which teardown deletes through). The
    # exemption is attribute-based rather than name-based so a renamed role
    # cannot silently gain or lose it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_block_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $fn$
        BEGIN
            IF EXISTS (
                SELECT FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'audit_log_append_only: % on audit_log refused', TG_OP;
        END
        $fn$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute(
        "CREATE TRIGGER audit_log_append_only "
        "BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation()"
    )

    # Read by the auth layer (built separately): tokens minted before this
    # instant are refused. NULL means nothing has ever been revoked.
    op.add_column(
        'users',
        sa.Column('tokens_valid_from', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Policies, grants and revokes deliberately stay in place: a schema
    # rollback must not silently reopen tenant isolation or the audit trail.
    # Only what this revision added structurally is reversed.
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_mutation()")
    op.drop_column('users', 'tokens_valid_from')
