"""Authentication: passwords, JWTs, OTP, and the request principal.

**`company_id` never comes from the client.** It is read from the verified token
and nowhere else. Accepting a tenant id from a request body, query string or
header is the single most common multi-tenancy hole, and it is the one this
module exists to make impossible.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, set_tenant
from app.identity.rbac import Permission, has_permission
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)

ALGORITHM = "HS256"
BCRYPT_MAX_BYTES = 72


# ------------------------------------------------------------------- passwords
# bcrypt directly rather than passlib: passlib's last release predates bcrypt 4,
# and its backend probe feeds an overlong secret to `bcrypt.hashpw`, which modern
# bcrypt refuses outright. The wrapper bought us nothing we use.


def hash_password(plain: str) -> str:
    # bcrypt silently truncates beyond 72 bytes. Refuse rather than accept a
    # password whose tail is decorative — the user would believe it counted.
    if len(plain.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"password exceeds bcrypt's {BCRYPT_MAX_BYTES}-byte limit")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str | None) -> bool:
    # A user with no local hash is Supabase-backed. There is no password here to
    # be right about, so this is a refusal rather than an error — and never an
    # accidental pass on an empty hash.
    if not hashed:
        return False
    encoded = plain.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------------ jwt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(
    *, user_id: UUID, company_id: UUID, roles, ttl_minutes: int | None = None
) -> str:
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    payload = {
        "sub": str(user_id),
        "company_id": str(company_id),
        "roles": sorted(roles),
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_refresh_token(*, user_id: UUID, company_id: UUID) -> str:
    payload = {
        "sub": str(user_id),
        "company_id": str(company_id),
        "type": "refresh",
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(days=settings.refresh_ttl_days)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, *, expected_type: str = "access") -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    if claims.get("type") != expected_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")
    return claims


# ------------------------------------------------------------------------ otp


def _otp_key(phone_e164: str) -> str:
    return f"otp:{phone_e164}"


def _otp_rate_key(phone_e164: str) -> str:
    return f"otp:rate:{phone_e164}"


def _hash_otp(code: str, phone_e164: str) -> str:
    """Store a hash, never the code itself. Salted by the number so two people
    holding the same code do not share a stored value."""
    return hashlib.sha256(f"{phone_e164}:{code}:{settings.jwt_secret}".encode()).hexdigest()


def request_otp(redis_client, phone_e164: str) -> str:
    """Generate and store a one-time code. Returns it for the sender to deliver.

    The code is never logged and never stored in clear. Rate-limited per number
    so the endpoint cannot be used to spray SMS at someone.
    """
    rate_key = _otp_rate_key(phone_e164)
    sent = redis_client.incr(rate_key)
    if sent == 1:
        redis_client.expire(rate_key, 3600)
    if sent > settings.otp_max_per_hour:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many OTP requests")

    code = f"{secrets.randbelow(1_000_000):06d}"
    redis_client.setex(_otp_key(phone_e164), settings.otp_ttl_seconds, _hash_otp(code, phone_e164))
    return code


def verify_otp(redis_client, phone_e164: str, code: str) -> bool:
    """Single use: the stored value is deleted whether or not it matched."""
    stored = redis_client.get(_otp_key(phone_e164))
    if stored is None:
        return False
    redis_client.delete(_otp_key(phone_e164))
    if isinstance(stored, bytes):
        stored = stored.decode()
    return hmac.compare_digest(stored, _hash_otp(code, phone_e164))


# ------------------------------------------------------------------ principal


@dataclass(frozen=True)
class Principal:
    user_id: UUID | None
    company_id: UUID
    roles: frozenset[str]
    label: str = ""
    via_api_key: bool = False

    def can(self, permission: Permission) -> bool:
        return has_permission(self.roles, permission)


def principal_from_token(token: str) -> Principal:
    claims = decode_token(token)
    return Principal(
        user_id=UUID(claims["sub"]),
        company_id=UUID(claims["company_id"]),
        roles=frozenset(claims.get("roles", [])),
    )


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    token = credentials.credentials

    # Three token shapes reach this line, and they are told apart by what they
    # are rather than by a header the caller controls.
    if token.startswith("crp_"):
        from app.identity.api_keys import principal_from_api_key

        principal = principal_from_api_key(token)
    elif settings.auth_backend == "supabase":
        from app.identity.supabase import principal_from_supabase_token

        principal = principal_from_supabase_token(token)
    else:
        principal = principal_from_token(token)

    request.state.principal = principal
    return principal


def tenant_db(
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Session:
    """A request-scoped session bound to the caller's tenant.

    Every route that touches tenant data depends on this rather than on
    `get_session`, so there is no path where a query runs without RLS bound.
    """
    set_tenant(session, principal.company_id)
    return session


def current_user(
    principal: Principal = Depends(current_principal),
    session: Session = Depends(tenant_db),
) -> User:
    if principal.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this endpoint requires a user")
    user = session.execute(
        select(User).where(User.id == principal.user_id)
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")
    return user


def require(permission: Permission):
    """Dependency factory. Deny by default — an unheld permission is a 403."""

    def _check(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"missing permission {permission.value}"
            )
        return principal

    return _check
