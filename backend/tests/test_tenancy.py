"""Multi-tenancy, access control and audit.

The two tests that matter most are `test_rls_isolation_both_halves` — which
proves isolation is real *today* — and `test_every_tenant_table_has_rls`, which
is what keeps it real after later phases add tables. The second one is the
reason this file will still be doing its job in six months.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
import redis as redis_lib
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import (
    TENANT_SETTING,
    admin_session,
    engine,
    tenant_session,
    tenant_tables,
)
from app.identity import api_keys, audit
from app.identity.auth import (
    Principal,
    create_access_token,
    hash_password,
    principal_from_token,
    request_otp,
    verify_otp,
    verify_password,
)
from app.identity.rbac import Permission, permissions_for
from app.models import ApiKey, AuditLog, Buyer


# --------------------------------------------------------------------- rls core


def test_rls_isolation_both_halves(tenants):
    """Absence is not isolation unless you also show the row exists."""
    # Half one: company A cannot see company B's buyer.
    with tenant_session(tenants.a.company_id) as s:
        found = s.execute(
            select(Buyer).where(Buyer.id == tenants.b.buyer_id)
        ).scalar_one_or_none()
    assert found is None

    # Half two: the row is genuinely there when RLS is bypassed.
    with admin_session() as s:
        actual = s.execute(
            select(Buyer).where(Buyer.id == tenants.b.buyer_id)
        ).scalar_one_or_none()
    assert actual is not None
    assert actual.name == "Buyer B"


def test_tenant_sees_only_its_own_buyers(tenants):
    with tenant_session(tenants.a.company_id) as s:
        ids = {b.id for b in s.execute(select(Buyer)).scalars()}
    assert tenants.a.buyer_id in ids
    assert tenants.b.buyer_id not in ids


def test_pooled_connection_does_not_leak_tenant(tenants):
    """Sequential requests as different tenants on the same pool.

    `SET` without `LOCAL` passes this test only by luck; `set_config(..., true)`
    passes it by construction.
    """
    seen = []
    for _ in range(4):
        for tenant in (tenants.a, tenants.b):
            with tenant_session(tenant.company_id) as s:
                rows = {b.id for b in s.execute(select(Buyer)).scalars()}
            seen.append((tenant.company_id, rows))

    for company_id, rows in seen:
        expected = (
            tenants.a.buyer_id if company_id == tenants.a.company_id else tenants.b.buyer_id
        )
        forbidden = (
            tenants.b.buyer_id if company_id == tenants.a.company_id else tenants.a.buyer_id
        )
        assert expected in rows
        assert forbidden not in rows


def test_tenant_binding_survives_a_commit(tenants):
    """A handler that commits mid-request must still see its own rows afterwards."""
    with tenant_session(tenants.a.company_id) as s:
        assert s.execute(select(Buyer)).scalars().all()
        s.commit()  # ends the transaction, and with it any SET LOCAL
        after = s.execute(select(Buyer)).scalars().all()
    assert after, "tenant binding was lost after commit"


def test_session_without_tenant_sees_nothing(tenants):
    """Fail closed: no tenant set means no rows, not all rows."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        assert s.execute(select(Buyer)).scalars().all() == []
    finally:
        s.close()


def test_cannot_write_a_row_for_another_tenant(tenants):
    """WITH CHECK stops a forged company_id on insert, not just on read."""
    with pytest.raises(Exception):
        with tenant_session(tenants.a.company_id) as s:
            s.add(Buyer(company_id=tenants.b.company_id, name="smuggled"))
            s.flush()

    with admin_session() as s:
        assert (
            s.execute(select(Buyer).where(Buyer.name == "smuggled")).scalar_one_or_none()
            is None
        )


