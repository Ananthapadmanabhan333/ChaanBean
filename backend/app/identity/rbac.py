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
    # Running a registry lookup is ordinary desk work: it reads a portal, records
    # what came back, and publishes nothing on its own.
    COMPANY_VERIFY = "company:verify"

    # privileged
    TEMPLATE_APPROVE_L3 = "template:approve_l3"
    USER_MANAGE = "user:manage"
    APIKEY_MANAGE = "apikey:manage"
    COMPANY_SETTINGS = "company:settings"
    # Confirming a weak match is the act that attaches a named company's registry
    # record to a debt — after which that company can be dunned, listed, or sent
    # a notice. It follows the TEMPLATE_APPROVE_L3 precedent of deliberately not
    # being an operator power: whoever is chasing the money should not also be
    # deciding, on a name resemblance, whose money it is.
    ENTITY_CONFIRM = "entity:confirm"
    # Recording a payment is operator work because a payment is a fact that can
    # be checked against a bank statement. A credit note cannot be: it moves a
    # balance down with no money behind it, on this side's say-so alone. That is
    # the shape a debt takes when it disappears on purpose, and it should not be
    # the same permission as running a campaign.
    LEDGER_CREDIT = "ledger:credit"
    # Writing off or cancelling ends the pursuit of a debt outright — the ladder
    # stops, the account closes, nothing dials again. TEMPLATE_APPROVE_L3's
    # reasoning applies unchanged: the person chasing the money is not the person
    # who decides to stop chasing it.
    LEDGER_WRITE_OFF = "ledger:write_off"
    # Lifting a dispute is the moment a contested debt becomes collectable again,
    # and the trace it leaves is what app.registry.eligibility reads before
    # anything is published. An operator able to clear disputes could clear the
    # one raised against the account they are about to escalate.
    DISPUTE_CLEAR = "dispute:clear"
    # Recording a withdrawal stays BUYER_WRITE: the operator taking the call is
    # exactly who should be able to write down "stop ringing me", and every such
    # act reduces contact. Undoing one points the dialler back at somebody who
    # refused — nothing else in the product turns contact on — and getting it
    # wrong is a DPDP complaint rather than a wasted call.
    CONSENT_RESTORE = "consent:restore"
    # Confirming a court link attaches a named company's litigation to a debtor's
    # file. app.legal.cases refuses anything below an identifier match precisely
    # because getting it wrong is defamation. Rejecting shares the permission
    # rather than sitting lower: alone it is safe, but "which cases count" is one
    # desk, and splitting it would let an operator drop an inconvenient suit out
    # of the pre-legal assessment.
    LEGAL_LINK_CONFIRM = "legal:link_confirm"
    # The review queue exists so that a person looks before the machine resumes.
    # Whoever is measured on how fast the queue empties should not also be the
    # one who can empty it without looking.
    REVIEW_RESOLVE = "review:resolve"


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
    Permission.COMPANY_VERIFY,
}

_ADMIN = _OPERATOR | {
    Permission.USER_MANAGE,
    Permission.APIKEY_MANAGE,
    Permission.COMPANY_SETTINGS,
    Permission.AUDIT_READ,
    Permission.ENTITY_CONFIRM,
    Permission.LEDGER_CREDIT,
    Permission.LEDGER_WRITE_OFF,
    Permission.DISPUTE_CLEAR,
    Permission.CONSENT_RESTORE,
    Permission.LEGAL_LINK_CONFIRM,
    Permission.REVIEW_RESOLVE,
}

# Note what is absent from every role below: TEMPLATE_APPROVE_L3 belongs only to
# legal_approver, including for the owner. ENTITY_CONFIRM stops at admin for the
# same kind of reason — an operator may run a verification all day and still not
# be the one who decides a borderline match names a real company.
#
# The six permissions added beside it share one shape. An operator may reduce a
# debt by recording money that arrived, may write down that a debtor asked to be
# left alone, and may search the courts all day. What stops at admin is the
# opposite direction in each pair: reducing a debt with no money behind it,
# turning contact back on, and deciding that a case found under a similar name
# is this debtor's.
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
            # An API-key scope may name one permission directly ("buyer:read");
            # it grants exactly that permission and nothing wider. Any other
            # unrecognised string grants nothing — it does not error open.
            try:
                granted.add(Permission(name))
            except ValueError:
                pass
            continue
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def has_permission(roles, permission: Permission) -> bool:
    return permission in permissions_for(roles)
