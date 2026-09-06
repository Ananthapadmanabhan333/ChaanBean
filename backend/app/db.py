"""Database engine, session factory, schema and Row-Level Security.

Schema is created with `create_all` for the MVP. Alembic arrives when the schema
stops moving daily — migrating a schema nobody depends on yet is ceremony.

Tenant isolation is enforced by PostgreSQL, not by `WHERE` clauses. Every table
carrying a `company_id` gets a policy bound to the `app.company_id` session
variable, and the variable is set with `SET LOCAL` inside the request
transaction. `SET` without `LOCAL` on a pooled connection leaks one request's
tenant into the next request that borrows that connection — a cross-tenant leak
waiting for load.

Usage:
    python -m app.db init     create tables and apply RLS
    python -m app.db rls      re-apply RLS only (run after adding tables)
    python -m app.db drop     drop everything (local only, refuses elsewhere)
    python -m app.db check    verify connectivity, list tables and RLS coverage
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

TENANT_SETTING = "app.company_id"

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,  # survives Postgres restarts during local development
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# A second engine for the few jobs that legitimately cross tenants: the ARI event
# consumer resolving a channel id to a call, the orphan reconciler, the seeder.
# Kept separate and named so its use stays explicit and rare — this must never be
# the connection the API serves requests on.
_worker_engine = None
_ddl_engine = None


def worker_engine():
    global _worker_engine
    if _worker_engine is None:
        _worker_engine = create_engine(
            settings.database_url_worker or settings.database_url,
            echo=False,
            pool_pre_ping=True,
            future=True,
        )
    return _worker_engine


def ddl_engine():
    """Superuser connection. Schema creation, roles and grants only."""
    global _ddl_engine
    if _ddl_engine is None:
        _ddl_engine = create_engine(
            settings.database_url_admin, echo=False, pool_pre_ping=True, future=True
        )
    return _ddl_engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope with no tenant set. RLS hides every tenant row.

    Use `tenant_session` for request work and `admin_session` for cross-tenant
    workers. This exists for schema-level work that touches no tenant rows.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@event.listens_for(Session, "after_begin")
def _bind_tenant_on_begin(session: Session, transaction, connection) -> None:
    """Re-assert the tenant at the start of every transaction on this session.

    `SET LOCAL` dies with its transaction, so a handler that commits and then
    issues another query would run the second one with no tenant set — and RLS
    would correctly return nothing, which reads as a baffling empty result. This
    hook makes the binding a property of the session rather than of one
    transaction.
    """
    company_id = session.info.get("company_id")
    if company_id is not None:
        connection.execute(
            text(f"SELECT set_config('{TENANT_SETTING}', :cid, true)"),
            {"cid": str(company_id)},
        )


@contextmanager
def tenant_session(company_id: UUID | str) -> Iterator[Session]:
    """A session scoped to one tenant, across every transaction it opens."""
    session = SessionLocal()
    session.info["company_id"] = str(company_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """Cross-tenant session for background workers. Bypasses RLS.

    Every call site is a deliberate decision to read across tenants. If you are
    reaching for this from request-handling code, you want `tenant_session`.
    """
    factory = sessionmaker(bind=worker_engine(), autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_tenant(session: Session, company_id: UUID | str) -> None:
    """Bind an existing session to one tenant, now and after every commit.

    `SET LOCAL` takes no bind parameters in PostgreSQL, so this goes through
    `set_config(..., is_local => true)`, which does.
    """
    session.info["company_id"] = str(company_id)
    session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :cid, true)"),
        {"cid": str(company_id)},
    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency. The tenant is bound by the auth dependency, never here,
    and never from anything the client sent."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ----------------------------------------------------------------------------- rls


def tenant_tables() -> list[str]:
    """Every mapped table carrying a `company_id`, discovered rather than listed.

    Later phases add tables; a hand-maintained list is a list someone forgets to
    update, and the table they forget is the one that leaks.

    `companies` is included even though it has no `company_id` — it is keyed on
    `id` instead, and without a policy one tenant could enumerate every other
    tenant on the platform.
    """
    names = {t.name for t in Base.metadata.tables.values() if "company_id" in t.c}
    names.add("companies")
    return sorted(names)


def _tenant_column(table: str) -> str:
    return "id" if table == "companies" else "company_id"


def apply_rls(conn: Connection) -> list[str]:
    names = tenant_tables()
    for name in names:
        col = _tenant_column(name)
        # FORCE matters: without it the owning role bypasses the policy, and the
        # owner is exactly the role the application connects as.
        conn.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {name}"))
        # NULLIF guards the empty string: an unset variable yields NULL, and
        # `company_id = NULL` is NULL, which is not true — so no rows. Fail closed.
        conn.execute(
            text(
                f"CREATE POLICY tenant_isolation ON {name} "
                f"USING ({col} = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid) "
                f"WITH CHECK ({col} = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid)"
            )
        )
    return names


def _ensure_role(conn: Connection, role: str, password: str, *, bypass_rls: bool) -> None:
    conn.execute(
        text(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}'; "
            f"END IF; END $$;"
        )
    )
    # Stated explicitly on every run rather than only at creation: an application
    # role that silently acquires BYPASSRLS turns every policy into decoration.
    conn.execute(
        text(f"ALTER ROLE {role} {'BYPASSRLS' if bypass_rls else 'NOBYPASSRLS'} NOSUPERUSER")
    )
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
    )
    # The blanket grant above hands UPDATE and DELETE on audit_log straight
    # back, silently undoing append-only every time this command re-runs. Take
    # them back in the same breath. The BYPASSRLS role keeps DELETE:
    # `admin_session` runs on it, and the test suite's teardown deletes its own
    # audit rows through that path — the audit_log_append_only trigger exempts
    # it for exactly that reason.
    if bypass_rls:
        conn.execute(text(f"REVOKE UPDATE ON audit_log FROM {role}"))
    else:
        conn.execute(text(f"REVOKE UPDATE, DELETE ON audit_log FROM {role}"))
    conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))


def ensure_roles(conn: Connection) -> bool:
    """Provision the application and worker roles.

    Returns False when the connected user lacks the privilege — in a managed
    environment a DBA does this once, and the application should say so rather
    than pretend isolation is configured.
    """
    try:
        _ensure_role(conn, settings.app_db_role, settings.app_db_password, bypass_rls=False)
        _ensure_role(
            conn, settings.worker_db_role, settings.worker_db_password, bypass_rls=True
        )
        return True
    except Exception as exc:  # pragma: no cover - depends on server privileges
        print(f"  ! could not provision roles: {exc}")
        return False


def rls_report(conn: Connection) -> list[tuple[str, bool, bool, int]]:
    """(table, rls_enabled, rls_forced, policy_count) for every tenant table."""
    rows = conn.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "  (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(:names) "
            "ORDER BY c.relname"
        ),
        {"names": tenant_tables()},
    ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# ------------------------------------------------------------------------ commands


def init() -> None:
    Base.metadata.create_all(ddl_engine())
    tables = sorted(inspect(ddl_engine()).get_table_names())
    print(f"created {len(tables)} tables:")
    for t in tables:
        print(f"  - {t}")
    rls()


def rls() -> None:
    with ddl_engine().begin() as conn:
        names = apply_rls(conn)
        ok = ensure_roles(conn)
    print(f"RLS applied to {len(names)} tenant tables")
    print(f"roles: {'provisioned' if ok else 'NOT provisioned'}")


def drop() -> None:
    if settings.env != "local":
        raise SystemExit(f"refusing to drop schema in env={settings.env!r}")
    Base.metadata.drop_all(ddl_engine())
    print("dropped all tables")


def check() -> None:
    with ddl_engine().connect() as conn:
        version = conn.execute(text("select version()")).scalar_one()
        report = rls_report(conn)
        bypass = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = ANY(:names) ORDER BY rolname"
            ),
            {"names": [settings.app_db_role, settings.worker_db_role]},
        ).all()
    tables = sorted(inspect(ddl_engine()).get_table_names())
    print(f"connected: {version.split(',')[0]}")
    print(f"tables ({len(tables)}): {', '.join(tables) if tables else '(none — run init)'}")
    print(f"\ntenant tables ({len(report)}):")
    bad = 0
    for name, enabled, forced, policies in report:
        ok = enabled and forced and policies
        bad += 0 if ok else 1
        print(
            f"  {'ok ' if ok else 'BAD'} {name:<20} "
            f"enabled={enabled} forced={forced} policies={policies}"
        )
    for name in sorted(set(tenant_tables()) - {r[0] for r in report}):
        bad += 1
        print(f"  BAD {name:<20} table not found in database")
    print("\nroles:")
    for name, is_super, bypasses in bypass:
        # The application role must be neither, or every policy above is inert.
        broken = name == settings.app_db_role and (is_super or bypasses)
        bad += 1 if broken else 0
        print(
            f"  {'BAD' if broken else 'ok '} {name:<20} "
            f"superuser={is_super} bypassrls={bypasses}"
        )
    print(f"\n{'all checks passed' if not bad else f'{bad} PROBLEM(S) FOUND'}")


if __name__ == "__main__":
    commands = {"init": init, "rls": rls, "drop": drop, "check": check}
    name = sys.argv[1] if len(sys.argv) > 1 else "check"
    if name not in commands:
        raise SystemExit(f"unknown command {name!r}; expected one of {', '.join(commands)}")
    commands[name]()