def test_every_tenant_table_has_rls():
    """Enumerated from the metadata, never listed by hand.

    Eight later phases add tables. A human will forget one, and this is what
    catches it before the forgotten table leaks a debtor list.

    Exactly one policy, and by name. Policies on a table are OR-ed together,
    so a stray second policy is a quiet widening, not defence in depth.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "  array_remove(array_agg(p.polname), NULL) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND c.relname = ANY(:names) "
                "GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity"
            ),
            {"names": tenant_tables()},
        ).all()
    report = {name: (enabled, forced, policies) for name, enabled, forced, policies in rows}

    missing = [t for t in tenant_tables() if t not in report]
    assert not missing, f"tables absent from the database: {missing}"

    unprotected = [
        name
        for name, (enabled, forced, policies) in report.items()
        if not (enabled and forced and policies == ["tenant_isolation"])
    ]
    assert not unprotected, f"tables without exactly tenant_isolation enforced: {unprotected}"


def test_companies_table_is_itself_isolated(tenants):
    """Otherwise one tenant can enumerate every customer on the platform."""
    from app.models import Company

    with tenant_session(tenants.a.company_id) as s:
        ids = {c.id for c in s.execute(select(Company)).scalars()}
    assert ids == {tenants.a.company_id}


# ------------------------------------------------------------------- principal


def test_company_id_comes_only_from_the_token(tenants):
    token = create_access_token(
        user_id=tenants.a.user_id, company_id=tenants.a.company_id, roles=["admin"]
    )
    principal = principal_from_token(token)
    assert principal.company_id == tenants.a.company_id

    # There is no code path that accepts a tenant from the caller: Principal is
    # constructed from verified claims, and `tenant_db` reads it from there.
    forged = create_access_token(
        user_id=tenants.a.user_id, company_id=tenants.b.company_id, roles=["admin"]
    )
    assert principal_from_token(forged).company_id == tenants.b.company_id, (
        "a token signed with our secret is trusted — which is why the secret matters"
    )


def test_expired_token_is_rejected(tenants):
    token = create_access_token(
        user_id=tenants.a.user_id,
        company_id=tenants.a.company_id,
        roles=["admin"],
        ttl_minutes=-1,
    )
    with pytest.raises(HTTPException) as exc:
        principal_from_token(token)
    assert exc.value.status_code == 401


def test_tampered_token_is_rejected(tenants):
    token = create_access_token(
        user_id=tenants.a.user_id, company_id=tenants.a.company_id, roles=["admin"]
    )
    head, payload, sig = token.split(".")
    with pytest.raises(HTTPException):
        principal_from_token(f"{head}.{payload}.{sig[:-4]}AAAA")


# ------------------------------------------------------------------------ rbac


def test_viewer_cannot_start_a_campaign():
    assert not Principal(
        user_id=uuid.uuid4(), company_id=uuid.uuid4(), roles=frozenset({"viewer"})
    ).can(Permission.CAMPAIGN_START)


def test_admin_cannot_approve_l3_without_legal_approver():
    """Approving legal content is not an administrative action."""
    admin = Principal(
        user_id=uuid.uuid4(), company_id=uuid.uuid4(), roles=frozenset({"admin"})
    )
    owner = Principal(
        user_id=uuid.uuid4(), company_id=uuid.uuid4(), roles=frozenset({"owner"})
    )
    both = Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        roles=frozenset({"admin", "legal_approver"}),
    )
    assert not admin.can(Permission.TEMPLATE_APPROVE_L3)
    assert not owner.can(Permission.TEMPLATE_APPROVE_L3)
    assert both.can(Permission.TEMPLATE_APPROVE_L3)


def test_unknown_role_grants_nothing():
    """A typo in a role name must not error open."""
    assert permissions_for(["superuser", "root", ""]) == frozenset()


# -------------------------------------------------------------------- api keys


def test_api_key_roundtrip_and_revocation(tenants):
    with admin_session() as s:
        key, full = api_keys.create_api_key(
            s,
            company_id=tenants.a.company_id,
            name="test key",
            scopes=["viewer"],
            created_by=tenants.a.user_id,
        )
        key_id = key.id

    try:
        principal = api_keys.principal_from_api_key(full)
        assert principal.company_id == tenants.a.company_id
        assert principal.via_api_key is True
        assert principal.can(Permission.BUYER_READ)
        assert not principal.can(Permission.CAMPAIGN_START)

        with admin_session() as s:
            api_keys.revoke(s, key_id)

        with pytest.raises(HTTPException) as exc:
            api_keys.principal_from_api_key(full)
        assert exc.value.status_code == 401
    finally:
        with admin_session() as s:
            s.execute(ApiKey.__table__.delete().where(ApiKey.id == key_id))


def test_api_key_secret_is_not_recoverable_from_storage(tenants):
    with admin_session() as s:
        key, full = api_keys.create_api_key(
            s,
            company_id=tenants.a.company_id,
            name="opaque",
            scopes=[],
            created_by=None,
        )
        key_id, stored_hash = key.id, key.key_hash
    try:
        secret = full.split("_", 2)[2]
        assert secret not in stored_hash
        assert full not in stored_hash
    finally:
        with admin_session() as s:
            s.execute(ApiKey.__table__.delete().where(ApiKey.id == key_id))


def test_bad_api_key_is_rejected():
    with pytest.raises(HTTPException):
        api_keys.principal_from_api_key("not-a-key")
    with pytest.raises(HTTPException):
        api_keys.principal_from_api_key("crp_deadbeef_wrongsecret")


# ------------------------------------------------------------------------- otp


@pytest.fixture
def redis_client():
    try:
        client = redis_lib.from_url(settings.redis_url)
        client.ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"redis unavailable: {exc}")
    return client


def test_otp_is_single_use(redis_client):
    phone = f"+9190000{uuid.uuid4().int % 100000:05d}"
    redis_client.delete(f"otp:{phone}", f"otp:rate:{phone}")
    code = request_otp(redis_client, phone)
    assert verify_otp(redis_client, phone, code) is True
    assert verify_otp(redis_client, phone, code) is False


def test_otp_wrong_code_fails_and_burns_the_code(redis_client):
    phone = f"+9190001{uuid.uuid4().int % 100000:05d}"
    redis_client.delete(f"otp:{phone}", f"otp:rate:{phone}")
    code = request_otp(redis_client, phone)
    assert verify_otp(redis_client, phone, "000000") is False
    assert verify_otp(redis_client, phone, code) is False


def test_otp_is_never_stored_in_clear(redis_client):
    phone = f"+9190002{uuid.uuid4().int % 100000:05d}"
    redis_client.delete(f"otp:{phone}", f"otp:rate:{phone}")
    code = request_otp(redis_client, phone)
    stored = redis_client.get(f"otp:{phone}")
    assert stored is not None
    assert code.encode() not in stored


def test_otp_is_rate_limited(redis_client):
    phone = f"+9190003{uuid.uuid4().int % 100000:05d}"
    redis_client.delete(f"otp:{phone}", f"otp:rate:{phone}")
    for _ in range(settings.otp_max_per_hour):
        request_otp(redis_client, phone)
    with pytest.raises(HTTPException) as exc:
        request_otp(redis_client, phone)
    assert exc.value.status_code == 429


# -------------------------------------------------------------------- passwords


def test_password_hashing_roundtrip():
    h = hash_password("correct-horse-battery-staple")
    assert h != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", h)
    assert not verify_password("wrong", h)


def test_overlong_password_is_refused_not_truncated():
    """bcrypt truncates past 72 bytes; silently accepting that is a security bug."""
    with pytest.raises(ValueError):
        hash_password("x" * 73)


# ------------------------------------------------------------------------ audit


def test_audit_rows_are_written_and_tenant_scoped(tenants):
    with tenant_session(tenants.a.company_id) as s:
        audit.record(
            s,
            action=audit.Action.LOGIN,
            company_id=tenants.a.company_id,
            actor_id=tenants.a.user_id,
            ip="203.0.113.7",
        )
        audit.record(
            s,
            action=audit.Action.CAMPAIGN_STARTED,
            company_id=tenants.a.company_id,
            actor_id=tenants.a.user_id,
            entity_type="campaign",
        )
        audit.record(
            s,
            action=audit.Action.TEMPLATE_APPROVED,
            company_id=tenants.a.company_id,
            actor_id=tenants.a.user_id,
            after={"level": "L3"},
        )

    with tenant_session(tenants.a.company_id) as s:
        actions = {a.action for a in s.execute(select(AuditLog)).scalars()}
    assert {
        audit.Action.LOGIN,
        audit.Action.CAMPAIGN_STARTED,
        audit.Action.TEMPLATE_APPROVED,
    } <= actions

    with tenant_session(tenants.b.company_id) as s:
        assert s.execute(select(AuditLog)).scalars().all() == []


def test_audit_log_is_append_only_for_the_application_role(tenants):
    """The audit trail is the defence in a DPDP or defamation complaint; a
    trail the application role can rewrite proves nothing. Refusal comes from
    the database — a revoked grant or the audit_log_append_only trigger — not
    from code discipline."""
    with tenant_session(tenants.a.company_id) as s:
        audit.record(
            s,
            action=audit.Action.LOGIN,
            company_id=tenants.a.company_id,
            actor_id=tenants.a.user_id,
        )

    with pytest.raises(DBAPIError, match="permission denied|audit_log_append_only"):
        with tenant_session(tenants.a.company_id) as s:
            s.execute(update(AuditLog).values(detail="rewritten"))

    with pytest.raises(DBAPIError, match="permission denied|audit_log_append_only"):
        with tenant_session(tenants.a.company_id) as s:
            s.execute(delete(AuditLog))

    # The row survived both attempts — a refusal, not a silent no-op.
    with tenant_session(tenants.a.company_id) as s:
        assert s.execute(select(AuditLog)).scalars().all()


def test_audit_rows_stay_deletable_through_the_admin_path(tenants):
    """conftest teardown removes its audit rows via `admin_session`, which runs
    on the BYPASSRLS worker role — the one mutation path append-only leaves
    open, and only for DELETE. UPDATE is refused even there: nothing may ever
    rewrite a row."""
    with tenant_session(tenants.a.company_id) as s:
        audit.record(
            s,
            action=audit.Action.LOGIN,
            company_id=tenants.a.company_id,
            actor_id=tenants.a.user_id,
        )

    with pytest.raises(DBAPIError, match="permission denied|audit_log_append_only"):
        with admin_session() as s:
            s.execute(
                update(AuditLog)
                .where(AuditLog.company_id == tenants.a.company_id)
                .values(detail="rewritten")
            )

    with admin_session() as s:
        deleted = s.execute(
            delete(AuditLog).where(AuditLog.company_id == tenants.a.company_id)
        ).rowcount
    assert deleted >= 1, "the admin path must still delete, or teardown breaks"
