"""Identity hardening.

Four defects, each with the attack it closes:

* invitation squatting — an invite used to pre-create a user row keyed on an
  email string, and the first Supabase sign-in with that email captured it. An
  admin of tenant A could type a stranger's address and own their account.
  Invites are now signed, expiring tokens; the row is created at acceptance,
  bound to a verified credential.
* revocation — local JWTs lived their full 12 hours no matter what. Tokens
  whose `iat` predates `users.tokens_valid_from` are now refused.
* unscoped email lookups — emails are unique per (company_id, email), so a
  global `scalar_one_or_none` on email was a 500 waiting for the second tenant
  to reuse an address. Login now lets the password pick the account.
* API-key scopes — an unknown scope was stored silently and granted nothing;
  it is now refused at creation, by name.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import admin_session
from app.identity import api_keys, supabase
from app.identity.auth import (
    ALGORITHM,
    create_access_token,
    create_invite_token,
    hash_password,
)
from app.identity.rbac import Permission
from app.main import app
from app.models import ApiKey, User

SUPABASE_SECRET = "test-supabase-shared-secret"
PROJECT = "https://testproject.supabase.co"


@pytest.fixture
def client(monkeypatch):
    """Local-token API client. The checked-in .env may select Supabase, so the
    backend is pinned rather than inherited (same pattern as
    test_buyers_and_profile)."""
    monkeypatch.setattr(settings, "auth_backend", "local")
    return TestClient(app)


@pytest.fixture
def supabase_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", PROJECT)
    monkeypatch.setattr(settings, "supabase_jwt_secret", SUPABASE_SECRET)
    supabase._jwks.reset()
    yield
    supabase._jwks.reset()


def _supabase_token(*, sub: str | None = None, email: str = "user@example.test") -> str:
    """A token shaped exactly like Supabase's, signed with the shared secret."""
    now = int(time.time())
    claims = {
        "sub": sub or str(uuid.uuid4()),
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "iat": now,
        "exp": now + 3600,
        "app_metadata": {"provider": "email"},
        "user_metadata": {},
    }
    return jwt.encode(claims, SUPABASE_SECRET, algorithm="HS256")


def _auth(t, roles=("admin",)) -> dict:
    token = create_access_token(
        user_id=t.user_id, company_id=t.company_id, roles=list(roles)
    )
    return {"Authorization": f"Bearer {token}"}


def _stale_token(user_id, company_id, *, seconds_ago: int = 300) -> str:
    """A local access token minted in the past — valid until revocation bites."""
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "company_id": str(company_id),
        "roles": ["admin"],
        "type": "access",
        "iat": now - seconds_ago,
        "exp": now + 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def _uniq_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.test"


# ------------------------------------------------------------------- invites


