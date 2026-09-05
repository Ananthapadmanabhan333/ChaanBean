"""Tenants, users, role grants, API keys and the audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _uuid_pk

if TYPE_CHECKING:  # relationship targets resolve through the registry at runtime
    from app.models.recovery import Campaign
    from app.models.trade import Buyer


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    buyers: Mapped[list[Buyer]] = relationship(back_populates="company")
    campaigns: Mapped[list[Campaign]] = relationship(back_populates="company")
    caller_ids: Mapped[list[CallerId]] = relationship(back_populates="company")


class CallerId(Base, TimestampMixin):
    """Numbers this company may present as CLI.

    Indian carriers whitelist the calling number you present; an unapproved CLI is
    rejected outright. Holding these as rows — rather than one free-text field on
    Company — means a tenant cannot present another tenant's number, and the
    carrier's approval state is recorded rather than assumed.
    """

    __tablename__ = "caller_ids"
    __table_args__ = (UniqueConstraint("e164"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    e164: Mapped[str] = mapped_column(String(20), nullable=False)
    carrier_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    company: Mapped[Company] = relationship(back_populates="caller_ids")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("company_id", "email"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable: a Supabase-backed user has no password here, because Supabase
    # holds it. Storing a placeholder hash would make "has a local password"
    # unanswerable.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # The external identity provider's stable subject (Supabase `auth.users.id`).
    # Indexed and unique because every request resolves a token through it.
    external_auth_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    phone_e164: Mapped[str | None] = mapped_column(String(20))  # OTP login
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # `foreign_keys` is required: RoleGrant points at users twice, once for the
    # holder and once for whoever granted it.
    role_grants: Mapped[list[RoleGrant]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="RoleGrant.user_id",
    )

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(g.role for g in self.role_grants)


class RoleGrant(Base, TimestampMixin):
    """One role held by one user.

    Roles are a grant list rather than a single column because `legal_approver` is
    deliberately orthogonal to seniority: an admin does not acquire the right to
    approve L3 legal content by being an admin, and the person who writes a
    template should not be the person who approves it.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("user_id", "role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="role_grants", foreign_keys=[user_id])


class ApiKey(Base, TimestampMixin):
    """Only the hash is stored. The secret is shown once, at creation."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_prefix", "prefix"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)  # lookup without the secret
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class AuditLog(Base):
    """Append-only. Never updated, never deleted.

    Cheap to write now and impossible to backfill later. An audit trail with gaps
    is worse than none, because it invites false confidence.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_company_occurred", "company_id", "occurred_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_label: Mapped[str | None] = mapped_column(String(255))  # survives user deletion
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(45))  # IPv6-sized
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
