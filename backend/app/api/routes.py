"""HTTP routes.

Every route that touches tenant data depends on `tenant_db`, which binds the
session to the tenant from the **verified token**. There is deliberately no
route anywhere that reads a company id from a path, query string or body.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AccountOut,
    AllocationOut,
    AuditEntryOut,
    AllocationPlanOut,
    BehaviourOut,
    BlackoutDateIn,
    BlackoutDateOut,
    BuyerIdentityIn,
    BuyerIdentityOut,
    BuyerIn,
    BuyerOut,
    BuyerPatchIn,
    CallerIdOut,
    CaseSearchOut,
    ChannelOptOutIn,
    ConsentIn,
    ContactChangeOut,
    CreditCheckIn,
    CreditCheckOut,
    CreditDecisionIn,
    CreditNoteIn,
    CreditNoteOut,
    CompanyProfileIn,
    CompanyProfileOut,
    DisputeClearIn,
    EntityCandidateReviewIn,
    InvoiceClosureIn,
    InvoiceOut,
    LegalLinkOut,
    LegalLinkReviewIn,
    OnAccountIn,
    PhoneIn,
    PhoneRetireIn,
    PrelegalIn,
    PrelegalOut,
    PromiseIn,
    PromiseOut,
    PromiseSettleIn,
    CallOut,
    CampaignIn,
    CampaignOut,
    CampaignPatchIn,
    ImportPreviewOut,
    InvoiceIn,
    LoginRequest,
    MessageOut,
    PaymentIn,
    ReviewItemOut,
    ReviewResolveIn,
    SuppressIn,
    TemplateIn,
    TokenResponse,
    VerificationOut,
)
from app.company import build_gst_backend, build_mca_backend
from app.company.identifiers import validate_cin, validate_gstin
from app.company.resolution import PAN_RE, Tier, hash_pan, state_from_gstin
from app.identity import audit
from app.identity.auth import (
    Principal,
    create_access_token,
    create_invite_token,
    create_refresh_token,
    current_principal,
    require,
    revoke_sessions,
    tenant_db,
    verify_password,
)
from app.identity.rbac import Permission
from app.ingestion import csv_import
from app.ingestion.normalise import NormalisationError, normalise_amount, normalise_phone
from app.models import (
    AccountStatus,
    AuditLog,
    BlackoutDate,
    Buyer,
    BuyerPhone,
    CallerId,
    Channel,
    Company,
    CompanyProfile,
    CourtCase,
    CreditAssessment,
    CreditNote,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    CreditAccount,
    EntityCandidate,
    EscalationLevel,
    EscalationState,
    GstRecord,
    ImportBatch,
    Invoice,
    InvoiceStatus,
    LegalLink,
    Message,
    MessageTemplate,
    Payment,
    PaymentBehaviour,
    Promise,
    TemplateVersion,
    User,
    VerificationReport,
)
from app.providers.base import REGISTRY
from app.render.numbers import format_inr
from app.scheduler import campaign as campaign_ops
from app.trade import (
    CLOSED_ACCOUNT_STATUSES,
    accounts as account_ops,
    consent as consent_ops,
    reduction,
)
from app.trade.ageing import buyer_position, days_past_due
from app.trade.allocation import AllocationError, apply_payment

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------- auth

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, session: Session = Depends(lambda: None)):
    # Login cannot use `tenant_db`: there is no tenant until the user is known.
    # It reads through the cross-tenant path and binds nothing.
    from app.db import admin_session

    with admin_session() as s:
        # Emails are unique per (company_id, email), so one address may exist
        # in several tenants and this cross-tenant lookup may return several
        # rows. The password is what picks the account — never row order — and
        # every candidate's hash is verified even after one matches, so the
        # response time does not say how many tenants know this address.
        candidates = list(
            s.execute(select(User).where(User.email == body.email)).scalars()
        )
        user = None
        for candidate in candidates:
            ok = verify_password(body.password, candidate.password_hash)
            if ok and candidate.is_active and user is None:
                user = candidate
        if user is None:
            # Deliberately identical for "no such user" and "wrong password".
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

        roles = sorted(user.roles)
        company_id = user.company_id
        user.last_login_at = datetime.now(timezone.utc)
        audit.record(
            s, action=audit.Action.LOGIN, company_id=company_id, actor_id=user.id,
            actor_label=user.email,
        )
        tokens = TokenResponse(
            access_token=create_access_token(
                user_id=user.id, company_id=company_id, roles=roles
            ),
            refresh_token=create_refresh_token(user_id=user.id, company_id=company_id),
            roles=roles,
            company_id=company_id,
        )
    return tokens


@auth_router.get("/config")
def auth_config():
    """What the sign-in page should render. Public by necessity.

    The anon key is safe to publish — that is its purpose; it grants nothing on
    its own, and every Supabase policy still applies. The JWT secret and service
    key are never exposed here.
    """
    from app.config import settings as cfg

    return {
        "backend": cfg.auth_backend,
        "supabase": (
            {"url": cfg.supabase_url, "anon_key": cfg.supabase_anon_key}
            if cfg.auth_backend == "supabase"
            else None
        ),
    }


@auth_router.post("/register", status_code=201)
def register(body: dict, request: Request):
    """Self-serve signup. Creates a **new company** — never joins an existing one.

    This distinction is the whole security model of the endpoint. Letting a
    stranger who can authenticate attach themselves to a company that already
    exists would hand them that company's debtor list, which is the single worst
    thing this system could do. So:

    * already linked -> return their existing account, unchanged
    * anyone else    -> create a fresh, empty company and make them owner

    Joining an existing tenant happens only through `/api/auth/accept-invite`,
    against a signed invite token. A matching email deliberately counts for
    nothing here: an email is a claim, not a credential, and honouring it let
    whoever typed a stranger's address into a tenant first capture the account
    that later signed in with it.
    """
    from app.db import admin_session
    from app.identity.supabase import verify
    from app.models import Company, RoleGrant

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    identity = verify(auth.split(" ", 1)[1])

    if not identity.email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "this identity provider returned no email"
        )

    company_name = (body or {}).get("company_name", "").strip()

    with admin_session() as s:
        existing = s.execute(
            select(User).where(User.external_auth_id == identity.subject)
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "already_registered",
                "company_id": str(existing.company_id),
                "roles": sorted(existing.roles),
            }

        if not company_name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "company_name is required to create a new account",
            )

        company = Company(name=company_name)
        s.add(company)
        s.flush()
        user = User(
            company_id=company.id,
            email=identity.email,
            external_auth_id=identity.subject,
            password_hash=None,  # the identity provider holds the credential
        )
        s.add(user)
        s.flush()
        # The first user of a company owns it. `legal_approver` is deliberately
        # NOT granted — approving legal content stays a separate, deliberate act.
        for role in ("owner", "admin"):
            s.add(RoleGrant(company_id=company.id, user_id=user.id, role=role))
        audit.record(
            s, action=audit.Action.USER_CREATED, company_id=company.id,
            actor_id=user.id, actor_label=identity.email,
            detail="self-serve registration; new company created",
        )
        return {
            "status": "company_created",
            "company_id": str(company.id),
            "company_name": company.name,
            "roles": ["admin", "owner"],
        }


@auth_router.post("/invite", status_code=201)
def invite_user(
    body: dict,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.USER_MANAGE)),
):
    """Invite a colleague into *your* company: a signed grant, not a row.

    Nothing is written to `users` here. The earlier design pre-created the row
    and let the first sign-in with a matching email claim it — so an admin
    could type anyone's address and capture the account behind it. The token
    that replaces the row is signed, names email + company + roles, and dies in
    seven days; the row is created at acceptance, bound to a real credential.
    """
    email = (body or {}).get("email", "").strip().lower()
    roles = (body or {}).get("roles") or ["viewer"]
    if not email or "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a valid email is required")

    from app.identity.rbac import Role

    valid = {r.value for r in Role}
    unknown = set(roles) - valid
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown role(s): {', '.join(sorted(unknown))}"
        )

    # Scoped to this company. The same address may exist in other tenants, and
    # whether it does is none of this tenant's business.
    if session.execute(
        select(User).where(
            User.company_id == principal.company_id, func.lower(User.email) == email
        )
    ).scalars().first() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that email already has an account in this company"
        )

    invite_token, expires_at = create_invite_token(
        email=email, company_id=principal.company_id, roles=list(roles)
    )
    audit.record_for(
        principal=principal, session=session, action=audit.Action.USER_INVITED,
        entity_type="invite", after={"email": email, "roles": list(roles)},
    )
    session.commit()
    return {
        "invite_token": invite_token,
        "expires_at": expires_at.isoformat(),
        "email": email,
        "roles": list(roles),
        "status": "invited",
    }


@auth_router.post("/accept-invite", status_code=201)
def accept_invite(body: dict, request: Request):
    """Join the company named in a signed invite token.

    The token — not any pre-created row — is the grant. The credential that
    must accompany it mirrors the auth backend:

    * supabase — a verified Supabase bearer token whose email equals the
      invite's. The row is created linked to that verified subject, with
      company and roles taken from the **invite**; the bearer token contributes
      identity and nothing else.
    * local — a password, hashed into the new row.
    """
    from sqlalchemy.exc import IntegrityError

    from app.config import settings
    from app.db import admin_session
    from app.identity.auth import decode_token, hash_password
    from app.models import RoleGrant

    invite_token = str((body or {}).get("invite_token") or "")
    if not invite_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invite_token is required")

    claims = decode_token(invite_token, expected_type="invite")
    invite_email = (claims.get("email") or "").lower()
    raw_company = claims.get("company_id")
    roles = claims.get("roles") or []
    if not invite_email or not raw_company:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed invite token")
    try:
        company_id = UUID(str(raw_company))
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed invite token")

    external_auth_id = None
    password_hash = None
    if settings.auth_backend == "supabase":
        from app.identity.supabase import verify

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "sign in first, then accept the invite"
            )
        identity = verify(auth.split(" ", 1)[1])
        # The invite grants membership to one mailbox. A verified token for any
        # other mailbox is someone else, however they came by the invite link.
        if not identity.email or identity.email.lower() != invite_email:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this invite was issued to a different email address",
            )
        external_auth_id = identity.subject
    else:
        password = (body or {}).get("password") or ""
        if not password:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "a password is required to accept an invite"
            )
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    with admin_session() as s:
        company = s.get(Company, company_id)
        if company is None or not company.is_active:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "the inviting company no longer exists"
            )
        if external_auth_id is not None and s.execute(
            select(User).where(User.external_auth_id == external_auth_id)
        ).scalars().first() is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "this identity is already linked to an account"
            )
        if s.execute(
            select(User).where(
                User.company_id == company_id, func.lower(User.email) == invite_email
            )
        ).scalars().first() is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "that email already has an account in this company",
            )
        user = User(
            company_id=company_id,
            email=invite_email,
            external_auth_id=external_auth_id,
            password_hash=password_hash,
        )
        s.add(user)
        try:
            s.flush()
        except IntegrityError:
            # A concurrent acceptance of the same invite. The unique
            # constraints are the arbiter, and second place is a conflict.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "that email already has an account in this company",
            )
        for role in roles:
            s.add(RoleGrant(company_id=company_id, user_id=user.id, role=role))
        audit.record(
            s, action=audit.Action.USER_CREATED, company_id=company_id,
            actor_id=user.id, actor_label=invite_email, detail="accepted invitation",
        )
        user_id = user.id

    return {
        "status": "joined",
        "user_id": str(user_id),
        "company_id": str(company_id),
        "email": invite_email,
        "roles": sorted(roles),
    }


@auth_router.post("/revoke-sessions")
def revoke_user_sessions(
    body: dict | None = None,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(current_principal),
):
    """Invalidate every token issued to a user before this moment.

    Self-service by design — walking away from a compromised session must not
    need an admin — and USER_MANAGE for anyone else in the company. Another
    tenant's user id is indistinguishable from a nonexistent one: RLS makes
    both a 404.
    """
    raw = (body or {}).get("user_id")
    target_id = None
    if raw is not None:
        try:
            target_id = UUID(str(raw))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "user_id must be a UUID"
            )

    if target_id is None or target_id == principal.user_id:
        if principal.user_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "an API key holds no user sessions to revoke"
            )
        target_id = principal.user_id
    elif not principal.can(Permission.USER_MANAGE):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"missing permission {Permission.USER_MANAGE.value}",
        )

    user = session.get(User, target_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user in this company")
    revoke_sessions(user)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.SESSIONS_REVOKED,
        entity_type="user", entity_id=user.id,
    )
    session.commit()
    return {"status": "sessions revoked", "user_id": str(user.id)}


@auth_router.get("/me")
def me(principal: Principal = Depends(current_principal)):
    return {
        "user_id": principal.user_id,
        "company_id": principal.company_id,
        "roles": sorted(principal.roles),
        "via_api_key": principal.via_api_key,
    }


# --------------------------------------------------------------------- trade

trade_router = APIRouter(prefix="/trade", tags=["trade"])


def _account_out(session: Session, account: CreditAccount, now: datetime) -> AccountOut:
    state = session.execute(
        select(EscalationState).where(EscalationState.account_id == account.id)
    ).scalar_one_or_none()
    return AccountOut(
        id=account.id,
        buyer_id=account.buyer_id,
        invoice_id=account.invoice_id,
        invoice_ref=account.invoice_ref,
        outstanding_paise=account.outstanding_paise,
        outstanding_display=format_inr(account.outstanding_paise),
        due_date=account.due_date,
        status=account.status.value,
        days_past_due=days_past_due(account.due_date.date(), now.date()),
        level=state.level.value if state else None,
    )


@trade_router.get("/buyers", response_model=list[BuyerOut])
def list_buyers(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    limit: int = Query(50, le=500),
    offset: int = 0,
    search: str | None = None,
):
    stmt = select(Buyer).order_by(Buyer.name).limit(limit).offset(offset)
    if search:
        stmt = stmt.where(Buyer.name.ilike(f"%{search}%"))
    return list(session.execute(stmt).scalars())


def _attach_phone(session, buyer, raw: str, *, company_id, priority: int = 0):
    """Normalise one number onto a buyer, or refuse the whole request.

    Refusing is the point. A number stored as the operator typed it is a number
    that will not dial, and it will not fail until the campaign runs at 09:00 —
    by which time nobody remembers typing it.
    """
    try:
        parsed = normalise_phone(raw)
    except NormalisationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{raw!r}: {exc}")

    # The unique constraint is (buyer_id, e164); catching it here gives the
    # operator a sentence instead of a 500 from the driver.
    existing = session.execute(
        select(BuyerPhone).where(
            BuyerPhone.buyer_id == buyer.id, BuyerPhone.e164 == parsed.e164
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    phone = BuyerPhone(
        company_id=company_id,
        buyer_id=buyer.id,
        e164=parsed.e164,
        number_type=parsed.number_type,
        priority=priority,
        # DND is deliberately left UNKNOWN. It is not something a form can
        # assert — it comes from a scrub, and until then the policy engine
        # treats it as blocking.
    )
    session.add(phone)
    return phone


@trade_router.post("/buyers", response_model=BuyerOut, status_code=201)
def create_buyer(
    body: BuyerIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    fields = body.model_dump()
    phones = fields.pop("phones", [])
    amount = fields.pop("amount", None)
    due_date = fields.pop("due_date", None)
    invoice_number = fields.pop("invoice_number", None)

    # Parse the money before writing anything. An amount that cannot be read is
    # not a reason to create a buyer with the debt silently missing — that buyer
    # would sit at zero outstanding and never be escalated against.
    amount_paise = None
    if amount is not None and str(amount).strip():
        if due_date is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a due date is required with an amount — ageing and the "
                "escalation ladder are both measured from it",
            )
        try:
            amount_paise = normalise_amount(amount)
        except NormalisationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        if amount_paise <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be more than zero"
            )

    buyer = Buyer(company_id=principal.company_id, **fields)
    session.add(buyer)
    # Flush, not commit: the phones need buyer.id, and a buyer that is saved
    # while its numbers are rejected is worse than no buyer at all.
    session.flush()
    for i, raw in enumerate(phones):
        if str(raw).strip():
            _attach_phone(session, buyer, raw, company_id=principal.company_id, priority=i)

    if amount_paise is not None:
        today = datetime.now(timezone.utc).date()
        invoice = Invoice(
            company_id=principal.company_id,
            buyer_id=buyer.id,
            invoice_number=invoice_number or f"OPEN-{str(buyer.id)[:8].upper()}",
            # An invoice already past due was issued before it fell due; dating
            # it today would make a 90-day debt look brand new in the ageing.
            issue_date=min(due_date, today),
            due_date=due_date,
            gross_paise=amount_paise,
            tax_paise=0,
            net_paise=amount_paise,
            outstanding_paise=amount_paise,
            status=InvoiceStatus.OPEN,
        )
        session.add(invoice)
        session.flush()
        # The recovery-facing CreditAccount is derived from the invoice rather
        # than written alongside it, so there is one way an account comes to
        # exist and the two cannot drift.
        account_ops.sync_account_from_invoice(session, invoice)

    session.commit()
    session.refresh(buyer)
    return buyer


@trade_router.get("/buyers/{buyer_id}", response_model=BuyerOut)
def get_buyer(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
):
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")
    return buyer


@trade_router.post("/buyers/{buyer_id}/phones", response_model=BuyerOut, status_code=201)
def add_buyer_phone(
    buyer_id: UUID,
    body: PhoneIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")
    _attach_phone(
        session, buyer, body.raw, company_id=principal.company_id, priority=body.priority
    )
    session.commit()
    session.refresh(buyer)
    return buyer


# ------------------------------------------- buyer identity and verification
#
# Who this debtor is in the registries' terms rather than in the operator's.
# Everything downstream that names a company outside this building — a demand
# call, a legal notice, a registry listing — keys off what these four routes
# establish, which is why a typed-in identifier is stored but never counts as
# verified, and why a borderline match waits for a person instead of merging.


def _buyer_profile(session: Session, buyer_id: UUID) -> CompanyProfile | None:
    """The resolved-entity row for one buyer.

    `CompanyProfile` doubles as the tenant's own profile — that is the row with
    *no* buyer attached, which is why `_profile_row` filters the mirror image of
    this. Without the filter each would happily return the other's row.
    """
    return session.execute(
        select(CompanyProfile)
        .where(CompanyProfile.buyer_id == buyer_id)
        .order_by(CompanyProfile.created_at.desc())
    ).scalars().first()


def _clean_gstin(raw: str | None, *, state_code: str | None) -> str | None:
    """Check a GSTIN, or refuse the request saying which check failed.

    The refusal carries `check.detail` verbatim. It is written for a person who
    has just mistyped something and has to find the character, and rephrasing it
    here would produce a second, vaguer version of the same sentence.
    """
    if not raw or not raw.strip():
        return None
    check = validate_gstin(raw)
    if not check.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{check.value!r} is not a usable GSTIN: {check.detail}",
        )
    if state_code and check.value[:2] != state_code:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"state code {state_code!r} contradicts the GSTIN, which is "
            f"registered in state {check.value[:2]!r} — one of the two is wrong, "
            f"and guessing which would be guessing at a company's identity",
        )
    return check.value


def _clean_cin(raw: str | None) -> str | None:
    """Structure only — a CIN carries no check digit to verify it against."""
    if not raw or not raw.strip():
        return None
    check = validate_cin(raw)
    if not check.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{check.value!r} is not a usable CIN: {check.detail}",
        )
    return check.value


def _identity_out(buyer: Buyer, profile: CompanyProfile | None) -> BuyerIdentityOut:
    return BuyerIdentityOut(
        buyer_id=buyer.id,
        name=(profile.legal_name if profile and profile.legal_name else buyer.name),
        gstin=profile.gstin if profile else None,
        cin=profile.cin if profile else None,
        pan_last4=profile.pan_last4 if profile else None,
        registered_address=profile.registered_address if profile else None,
        state_code=profile.state_code if profile else None,
    )


@trade_router.put("/buyers/{buyer_id}/identity", response_model=BuyerIdentityOut)
def set_buyer_identity(
    buyer_id: UUID,
    body: BuyerIdentityIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Record what we have been *told* this buyer is.

    A full replacement of the declared identity, and never a promotion. Editing
    it voids whatever tier a previous verification reached: the row goes back to
    `self_declared` and has to be verified again. That is heavier than tracking
    which particular field moved, and it is the right weight — the alternative
    is a profile still labelled IDENTIFIER while carrying a name, an address or
    a number that a person typed after the registry was last consulted, and it
    is the label that a notice or a listing is issued against.
    """
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    state_code = (body.state_code or "").strip().upper() or None
    gstin = _clean_gstin(body.gstin, state_code=state_code)
    cin = _clean_cin(body.cin)
    state_code = state_code or state_from_gstin(gstin)

    pan_hash = pan_last4 = None
    if body.pan and body.pan.strip():
        pan = body.pan.strip().upper()
        if not PAN_RE.match(pan):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "that is not shaped like a PAN — five letters, four digits, "
                "one letter",
            )
        # Hashed in this frame and dropped. Nothing downstream holds the number.
        pan_hash, pan_last4 = hash_pan(pan)

    row = _buyer_profile(session, buyer_id)
    if row is None:
        row = CompanyProfile(
            company_id=principal.company_id,
            buyer_id=buyer_id,
            legal_name=(body.legal_name or "").strip() or buyer.name,
        )
        session.add(row)

    if body.legal_name and body.legal_name.strip():
        row.legal_name = body.legal_name.strip()
    row.gstin = gstin
    row.cin = cin
    # The buyer row carries whatever an import or an invoice put there, and
    # verification refuses to run at all while the two declarations name
    # different companies. This endpoint is the only place a person answers that
    # question, and the answer has to land on both rows or the refusal has no
    # exit: nothing else in the HTTP surface can write these two columns.
    buyer.gstin = gstin
    buyer.cin = cin
    row.pan_hash = pan_hash
    row.pan_last4 = pan_last4
    row.registered_address = (body.registered_address or "").strip() or None
    row.state_code = state_code
    # Registry findings, cleared along with the tier that vouched for them.
    row.status = None
    row.resolution_tier = "self_declared"
    row.confidence = 0
    row.resolved_at = None
    session.flush()

    audit.record_for(
        session,
        principal,
        action="company.identity.declared",
        entity_type="buyer",
        entity_id=buyer_id,
        after={
            "legal_name": row.legal_name,
            "gstin": row.gstin,
            "cin": row.cin,
            # The last four only, for the same reason the column holds only that.
            "pan_last4": row.pan_last4,
            "state_code": row.state_code,
            "resolution_tier": row.resolution_tier,
        },
    )
    session.commit()
    session.refresh(row)
    return _identity_out(buyer, row)


