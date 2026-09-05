"""Request and response shapes.

Money crosses this boundary as **integer paise with an explicit field name** —
`amount_paise`, never a float and never a bare `amount`. Display formatting is
the caller's job, using the same `format_inr` the rest of the system uses, so
there is exactly one place that decides what 42,00,000 looks like.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # A plain string, not EmailStr. At login this is a lookup key, and
    # `email-validator` rejects the RFC 2606 reserved TLDs (.test, .example)
    # that every local fixture and demo correctly uses. Address validity is
    # checked where it actually matters — by the email provider at send time.
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    roles: list[str]
    company_id: UUID


class BuyerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    external_ref: str | None = None
    language: str = "en-IN"
    email: str | None = None
    # A buyer with no number cannot be called, and this is a voice product. The
    # numbers arrive raw ("98765 43210", "+91-98765-43210") and are normalised
    # server-side, so the client never has to know what E.164 is.
    phones: list[str] = Field(default_factory=list, max_length=10)

    # What they owe, recorded at the same time. A credit buyer with no amount is
    # a contact, not a debtor — nothing can be escalated against it. Optional,
    # because a buyer may legitimately be created before their first invoice.
    #
    # `amount` is deliberately a string: it is typed by a person, in Indian
    # grouping ("4,28,600", "₹4.5 lakh"), and normalise_amount is the one place
    # that knows 4,50,000 is four lakh fifty thousand rather than forty-five
    # thousand. Accepting a float here would lose that and the paise.
    amount: str | None = None
    due_date: date | None = None
    invoice_number: str | None = Field(default=None, max_length=100)


class CreditCheckIn(BaseModel):
    """The credit details a buyer declares when asking for a limit.

    Money arrives as typed strings for the same reason it does elsewhere here:
    a person enters `50,00,000` and only `normalise_amount` knows that is fifty
    lakh rather than five million read the Western way.
    """

    requested_limit: str
    annual_turnover: str | None = None
    years_trading: float | None = Field(default=None, ge=0, le=200)
    other_creditor_exposure: str | None = None
    trade_references: int = Field(default=0, ge=0, le=99)
    gst_registered: bool | None = None
    # Set only when someone has actually seen a GST return or a bank statement.
    # It roughly doubles the weight the declared turnover carries, so it is a
    # claim about evidence, not a convenience toggle.
    turnover_verified: bool = False
    notes: str | None = Field(default=None, max_length=500)


class CreditDecisionIn(BaseModel):
    """What the human decided. The system never fills this in for them."""

    approved_limit: str
    note: str | None = Field(default=None, max_length=500)


class CreditFactorOut(BaseModel):
    name: str
    value: float
    weight: float
    contribution: float
    explanation: str


class CreditCheckOut(BaseModel):
    id: UUID
    buyer_id: UUID
    buyer_name: str
    recommendation: str
    score: float
    suggested_limit_paise: int
    suggested_limit_display: str
    requested_limit_paise: int
    requested_limit_display: str
    factors: list[CreditFactorOut] = []
    blockers: list[str] = []
    model_version: str
    as_of: date
    computed_at: datetime | None = None
    decided_limit_paise: int | None = None
    decided_limit_display: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None


class PhoneIn(BaseModel):
    raw: str = Field(min_length=4, max_length=32)
    priority: int = 0


class CompanyProfileIn(BaseModel):
    """The creditor's own profile — who the debtor is told is calling."""

    legal_name: str = Field(min_length=1, max_length=300)
    gstin: str | None = Field(default=None, max_length=15)
    cin: str | None = Field(default=None, max_length=21)
    registered_address: str | None = None
    state_code: str | None = Field(default=None, max_length=2)


class CallerIdOut(BaseModel):
    id: UUID
    e164: str
    carrier_approved: bool
    is_default: bool

    model_config = {"from_attributes": True}


class CompanyProfileOut(BaseModel):
    company_id: UUID
    company_name: str
    legal_name: str | None
    gstin: str | None
    cin: str | None
    registered_address: str | None
    state_code: str | None
    caller_ids: list[CallerIdOut] = []
    buyer_count: int = 0
    outstanding_display: str = "₹0"


class PhoneOut(BaseModel):
    id: UUID
    e164: str
    number_type: str | None
    priority: int
    is_valid: bool
    dnd_status: str
    dnd_checked_at: datetime | None

    model_config = {"from_attributes": True}


class BuyerOut(BaseModel):
    id: UUID
    name: str
    external_ref: str | None
    language: str
    email: str | None
    consent_withdrawn: bool
    next_action_at: datetime | None
    phones: list[PhoneOut] = []

    model_config = {"from_attributes": True}


class AccountOut(BaseModel):
    id: UUID
    buyer_id: UUID
    invoice_ref: str | None
    outstanding_paise: int
    outstanding_display: str
    due_date: datetime
    status: str
    days_past_due: int
    level: str | None = None

    model_config = {"from_attributes": True}


class InvoiceIn(BaseModel):
    buyer_id: UUID
    invoice_number: str
    issue_date: date
    due_date: date
    amount_paise: int = Field(gt=0)


class PaymentIn(BaseModel):
    buyer_id: UUID
    amount_paise: int = Field(gt=0)
    received_date: date
    reference: str | None = None
    allocations: dict[UUID, int] | None = None


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Asia/Kolkata"
    window_start: str = "10:00"
    window_end: str = "19:00"
    call_on_weekends: bool = False
    max_attempts_per_day: int = Field(default=1, ge=1, le=5)
    max_attempts_per_week: int = Field(default=3, ge=1, le=20)
    min_hours_between_calls: int = Field(default=24, ge=1, le=168)
    channels: list[str] = ["SMS", "WHATSAPP", "EMAIL", "VOICE"]


class CampaignOut(BaseModel):
    id: UUID
    name: str
    status: str
    timezone: str
    channels: list[str]

    model_config = {"from_attributes": True}


class TemplateIn(BaseModel):
    key: str
    level: str
    channel: str = "VOICE"
    language: str = "en-IN"
    body: str
    voice_id: str = "Kajal"
    dlt_template_id: str | None = None


class CallOut(BaseModel):
    id: UUID
    buyer_id: UUID
    level: str
    to_e164: str
    status: str
    block_reason: str | None
    connected: bool
    delivered: bool
    acknowledged: bool
    hangup_cause: int | None
    scheduled_at: datetime
    duration_sec: int | None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: UUID
    buyer_id: UUID
    channel: str
    level: str
    to_address: str
    status: str
    sent: bool
    delivered: bool
    read: bool

    model_config = {"from_attributes": True}


class ImportPreviewOut(BaseModel):
    batch_id: UUID
    total: int
    counts: dict[str, int]
    rejections: list[dict]
    review_rows: list[dict]
    new_phone_count: int
    note: str
