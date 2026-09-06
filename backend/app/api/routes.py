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
from sqlalchemy.orm import Session

from app.api.schemas import (
    AccountOut,
    BuyerIdentityIn,
    BuyerIdentityOut,
    BuyerIn,
    BuyerOut,
    CallerIdOut,
    CreditCheckIn,
    CreditCheckOut,
    CreditDecisionIn,
    CompanyProfileIn,
    CompanyProfileOut,
    EntityCandidateReviewIn,
    PhoneIn,
    CallOut,
    CampaignIn,
    CampaignOut,
    ImportPreviewOut,
    InvoiceIn,
    LoginRequest,
    MessageOut,
    PaymentIn,
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
    Buyer,
    BuyerPhone,
    CallerId,
    Company,
    CompanyProfile,
    CreditAssessment,
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
    Message,
    MessageTemplate,
    Payment,
    PaymentBehaviour,
    TemplateVersion,
    User,
    VerificationReport,
)
from app.providers.base import REGISTRY
from app.render.numbers import format_inr
from app.scheduler import campaign as campaign_ops
from app.trade import accounts as account_ops
from app.trade.ageing import buyer_position, days_past_due
from app.trade.allocation import apply_payment

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

    open_rows = list(
        session.execute(
            select(CreditAccount).where(
                CreditAccount.buyer_id == buyer_id,
                CreditAccount.status != AccountStatus.SETTLED,
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
    """Recording a payment settles accounts and stops their ladders immediately."""
    payment = Payment(
        company_id=principal.company_id,
        buyer_id=body.buyer_id,
        amount_paise=body.amount_paise,
        unallocated_paise=body.amount_paise,
        received_date=body.received_date,
        reference=body.reference,
    )
    session.add(payment)
    session.flush()
    plan = apply_payment(session, payment, instructions=body.allocations)

    for allocation in plan.allocations:
        account_ops.sync_account_from_invoice(
            session, session.get(Invoice, allocation.invoice_id)
        )
    session.commit()
    return {
        "payment_id": payment.id,
        "allocated_paise": plan.allocated_paise,
        "on_account_paise": plan.unallocated_paise,
        "allocations": [
            {"invoice_id": a.invoice_id, "amount_paise": a.amount_paise, "rule": a.rule.value}
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
            CreditAccount.status != AccountStatus.SETTLED
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
                .where(CreditAccount.status != AccountStatus.SETTLED)
                .group_by(CreditAccount.buyer_id)
            ).all()
        )
        oldest = dict(
            session.execute(
                select(CreditAccount.buyer_id, func.min(CreditAccount.due_date))
                .where(CreditAccount.status != AccountStatus.SETTLED)
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


for sub in (
    auth_router,
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