@trade_router.post(
    "/buyers/{buyer_id}/verify", response_model=VerificationOut, status_code=201
)
def verify_buyer_identity(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_VERIFY)),
):
    """Run the registries against what we hold, and record the answer.

    201 because every run writes a new record rather than replacing the last
    one. "What did we know in March" has to stay answerable, and a verification
    that overwrote its predecessor would make March unanswerable the moment
    somebody clicked the button again.
    """
    from app.company.verification import verify_buyer

    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    gst, mca = build_gst_backend(), build_mca_backend()
    now = datetime.now(timezone.utc)

    # Two audit rows for one click, and deliberately not the same row twice:
    # this one says who asked and which adapters were configured when they did,
    # and `verify_buyer` writes what came back. A run that found nothing and a
    # run against a manual backend look identical in the outcome; here they do
    # not.
    audit.record_for(
        session,
        principal,
        action="company.verification.requested",
        entity_type="buyer",
        entity_id=buyer_id,
        after={
            "gst_backend": getattr(gst, "name", None),
            "mca_backend": getattr(mca, "name", None),
        },
    )

    outcome = verify_buyer(
        session,
        buyer_id,
        company_id=principal.company_id,
        actor_label=principal.label or str(principal.user_id),
        gst=gst,
        mca=mca,
        now=now,
        issued_by=principal.user_id,
    )
    session.commit()

    return VerificationOut(
        buyer_id=buyer_id,
        buyer_name=buyer.name,
        tier=outcome.tier,
        confidence=outcome.confidence,
        publishable=outcome.publishable,
        gst_status=outcome.gst_status,
        mca_status=outcome.mca_status,
        signals=list(outcome.signals),
        blockers=list(outcome.blockers),
        candidate_id=outcome.candidate_id,
        verified_at=now,
    )


@trade_router.get("/buyers/{buyer_id}/verification", response_model=VerificationOut)
def get_buyer_verification(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
):
    """The most recent verification. 404 when nobody has ever run one.

    404 rather than an empty body with `publishable: false`: "never checked" and
    "checked and failed" are different facts, and a caller that cannot tell them
    apart will eventually treat the first as the second.
    """
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    row = session.execute(
        select(VerificationReport)
        .join(CompanyProfile, CompanyProfile.id == VerificationReport.profile_id)
        .where(CompanyProfile.buyer_id == buyer_id)
        .order_by(VerificationReport.issued_at.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "this buyer has never been verified"
        )

    payload = row.payload or {}
    return VerificationOut(
        buyer_id=buyer_id,
        buyer_name=buyer.name,
        tier=payload.get("tier") or "NONE",
        confidence=float(payload.get("confidence", row.confidence or 0)),
        publishable=bool(payload.get("publishable", False)),
        gst_status=payload.get("gst_status"),
        mca_status=payload.get("mca_status"),
        signals=payload.get("signals") or [],
        blockers=payload.get("blockers") or [],
        candidate_id=payload.get("candidate_id"),
        verified_at=row.issued_at,
    )


@trade_router.post("/entity-candidates/{candidate_id}/review")
def review_entity_candidate(
    candidate_id: UUID,
    body: EntityCandidateReviewIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.ENTITY_CONFIRM)),
):
    """Confirm or reject a proposed match. Once, and then never again.

    Note what confirming does *not* do: it does not raise the tier. A candidate
    exists precisely because the evidence fell short of an identifier match, and
    a person agreeing with a name resemblance does not turn it into one. The
    profile records the tier the evidence actually reached, so the publication
    gate and the credit-scoring gate both still see a resemblance for what it
    is; what the confirmation buys is a usable profile and a named reviewer.
    """
    # The named reviewer is half of what this route produces, and `reviewed_by`
    # takes a user. An API key scoped `entity:confirm` clears the permission
    # check carrying no user id, and would file the decision as nobody's —
    # which is the one thing a separation-of-duties gate cannot record.
    if principal.user_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "deciding whose company this is needs a named user; an API key "
            "cannot be the person who signed off on it",
        )

    candidate = session.get(EntityCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no such entity candidate"
        )
    if candidate.status != "PROPOSED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this candidate was already {candidate.status.lower()}; run a new "
            f"verification rather than overwriting the record of who decided "
            f"what, and when",
        )

    now = datetime.now(timezone.utc)
    decision = "CONFIRMED" if body.decision == "CONFIRM" else "REJECTED"
    profile_id = None

    if decision == "CONFIRMED":
        row = _buyer_profile(session, candidate.buyer_id)
        if row is None:
            row = CompanyProfile(
                company_id=principal.company_id,
                buyer_id=candidate.buyer_id,
                legal_name=candidate.candidate_name,
            )
            session.add(row)
        if row.resolution_tier != Tier.IDENTIFIER.value:
            # Never downgrade a profile that already matched on an identifier.
            # A weaker candidate arriving afterwards is new evidence about the
            # same buyer, not a reason to forget the decisive evidence — and the
            # identifier is the decisive part, so it is inside this guard too.
            # Left outside it, confirming a stale candidate would swap the GSTIN
            # on a row still stamped IDENTIFIER, and every downstream reader
            # (`_registry_signals`, `_gst_filing_regular`) trusts that stamp.
            row.legal_name = candidate.candidate_name
            row.resolution_tier = candidate.tier
            row.confidence = candidate.score
            row.resolved_at = now
            if candidate.identifier_kind == "gstin" and candidate.identifier_value:
                row.gstin = candidate.identifier_value
                row.state_code = state_from_gstin(row.gstin) or row.state_code
            elif candidate.identifier_kind == "cin" and candidate.identifier_value:
                row.cin = candidate.identifier_value
        session.flush()
        profile_id = row.id

    candidate.status = decision
    candidate.reviewed_by = principal.user_id
    candidate.reviewed_at = now

    audit.record_for(
        session,
        principal,
        action="company.candidate.reviewed",
        entity_type="entity_candidate",
        entity_id=candidate_id,
        after={
            "decision": decision,
            "buyer_id": str(candidate.buyer_id),
            "candidate_name": candidate.candidate_name,
            "tier": candidate.tier,
            "profile_id": str(profile_id) if profile_id else None,
            "note": body.note,
        },
    )
    session.commit()
    return {"status": decision}