def test_invite_returns_a_signed_token_and_creates_no_user_row(client, tenants):
    email = _uniq_email("colleague")
    res = client.post(
        "/api/auth/invite",
        headers=_auth(tenants.a),
        json={"email": email, "roles": ["operator"]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["invite_token"]
    # Roughly seven days of validity, and no row anywhere until acceptance.
    expires = datetime.fromisoformat(body["expires_at"])
    assert 6 <= (expires - datetime.now(timezone.utc)).days <= 7
    with admin_session() as s:
        assert (
            s.execute(select(User).where(User.email == email)).scalars().first() is None
        )


def test_invite_requires_user_manage(client, tenants):
    res = client.post(
        "/api/auth/invite",
        headers=_auth(tenants.a, roles=("viewer",)),
        json={"email": _uniq_email("x")},
    )
    assert res.status_code == 403


def test_inviting_an_email_already_in_the_company_is_409(client, tenants):
    res = client.post(
        "/api/auth/invite",
        headers=_auth(tenants.a),
        json={"email": tenants.a.user_email},
    )
    assert res.status_code == 409


def test_accept_invite_local_mode_creates_the_user_with_a_password(client, tenants):
    email = _uniq_email("localjoin")
    invited = client.post(
        "/api/auth/invite",
        headers=_auth(tenants.a),
        json={"email": email, "roles": ["operator"]},
    ).json()

    res = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invited["invite_token"], "password": "correct-horse"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["company_id"] == str(tenants.a.company_id)
    assert body["roles"] == ["operator"]

    login = client.post(
        "/api/auth/login", json={"email": email, "password": "correct-horse"}
    )
    assert login.status_code == 200, login.text
    assert login.json()["company_id"] == str(tenants.a.company_id)
    assert login.json()["roles"] == ["operator"]


def test_accept_invite_local_mode_requires_a_password(client, tenants):
    token, _ = create_invite_token(
        email=_uniq_email("nopass"), company_id=tenants.a.company_id, roles=["viewer"]
    )
    res = client.post("/api/auth/accept-invite", json={"invite_token": token})
    assert res.status_code == 400


def test_accept_invite_supabase_mode_links_the_verified_subject(tenants, supabase_mode):
    email = _uniq_email("sbjoin")
    subject = str(uuid.uuid4())
    token, _ = create_invite_token(
        email=email, company_id=tenants.a.company_id, roles=["viewer"]
    )

    res = TestClient(app).post(
        "/api/auth/accept-invite",
        json={"invite_token": token},
        headers={"Authorization": f"Bearer {_supabase_token(sub=subject, email=email)}"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["company_id"] == str(tenants.a.company_id)

    # Company and roles came from the invite; the subject from the verified token.
    with admin_session() as s:
        user = s.execute(
            select(User).where(User.external_auth_id == subject)
        ).scalar_one()
        assert user.company_id == tenants.a.company_id
        assert user.email == email
        assert user.roles == frozenset({"viewer"})

    principal = supabase.principal_from_supabase_token(
        _supabase_token(sub=subject, email=email)
    )
    assert principal.company_id == tenants.a.company_id
    assert principal.roles == frozenset({"viewer"})


def test_accept_invite_for_a_different_email_is_403(tenants, supabase_mode):
    invited = _uniq_email("intended")
    other = _uniq_email("interloper")
    token, _ = create_invite_token(
        email=invited, company_id=tenants.a.company_id, roles=["viewer"]
    )

    res = TestClient(app).post(
        "/api/auth/accept-invite",
        json={"invite_token": token},
        headers={"Authorization": f"Bearer {_supabase_token(email=other)}"},
    )
    assert res.status_code == 403
    with admin_session() as s:
        rows = s.execute(
            select(User).where(User.email.in_([invited, other]))
        ).scalars().all()
        assert rows == []


def test_an_expired_invite_is_401(client, tenants):
    token, _ = create_invite_token(
        email=_uniq_email("late"), company_id=tenants.a.company_id,
        roles=["viewer"], ttl_days=-1,
    )
    res = client.post(
        "/api/auth/accept-invite", json={"invite_token": token, "password": "irrelevant"}
    )
    assert res.status_code == 401
    assert "expired" in res.json()["detail"]


def test_a_tampered_invite_is_401(client, tenants):
    token, _ = create_invite_token(
        email=_uniq_email("forged"), company_id=tenants.a.company_id, roles=["viewer"]
    )
    head, payload, sig = token.split(".")
    res = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": f"{head}.{payload}.{sig[:-4]}AAAA", "password": "x"},
    )
    assert res.status_code == 401


def test_an_access_token_is_not_an_invite(client, tenants):
    """Type confusion between our own token kinds must stay a refusal."""
    access = create_access_token(
        user_id=tenants.a.user_id, company_id=tenants.a.company_id, roles=["admin"]
    )
    res = client.post(
        "/api/auth/accept-invite", json={"invite_token": access, "password": "x"}
    )
    assert res.status_code == 401
    assert "wrong token type" in res.json()["detail"]


def test_accepting_the_same_invite_twice_is_409(client, tenants):
    email = _uniq_email("replay")
    token, _ = create_invite_token(
        email=email, company_id=tenants.a.company_id, roles=["viewer"]
    )
    first = client.post(
        "/api/auth/accept-invite", json={"invite_token": token, "password": "one"}
    )
    assert first.status_code == 201
    second = client.post(
        "/api/auth/accept-invite", json={"invite_token": token, "password": "two"}
    )
    assert second.status_code == 409


def test_invitation_squatting_fails_closed(tenants, monkeypatch):
    """THE scenario this rework exists for.

    Tenant A's admin invites an address they do not own. The person behind that
    address signs in with a perfectly valid Supabase token — but presents no
    invite token. Before: the pre-created row captured them into tenant A.
    Now: 403, no membership, no row.
    """
    monkeypatch.setattr(settings, "auth_backend", "local")
    victim = f"cfo-{uuid.uuid4().hex[:8]}@victimcorp.test"
    res = TestClient(app).post(
        "/api/auth/invite",
        headers=_auth(tenants.a),
        json={"email": victim, "roles": ["admin"]},
    )
    assert res.status_code == 201

    monkeypatch.setattr(settings, "auth_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", PROJECT)
    monkeypatch.setattr(settings, "supabase_jwt_secret", SUPABASE_SECRET)
    supabase._jwks.reset()
    try:
        with pytest.raises(HTTPException) as exc:
            supabase.principal_from_supabase_token(_supabase_token(email=victim))
        assert exc.value.status_code == 403
        with admin_session() as s:
            assert (
                s.execute(select(User).where(User.email == victim)).scalars().first()
                is None
            )
    finally:
        supabase._jwks.reset()


def test_removing_the_email_fallback_keeps_linked_users_signing_in(tenants, supabase_mode):
    """Accounts already linked by external_auth_id — every real account in
    production — must be untouched by the fallback's removal."""
    subject = str(uuid.uuid4())
    with admin_session() as s:
        s.get(User, tenants.a.user_id).external_auth_id = subject
    try:
        principal = supabase.principal_from_supabase_token(
            # The email claim deliberately matches nothing: the stable subject
            # alone must resolve the account.
            _supabase_token(sub=subject, email=_uniq_email("renamed"))
        )
        assert principal.user_id == tenants.a.user_id
        assert principal.company_id == tenants.a.company_id
    finally:
        with admin_session() as s:
            s.get(User, tenants.a.user_id).external_auth_id = None


# ---------------------------------------------------------------- revocation


def test_revoke_sessions_invalidates_old_tokens_but_not_new_ones(client, tenants):
    stale = _stale_token(tenants.a.user_id, tenants.a.company_id)
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"})
        .status_code == 200
    )

    res = client.post("/api/auth/revoke-sessions", headers=_auth(tenants.a), json={})
    assert res.status_code == 200, res.text

    refused = client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"})
    assert refused.status_code == 401
    assert "revocation" in refused.json()["detail"]

    # A token minted after the revocation is a new session and must work.
    assert client.get("/api/auth/me", headers=_auth(tenants.a)).status_code == 200


def test_an_admin_can_revoke_another_users_sessions(client, tenants):
    with admin_session() as s:
        other = User(
            company_id=tenants.a.company_id,
            email=_uniq_email("colleague"),
            password_hash=hash_password("irrelevant"),
        )
        s.add(other)
        s.flush()
        other_id = other.id

    stale = _stale_token(other_id, tenants.a.company_id)
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"})
        .status_code == 200
    )

    res = client.post(
        "/api/auth/revoke-sessions",
        headers=_auth(tenants.a),
        json={"user_id": str(other_id)},
    )
    assert res.status_code == 200, res.text
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"})
        .status_code == 401
    )
    with admin_session() as s:
        assert s.get(User, other_id).tokens_valid_from is not None


