"""Supabase as the identity provider.

**What Supabase does here, and what it deliberately does not.**

It answers *who is this person* — signup, login, password reset, OTP, social
providers, MFA. That is genuinely tedious to build well and worth handing over.

It does **not** answer *which tenant they belong to* or *what they may do*.
Those stay in our `users`, `roles` and `companies` tables, behind our own
Row-Level Security, for one reason that matters more than convenience:

    A Supabase JWT carries `user_metadata`, and `user_metadata` is writable by
    the user themselves through the client SDK.

So a token claiming `user_metadata.role = "legal_approver"` proves nothing. If
authorization read from there, any signed-up user could approve L3 legal content
by editing their own profile. This module therefore takes exactly two facts from
the token — the verified `sub` and `email` — and looks up everything else in our
database, keyed on `sub`.

`app_metadata` is admin-only and safer, but it still lives in a system whose
purpose is authentication, not tenancy. Our database stays authoritative.

**Verification.** Signature always checked, never `verify_signature=False`.
Newer Supabase projects sign asymmetrically and publish a JWKS; older ones use a
shared HS256 secret. Both work; JWKS is preferred because the key never leaves
Supabase.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, status

from app.config import settings

log = logging.getLogger(__name__)

# Supabase issues every end-user token with this audience.
EXPECTED_AUDIENCE = "authenticated"
ALGORITHMS = ["RS256", "ES256", "HS256"]


class SupabaseAuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status.HTTP_401_UNAUTHORIZED, detail)


@dataclass(frozen=True)
class SupabaseIdentity:
    """The only things taken from a Supabase token, both signature-verified."""

    subject: str          # auth.users.id — the stable join key to our users table
    email: str | None
    provider: str | None  # email | google | phone …


class _JwksCache:
    """JWKS with a TTL.

    Supabase rotates signing keys, so this cannot be fetched once and kept
    forever; equally it must not be fetched on every request, which would put a
    network call on the hot path of every API call.
    """

    def __init__(self) -> None:
        self._client: jwt.PyJWKClient | None = None
        self._url: str | None = None
        self._fetched_at: float = 0.0

    def client(self) -> jwt.PyJWKClient:
        url = settings.supabase_jwks_url
        if not url:
            raise SupabaseAuthError("SUPABASE_URL is not configured")
        expired = (time.time() - self._fetched_at) > settings.supabase_jwks_ttl_seconds
        if self._client is None or self._url != url or expired:
            self._client = jwt.PyJWKClient(url, cache_keys=True)
            self._url = url
            self._fetched_at = time.time()
        return self._client

    def reset(self) -> None:
        self._client = None
        self._fetched_at = 0.0


_jwks = _JwksCache()


def _decode(token: str) -> dict:
    """Verify and decode. Raises rather than returning anything unverified."""
    options = {"require": ["exp", "sub"], "verify_aud": True}

    # A shared secret means a legacy project; verify HS256 against it directly.
    if settings.supabase_jwt_secret:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=EXPECTED_AUDIENCE,
            options=options,
        )

    # Otherwise the project signs asymmetrically and publishes its public keys.
    client = _jwks.client()

    # Fetching the key set and matching a key against the token are two
    # different failures and must not collapse into one status. Not reaching
    # Supabase is *our* outage (503, retry later); a token that matches no
    # published key is simply an invalid token (401, re-authenticate). A client
    # told 503 for a stale token will retry forever instead of logging in again.
    try:
        client.get_jwk_set()
    except Exception as exc:
        log.warning("could not fetch Supabase JWKS: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "cannot reach the identity provider"
        )

    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        raise SupabaseAuthError(f"token does not match any published signing key: {exc}")

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=[a for a in ALGORITHMS if a != "HS256"],
        audience=EXPECTED_AUDIENCE,
        options=options,
    )


def verify(token: str) -> SupabaseIdentity:
    """Verify a Supabase access token and return the identity it proves."""
    if not settings.supabase_url:
        raise SupabaseAuthError("Supabase auth is selected but SUPABASE_URL is unset")

    try:
        claims = _decode(token)
    except jwt.ExpiredSignatureError:
        raise SupabaseAuthError("Supabase token expired")
    except jwt.InvalidAudienceError:
        raise SupabaseAuthError("Supabase token has the wrong audience")
    except jwt.PyJWKClientError as exc:
        # A key-fetch failure is not the caller's fault; say so rather than
        # implying their token is bad.
        log.warning("could not fetch Supabase JWKS: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "cannot reach the identity provider"
        )
    except jwt.InvalidTokenError as exc:
        raise SupabaseAuthError(f"invalid Supabase token: {exc}")

    subject = claims.get("sub")
    if not subject:
        raise SupabaseAuthError("Supabase token carries no subject")

    # Deliberately NOT read: user_metadata (user-writable), app_metadata roles,
    # or any tenant hint in the token. Those come from our database.
    return SupabaseIdentity(
        subject=str(subject),
        email=claims.get("email"),
        provider=(claims.get("app_metadata") or {}).get("provider"),
    )


def principal_from_supabase_token(token: str):
    """Map a verified Supabase identity onto a tenant Principal.

    The lookup is cross-tenant by necessity — a token arrives with no tenant
    attached, and resolving which one it belongs to is exactly what the worker
    role exists for. Roles and company come from our rows, never the token.
    """
    from sqlalchemy import select

    from app.db import admin_session
    from app.identity.auth import Principal
    from app.models import User

    identity = verify(token)

    with admin_session() as session:
        user = session.execute(
            select(User).where(User.external_auth_id == identity.subject)
        ).scalar_one_or_none()

        # There is deliberately no fallback to email here. An email in a token
        # proves control of a mailbox, not membership of a tenant — and linking
        # on first sight meant whoever wrote an address into a tenant first
        # captured the account that later signed in with it. Rows are bound to
        # a verified subject at exactly two audited moments instead:
        # registration, and invite acceptance.

        if user is None:
            # Authenticated by Supabase, but unknown here. That is not an
            # account — an admin must invite them into a company first, because
            # otherwise anyone who can sign up to the Supabase project would
            # land inside a tenant.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "authenticated, but this user is not a member of any company",
            )
        if not user.is_active:
            raise SupabaseAuthError("user is inactive")

        return Principal(
            user_id=user.id,
            company_id=user.company_id,
            roles=frozenset(user.roles),
            label=user.email,
        )
