"""API keys for the public API.

Only a hash is stored. The secret is shown once, at creation, and cannot be
recovered — a key store you can read is a key store an attacker can read.

The key format is `crp_<prefix>_<secret>`. The prefix is indexed and looked up
directly, so verification is one indexed read plus one constant-time compare
rather than a scan-and-hash over every key in the table.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from app.db import admin_session
from app.identity.rbac import Permission, Role
from app.models import ApiKey

PREFIX_BYTES = 6
SECRET_BYTES = 32


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Return `(full_key, prefix, key_hash)`. Only the caller ever sees full_key."""
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"crp_{prefix}_{secret}", prefix, _hash_secret(secret)


def _validate_scopes(scopes: list[str]) -> None:
    """A scope matching nothing grants nothing — silently. Refusing at creation
    turns that silently-dead key into a sentence while whoever typed it can
    still fix the typo. Valid scopes are role names or single permission values
    (see `rbac.permissions_for` for how each is read)."""
    valid = {p.value for p in Permission} | {r.value for r in Role}
    unknown = [s for s in scopes if s not in valid]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown scope(s): {', '.join(sorted(set(unknown)))}",
        )


def create_api_key(
    session, *, company_id: UUID, name: str, scopes: list[str], created_by: UUID | None
) -> tuple[ApiKey, str]:
    _validate_scopes(scopes)
    full_key, prefix, key_hash = generate_key()
    key = ApiKey(
        company_id=company_id,
        name=name,
        prefix=prefix,
        key_hash=key_hash,
        scopes=scopes,
        created_by=created_by,
    )
    session.add(key)
    session.flush()
    return key, full_key


def _parse(token: str) -> tuple[str, str]:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "crp":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed API key")
    return parts[1], parts[2]


def principal_from_api_key(token: str):
    """Resolve a key to a Principal.

    Uses an admin session deliberately: the lookup must happen *before* a tenant
    is known, so it cannot itself be tenant-scoped. It reads one row by prefix
    and nothing else.
    """
    from app.identity.auth import Principal

    prefix, secret = _parse(token)
    with admin_session() as session:
        key = session.execute(
            select(ApiKey).where(ApiKey.prefix == prefix)
        ).scalar_one_or_none()
        if key is None or not hmac.compare_digest(key.key_hash, _hash_secret(secret)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        if key.revoked_at is not None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key revoked")
        key.last_used_at = datetime.now(timezone.utc)
        company_id = key.company_id
        scopes = frozenset(key.scopes or [])
        label = key.name

    return Principal(
        user_id=None,
        company_id=company_id,
        roles=scopes,
        label=label,
        via_api_key=True,
    )


def revoke(session, key_id: UUID) -> None:
    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    key.revoked_at = datetime.now(timezone.utc)