# ------------------------------------------------------- credit eligibility
#
# Recommendation only. Nothing here approves or declines a limit: the score, the
# suggested ceiling and every reason behind them are returned, and a person then
# records what they decided. See app/intelligence/creditworthiness.py for why
# that split is deliberate rather than unfinished.


def _money(raw, field: str) -> int:
    try:
        return normalise_amount(raw)
    except NormalisationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field}: {exc}")


def _ledger_facts(session: Session, buyer_id: UUID, now: datetime):
    """What our own books say about this buyer.

    Read from the ledger, never from the submitted form. The declared fields on
    a credit application are the buyer's claims; these are facts. Letting an
    applicant supply their own payment history would defeat the whole check.
    """
    from app.intelligence.creditworthiness import LedgerFacts

    settled = session.execute(
        select(func.count())
        .select_from(CreditAccount)
        .where(
            CreditAccount.buyer_id == buyer_id,
            CreditAccount.status == AccountStatus.SETTLED,
        )
    ).scalar_one()

    # Closed, not merely settled. A written-off account keeps the balance that
    # is genuinely still owed, so excluding SETTLED alone would score a debt this
    # side abandoned as currently overdue against the buyer.
    open_rows = list(
        session.execute(
            select(CreditAccount).where(
                CreditAccount.buyer_id == buyer_id,
                CreditAccount.status.notin_(CLOSED_ACCOUNT_STATUSES),
            )
        ).scalars()
    )
    overdue = sum(
        a.outstanding_paise for a in open_rows if a.due_date.date() < now.date()
    )
    worst = max(
        (days_past_due(a.due_date.date(), now.date()) for a in open_rows), default=0
    )

    behaviour = session.execute(
        select(PaymentBehaviour)
        .where(PaymentBehaviour.buyer_id == buyer_id)
        .order_by(PaymentBehaviour.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()

    return LedgerFacts(
        invoices_settled=settled,
        mean_days_to_pay=(
            float(behaviour.mean_days_to_pay)
            if behaviour is not None and behaviour.mean_days_to_pay is not None
            else None
        ),
        promise_kept_rate=(
            float(behaviour.promise_kept_rate)
            if behaviour is not None and behaviour.promise_kept_rate is not None
            else None
        ),
        currently_overdue_paise=int(overdue),
        max_days_past_due=int(worst),
    )


def _gst_filing_regular(session: Session, profile: CompanyProfile) -> bool | None:
    """Whether GST returns are being filed, or None when we cannot say.

    Read from the registration status rather than from the filing list. A GSTIN
    cancelled suo motu is cancelled *for* non-filing, which makes the status the
    more reliable of the two — and the only one whose shape is fixed by the
    adapter contract rather than by whatever a portal happened to return.

    Only registry-fetched records are considered, which is the provenance rule
    reaching this far down: a status a person read off a website and typed in is
    a claim, and a claim must not become a scored fact about somebody's
    creditworthiness. The filter is in the query rather than on the row that
    comes back, so an operator-entered record landing after a real fetch does
    not erase what the fetch found.
    """
    if not profile.gstin:
        return None
    record = session.execute(
        select(GstRecord)
        .where(
            GstRecord.gstin == profile.gstin,
            GstRecord.provenance == REGISTRY,
        )
        .order_by(GstRecord.fetched_at.desc())
        .limit(1)
    ).scalars().first()
    if record is None:
        return None
    registration = (record.status or "").strip().lower()
    if not registration:
        return None
    return registration == "active"


def _registry_signals(
    session: Session, profile: CompanyProfile | None
) -> tuple[str | None, bool | None]:
    """Registry facts fit to score with, or nothing at all.

    Nothing at all is the safe answer and the usual one: `score_buyer` reacts to
    `company_status` only when it is something other than Active, and to
    `gst_filing_regular` only when it is explicitly False. A buyer nobody has
    verified therefore scores exactly as they did before any of this existed.

    The tier gate is the whole point of the feature. Below IDENTIFIER a profile
    is a resemblance — a similar name, or a figure somebody copied off a portal
    — and a resemblance must not be allowed to decide whether a real company
    gets credit. Only an identifier match, which the provenance rule reserves to
    data a registry actually returned, opens this gate.
    """
    if profile is None or profile.resolution_tier != Tier.IDENTIFIER.value:
        return None, None
    return profile.status, _gst_filing_regular(session, profile)


def _assessment_out(row: CreditAssessment, buyer_name: str) -> CreditCheckOut:
    return CreditCheckOut(
        id=row.id,
        buyer_id=row.buyer_id,
        buyer_name=buyer_name,
        recommendation=row.recommendation,
        score=float(row.score),
        suggested_limit_paise=row.suggested_limit_paise,
        suggested_limit_display=format_inr(row.suggested_limit_paise),
        requested_limit_paise=row.requested_limit_paise,
        requested_limit_display=format_inr(row.requested_limit_paise),
        factors=row.factors,
        blockers=row.blockers,
        model_version=row.model_version,
        as_of=row.as_of,
        computed_at=row.computed_at,
        decided_limit_paise=row.decided_limit_paise,
        decided_limit_display=(
            format_inr(row.decided_limit_paise)
            if row.decided_limit_paise is not None
            else None
        ),
        decided_at=row.decided_at,
        decision_note=row.decision_note,
    )


@trade_router.post(
    "/buyers/{buyer_id}/credit-check", response_model=CreditCheckOut, status_code=201
)
def run_credit_check(
    buyer_id: UUID,
    body: CreditCheckIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    from app.intelligence import creditworthiness as cw
    from app.intelligence.scoring import ScoringContext, score_buyer

    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    now = datetime.now(timezone.utc)
    requested = _money(body.requested_limit, "requested limit")
    if requested <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "requested limit must be more than zero",
        )
    turnover = (
        _money(body.annual_turnover, "annual turnover") if body.annual_turnover else None
    )
    other = (
        _money(body.other_creditor_exposure, "other creditor exposure")
        if body.other_creditor_exposure
        else 0
    )

    ledger = _ledger_facts(session, buyer_id, now)

    # What the registries say, but only if this buyer was resolved decisively.
    profile = _buyer_profile(session, buyer_id)
    company_status, gst_filing_regular = _registry_signals(session, profile)

    # Recovery risk is computed from our own ledger too, and is the heaviest
    # single input. A buyer we have never traded with has none, and the engine
    # treats "no history" as weak evidence rather than as good news — but a
    # struck-off company is not a good risk merely because it has not owed us
    # anything yet, so a registry signal is enough on its own to score.
    risk = None
    if (
        ledger.invoices_settled
        or ledger.currently_overdue_paise
        or ledger.max_days_past_due
        or company_status
        or gst_filing_regular is False
    ):
        risk = score_buyer(
            ScoringContext(
                as_of=now.date(),
                mean_days_to_pay=ledger.mean_days_to_pay,
                promise_kept_rate=ledger.promise_kept_rate,
                outstanding_paise=ledger.currently_overdue_paise,
                max_days_past_due=ledger.max_days_past_due,
                company_status=company_status,
                gst_filing_regular=gst_filing_regular,
            )
        )

    # One question, one answer. A declared "GST registered?" tick and a resolved
    # profile can disagree, and an assessment recording both is unreadable six
    # months later when somebody asks which one the decision rested on. Where the
    # buyer was resolved against a registry, what we resolved wins and the form is
    # discarded — including when it says no and the registry returned a GSTIN.
    #
    # Gated on the same tier as `_registry_signals`, and for the same reason. A
    # GSTIN somebody typed into the identity form is a claim; scoring it as a
    # positive factor and filing it as `resolved_profile` would make the
    # assessment say a registry confirmed something nobody checked.
    gst_registered = body.gst_registered
    gst_registered_source = "declared"
    if profile is not None and profile.resolution_tier == Tier.IDENTIFIER.value:
        gst_registered = bool(profile.gstin)
        gst_registered_source = "resolved_profile"

    application = cw.CreditApplication(
        requested_limit_paise=requested,
        annual_turnover_paise=turnover,
        years_trading=body.years_trading,
        # From our ledger, not the form. What they owe *us* is not something the
        # applicant gets to state.
        existing_exposure_paise=ledger.currently_overdue_paise,
        other_creditor_exposure_paise=other,
        trade_references=body.trade_references,
        gst_registered=gst_registered,
        turnover_verified=body.turnover_verified,
        notes=body.notes,
    )
    result = cw.assess(application, as_of=now.date(), risk=risk, ledger=ledger)

    row = CreditAssessment(
        company_id=principal.company_id,
        buyer_id=buyer_id,
        requested_limit_paise=result.requested_limit_paise,
        suggested_limit_paise=result.suggested_limit_paise,
        score=result.score,
        recommendation=result.recommendation.value,
        factors=[f.as_dict() for f in result.factors],
        blockers=list(result.blockers),
        # Stored so the assessment can be reconstructed later: the declared
        # figures are a snapshot of what was claimed on the day.
        application={
            "requested_limit_paise": requested,
            "annual_turnover_paise": turnover,
            "years_trading": body.years_trading,
            "other_creditor_exposure_paise": other,
            "trade_references": body.trade_references,
            "gst_registered": gst_registered,
            # Which of the two answers was used, kept alongside the value so the
            # assessment still explains itself after the profile has moved on.
            "gst_registered_source": gst_registered_source,
            "gst_registered_declared": body.gst_registered,
            "registry_company_status": company_status,
            "registry_gst_filing_regular": gst_filing_regular,
            "turnover_verified": body.turnover_verified,
            "notes": body.notes,
        },
        model_version=result.model_version,
        as_of=result.as_of,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    audit.record_for(
        session,
        principal,
        action="credit.assessed",
        entity_type="buyer",
        entity_id=buyer_id,
        after={
            "recommendation": result.recommendation.value,
            "score": result.score,
            "suggested_limit_paise": result.suggested_limit_paise,
            "model_version": result.model_version,
        },
    )
    session.commit()
    return _assessment_out(row, buyer.name)


@trade_router.get("/buyers/{buyer_id}/credit-check", response_model=list[CreditCheckOut])
def list_credit_checks(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    limit: int = Query(10, le=50),
):
    """Newest first, and history is kept.

    A limit granted in March is explained by the assessment that was current in
    March, not by today's.
    """
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")
    rows = session.execute(
        select(CreditAssessment)
        .where(CreditAssessment.buyer_id == buyer_id)
        .order_by(CreditAssessment.computed_at.desc())
        .limit(limit)
    ).scalars()
    return [_assessment_out(r, buyer.name) for r in rows]


@trade_router.post("/credit-check/{assessment_id}/decide", response_model=CreditCheckOut)
def decide_credit_limit(
    assessment_id: UUID,
    body: CreditDecisionIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_SETTINGS)),
):
    """Record what a person decided.

    Separate endpoint, separate permission, separate columns from the
    suggestion. Granting credit is a commercial decision with money behind it,
    so it should not be something an operator does by accident while reading a
    score — and where the decision differs from the recommendation, that gap is
    exactly what an auditor will want to see.
    """
    row = session.get(CreditAssessment, assessment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such assessment")
    if row.decided_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this assessment has already been decided; run a new check rather "
            "than overwriting the record of what was decided before",
        )

    approved = _money(body.approved_limit, "approved limit")
    if approved < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "limit cannot be negative"
        )

    row.decided_by = principal.user_id
    row.decided_limit_paise = approved
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = body.note
    session.commit()

    buyer = session.get(Buyer, row.buyer_id)
    audit.record_for(
        session,
        principal,
        action="credit.decided",
        entity_type="buyer",
        entity_id=row.buyer_id,
        after={
            "approved_limit_paise": approved,
            "suggested_limit_paise": row.suggested_limit_paise,
            "recommendation": row.recommendation,
            "note": body.note,
        },
    )
    session.commit()
    session.refresh(row)
    return _assessment_out(row, buyer.name if buyer is not None else "")


@trade_router.get("/buyers/{buyer_id}/position")
def get_position(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    as_of: date | None = None,
):
    """Ageing is always as of a date, so a report reproduces historically."""
    as_of = as_of or datetime.now(timezone.utc).date()
    position = buyer_position(session, buyer_id, as_of)
    return {
        "buyer_id": position.buyer_id,
        "as_of": position.as_of,
        "total_outstanding_paise": position.total_outstanding_paise,
        "total_outstanding_display": format_inr(position.total_outstanding_paise),
        "invoice_count": position.invoice_count,
        "max_days_past_due": position.max_days_past_due,
        "buckets": {b.value: v for b, v in position.buckets.items()},
    }


