"""Supabase as identity provider.

The test that matters most is `test_user_metadata_cannot_grant_a_role`.
`user_metadata` is writable by the user through the Supabase client SDK, so a
token claiming `role: legal_approver` proves nothing at all. If authorization
read from it, anyone who could sign up to the project could approve L3 legal
content. Roles come from our tables, keyed on the verified subject.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db import admin_session
from app.identity import supabase
from app.identity.rbac import Permission
from app.models import User

SECRET = "test-supabase-shared-secret"
PROJECT = "https://testproject.supabase.co"


def make_token(**over) -> str:
    """A token shaped exactly like Supabase's, signed with the shared secret."""
    now = int(time.time())
    claims = {
        "sub": over.pop("sub", str(uuid.uuid4())),
        "aud": over.pop("aud", "authenticated"),
        "role": "authenticated",
        "email": over.pop("email", "user@example.test"),
        "iat": now,
        "exp": over.pop("exp", now + 3600),
        "app_metadata": over.pop("app_metadata", {"provider": "email"}),
        "user_metadata": over.pop("user_metadata", {}),
    }
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
def supabase_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", PROJECT)
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    supabase._jwks.reset()
    yield
    supabase._jwks.reset()


@pytest.fixture
def linked_user(tenants, supabase_mode):
    """One of our users, linked to a Supabase subject."""
    subject = str(uuid.uuid4())
    with admin_session() as s:
        user = s.get(User, tenants.a.user_id)
        user.external_auth_id = subject
        email = user.email
    yield {"subject": subject, "email": email, "company_id": tenants.a.company_id,
           "user_id": tenants.a.user_id}
    with admin_session() as s:
        s.get(User, tenants.a.user_id).external_auth_id = None


# ------------------------------------------------------------- verification


def test_a_valid_token_verifies(supabase_mode):
    identity = supabase.verify(make_token(email="a@example.test"))
    assert identity.email == "a@example.test"
    assert identity.provider == "email"
    assert identity.subject


