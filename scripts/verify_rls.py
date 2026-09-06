"""Prove the RLS retrofit did what the migration claims.

Written because a migration that runs without error is not the same as a
migration that isolates tenants. The question is not "did the DDL execute" but
"is every tenant table now unreadable across tenants, with FORCE set so the
owning role cannot walk past the policy".
"""

from sqlalchemy import text

from app.db import Base, ddl_engine, tenant_tables

expected = set(tenant_tables())

with ddl_engine().connect() as conn:
    rows = conn.execute(
        text(
            """
            SELECT c.relname,
                   c.relrowsecurity,
                   c.relforcerowsecurity,
                   COALESCE(p.n, 0) AS policies
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN (
                SELECT polrelid, count(*) AS n, array_agg(polname) AS names
                FROM pg_policy GROUP BY polrelid
            ) p ON p.polrelid = c.oid
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
    ).all()

by_name = {r[0]: r for r in rows}
missing = sorted(expected - set(by_name))
bad = []
for name in sorted(expected):
    r = by_name.get(name)
    if r is None:
        continue
    _, rls, force, policies = r
    if not (rls and force and policies == 1):
        bad.append((name, rls, force, policies))

print(f"tenant tables expected : {len(expected)}")
print(f"tables present in db   : {len(by_name)}")
print(f"missing from database  : {missing or 'none'}")
if bad:
    print(f"\nTABLES WITHOUT PROPER RLS ({len(bad)}):")
    for name, rls, force, pol in bad:
        print(f"  {name:34} rls={rls} force={force} policies={pol}")
else:
    print("\nEVERY tenant table: RLS enabled, FORCE set, exactly one policy.")

# The audit trail must be append-only for the role that serves web requests.
with ddl_engine().connect() as conn:
    acl = conn.execute(
        text(
            """
            SELECT grantee, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_name = 'audit_log' AND grantee IN ('crp_app','crp_worker')
            ORDER BY grantee, privilege_type
            """
        )
    ).all()
    trig = conn.execute(
        text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal")
    ).all()

print("\naudit_log grants:", {(g, p) for g, p in acl})
print("audit_log triggers:", [t[0] for t in trig])

app_privs = {p for g, p in acl if g == "crp_app"}
if app_privs & {"UPDATE", "DELETE"}:
    print("  ** crp_app can still mutate the audit trail:", sorted(app_privs & {"UPDATE", "DELETE"}))
else:
    print("  crp_app cannot UPDATE or DELETE the audit trail.")
