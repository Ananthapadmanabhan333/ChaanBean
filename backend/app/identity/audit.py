"""The audit trail.

Append-only. Written through one function rather than scattered call sites,
because an audit trail with gaps is worse than none — it invites false
confidence in exactly the situation where you need certainty.

What must be recorded is anything a regulator, a court or an angry customer
would ask about: who logged in, who approved the words that were played, who
started the campaign, who changed a debtor's contact status.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditLog


class Action:
    """Closed-ish set of audited actions. Strings, so later phases can extend."""

    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    OTP_REQUESTED = "auth.otp_requested"
    LOGOUT = "auth.logout"
    SESSIONS_REVOKED = "auth.sessions_revoked"

    USER_CREATED = "user.created"
    USER_INVITED = "user.invited"
    ROLE_GRANTED = "user.role_granted"
    ROLE_REVOKED = "user.role_revoked"

    APIKEY_CREATED = "apikey.created"
    APIKEY_REVOKED = "apikey.revoked"

    TEMPLATE_CREATED = "template.created"
    TEMPLATE_VERSION_CREATED = "template.version_created"
    TEMPLATE_APPROVED = "template.approved"

    CAMPAIGN_CREATED = "campaign.created"
    CAMPAIGN_STARTED = "campaign.started"
    CAMPAIGN_PAUSED = "campaign.paused"

    BUYER_IMPORTED = "buyer.imported"
    BUYER_CONSENT_CHANGED = "buyer.consent_changed"
    BUYER_SUPPRESSED = "buyer.suppressed"
    ACCOUNT_STATUS_CHANGED = "account.status_changed"

    CALL_BLOCKED = "call.blocked"
    CALL_PLACED = "call.placed"
    MESSAGE_BLOCKED = "message.blocked"


def record(
    session: Session,
    *,
    action: str,
    company_id: UUID | None = None,
    actor_id: UUID | None = None,
    actor_label: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        company_id=company_id,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        ip=ip,
        detail=detail,
    )
    session.add(entry)
    session.flush()
    return entry


def record_for(session: Session, principal, *, action: str, **kwargs) -> AuditLog:
    """Convenience wrapper that fills actor and tenant from the request principal."""
    return record(
        session,
        action=action,
        company_id=principal.company_id,
        actor_id=principal.user_id,
        actor_label=principal.label or None,
        **kwargs,
    )