def test_an_expired_token_is_refused(supabase_mode):
    with pytest.raises(HTTPException) as exc:
        supabase.verify(make_token(exp=int(time.time()) - 10))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_a_token_signed_with_the_wrong_key_is_refused(supabase_mode):
    forged = jwt.encode(
        {"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
        "not-the-real-secret",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        supabase.verify(forged)
    assert exc.value.status_code == 401


def test_the_wrong_audience_is_refused(supabase_mode):
    """A service-role or anon token must not pass as an end user."""
    with pytest.raises(HTTPException) as exc:
        supabase.verify(make_token(aud="anon"))
    assert "audience" in exc.value.detail


def test_an_unsigned_token_is_refused(supabase_mode):
    """alg=none is the oldest JWT attack there is."""
    unsigned = jwt.encode(
        {"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
        key="",
        algorithm="none",
    )
    with pytest.raises(HTTPException):
        supabase.verify(unsigned)


def test_a_token_without_a_subject_is_refused(supabase_mode):
    now = int(time.time())
    token = jwt.encode(
        {"aud": "authenticated", "exp": now + 60}, SECRET, algorithm="HS256"
    )
    with pytest.raises(HTTPException):
        supabase.verify(token)


# ------------------------------------------------------- the escalation gate


def test_user_metadata_cannot_grant_a_role(linked_user):
    """The reason tenancy and roles never come from the token.

    `user_metadata` is writable by the user via the Supabase client SDK. A token
    asserting `legal_approver` must buy exactly nothing.
    """
    token = make_token(
        sub=linked_user["subject"],
        user_metadata={"role": "legal_approver", "roles": ["owner", "legal_approver"],
                       "company_id": str(uuid.uuid4())},
    )
    principal = supabase.principal_from_supabase_token(token)

    # Roles are whatever our own rows say — the fixture grants "admin" only.
    assert principal.roles == frozenset({"admin"})
    assert not principal.can(Permission.TEMPLATE_APPROVE_L3)


def test_app_metadata_cannot_grant_a_role_either(linked_user):
    token = make_token(
        sub=linked_user["subject"],
        app_metadata={"provider": "email", "roles": ["legal_approver"]},
    )
    principal = supabase.principal_from_supabase_token(token)
    assert not principal.can(Permission.TEMPLATE_APPROVE_L3)


def test_a_forged_company_id_in_the_token_is_ignored(linked_user):
    """Tenancy comes from our user row, so a token cannot point at another."""
    other = uuid.uuid4()
    token = make_token(
        sub=linked_user["subject"],
        company_id=str(other),
        user_metadata={"company_id": str(other)},
    )
    principal = supabase.principal_from_supabase_token(token)
    assert principal.company_id == linked_user["company_id"]
    assert principal.company_id != other


# ------------------------------------------------------------------ mapping


def test_a_linked_subject_resolves_to_our_user(linked_user):
    principal = supabase.principal_from_supabase_token(
        make_token(sub=linked_user["subject"])
    )
    assert principal.user_id == linked_user["user_id"]
    assert principal.company_id == linked_user["company_id"]
    assert principal.label == linked_user["email"]


def test_an_unknown_supabase_user_gets_403_not_a_tenant(supabase_mode, tenants):
    """Authenticated is not the same as authorised.

    Anyone can sign up to a Supabase project. Landing them inside a company
    because they hold a valid token would be the whole tenancy model gone.
    """
    with pytest.raises(HTTPException) as exc:
        supabase.principal_from_supabase_token(
            make_token(email=f"stranger-{uuid.uuid4().hex}@example.test")
        )
    assert exc.value.status_code == 403
    assert "not a member of any company" in exc.value.detail


def test_an_email_match_alone_grants_nothing(tenants, supabase_mode):
    """Deliberate change: the email-fallback link is gone.

    Binding a subject to whichever row carried a matching email meant whoever
    wrote an address into a tenant first captured the account that later signed
    in with it — invitation squatting. Membership now arrives only through
    registration or a signed invite token; a matching email by itself is 403,
    and the row stays unlinked.
    """
    subject = str(uuid.uuid4())
    with admin_session() as s:
        email = s.get(User, tenants.b.user_id).email

    with pytest.raises(HTTPException) as exc:
        supabase.principal_from_supabase_token(make_token(sub=subject, email=email))
    assert exc.value.status_code == 403

    with admin_session() as s:
        assert s.get(User, tenants.b.user_id).external_auth_id is None


def test_an_inactive_user_is_refused(linked_user):
    with admin_session() as s:
        s.get(User, linked_user["user_id"]).is_active = False
    try:
        with pytest.raises(HTTPException) as exc:
            supabase.principal_from_supabase_token(make_token(sub=linked_user["subject"]))
        assert exc.value.status_code == 401
    finally:
        with admin_session() as s:
            s.get(User, linked_user["user_id"]).is_active = True


# ------------------------------------------------------------------ local mode


def test_a_supabase_backed_user_has_no_local_password(tenants):
    """No password hash means no password login — never an empty-hash pass."""
    from app.identity.auth import verify_password

    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False


def test_local_mode_is_unaffected(tenants):
    """Switching identity provider must not change what the rest of the system
    does, which is the point of putting it behind a seam."""
    from app.identity.auth import create_access_token, principal_from_token

    token = create_access_token(
        user_id=tenants.a.user_id, company_id=tenants.a.company_id, roles=["admin"]
    )
    assert principal_from_token(token).company_id == tenants.a.company_id


def test_a_foreign_token_is_401_not_503(monkeypatch):
    """A token signed by someone else is invalid, not an outage.

    Returning 503 tells the caller "retry later" for a token that will never
    work, so a stale session retries forever instead of re-authenticating — and
    it pollutes the 5xx rate that is supposed to mean *we* have a problem.
    """
    monkeypatch.setattr(settings, "auth_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", PROJECT)
    monkeypatch.setattr(settings, "supabase_jwt_secret", None)  # force the JWKS path
    supabase._jwks.reset()

    class FakeClient:
        def get_jwk_set(self):
            return object()          # reachable

        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")

    monkeypatch.setattr(supabase._jwks, "client", lambda: FakeClient())

    with pytest.raises(HTTPException) as exc:
        supabase.verify(make_token())
    assert exc.value.status_code == 401
    supabase._jwks.reset()


def test_an_unreachable_provider_is_503_not_401(monkeypatch):
    """The inverse: if we genuinely cannot reach Supabase, that is ours to own
    and the caller should retry rather than be told their token is bad."""
    monkeypatch.setattr(settings, "auth_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", PROJECT)
    monkeypatch.setattr(settings, "supabase_jwt_secret", None)
    supabase._jwks.reset()

    class DeadClient:
        def get_jwk_set(self):
            raise ConnectionError("network is down")

    monkeypatch.setattr(supabase._jwks, "client", lambda: DeadClient())

    with pytest.raises(HTTPException) as exc:
        supabase.verify(make_token())
    assert exc.value.status_code == 503
    supabase._jwks.reset()


# ------------------------------------------------------------- registration


@pytest.fixture
def api_client(supabase_mode):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _cleanup_company(name_fragment: str) -> None:
    """Tear down in dependency order.

    Registration writes an audit row referencing the new user, and audit_log is
    append-only by design — so it has to go before the user it points at.
    """
    from sqlalchemy import delete
    from app.models import AuditLog, Company, RoleGrant

    with admin_session() as s:
        rows = s.execute(
            select(Company).where(Company.name.like(f"%{name_fragment}%"))
        ).scalars().all()
        for c in rows:
            s.execute(delete(AuditLog).where(AuditLog.company_id == c.id))
            s.execute(delete(RoleGrant).where(RoleGrant.company_id == c.id))
            s.execute(delete(User).where(User.company_id == c.id))
            s.execute(delete(Company).where(Company.id == c.id))


def test_signup_creates_a_new_company_not_access_to_an_existing_one(api_client, tenants):
    """The security boundary of self-serve signup.

    A stranger who can authenticate must land in their own empty company. If
    signup could attach them to a company that already exists, it would hand
    them that company's debtor list.
    """
    marker = uuid.uuid4().hex[:8]
    token = make_token(email=f"founder-{marker}@example.com")
    try:
        res = api_client.post(
            "/api/auth/register",
            json={"company_name": f"Fresh Co {marker}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["status"] == "company_created"
        assert body["roles"] == ["admin", "owner"]
        # Crucially, NOT either existing tenant.
        assert body["company_id"] != str(tenants.a.company_id)
        assert body["company_id"] != str(tenants.b.company_id)
    finally:
        _cleanup_company(marker)


def test_a_new_owner_cannot_see_another_companys_data(api_client, tenants):
    """The whole point, asserted end to end through the API."""
    marker = uuid.uuid4().hex[:8]
    token = make_token(email=f"founder-{marker}@example.com")
    try:
        api_client.post(
            "/api/auth/register",
            json={"company_name": f"Fresh Co {marker}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        state = api_client.get(
            "/api/portal/state", headers={"Authorization": f"Bearer {token}"}
        )
        assert state.status_code == 200
        data = state.json()
        assert data["company"]["name"] == f"Fresh Co {marker}"
        assert data["campaigns"] == []          # a brand new, empty tenant
        assert data["summary"]["outstanding_paise"] == 0
    finally:
        _cleanup_company(marker)


def test_signup_without_a_company_name_is_refused(api_client):
    marker = uuid.uuid4().hex[:8]
    res = api_client.post(
        "/api/auth/register",
        json={"company_name": "  "},
        headers={"Authorization": f"Bearer {make_token(email=f'x-{marker}@example.com')}"},
    )
    assert res.status_code == 400


def test_signup_is_idempotent_for_an_already_linked_user(api_client, linked_user):
    res = api_client.post(
        "/api/auth/register",
        json={"company_name": "Should Be Ignored"},
        headers={"Authorization": f"Bearer {make_token(sub=linked_user['subject'])}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "already_registered"
    assert body["company_id"] == str(linked_user["company_id"])


def test_a_matching_email_no_longer_joins_an_existing_company(api_client, tenants):
    """Deliberate change: registration ignores email matches entirely.

    Joining a tenant by registering with an email someone there had written
    down was the same squatting hole as the login-time fallback. Joining now
    happens only through `/api/auth/accept-invite` with a signed invite token;
    a stranger whose email happens to match an existing user gets a fresh,
    empty company like anyone else — and the existing user stays untouched.
    """
    marker = uuid.uuid4().hex[:8]
    with admin_session() as s:
        existing_email = s.get(User, tenants.b.user_id).email

    res = api_client.post(
        "/api/auth/register",
        json={"company_name": f"Not Tenant B {marker}"},
        headers={
            "Authorization": f"Bearer {make_token(sub=str(uuid.uuid4()), email=existing_email)}"
        },
    )
    try:
        assert res.status_code == 201
        body = res.json()
        assert body["status"] == "company_created"
        assert body["company_id"] != str(tenants.b.company_id)
        with admin_session() as s:
            assert s.get(User, tenants.b.user_id).external_auth_id is None
    finally:
        _cleanup_company(marker)


def test_registration_requires_a_verified_token(api_client):
    assert api_client.post("/api/auth/register", json={"company_name": "X"}).status_code == 401
    assert api_client.post(
        "/api/auth/register", json={"company_name": "X"},
        headers={"Authorization": "Bearer garbage"},
    ).status_code == 401