@trade_router.get("/accounts", response_model=list[AccountOut])
def list_accounts(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
):
    now = datetime.now(timezone.utc)
    stmt = select(CreditAccount).limit(limit)
    if status_filter:
        stmt = stmt.where(CreditAccount.status == AccountStatus(status_filter))
    return [_account_out(session, a, now) for a in session.execute(stmt).scalars()]


@trade_router.post("/invoices", status_code=201)
def create_invoice(
    body: InvoiceIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    invoice = Invoice(
        company_id=principal.company_id,
        buyer_id=body.buyer_id,
        invoice_number=body.invoice_number,
        issue_date=body.issue_date,
        due_date=body.due_date,
        gross_paise=body.amount_paise,
        tax_paise=0,
        net_paise=body.amount_paise,
        outstanding_paise=body.amount_paise,
        status=InvoiceStatus.OPEN,
    )
    session.add(invoice)
    session.flush()
    account_ops.sync_account_from_invoice(session, invoice)
    session.commit()
    return {"id": invoice.id, "outstanding_display": format_inr(invoice.outstanding_paise)}


@trade_router.post("/payments", status_code=201)
def record_payment(
    body: PaymentIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Recording a payment settles accounts and stops their ladders immediately.

    A resubmitted form, a retried request and an ERP replaying a webhook all
    arrive here as a second identical payment, and until there was an index to
    catch it each one allocated a second time — telling the debtor they owed
    less than they do, and eventually that they were in credit. The 409 below is
    the index speaking; the caller can send the same reference all day and only
    the first one moves the ledger.
    """
    payment = Payment(
        company_id=principal.company_id,
        buyer_id=body.buyer_id,
        amount_paise=body.amount_paise,
        unallocated_paise=body.amount_paise,
        received_date=body.received_date,
        reference=body.reference,
    )
    session.add(payment)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a payment referenced {body.reference!r} is already recorded for this "
            f"buyer. If this is a genuinely separate receipt it needs its own "
            f"reference; allocating the same one twice would credit money that "
            f"arrived once.",
        )
    try:
        plan = apply_payment(session, payment, instructions=body.allocations)
    except AllocationError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    for allocation in plan.allocations:
        account_ops.sync_account_from_invoice(
            session, session.get(Invoice, allocation.invoice_id)
        )
    audit.record_for(
        session,
        principal,
        action="ledger.payment_recorded",
        entity_type="payment",
        entity_id=payment.id,
        after={
            "buyer_id": str(payment.buyer_id),
            "amount_paise": payment.amount_paise,
            "reference": payment.reference,
            "allocated_paise": plan.allocated_paise,
            "on_account_paise": plan.unallocated_paise,
        },
    )
    session.commit()
    return {
        "payment_id": payment.id,
        "allocated_paise": plan.allocated_paise,
        "allocated_display": format_inr(plan.allocated_paise),
        "on_account_paise": plan.unallocated_paise,
        "on_account_display": format_inr(plan.unallocated_paise),
        "allocations": [
            {
                "invoice_id": a.invoice_id,
                "amount_paise": a.amount_paise,
                "amount_display": format_inr(a.amount_paise),
                "rule": a.rule.value,
            }
            for a in plan.allocations
        ],
    }


@trade_router.post("/accounts/{account_id}/dispute")
def raise_dispute(
    account_id: UUID,
    reason: str = Query(..., min_length=3),
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    account = session.get(CreditAccount, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    account_ops.raise_dispute(session, account, reason=reason)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.ACCOUNT_STATUS_CHANGED,
        entity_type="credit_account", entity_id=account_id, after={"status": "IN_DISPUTE"},
    )
    session.commit()
    return {"status": account.status.value}


# --------------------------------------------------------- making a debt smaller
#
# Every write path above this line can only grow what a buyer owes. That
# asymmetry is what these routes close, and it is why they are separately
# permissioned: a balance nobody can reduce keeps generating dunning calls,
# assessments and notice figures long after the reason for them has gone, and a
# balance anybody can reduce is a debt that quietly disappears.
#
# None of them assigns `outstanding_paise`. Each calls into `app.trade.reduction`,
# which ends at the single writer, and each returns the recomputed balance —
# because the caller has just changed a number that ends up in a legal notice,
# and the number they should read next is the one the ledger produced.


def _invoice_out(session: Session, invoice: Invoice) -> InvoiceOut:
    account = account_ops.account_for_invoice(session, invoice.id)
    return InvoiceOut(
        id=invoice.id,
        buyer_id=invoice.buyer_id,
        invoice_number=invoice.invoice_number,
        status=invoice.status.value,
        net_paise=invoice.net_paise,
        net_display=format_inr(invoice.net_paise),
        outstanding_paise=invoice.outstanding_paise,
        outstanding_display=format_inr(invoice.outstanding_paise),
        closure_reason=invoice.closure_reason,
        closed_at=invoice.closed_at,
        account_status=account.status.value if account is not None else None,
    )


def _require_named_user(principal: Principal, act: str) -> None:
    """Refuse an API key on a decision that has to be somebody's.

    An API key scoped to one of the admin permissions clears `require(...)`
    carrying no user id, and the decision is then filed as nobody's — written
    into `invoices.closed_by` as NULL, which is the column that exists to answer
    "who decided that". A permission gate answers whether the act is allowed; it
    cannot answer who performed it, and for these six that second answer is the
    point of the gate.
    """
    if principal.user_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{act} needs a named user; an API key cannot be the person who "
            f"signed off on it",
        )


def _refuse_reduction(session: Session, exc: reduction.ReductionError) -> HTTPException:
    """Turn a named ledger refusal into a 409 that quotes it.

    The refusal member travels in the body as well as the sentence: a caller
    branching on `INVOICE_HAS_SETTLEMENTS` should not have to match on English.
    """
    session.rollback()
    return HTTPException(
        status.HTTP_409_CONFLICT, f"{exc.refusal.value}: {exc.detail}"
    )


@trade_router.post("/credit-notes", response_model=CreditNoteOut, status_code=201)
def issue_credit_note(
    body: CreditNoteIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.LEDGER_CREDIT)),
):
    """Issue a credit note, and bring the recovery projection with it.

    Not `BUYER_WRITE`, unlike recording a payment. A payment asserts money
    arrived and can be checked against a bank statement; a credit note asserts
    nothing arrived and the debt shrank anyway. See `Permission.LEDGER_CREDIT`.

    The note is added to the session before it is applied because the
    over-credit guard works by summing `credit_notes` rows: an unsaved note is
    invisible to it, and the credit would land unchecked.
    """
    amount_paise = _money(body.amount, "credit note amount")
    if amount_paise <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a credit note must be for more than zero",
        )

    buyer = session.get(Buyer, body.buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    invoice = None
    if body.invoice_id is not None:
        invoice = session.get(Invoice, body.invoice_id)
        if invoice is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invoice")
        if invoice.buyer_id != buyer.id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"invoice {invoice.invoice_number} belongs to a different buyer; "
                f"crediting it here would reduce a debt this note has nothing to "
                f"do with",
            )

    now = datetime.now(timezone.utc)
    note = CreditNote(
        company_id=principal.company_id,
        buyer_id=buyer.id,
        invoice_id=invoice.id if invoice is not None else None,
        note_number=body.note_number,
        amount_paise=amount_paise,
        issue_date=body.issue_date,
        reason=body.reason,
    )
    session.add(note)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"credit note {body.note_number!r} already exists in this company",
        )

    try:
        reduction.apply_credit_note(session, note, now=now)
    except reduction.ReductionError as exc:
        raise _refuse_reduction(session, exc)
    except AllocationError as exc:
        # The over-credit refusal, which names the excess in paise. It stays
        # `app.trade.allocation`'s sentence rather than being rephrased here.
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    audit.record_for(
        session,
        principal,
        action="ledger.credit_note_issued",
        entity_type="credit_note",
        entity_id=note.id,
        after={
            "buyer_id": str(note.buyer_id),
            "invoice_id": str(note.invoice_id) if note.invoice_id else None,
            "note_number": note.note_number,
            "amount_paise": note.amount_paise,
            "reason": note.reason,
        },
    )
    session.commit()
    session.refresh(note)
    if invoice is not None:
        session.refresh(invoice)
    return CreditNoteOut(
        id=note.id,
        buyer_id=note.buyer_id,
        invoice_id=note.invoice_id,
        note_number=note.note_number,
        amount_paise=note.amount_paise,
        amount_display=format_inr(note.amount_paise),
        issue_date=note.issue_date,
        reason=note.reason,
        invoice_outstanding_paise=invoice.outstanding_paise if invoice else None,
        invoice_outstanding_display=(
            format_inr(invoice.outstanding_paise) if invoice else None
        ),
        invoice_status=invoice.status.value if invoice else None,
    )


@trade_router.post("/payments/{payment_id}/allocate", response_model=AllocationPlanOut)
def allocate_on_account(
    payment_id: UUID,
    body: OnAccountIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Apply money already received to the invoices named.

    `BUYER_WRITE` deliberately, and not one of the new ledger permissions: the
    money is already recorded, nothing about the total moves, and deciding which
    invoice it lands against is something the same permission already does at
    the moment of receipt through `allocations` on `POST /trade/payments`.

    What this must never become is a second `apply_payment`. That plans against
    the payment's full amount and would allocate the whole sum a second time;
    `reduction.apply_on_account` plans against what is genuinely left.
    """
    payment = session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such payment")

    before_unallocated = payment.unallocated_paise
    try:
        plan = reduction.apply_on_account(
            session, payment, instructions=body.allocations
        )
    except reduction.ReductionError as exc:
        raise _refuse_reduction(session, exc)
    except AllocationError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    audit.record_for(
        session,
        principal,
        action="ledger.on_account_allocated",
        entity_type="payment",
        entity_id=payment.id,
        before={"unallocated_paise": before_unallocated},
        after={
            "unallocated_paise": plan.unallocated_paise,
            "allocations": [
                {"invoice_id": str(a.invoice_id), "amount_paise": a.amount_paise}
                for a in plan.allocations
            ],
        },
    )
    session.commit()
    return AllocationPlanOut(
        payment_id=payment.id,
        allocated_paise=plan.allocated_paise,
        allocated_display=format_inr(plan.allocated_paise),
        on_account_paise=plan.unallocated_paise,
        on_account_display=format_inr(plan.unallocated_paise),
        allocations=[
            AllocationOut(
                invoice_id=a.invoice_id,
                amount_paise=a.amount_paise,
                amount_display=format_inr(a.amount_paise),
                rule=a.rule.value,
            )
            for a in plan.allocations
        ],
    )


def _close_invoice(
    session: Session,
    principal: Principal,
    invoice: Invoice,
    *,
    reason: str,
    action: str,
    close,
) -> InvoiceOut:
    """Shared body of write-off and cancellation.

    The two differ only in which `reduction` function runs and what the act is
    called; everything around it — the refusal, the closure columns, the audit
    row — is the same, and duplicating it is how the two drift.

    `closure_reason` and its companions are written here rather than inside
    `reduction`, which takes the reason as an argument and puts it on the ladder
    history. That history is durable but it is JSONB on the recovery projection,
    and it is skipped entirely when the account has no escalation row; the
    columns are where the reason stays queryable and exportable.
    """
    _require_named_user(principal, "abandoning a debt")

    now = datetime.now(timezone.utc)
    before = {
        "status": invoice.status.value,
        "outstanding_paise": invoice.outstanding_paise,
    }
    try:
        close(session, invoice, reason=reason, now=now)
    except reduction.ReductionError as exc:
        raise _refuse_reduction(session, exc)
    except AllocationError as exc:
        # `ReductionError` is a subclass, so the clause above does not catch the
        # parent. Both closing paths reach `recompute_invoice`, which raises the
        # bare error on an over-settled invoice — and that has to arrive as the
        # named refusal the sibling ledger routes give, not a 500 over a dirty
        # session.
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    invoice.closure_reason = reason
    invoice.closed_by = principal.user_id
    invoice.closed_at = now

    account = account_ops.account_for_invoice(session, invoice.id)
    audit.record_for(
        session,
        principal,
        action=action,
        entity_type="invoice",
        entity_id=invoice.id,
        before=before,
        after={
            "status": invoice.status.value,
            "outstanding_paise": invoice.outstanding_paise,
            "reason": reason,
            "account_status": account.status.value if account is not None else None,
        },
    )
    session.commit()
    session.refresh(invoice)
    return _invoice_out(session, invoice)


@trade_router.post("/invoices/{invoice_id}/write-off", response_model=InvoiceOut)
def write_off_invoice(
    invoice_id: UUID,
    body: InvoiceClosureIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.LEDGER_WRITE_OFF)),
):
    """Stop expecting the money. The ladder stops with it.

    A write-off does not extinguish the debt and does not zero the balance: the
    sale happened, the buyer's books still carry the payable, and the invoice
    stays on their statement. What changes is that this side stops pursuing it,
    which is why the account status in the response is the part worth reading.
    """
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invoice")
    return _close_invoice(
        session,
        principal,
        invoice,
        reason=body.reason,
        action="ledger.invoice_written_off",
        close=reduction.write_off,
    )


@trade_router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceOut)
def cancel_invoice(
    invoice_id: UUID,
    body: InvoiceClosureIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.LEDGER_WRITE_OFF)),
):
    """Void an invoice that should never have stood.

    The 409 to expect is `INVOICE_HAS_SETTLEMENTS`. Dropping the debit while the
    payment against it stays as a credit reads, on a statement already sent, as
    though the buyer had overpaid — so an invoice with money against it is
    written off instead, or the payment is reversed first.
    """
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invoice")
    return _close_invoice(
        session,
        principal,
        invoice,
        reason=body.reason,
        action="ledger.invoice_cancelled",
        close=reduction.cancel_invoice,
    )


