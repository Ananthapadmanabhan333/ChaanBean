"""Roles and permissions.

Deny by default. A permission that appears in no role's grant list is refused,
and an unmapped permission is a refusal rather than an allow — a typo in a
permission name must not become an access grant.

`legal_approver` is deliberately orthogonal to seniority. Approving L3 legal
content is not an administrative action, and the person who writes a template
should not be the person who approves it. An `owner` does not get it for free.
"""

from __future__ import annotations

import enum


class Role(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    LEGAL_APPROVER = "legal_approver"


class Permission(str, enum.Enum):
    # read
    BUYER_READ = "buyer:read"
    CAMPAIGN_READ = "campaign:read"
    CALL_READ = "call:read"
    REPORT_READ = "report:read"
    TEMPLATE_READ = "template:read"
    AUDIT_READ = "audit:read"

    # write
    BUYER_WRITE = "buyer:write"
    IMPORT_RUN = "import:run"
    CAMPAIGN_WRITE = "campaign:write"
    CAMPAIGN_START = "campaign:start"
    TEMPLATE_WRITE = "template:write"

    # privileged
    TEMPLATE_APPROVE_L3 = "template:approve_l3"
    USER_MANAGE = "user:manage"
    APIKEY_MANAGE = "apikey:manage"
    COMPANY_SETTINGS = "company:settings"


_READ_ONLY = frozenset(
    {
        Permission.BUYER_READ,
        Permission.CAMPAIGN_READ,
        Permission.CALL_READ,
        Permission.REPORT_READ,
        Permission.TEMPLATE_READ,
    }
)

_OPERATOR = _READ_ONLY | {
    Permission.BUYER_WRITE,
    Permission.IMPORT_RUN,
    Permission.CAMPAIGN_WRITE,
    Permission.CAMPAIGN_START,
    Permission.TEMPLATE_WRITE,
}

_ADMIN = _OPERATOR | {
    Permission.USER_MANAGE,
    Permission.APIKEY_MANAGE,
    Permission.COMPANY_SETTINGS,
    Permission.AUDIT_READ,
}

# Note what is absent from every role below: TEMPLATE_APPROVE_L3 belongs only to
# legal_approver, including for the owner.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_READ_ONLY),
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_ADMIN),
    Role.LEGAL_APPROVER: frozenset(
        {
            Permission.TEMPLATE_READ,
            Permission.TEMPLATE_APPROVE_L3,
            Permission.AUDIT_READ,
        }
    ),
}


def permissions_for(roles) -> frozenset[Permission]:
    """Union of everything the held roles grant. Unknown role names grant nothing."""
    granted: set[Permission] = set()
    for name in roles:
        try:
            role = Role(name)
        except ValueError:
            continue  # an unrecognised role grants nothing, it does not error open
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def has_permission(roles, permission: Permission) -> bool:
    return permission in permissions_for(roles)