def test_revoking_someone_else_requires_user_manage(client, tenants):
    res = client.post(
        "/api/auth/revoke-sessions",
        headers=_auth(tenants.a, roles=("viewer",)),
        json={"user_id": str(tenants.b.user_id)},
    )
    assert res.status_code == 403


def test_revocation_cannot_reach_across_tenants(client, tenants):
    """Another tenant's user id answers exactly like a nonexistent one."""
    res = client.post(
        "/api/auth/revoke-sessions",
        headers=_auth(tenants.a),
        json={"user_id": str(tenants.b.user_id)},
    )
    assert res.status_code == 404
    with admin_session() as s:
        assert s.get(User, tenants.b.user_id).tokens_valid_from is None


def test_a_deactivated_users_token_stops_working(client, tenants):
    """Deactivation must bite mid-token, not at the next 12-hour expiry."""
    headers = _auth(tenants.a)
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    with admin_session() as s:
        s.get(User, tenants.a.user_id).is_active = False
    try:
        assert client.get("/api/auth/me", headers=headers).status_code == 401
    finally:
        with admin_session() as s:
            s.get(User, tenants.a.user_id).is_active = True


# ----------------------------------------------------- per-tenant email login


def test_the_same_email_in_two_tenants_logs_into_the_right_one(client, tenants):
    """Emails are unique per (company_id, email); the password picks the tenant.

    Before this fix the second row made the login lookup raise
    MultipleResultsFound — a 500 for both legitimate users.
    """
    email = _uniq_email("shared")
    with admin_session() as s:
        s.add(
            User(
                company_id=tenants.a.company_id,
                email=email,
                password_hash=hash_password("password-for-a"),
            )
        )
        s.add(
            User(
                company_id=tenants.b.company_id,
                email=email,
                password_hash=hash_password("password-for-b"),
            )
        )

    res_a = client.post(
        "/api/auth/login", json={"email": email, "password": "password-for-a"}
    )
    assert res_a.status_code == 200, res_a.text
    assert res_a.json()["company_id"] == str(tenants.a.company_id)

    res_b = client.post(
        "/api/auth/login", json={"email": email, "password": "password-for-b"}
    )
    assert res_b.status_code == 200, res_b.text
    assert res_b.json()["company_id"] == str(tenants.b.company_id)

    wrong = client.post(
        "/api/auth/login", json={"email": email, "password": "password-for-nobody"}
    )
    assert wrong.status_code == 401