@trade_router.post("/accounts/{account_id}/dispute/clear")
def clear_account_dispute(
    account_id: UUID,
    body: DisputeClearIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.DISPUTE_CLEAR)),
):
    """Lift a dispute, leaving evidence that there was one.

    Admin-only, and this is the route the permission was minted for: clearing a
    dispute is the moment a contested debt becomes collectable again, and the
    ladder entry it appends is the only surviving record that the debt was ever
    contested — which is what `app.registry.eligibility` gates publication on.

    Two things it deliberately does not do. It does not clear
    `needs_human_review`: whatever else asked for a person to look is still
    asking, and the review queue is where that is answered. And it does not
    resume contact by itself — the account returns to OVERDUE and the scheduler
    picks it up on its own cadence.
    """
    _require_named_user(principal, "declaring a dispute resolved")

    account = session.get(CreditAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    if account.status is not AccountStatus.IN_DISPUTE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this account is {account.status.value}, not in dispute; there is "
            f"nothing to clear",
        )

    before = {
        "status": account.status.value,
        "disputed_reason": account.disputed_reason,
    }
    account_ops.clear_dispute(session, account, resolution=body.resolution)
    audit.record_for(
        session,
        principal,
        action=audit.Action.ACCOUNT_STATUS_CHANGED,
        entity_type="credit_account",
        entity_id=account_id,
        before=before,
        after={
            "status": account.status.value,
            "resolution": body.resolution,
            "ever_disputed": True,
        },
    )
    session.commit()
    return {
        "status": account.status.value,
        # Stated in the response because it is the surprising half: the dispute
        # is over and the account is still permanently marked as having been
        # contested.
        "ever_disputed": account_ops.ever_disputed(session, account),
    }


# ------------------------------------------- corrections, consent and cessation
#
# The other direction from everything else here: the debtor saying "that is not
# my name" or "stop calling me". Each of these routes writes the audit row from
# what the service returns, because the service deliberately writes none — it
# has no way to know who asked, and a `restore_consent` with no audit row behind
# it silently erases that a withdrawal ever happened.


def _refuse_consent(session: Session, exc: consent_ops.ConsentError) -> HTTPException:
    session.rollback()
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY, f"{exc.refusal.value}: {exc.detail}"
    )


def _change_out(change: consent_ops.ContactChange) -> ContactChangeOut:
    return ContactChangeOut(
        entity_type=change.entity_type,
        entity_id=change.entity_id,
        before=change.before,
        after=change.after,
        detail=change.detail,
        changed=change.changed,
    )


def _record_change(
    session: Session,
    principal: Principal,
    change: consent_ops.ContactChange,
    *,
    action: str,
) -> ContactChangeOut:
    """Write the audit row, whether or not the row moved.

    Unlike a correction, which records nothing when a resubmitted form changes
    nothing: a second withdrawal letter is a real instruction from a real person
    on a real date, and "they told us again and we have it in writing" is
    precisely what gets asked for when a withdrawal is disputed. `changed` in
    the response says the stored state stayed put.
    """
    audit.record_for(
        session,
        principal,
        action=action,
        entity_type=change.entity_type,
        entity_id=change.entity_id,
        before=change.before,
        after=change.after,
        detail=change.detail,
    )
    session.commit()
    return _change_out(change)


def _buyer_or_404(session: Session, buyer_id: UUID) -> Buyer:
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")
    return buyer


@trade_router.patch("/buyers/{buyer_id}", response_model=BuyerOut)
def correct_buyer(
    buyer_id: UUID,
    body: BuyerPatchIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Correct name, email or language. Nothing else is correctable here.

    The declared GSTIN and CIN are absent from the whitelist on purpose: editing
    them is what voids a verification tier, and that goes through
    `PUT /trade/buyers/{id}/identity`, which resets the profile to
    self-declared. Written here they would leave a row still stamped IDENTIFIER
    while carrying a number nobody has checked — and that stamp is what a notice
    is issued against.
    """
    buyer = _buyer_or_404(session, buyer_id)
    # `exclude_unset` is what separates "leave the email alone" from "clear it".
    # Both are legitimate, and a model_dump without it would silently do the
    # second every time somebody edited a name.
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "no correctable field was sent"
        )

    try:
        before, after = consent_ops.correct_buyer(session, buyer, fields=fields)
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)

    # No audit row when nothing moved. A resubmitted form is not a write, and an
    # entry recording that a name stayed the same is noise in the one trail that
    # has to stay readable.
    if after:
        audit.record_for(
            session,
            principal,
            action="buyer.corrected",
            entity_type="buyer",
            entity_id=buyer.id,
            before=before,
            after=after,
        )
    session.commit()
    session.refresh(buyer)
    return buyer


@trade_router.post("/buyers/{buyer_id}/consent/withdraw", response_model=ContactChangeOut)
def withdraw_consent(
    buyer_id: UUID,
    body: ConsentIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Record that this person has told us to stop contacting them.

    `BUYER_WRITE` rather than one of the privileged permissions: the operator
    who just took the call is exactly who should be able to write this down, and
    every effect of it reduces contact. Making it an admin act would mean a
    withdrawal waiting in a queue while the campaign kept dialling.

    It touches no balance. A debtor who says "stop calling" owes precisely what
    they owed a moment before.
    """
    buyer = _buyer_or_404(session, buyer_id)
    try:
        change = consent_ops.record_consent_withdrawal(
            session, buyer, reason=body.reason, source=body.source
        )
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)
    return _record_change(
        session, principal, change, action=audit.Action.BUYER_CONSENT_CHANGED
    )


@trade_router.post("/buyers/{buyer_id}/consent/restore", response_model=ContactChangeOut)
def restore_consent(
    buyer_id: UUID,
    body: ConsentIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CONSENT_RESTORE)),
):
    """Record that the person has agreed to be contacted again.

    The one act in the product that turns contact back on, hence its own
    admin-only permission. The buyer row carries current state rather than
    history, so this clears `consent_withdrawn_at` — which makes the audit row
    written from `before` the only surviving evidence that a withdrawal ever
    happened. That is why the reason and the source are required.
    """
    _require_named_user(principal, "turning contact back on")

    buyer = _buyer_or_404(session, buyer_id)
    try:
        change = consent_ops.restore_consent(
            session, buyer, reason=body.reason, source=body.source
        )
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)
    return _record_change(
        session, principal, change, action=audit.Action.BUYER_CONSENT_CHANGED
    )


@trade_router.post("/buyers/{buyer_id}/suppress", response_model=ContactChangeOut)
def suppress_buyer(
    buyer_id: UUID,
    body: SuppressIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Hold all contact until a date.

    Only ever extends. A hold that already runs past `until` stays where it is,
    so a promise to pay on Friday cannot cut a recorded fortnight of hospital
    leave short — and the effective date comes back in `after`, which may not be
    the one that was asked for.
    """
    buyer = _buyer_or_404(session, buyer_id)
    try:
        change = consent_ops.suppress_until(
            session, buyer, until=body.until, reason=body.reason
        )
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)
    return _record_change(
        session, principal, change, action=audit.Action.BUYER_SUPPRESSED
    )


@trade_router.post(
    "/buyers/{buyer_id}/channel-optout", response_model=ContactChangeOut, status_code=201
)
def opt_out_channel(
    buyer_id: UUID,
    body: ChannelOptOutIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Suppress one channel, and only that channel.

    STOP on SMS says nothing about the phone ringing. Reading it as a full
    withdrawal silences a channel the debtor never objected to; recording a real
    withdrawal as one channel under-blocks, and that is the compliance incident.
    Until this route existed the engine's CHANNEL_OPTED_OUT refusal could never
    fire, because nothing wrote the row it reads.
    """
    buyer = _buyer_or_404(session, buyer_id)
    try:
        change = consent_ops.record_channel_optout(
            session, buyer, channel=body.channel, source=body.source
        )
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)
    return _record_change(
        session, principal, change, action="buyer.channel_opted_out"
    )


@trade_router.post("/phones/{phone_id}/retire", response_model=ContactChangeOut)
def retire_phone(
    phone_id: UUID,
    body: PhoneRetireIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Flag a number as no longer valid. Never deletes it.

    Calls point at this row, and "who did we ring on 12 March, and on what
    number" has to stay answerable afterwards. The reason has no column, which
    is why it goes to the audit entry beside the person who decided.
    """
    phone = session.get(BuyerPhone, phone_id)
    if phone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such phone number")
    try:
        change = consent_ops.invalidate_phone(session, phone, reason=body.reason)
    except consent_ops.ConsentError as exc:
        raise _refuse_consent(session, exc)
    return _record_change(session, principal, change, action="buyer.phone_retired")


# --------------------------------------------------------------------- promises


def _promise_out(promise: Promise) -> PromiseOut:
    from app.intelligence import promises as promise_ops

    return PromiseOut(
        id=promise.id,
        buyer_id=promise.buyer_id,
        account_id=promise.account_id,
        promised_amount_paise=promise.promised_amount_paise,
        promised_amount_display=format_inr(promise.promised_amount_paise),
        promised_on=promise.promised_on,
        promised_by_date=promise.promised_by_date,
        status=promise.status,
        settled_at=promise.settled_at,
        chase_resumes_at=promise_ops.chase_resumes_at(promise.promised_by_date),
    )


@trade_router.post("/promises", response_model=PromiseOut, status_code=201)
def record_promise(
    body: PromiseIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Record a promise to pay, and pause chasing until it falls due.

    Operator work by design — the person taking the promise is on the call — and
    the horizon cap is what stops that being abusable from the debtor's side: a
    promise more than ninety days out is refused rather than becoming a way to
    switch the ladder off.

    `promised_on` defaults to today in the promise timezone rather than to UTC's
    today. Between 18:30 and midnight IST those are different dates, and the
    wrong one makes a same-day promise look retrospective.
    """
    from zoneinfo import ZoneInfo

    from app.intelligence import promises as promise_ops

    buyer = _buyer_or_404(session, body.buyer_id)
    account = None
    if body.account_id is not None:
        account = session.get(CreditAccount, body.account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")

    amount_paise = _money(body.amount, "promised amount")
    promised_on = body.promised_on or datetime.now(
        ZoneInfo(promise_ops.PROMISE_TIMEZONE)
    ).date()

    try:
        promise = promise_ops.record_promise(
            session,
            buyer=buyer,
            promised_amount_paise=amount_paise,
            promised_on=promised_on,
            promised_by_date=body.promised_by_date,
            account=account,
            recorded_by=principal.user_id,
        )
    except promise_ops.PromiseRefused as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{exc.reason.value}: {exc.detail}"
        )

    audit.record_for(
        session,
        principal,
        action="promise.recorded",
        entity_type="promise",
        entity_id=promise.id,
        after={
            "buyer_id": str(promise.buyer_id),
            "account_id": str(promise.account_id) if promise.account_id else None,
            "promised_amount_paise": promise.promised_amount_paise,
            "promised_by_date": promise.promised_by_date.isoformat(),
            "suppressed_until": (
                buyer.suppressed_until.isoformat() if buyer.suppressed_until else None
            ),
        },
    )
    session.commit()
    session.refresh(promise)
    return _promise_out(promise)


@trade_router.get("/buyers/{buyer_id}/promises", response_model=list[PromiseOut])
def list_promises(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    limit: int = Query(50, le=200),
):
    """Newest first, broken ones included.

    A kept-rate that only counted successes would report every debtor as
    perfectly reliable right up to the day they stop answering, so BROKEN rows
    are never removed and this never filters them out.
    """
    _buyer_or_404(session, buyer_id)
    rows = session.execute(
        select(Promise)
        .where(Promise.buyer_id == buyer_id)
        .order_by(Promise.promised_by_date.desc())
        .limit(limit)
    ).scalars()
    return [_promise_out(p) for p in rows]


@trade_router.post("/promises/resolve-due", response_model=list[PromiseOut])
def resolve_due_promises(
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Judge every promise whose grace has run out, from the ledger.

    Nothing else writes BROKEN. This belongs on a nightly sweep rather than on a
    button — it is here so the sweep has something to call and so the state is
    reachable at all until one exists.
    """
    from app.intelligence import promises as promise_ops

    now = datetime.now(timezone.utc)
    settled = promise_ops.resolve_due_promises(session, as_of=now)
    for promise in settled:
        audit.record_for(
            session,
            principal,
            action="promise.settled",
            entity_type="promise",
            entity_id=promise.id,
            after={"status": promise.status, "judged_from": "ledger"},
        )
    session.commit()
    return [_promise_out(p) for p in settled]


@trade_router.post(
    "/promises/{promise_id}/settle", response_model=PromiseOut, status_code=200
)
def settle_promise(
    promise_id: UUID,
    body: PromiseSettleIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Close one promise by hand as kept or broken.

    A correction to the sweep above, not a substitute for it: the sweep judges
    from money actually received, and this exists for the case it cannot see —
    a cheque handed over in person, a payment against a different ledger. It
    never re-opens, and the audit row is what says a person rather than the
    ledger decided.
    """
    from app.intelligence import promises as promise_ops

    promise = session.get(Promise, promise_id)
    if promise is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such promise")

    try:
        promise_ops.settle_promise(
            session,
            promise,
            kept=body.kept,
            settled_at=datetime.now(timezone.utc),
        )
    except promise_ops.PromiseRefused as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{exc.reason.value}: {exc.detail}"
        )

    audit.record_for(
        session,
        principal,
        action="promise.settled",
        entity_type="promise",
        entity_id=promise.id,
        after={"status": promise.status, "judged_from": "operator", "note": body.note},
    )
    session.commit()
    session.refresh(promise)
    return _promise_out(promise)


@trade_router.post("/buyers/{buyer_id}/behaviour", response_model=BehaviourOut, status_code=201)
def roll_up_behaviour(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
    as_of: date | None = None,
):
    """Append one payment-behaviour row for this buyer.

    201 because every run appends: a rollup is what the ledger said on a date,
    and `as_of` exists so a rollup for March is reconstructed from March rather
    than stamped with today. This is the row `run_credit_check` reads for
    mean-days-to-pay and the promise-kept rate, and until a nightly job calls
    this, both stay absent from every score.
    """
    from app.intelligence import behaviour as behaviour_ops

    try:
        row = behaviour_ops.rollup_payment_behaviour(
            session, buyer_id, as_of=as_of or datetime.now(timezone.utc).date()
        )
    except behaviour_ops.RollupRefused as exc:
        # UNKNOWN_BUYER, which under RLS also covers another tenant's buyer.
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.detail)

    audit.record_for(
        session,
        principal,
        action="buyer.behaviour_rolled_up",
        entity_type="buyer",
        entity_id=buyer_id,
        after={
            "as_of": row.as_of.isoformat(),
            "invoices_settled": row.invoices_settled,
        },
    )
    session.commit()
    session.refresh(row)
    return BehaviourOut(
        id=row.id,
        buyer_id=row.buyer_id,
        as_of=row.as_of,
        invoices_settled=row.invoices_settled,
        mean_days_to_pay=row.mean_days_to_pay,
        median_days_to_pay=row.median_days_to_pay,
        days_to_pay_trend=row.days_to_pay_trend,
        part_payment_rate=row.part_payment_rate,
        promise_kept_rate=row.promise_kept_rate,
        dispute_rate=row.dispute_rate,
    )


# ------------------------------------------------------------- the review queue
#
# Accounts the machine has refused to act on until a person looks. The flag is
# set by the dispatcher (an L3 rung with no prior delivered contact, a number on
# the DND registry) and by a dispute being raised; without a way to see and
# clear it, every one of those accumulates silently and the queue is a column
# nobody reads.


@trade_router.get("/review-queue", response_model=list[ReviewItemOut])
def list_review_queue(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    limit: int = Query(100, le=500),
):
    rows = session.execute(
        select(EscalationState, CreditAccount, Buyer)
        .join(CreditAccount, CreditAccount.id == EscalationState.account_id)
        .join(Buyer, Buyer.id == CreditAccount.buyer_id)
        .where(EscalationState.needs_human_review.is_(True))
        .order_by(CreditAccount.due_date)
        .limit(limit)
    ).all()

    out = []
    for state, account, buyer in rows:
        history = list(state.history or [])
        out.append(
            ReviewItemOut(
                account_id=account.id,
                buyer_id=buyer.id,
                buyer_name=buyer.name,
                invoice_ref=account.invoice_ref,
                outstanding_paise=account.outstanding_paise,
                outstanding_display=format_inr(account.outstanding_paise),
                status=account.status.value,
                level=state.level.value,
                disputed_reason=account.disputed_reason,
                ever_disputed=account_ops.ever_disputed(session, account),
                # The last entry is what put this row here, and it is the first
                # thing a reviewer needs — the alternative is opening each
                # account to find out why it is waiting.
                last_event=history[-1] if history else None,
            )
        )
    return out


@trade_router.post("/review-queue/{account_id}/resolve")
def resolve_review(
    account_id: UUID,
    body: ReviewResolveIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.REVIEW_RESOLVE)),
):
    """Record that a person has looked, and release the account back to the ladder.

    The note is required. An emptied queue with nothing written beside it is
    indistinguishable from a queue somebody cleared without reading, and the
    whole point of the flag is that a human saw the thing before the machine
    resumed.

    Refused while the account is still in dispute. The dispute is the more
    specific fact and it has its own route; clearing the review flag underneath
    one would leave the account halfway between two states, contactable
    according to the queue and blocked according to the ledger.
    """
    _require_named_user(principal, "recording that a person has reviewed this")

    account = session.get(CreditAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    if account.status is AccountStatus.IN_DISPUTE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this account is in dispute; clear the dispute first — that is a "
            "separate decision with its own record",
        )

    # Looked up rather than created. `ensure_escalation_state` would write a
    # ladder row for an account that has never had one, on the path where the
    # answer is "there was nothing to resolve" — a refusal that leaves a row
    # behind is not a refusal.
    state = session.execute(
        select(EscalationState).where(EscalationState.account_id == account_id)
    ).scalar_one_or_none()
    if state is None or not state.needs_human_review:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this account is not waiting for a review"
        )

    now = datetime.now(timezone.utc)
    state.needs_human_review = False
    # Appended, never replaced. The ladder history is the durable trace of what
    # was decided about this account and when, and it is read by `ever_disputed`.
    state.history = list(state.history or []) + [
        {
            "at": now.isoformat(),
            "event": "review_resolved",
            "note": body.note,
            "by": str(principal.user_id) if principal.user_id else None,
        }
    ]
    audit.record_for(
        session,
        principal,
        action="account.review_resolved",
        entity_type="credit_account",
        entity_id=account_id,
        after={"needs_human_review": False, "note": body.note},
    )
    session.commit()
    return {"account_id": str(account_id), "needs_human_review": False}


# ------------------------------------------------------------------ court records
#
# Searching is desk work; deciding that a case found under a similar name is
# this debtor's is not. The split below is the whole safety property: a search
# proposes and counts for nothing, and only a confirmation on an identifier the
# registry holds can reach a report or a score.


def _court_backend():
    """The configured court adapter, or None when courts are switched off.

    A name nobody has wired is refused rather than quietly falling back to
    `local`. The local backend returns invented filings — it is a fixture set —
    and handing those to a route whose job is to attach litigation to a named
    company would be defamation with our name on it.
    """
    from app.config import settings as cfg
    from app.providers.courts import LocalCourtBackend, ManualCourtBackend

    if cfg.court_backend == "none":
        return None
    if cfg.court_backend == "local":
        return LocalCourtBackend()
    if cfg.court_backend == "manual":
        return ManualCourtBackend()
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"court backend {cfg.court_backend!r} is named in configuration but "
        f"nothing implements it",
    )


def _link_out(link: LegalLink, case: CourtCase) -> LegalLinkOut:
    signals = link.signals or {}
    return LegalLinkOut(
        case_id=case.id,
        court_id=case.court_id,
        case_number=case.case_number,
        case_type=case.case_type,
        link_id=link.id,
        status=link.status,
        tier=signals.get("grade") or "UNKNOWN",
        confidence=float(link.confidence or 0),
        reasons=list(signals.get("grade_reasons") or []),
    )


@trade_router.post("/buyers/{buyer_id}/court-cases/search", response_model=CaseSearchOut)
def search_court_cases(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_VERIFY)),
    filed_after: date | None = None,
):
    """Search the courts for this buyer and propose links for review.

    `COMPANY_VERIFY` on the precedent set for registry lookups: this reads a
    portal, records what came back, and publishes nothing on its own. Every link
    it writes is PROPOSED, which moves no score and appears on no report.

    A run that reaches a backend appends — a fresh `ProviderFetch` holding the
    payload verbatim and a fresh `LegalHistory` recording what was claimed on
    the day. "What did we know on 12 March" is answered from those, never from
    the case row, which is refreshed in place because a hearing date moving is
    not a second answer to the same question. 200 rather than 201 because a
    refused run creates nothing at all, and `history_id` is the honest signal of
    whether anything was written.

    Refusals come back on the body rather than as errors: "we searched and could
    confirm nothing" is a result an operator has to be shown, most often
    PROFILE_NOT_IDENTIFIER_RESOLVED — meaning the buyer's own identity has never
    been verified, so nothing found here could ever be confirmed as theirs.
    """
    from app.company.verification import BuyerNotFound
    from app.legal import cases as legal_cases

    try:
        outcome = legal_cases.search_and_link(
            session,
            buyer_id,
            company_id=principal.company_id,
            backend=_court_backend(),
            now=datetime.now(timezone.utc),
            filed_after=filed_after,
            actor_label=principal.label or str(principal.user_id),
        )
    except BuyerNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such buyer")

    signals = (
        legal_cases.legal_signals(session, outcome.profile_id)
        if outcome.profile_id is not None
        else legal_cases.LegalSignals()
    )
    # `search_and_link` writes its own audit row naming the actor passed above,
    # so a second one here would record the same act twice under two names.
    session.commit()
    return CaseSearchOut(
        profile_id=outcome.profile_id,
        searched_name=outcome.searched_name,
        cases_seen=outcome.cases_seen,
        links=[
            LegalLinkOut(
                case_id=link.case_id,
                court_id=link.court_id,
                case_number=link.case_number,
                case_type=link.case_type,
                link_id=link.link_id,
                status=link.status,
                tier=link.tier,
                confidence=link.confidence,
                reasons=list(link.reasons),
            )
            for link in outcome.links
        ],
        refusals=[r.value for r in outcome.refusals],
        notes=list(outcome.notes),
        history_id=outcome.history_id,
        confirmed_legal_cases=signals.confirmed_legal_cases,
        recovery_suits=signals.recovery_suits,
        insolvency_cases=signals.insolvency_cases,
        awaiting_review=signals.awaiting_review,
    )


@trade_router.get("/buyers/{buyer_id}/court-cases", response_model=CaseSearchOut)
def list_court_cases(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
):
    """Every link proposed or decided for this buyer, with the counts that score.

    Rejected links are returned too. A case somebody has already looked at and
    said is not this company's is exactly the case that will be proposed again
    on the next search, and hiding the rejection invites the same review twice.
    """
    from app.legal import cases as legal_cases

    _buyer_or_404(session, buyer_id)
    profile = _buyer_profile(session, buyer_id)
    if profile is None:
        return CaseSearchOut(notes=["this buyer has no company profile"])

    rows = session.execute(
        select(LegalLink, CourtCase)
        .join(CourtCase, CourtCase.id == LegalLink.case_id)
        .where(LegalLink.profile_id == profile.id)
        .order_by(CourtCase.filing_date.desc())
    ).all()
    signals = legal_cases.legal_signals(session, profile.id)
    return CaseSearchOut(
        profile_id=profile.id,
        searched_name=profile.legal_name,
        cases_seen=len(rows),
        links=[_link_out(link, case) for link, case in rows],
        confirmed_legal_cases=signals.confirmed_legal_cases,
        recovery_suits=signals.recovery_suits,
        insolvency_cases=signals.insolvency_cases,
        awaiting_review=signals.awaiting_review,
    )


@trade_router.post("/legal-links/{link_id}/review")
def review_legal_link(
    link_id: UUID,
    body: LegalLinkReviewIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.LEGAL_LINK_CONFIRM)),
):
    """Decide whether a case is this company's.

    Confirming is refused unless the filing itself recorded an identifier
    against a party and that identifier is one the registry holds for this
    buyer. A perfect name match, a director named as a respondent, the company's
    own GSTIN sitting in a recital — all of them come back 409, and a reviewer
    who wants to confirm anyway cannot: the grade is recomputed from the stored
    case and the profile's *current* identifiers, never read back from the
    link's signals.

    Rejecting is unconditional, because detaching a case is always safe.
    """
    from app.legal import cases as legal_cases

    _require_named_user(principal, "deciding whose litigation this is")

    now = datetime.now(timezone.utc)
    actor = principal.label or str(principal.user_id)
    try:
        if body.decision == "CONFIRM":
            link = legal_cases.confirm_link(
                session,
                link_id,
                company_id=principal.company_id,
                reviewed_by=principal.user_id,
                now=now,
                actor_label=actor,
            )
        else:
            reason = (body.reason or "").strip()
            if not reason:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "say why this case is not theirs; the rejection is what stops "
                    "the same case coming back through review unexplained",
                )
            link = legal_cases.reject_link(
                session,
                link_id,
                company_id=principal.company_id,
                reviewed_by=principal.user_id,
                reason=reason,
                now=now,
                actor_label=actor,
            )
    except legal_cases.LinkRefused as exc:
        session.rollback()
        if exc.reason is legal_cases.Refusal.LINK_NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, exc.detail)
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{exc.reason.value}: {exc.detail}"
        )

    # `confirm_link` and `reject_link` each write their own audit row naming the
    # reviewer passed above; a second one here would double-count the decision.
    session.commit()
    return {"link_id": str(link.id), "status": link.status}


@trade_router.post(
    "/accounts/{account_id}/prelegal-assessment", response_model=PrelegalOut, status_code=201
)
def assess_prelegal(
    account_id: UUID,
    body: PrelegalIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_WRITE)),
):
    """Assess whether this account is ready for a demand notice.

    A recommendation and nothing else, which is why it sits at `BUYER_WRITE`
    alongside the credit check rather than behind a legal permission: it
    publishes nothing, sends nothing, and its most common answer is NOT_READY.
    The gate belongs on the notice, and there is no route that sends one.

    201 for the same reason as every other assessment here — it appends, because
    the question afterwards is always "what did you know when you decided to
    send it". Only court links a person has confirmed reach it; cases awaiting
    review are reported as a count and change nothing.
    """
    from app.legal import assessment as legal_assessment

    try:
        outcome = legal_assessment.assess_account(
            session,
            account_id,
            company_id=principal.company_id,
            now=datetime.now(timezone.utc),
            delivered_contacts_at_l2=body.delivered_contacts_at_l2,
            has_delivery_proof=body.has_delivery_proof,
            last_acknowledgement=body.last_acknowledgement,
            last_part_payment=body.last_part_payment,
            assessed_by=principal.user_id,
            actor_label=principal.label or str(principal.user_id),
        )
    except legal_assessment.AccountNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")

    # `assess_account` records the assessment in the audit trail itself, naming
    # the actor passed above.
    session.commit()
    result = outcome.assessment
    return PrelegalOut(
        assessment_id=outcome.assessment_id,
        account_id=account_id,
        outcome=result.outcome.value,
        factors=list(result.factors),
        blockers=list(result.blockers),
        estimated_cost_paise=result.estimated_cost_paise,
        estimated_cost_display=format_inr(result.estimated_cost_paise),
        recoverable_paise=result.recoverable_paise,
        recoverable_display=format_inr(result.recoverable_paise),
        limitation_expires_on=result.limitation_expires_on,
        limitation_urgent=result.limitation_urgent,
        confirmed_legal_cases=outcome.legal.confirmed_legal_cases,
        recovery_suits=outcome.legal.recovery_suits,
        insolvency_cases=outcome.legal.insolvency_cases,
        awaiting_review=outcome.legal.awaiting_review,
    )