# -------------------------------------------------------------- api key scopes


def test_an_unknown_api_key_scope_is_refused_by_name(tenants):
    name = f"bad-key-{uuid.uuid4().hex[:8]}"
    with admin_session() as s:
        with pytest.raises(HTTPException) as exc:
            api_keys.create_api_key(
                s,
                company_id=tenants.a.company_id,
                name=name,
                scopes=["buyer:read", "nonsense"],
                created_by=None,
            )
        assert exc.value.status_code == 422
        assert "nonsense" in exc.value.detail
        assert "buyer:read" not in exc.value.detail

    with admin_session() as s:
        assert (
            s.execute(select(ApiKey).where(ApiKey.name == name)).scalars().first()
            is None
        )


def test_a_permission_scope_grants_exactly_that_permission(tenants):
    with admin_session() as s:
        key, full = api_keys.create_api_key(
            s,
            company_id=tenants.a.company_id,
            name=f"narrow-{uuid.uuid4().hex[:8]}",
            scopes=["buyer:read"],
            created_by=None,
        )
        key_id = key.id
    try:
        principal = api_keys.principal_from_api_key(full)
        assert principal.can(Permission.BUYER_READ)
        assert not principal.can(Permission.BUYER_WRITE)
        assert not principal.can(Permission.USER_MANAGE)
    finally:
        with admin_session() as s:
            s.execute(ApiKey.__table__.delete().where(ApiKey.id == key_id))