# ----------------------------------------------------------------- ingestion

import_router = APIRouter(prefix="/import", tags=["import"])


@import_router.post("/csv", response_model=ImportPreviewOut)
async def upload_csv(
    file: UploadFile,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.IMPORT_RUN)),
    date_format: str = "%d/%m/%Y",
):
    """Parse and classify. Commits nothing — the preview is the whole point."""
    content = (await file.read()).decode("utf-8-sig", errors="replace")
    batch = csv_import.stage(
        session,
        company_id=principal.company_id,
        filename=file.filename or "upload.csv",
        content=content,
        date_format=date_format,
        uploaded_by=principal.user_id,
    )
    preview = csv_import.preview(session, batch)
    session.commit()
    return ImportPreviewOut(
        batch_id=preview.batch_id,
        total=preview.total,
        counts=preview.counts,
        rejections=preview.rejections[:50],
        review_rows=preview.review_rows[:50],
        new_phone_count=preview.new_phone_count,
        note=(
            f"{preview.new_phone_count} number(s) will be created with DND status "
            f"UNKNOWN, which blocks calling until they are scrubbed. This is "
            f"correct, not a bug."
        ),
    )


@import_router.post("/{batch_id}/commit")
def commit_import(
    batch_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.IMPORT_RUN)),
):
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    applied = csv_import.commit(session, batch, committed_by=principal.user_id)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.BUYER_IMPORTED,
        entity_type="import_batch", entity_id=batch_id, after=applied,
    )
    session.commit()
    return applied


# ----------------------------------------------------------------- campaigns

campaign_router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _parse_time(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


@campaign_router.post("", response_model=CampaignOut, status_code=201)
def create_campaign(
    body: CampaignIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CAMPAIGN_WRITE)),
):
    campaign = Campaign(
        company_id=principal.company_id,
        name=body.name,
        timezone=body.timezone,
        window_start=_parse_time(body.window_start),
        window_end=_parse_time(body.window_end),
        call_on_weekends=body.call_on_weekends,
        max_attempts_per_day=body.max_attempts_per_day,
        max_attempts_per_week=body.max_attempts_per_week,
        min_hours_between_calls=body.min_hours_between_calls,
        channels=body.channels,
        status=CampaignStatus.DRAFT,
    )
    session.add(campaign)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.CAMPAIGN_CREATED,
        entity_type="campaign",
    )
    session.commit()
    return campaign


@campaign_router.get("", response_model=list[CampaignOut])
def list_campaigns(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.CAMPAIGN_READ)),
):
    return list(session.execute(select(Campaign).order_by(Campaign.created_at.desc())).scalars())


def _time_or_422(raw: str, field: str) -> time:
    try:
        return _parse_time(raw)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{field} must look like '09:30'; got {raw!r}",
        )


@campaign_router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: UUID,
    body: CampaignPatchIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CAMPAIGN_WRITE)),
):
    """Change a campaign's window, cadence or channels.

    Editable while the campaign is running, deliberately. The usual reason a
    window gets narrowed is a complaint about the calls going out under it, and
    making that wait for a pause means the calls carry on while somebody looks
    for the button.

    The 08:00-19:00 ceiling and `window_end > window_start` are check
    constraints on the table; those are the authority. They are re-stated here
    only so that an operator who gets it wrong reads a sentence instead of a
    driver error.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "no campaign field was sent"
        )
    # Absent means "leave it alone"; an explicit null means "clear it", and none
    # of these can be cleared — the scheduler reads every one on every tick.
    # Refused rather than skipped, for the same reason an unknown key is.
    cleared = sorted(k for k, v in fields.items() if v is None)
    if cleared:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{', '.join(cleared)} cannot be cleared; omit a field to leave it "
            f"unchanged",
        )

    window_start = (
        _time_or_422(fields["window_start"], "window_start")
        if "window_start" in fields
        else campaign.window_start
    )
    window_end = (
        _time_or_422(fields["window_end"], "window_end")
        if "window_end" in fields
        else campaign.window_end
    )
    if window_end <= window_start:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the calling window must end after it starts; got "
            f"{window_start.isoformat()} to {window_end.isoformat()}",
        )
    if window_start < time(8, 0) or window_end > time(19, 0):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "recovery calls are permitted between 08:00 and 19:00 local time; "
            "that ceiling is a property of the system, not a campaign setting",
        )

    channels = fields.get("channels")
    if channels is not None:
        unknown = sorted(set(channels) - {c.value for c in Channel})
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown channel(s): {', '.join(unknown)}",
            )

    before = {
        "name": campaign.name,
        "timezone": campaign.timezone,
        "window_start": campaign.window_start.isoformat(),
        "window_end": campaign.window_end.isoformat(),
        "call_on_weekends": campaign.call_on_weekends,
        "max_attempts_per_day": campaign.max_attempts_per_day,
        "max_attempts_per_week": campaign.max_attempts_per_week,
        "min_hours_between_calls": campaign.min_hours_between_calls,
        "channels": list(campaign.channels or []),
    }

    for field in (
        "name",
        "timezone",
        "call_on_weekends",
        "max_attempts_per_day",
        "max_attempts_per_week",
        "min_hours_between_calls",
    ):
        if field in fields:
            setattr(campaign, field, fields[field])
    campaign.window_start = window_start
    campaign.window_end = window_end
    if channels is not None:
        campaign.channels = list(channels)

    after = {
        "name": campaign.name,
        "timezone": campaign.timezone,
        "window_start": campaign.window_start.isoformat(),
        "window_end": campaign.window_end.isoformat(),
        "call_on_weekends": campaign.call_on_weekends,
        "max_attempts_per_day": campaign.max_attempts_per_day,
        "max_attempts_per_week": campaign.max_attempts_per_week,
        "min_hours_between_calls": campaign.min_hours_between_calls,
        "channels": list(campaign.channels or []),
    }
    audit.record_for(
        session,
        principal,
        action="campaign.updated",
        entity_type="campaign",
        entity_id=campaign_id,
        # The whole settings block both sides rather than only the moved keys.
        # "What was this campaign allowed to do on the day it rang them" is the
        # question these rows get read for, and a diff does not answer it.
        before=before,
        after=after,
    )
    session.commit()
    session.refresh(campaign)
    return campaign


@campaign_router.post("/{campaign_id}/enrol")
def enrol_buyers(
    campaign_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CAMPAIGN_WRITE)),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    buyer_ids = campaign_ops.eligible_buyers(session, principal.company_id)
    result = campaign_ops.enrol(session, campaign, buyer_ids)
    session.commit()
    return result


@campaign_router.post("/{campaign_id}/start", response_model=CampaignOut)
def start_campaign(
    campaign_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CAMPAIGN_START)),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    campaign_ops.start(session, campaign)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.CAMPAIGN_STARTED,
        entity_type="campaign", entity_id=campaign_id,
    )
    session.commit()
    return campaign


@campaign_router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(
    campaign_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CAMPAIGN_START)),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    campaign_ops.pause(session, campaign)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.CAMPAIGN_PAUSED,
        entity_type="campaign", entity_id=campaign_id,
    )
    session.commit()
    return campaign


@campaign_router.get("/{campaign_id}/progress")
def campaign_progress(
    campaign_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.CAMPAIGN_READ)),
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    return campaign_ops.progress(session, campaign)


# ----------------------------------------------------------------- templates

template_router = APIRouter(prefix="/templates", tags=["templates"])


@template_router.post("", status_code=201)
def create_template(
    body: TemplateIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.TEMPLATE_WRITE)),
):
    template = MessageTemplate(
        company_id=principal.company_id,
        key=body.key,
        level=EscalationLevel(body.level),
        channel=body.channel,
        language=body.language,
    )
    session.add(template)
    session.flush()
    version = TemplateVersion(
        company_id=principal.company_id,
        template_id=template.id,
        version=1,
        body=body.body,
        voice_id=body.voice_id,
        dlt_template_id=body.dlt_template_id,
    )
    session.add(version)
    session.flush()
    template.current_version_id = version.id
    audit.record_for(
        principal=principal, session=session, action=audit.Action.TEMPLATE_CREATED,
        entity_type="template", entity_id=template.id,
    )
    session.commit()
    return {"template_id": template.id, "version_id": version.id, "approved": False}


@template_router.post("/versions/{version_id}/approve")
def approve_version(
    version_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.TEMPLATE_APPROVE_L3)),
):
    """Approving legal content requires `legal_approver`, deliberately separate
    from `admin`. The person who writes a template should not approve it."""
    version = session.get(TemplateVersion, version_id)
    if version is None:
        raise HTTPException(404, "template version not found")
    version.approved_by = principal.user_id
    version.approved_at = datetime.now(timezone.utc)
    version.approved_by_label = principal.label or str(principal.user_id)
    audit.record_for(
        principal=principal, session=session, action=audit.Action.TEMPLATE_APPROVED,
        entity_type="template_version", entity_id=version_id,
        after={"approved_at": version.approved_at.isoformat()},
    )
    session.commit()
    return {"version_id": version_id, "approved": True}


# -------------------------------------------------------------------- portal

portal_router = APIRouter(prefix="/portal", tags=["portal"])


def _etag(payload: dict) -> str:
    """Stable hash of the response body.

    `default=str` because the payload carries UUIDs and dates; sort_keys so an
    unchanged payload always hashes the same regardless of dict ordering.
    """
    import hashlib
    import json

    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return f'W/"{hashlib.sha256(blob).hexdigest()[:32]}"'


# ------------------------------------------------------- the creditor profile
#
# This is the company doing the recovering — the "credit owner". It matters
# beyond bookkeeping: the legal name and GSTIN here are what a debtor is told
# is calling, and an L3 notice that names the wrong entity is not a notice.


def _profile_row(session: Session, company_id) -> CompanyProfile | None:
    """The creditor's own profile.

    `CompanyProfile` doubles as the resolved-entity record for buyers, so the
    tenant's own row is the one with no buyer attached. Without that filter this
    returns whichever debtor happens to sort first.
    """
    return session.execute(
        select(CompanyProfile).where(
            CompanyProfile.company_id == company_id,
            CompanyProfile.buyer_id.is_(None),
        )
    ).scalar_one_or_none()


def _profile_out(session: Session, company: Company) -> CompanyProfileOut:
    row = _profile_row(session, company.id)
    buyer_count = session.execute(
        select(func.count()).select_from(Buyer).where(Buyer.company_id == company.id)
    ).scalar_one()
    outstanding = session.execute(
        select(func.coalesce(func.sum(CreditAccount.outstanding_paise), 0)).where(
            CreditAccount.status.notin_(CLOSED_ACCOUNT_STATUSES)
        )
    ).scalar_one()
    return CompanyProfileOut(
        company_id=company.id,
        company_name=company.name,
        legal_name=row.legal_name if row else None,
        gstin=row.gstin if row else None,
        cin=row.cin if row else None,
        registered_address=row.registered_address if row else None,
        state_code=row.state_code if row else None,
        caller_ids=[
            CallerIdOut.model_validate(c)
            for c in session.execute(
                select(CallerId).where(CallerId.company_id == company.id)
            ).scalars()
        ],
        buyer_count=buyer_count,
        outstanding_display=format_inr(int(outstanding or 0)),
    )


@portal_router.get("/profile", response_model=CompanyProfileOut)
def get_company_profile(
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.BUYER_READ)),
):
    company = session.get(Company, principal.company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such company")
    return _profile_out(session, company)


@portal_router.put("/profile", response_model=CompanyProfileOut)
def update_company_profile(
    body: CompanyProfileIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_SETTINGS)),
):
    company = session.get(Company, principal.company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such company")

    row = _profile_row(session, principal.company_id)
    if row is None:
        row = CompanyProfile(
            company_id=principal.company_id,
            legal_name=body.legal_name,
            # Entered by the account holder about themselves, not inferred from
            # a registry, so it is not evidence of anything and gets no
            # confidence score.
            resolution_tier="self_declared",
        )
        session.add(row)

    row.legal_name = body.legal_name
    row.gstin = body.gstin or None
    row.cin = body.cin or None
    row.registered_address = body.registered_address or None
    row.state_code = body.state_code or None
    session.commit()

    audit.record_for(
        session,
        principal,
        action="company.profile.update",
        entity_type="company_profile",
        entity_id=row.id,
        after={"legal_name": row.legal_name, "gstin": row.gstin},
    )
    session.commit()
    return _profile_out(session, company)


# ------------------------------------------------------------- no-call days
#
# Under /portal rather than /campaigns because a blackout is a property of the
# company, not of one campaign: every campaign the tenant runs is silent on
# these days. Filing them under a campaign would invite a second set beside a
# second campaign, and the day a festival is observed is not something two
# campaigns should be able to disagree about.


@portal_router.get("/blackout-dates", response_model=list[BlackoutDateOut])
def list_blackout_dates(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.CAMPAIGN_READ)),
    from_day: date | None = None,
):
    """Readable by anyone who can read campaigns — "why did nothing dial on
    Tuesday" is the question an operator asks first, and it should not need an
    admin to answer."""
    stmt = select(BlackoutDate).order_by(BlackoutDate.day)
    if from_day is not None:
        stmt = stmt.where(BlackoutDate.day >= from_day)
    return list(session.execute(stmt).scalars())


@portal_router.post("/blackout-dates", response_model=BlackoutDateOut, status_code=201)
def add_blackout_date(
    body: BlackoutDateIn,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_SETTINGS)),
):
    """Stop all contact on one day.

    `COMPANY_SETTINGS`, alongside the creditor's own profile, because this
    silences every campaign at once — collection calls on a major festival
    generate complaints that cost more than the day of calling was worth, and
    the converse mistake, removing a day, is just as company-wide.
    """
    row = BlackoutDate(
        company_id=principal.company_id, day=body.day, label=body.label
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{body.day.isoformat()} is already a blackout date",
        )
    audit.record_for(
        session,
        principal,
        action="company.blackout_added",
        entity_type="blackout_date",
        entity_id=row.id,
        after={"day": body.day.isoformat(), "label": body.label},
    )
    session.commit()
    session.refresh(row)
    return row


@portal_router.delete("/blackout-dates/{blackout_id}", status_code=200)
def remove_blackout_date(
    blackout_id: UUID,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.COMPANY_SETTINGS)),
):
    """Delete rather than flag, unlike everything evidential here.

    A blackout is a setting: it says what the dialler may do tomorrow, and it is
    not evidence of anything that happened. What must survive is the change
    itself, which is why the audit row carries the day and the label in `before`
    — deleting the row must not delete the fact that somebody re-opened a day
    for calling.
    """
    row = session.get(BlackoutDate, blackout_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such blackout date")
    removed = {"day": row.day.isoformat(), "label": row.label}
    session.delete(row)
    audit.record_for(
        session,
        principal,
        action="company.blackout_removed",
        entity_type="blackout_date",
        entity_id=blackout_id,
        before=removed,
    )
    session.commit()
    return {"status": "removed", **removed}


@portal_router.get("/state")
def portal_state(
    request: Request,
    response: Response,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.REPORT_READ)),
):
    """Everything the console polls for, in one round trip.

    The portal previously made 6 + N requests every 15 seconds — one per
    campaign for progress alone — and each one re-opened a session and re-ran
    its own aggregates. Per open tab. Per tenant. This collapses that into a
    single request served from one session.

    It also carries an ETag: a poll that finds nothing changed costs a header
    exchange rather than a JSON body and a screenful of aggregate queries, which
    is the common case on a campaign that is between ticks.
    """
    from app.analytics import reports

    company = session.get(Company, principal.company_id)
    campaigns = list(
        session.execute(select(Campaign).order_by(Campaign.created_at.desc())).scalars()
    )

    payload = {
        # Whose portal this is. Every tenant sees only their own row — RLS is
        # bound from the token, so this cannot be pointed at anyone else.
        "company": {
            "id": str(principal.company_id),
            "name": company.name if company else "—",
        },
        "user": {"roles": sorted(principal.roles), "via_api_key": principal.via_api_key},
        "summary": summary(session=session, _=principal),
        # Progress inlined rather than one HTTP call per campaign.
        "campaigns": [
            {
                "id": str(c.id),
                "name": c.name,
                "status": c.status.value,
                "timezone": c.timezone,
                "channels": c.channels or [],
                "progress": campaign_ops.progress(session, c),
            }
            for c in campaigns
        ],
        "reports": reports.full_report(session),
    }

    tag = _etag(payload)
    if request.headers.get("if-none-match") == tag:
        # Nothing moved since the last poll.
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": tag})
    response.headers["ETag"] = tag
    # `no-store`, not `no-cache`. The revalidation here is done explicitly by the
    # portal, which holds the ETag itself and sends If-None-Match. Letting the
    # browser's HTTP cache *also* revalidate means two layers racing over the
    # same conditional request, which shows up in devtools as aborted 304s and
    # buys nothing.
    response.headers["Cache-Control"] = "no-store"
    return payload


@portal_router.get("/list/{kind}")
def portal_list(
    kind: str,
    session: Session = Depends(tenant_db),
    principal: Principal = Depends(require(Permission.CALL_READ)),
    limit: int = Query(50, le=200),
):
    """Row lists, fetched only when the view that shows them is open.

    The Overview never displays these, so loading fifty calls, fifty messages
    and fifty accounts alongside it was work thrown away on every poll.
    """
    now = datetime.now(timezone.utc)
    if kind == "calls":
        rows = session.execute(
            select(Call).order_by(Call.created_at.desc()).limit(limit)
        ).scalars()
        return [
            CallOut(
                id=c.id, buyer_id=c.buyer_id, level=c.level.value, to_e164=c.to_e164,
                status=c.status.value, block_reason=c.block_reason, connected=c.connected,
                delivered=c.delivered, acknowledged=c.acknowledged,
                hangup_cause=c.hangup_cause, scheduled_at=c.scheduled_at,
                duration_sec=c.duration_sec,
            )
            for c in rows
        ]
    if kind == "messages":
        rows = session.execute(
            select(Message).order_by(Message.created_at.desc()).limit(limit)
        ).scalars()
        return [
            MessageOut(
                id=m.id, buyer_id=m.buyer_id, channel=m.channel.value, level=m.level.value,
                to_address=m.to_address, status=m.status.value, sent=m.sent,
                delivered=m.delivered, read=m.read,
            )
            for m in rows
        ]
    if kind == "accounts":
        rows = session.execute(select(CreditAccount).limit(limit)).scalars()
        return [_account_out(session, a, now) for a in rows]
    if kind == "buyers":
        # A name on its own is not worth a screen. What the operator needs is
        # who owes the most and how long it has been — so the outstanding total
        # and worst days-past-due are aggregated here rather than by firing one
        # request per buyer from the browser.
        totals = dict(
            session.execute(
                select(
                    CreditAccount.buyer_id,
                    func.coalesce(func.sum(CreditAccount.outstanding_paise), 0),
                )
                .where(CreditAccount.status.notin_(CLOSED_ACCOUNT_STATUSES))
                .group_by(CreditAccount.buyer_id)
            ).all()
        )
        oldest = dict(
            session.execute(
                select(CreditAccount.buyer_id, func.min(CreditAccount.due_date))
                .where(CreditAccount.status.notin_(CLOSED_ACCOUNT_STATUSES))
                .group_by(CreditAccount.buyer_id)
            ).all()
        )
        out = []
        for b in session.execute(
            select(Buyer).order_by(Buyer.name).limit(limit)
        ).scalars():
            due = oldest.get(b.id)
            out.append(
                {
                    "id": str(b.id),
                    "name": b.name,
                    "external_ref": b.external_ref,
                    "language": b.language,
                    "email": b.email,
                    "consent_withdrawn": b.consent_withdrawn,
                    "phones": [
                        {"e164": p.e164, "dnd_status": p.dnd_status.value}
                        for p in sorted(b.phones, key=lambda p: p.priority)
                    ],
                    "outstanding_paise": int(totals.get(b.id, 0)),
                    "outstanding_display": format_inr(int(totals.get(b.id, 0))),
                    "days_past_due": days_past_due(due.date(), now.date()) if due else 0,
                }
            )
        return out
    raise HTTPException(404, f"unknown list {kind!r}")


# ---------------------------------------------------------------- analytics

analytics_router = APIRouter(prefix="/reports", tags=["reports"])


@analytics_router.get("/full")
def full_report(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.REPORT_READ)),
    as_of: date | None = None,
):
    """Everything, as of a date. Reports must reproduce historically."""
    from app.analytics import reports

    return reports.full_report(session, as_of)


@analytics_router.get("/ageing")
def ageing_report(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.REPORT_READ)),
    as_of: date | None = None,
):
    from app.analytics import reports

    return reports.ageing_summary(session, as_of or datetime.now(timezone.utc).date())


@analytics_router.get("/contact-effectiveness")
def contact_report(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.REPORT_READ)),
    as_of: date | None = None,
    days: int = 30,
):
    """Sent, delivered and acknowledged, reported separately and never summed."""
    from app.analytics import reports

    return reports.contact_effectiveness(
        session, as_of or datetime.now(timezone.utc).date(), days=days
    )


# ---------------------------------------------------------------- public api

public_router = APIRouter(prefix="/v1", tags=["public"])


@public_router.get("/buyers/{buyer_id}/position")
def public_position(
    buyer_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
    as_of: date | None = None,
):
    """Stable, versioned surface for API-key callers.

    Under /v1 deliberately: the portal routes above may change with the portal,
    but anything a customer integrates against gets a version in the path and a
    compatibility promise.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    position = buyer_position(session, buyer_id, as_of)
    return {
        "buyer_id": str(position.buyer_id),
        "as_of": position.as_of.isoformat(),
        "outstanding_paise": position.total_outstanding_paise,
        "outstanding_display": format_inr(position.total_outstanding_paise),
        "invoice_count": position.invoice_count,
        "max_days_past_due": position.max_days_past_due,
        "buckets_paise": {b.value: v for b, v in position.buckets.items()},
    }


@public_router.get("/accounts/{account_id}")
def public_account(
    account_id: UUID,
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.BUYER_READ)),
):
    account = session.get(CreditAccount, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    return _account_out(session, account, datetime.now(timezone.utc)).model_dump()


# ---------------------------------------------------------------- dashboard

dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@dashboard_router.get("/summary")
def summary(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.REPORT_READ)),
):
    """What an operator needs on one screen, including why nothing is dialling."""
    outstanding = session.execute(
        select(func.coalesce(func.sum(CreditAccount.outstanding_paise), 0)).where(
            CreditAccount.status == AccountStatus.OVERDUE
        )
    ).scalar_one()

    by_status = {
        s.value: int(n)
        for s, n in session.execute(
            select(Call.status, func.count()).group_by(Call.status)
        ).all()
    }
    block_reasons = {
        (r or "UNSPECIFIED"): int(n)
        for r, n in session.execute(
            select(Call.block_reason, func.count())
            .where(Call.status == CallStatus.BLOCKED)
            .group_by(Call.block_reason)
        ).all()
    }
    by_level = {
        lvl.value: int(n)
        for lvl, n in session.execute(
            select(EscalationState.level, func.count()).group_by(EscalationState.level)
        ).all()
    }
    delivered = session.execute(
        select(func.count())
        .select_from(Call)
        .where(Call.status == CallStatus.ANSWERED, Call.playback_completed.is_(True))
    ).scalar_one()

    return {
        "outstanding_paise": int(outstanding),
        "outstanding_display": format_inr(int(outstanding)),
        "calls_by_status": by_status,
        "block_reasons": block_reasons,
        "escalation_levels": by_level,
        "delivered_calls": int(delivered),
        "messages": {
            s: int(n)
            for s, n in session.execute(
                select(Message.status, func.count()).group_by(Message.status)
            ).all()
        },
    }


@dashboard_router.get("/calls", response_model=list[CallOut])
def list_calls(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.CALL_READ)),
    limit: int = Query(100, le=500),
):
    calls = session.execute(
        select(Call).order_by(Call.created_at.desc()).limit(limit)
    ).scalars()
    return [
        CallOut(
            id=c.id, buyer_id=c.buyer_id, level=c.level.value, to_e164=c.to_e164,
            status=c.status.value, block_reason=c.block_reason, connected=c.connected,
            delivered=c.delivered, acknowledged=c.acknowledged,
            hangup_cause=c.hangup_cause, scheduled_at=c.scheduled_at,
            duration_sec=c.duration_sec,
        )
        for c in calls
    ]


@dashboard_router.get("/messages", response_model=list[MessageOut])
def list_messages(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.CALL_READ)),
    limit: int = Query(100, le=500),
):
    rows = session.execute(
        select(Message).order_by(Message.created_at.desc()).limit(limit)
    ).scalars()
    return [
        MessageOut(
            id=m.id, buyer_id=m.buyer_id, channel=m.channel.value, level=m.level.value,
            to_address=m.to_address, status=m.status.value, sent=m.sent,
            delivered=m.delivered, read=m.read,
        )
        for m in rows
    ]


# ------------------------------------------------------------------- the trail

audit_router = APIRouter(prefix="/audit", tags=["audit"])


@audit_router.get("", response_model=list[AuditEntryOut])
def list_audit(
    session: Session = Depends(tenant_db),
    _: Principal = Depends(require(Permission.AUDIT_READ)),
    entity_type: str | None = Query(None),
    entity_id: UUID | None = Query(None),
    action: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(100, le=500),
) -> list[AuditEntryOut]:
    """Read the trail. Until this existed, `AUDIT_READ` granted nothing.

    The trail is append-only in the database and was unreadable through the
    product, which is the same as not having one when somebody asks. Consent is
    the case that makes it urgent: `restore_consent` clears both consent columns
    on the buyer, so the row written from `before` is the only surviving
    evidence that a withdrawal ever happened — filter by
    `entity_type=buyer&entity_id=<id>` to read that history back.

    Tenant-scoped by RLS like every other read here, so no `company_id`
    predicate belongs in the query.
    """
    stmt = select(AuditLog)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if since is not None:
        stmt = stmt.where(AuditLog.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.occurred_at <= until)

    rows = session.execute(
        stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
    ).scalars()
    return [AuditEntryOut.model_validate(row) for row in rows]


for sub in (
    auth_router,
    audit_router,
    portal_router,
    analytics_router,
    public_router,
    trade_router,
    import_router,
    campaign_router,
    template_router,
    dashboard_router,
):
    router.include_router(sub)
