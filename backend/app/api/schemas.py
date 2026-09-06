"""Request and response shapes.

Money crosses this boundary as **integer paise with an explicit field name** —
`amount_paise`, never a float and never a bare `amount`. Display formatting is
the caller's job, using the same `format_inr` the rest of the system uses, so
there is exactly one place that decides what 42,00,000 looks like.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
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


# --------------------------------------------- the *buyer's* legal identity
#
# `CompanyProfileIn`/`Out` above are the tenant describing themselves. These
# describe a debtor, which is a different question with a different failure: get
# the creditor's own name wrong and a notice looks unprofessional; get a
# debtor's wrong and it is aimed at a company that owes nothing.


class BuyerIdentityIn(BaseModel):
    """What a person types about a buyer. Claims, all of it.

    Every field here arrives from a keyboard, so nothing in it is evidence of
    anything. It is stored so a lookup has something to look up — never as a
    verification result.
    """

    legal_name: str | None = Field(default=None, max_length=300)
    gstin: str | None = Field(default=None, max_length=15)
    cin: str | None = Field(default=None, max_length=21)
    # Accepted, hashed on arrival, and never echoed. See `BuyerIdentityOut`.
    pan: str | None = Field(default=None, max_length=10)
    registered_address: str | None = None
    state_code: str | None = Field(default=None, max_length=2)


class BuyerIdentityOut(BaseModel):
    buyer_id: UUID
    # The legal name once one is recorded, the trading name until then.
    name: str
    gstin: str | None = None
    cin: str | None = None
    # There is deliberately **no `pan` field**. A PAN is a national identifier;
    # it goes in as a claim, is stored as a hash plus four characters, and does
    # not come back out. A convenience field here would be the one place a
    # database of them could be read back over HTTP.
    pan_last4: str | None = None
    registered_address: str | None = None
    state_code: str | None = None


class VerificationOut(BaseModel):
    """One verification run, as it stood at `verified_at`.

    `signals` carries the evidence both ways and `blockers` says, in sentences a
    non-specialist can act on, why this is not publishable. Both are returned
    even when the answer is yes — "we were sure" is not a reason.
    """

    buyer_id: UUID
    buyer_name: str
    tier: str
    confidence: float
    publishable: bool
    gst_status: str | None = None
    mca_status: str | None = None
    signals: list[dict] = []
    blockers: list[str] = []
    candidate_id: UUID | None = None
    verified_at: datetime | None = None


class EntityCandidateReviewIn(BaseModel):
    """A person's answer to "is this the same company?".

    Two words, no third. There is no "probably" here on purpose: the whole
    reason a candidate exists is that the machine already said "probably".
    """

    decision: Literal["CONFIRM", "REJECT"]
    note: str | None = Field(default=None, max_length=500)


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


class AuditEntryOut(BaseModel):
    """One row of the trail, as read back.

    `before` and `after` are carried whole rather than summarised: for a consent
    restore they are the only surviving evidence that a withdrawal ever
    happened, because the restore clears both columns on the buyer.
    """

    id: UUID
    occurred_at: datetime
    action: str
    actor_id: UUID | None = None
    actor_label: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    before: dict | None = None
    after: dict | None = None
    detail: str | None = None

    model_config = {"from_attributes": True}


class AccountOut(BaseModel):
    id: UUID
    buyer_id: UUID
    # The id, not only the human-readable ref: credit note, write-off and
    # cancellation all address the invoice, so without it the console can list
    # an account and reach none of the three routes that close it.
    invoice_id: UUID | None = None
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


# ------------------------------------------------ making a debt smaller
#
# Everything above this line can only grow what a buyer owes. These are the
# other direction, and each one carries the invoice's recomputed balance back —
# not as a convenience, but because the caller has just changed a number that
# appears in a demand call, and the number they should read next is the one the
# single writer produced rather than the one they expected.


class CreditNoteIn(BaseModel):
    """A credit note as a person enters it.

    `amount` is a typed string for the same reason it is on `BuyerIn`: a note
    for `1,50,000` is one lakh fifty thousand, and only `normalise_amount` knows
    that. `invoice_id` may be omitted for an on-account credit, which reduces
    nothing yet and waits to be applied.
    """

    buyer_id: UUID
    note_number: str = Field(min_length=1, max_length=100)
    amount: str
    issue_date: date
    invoice_id: UUID | None = None
    reason: str | None = None


class CreditNoteOut(BaseModel):
    id: UUID
    buyer_id: UUID
    invoice_id: UUID | None
    note_number: str
    amount_paise: int
    amount_display: str
    issue_date: date
    reason: str | None = None
    # The invoice as it stands after the note, or nulls for an on-account credit
    # that has not been applied to one.
    invoice_outstanding_paise: int | None = None
    invoice_outstanding_display: str | None = None
    invoice_status: str | None = None


class OnAccountIn(BaseModel):
    """Where to put money the creditor is already holding.

    Omitting `allocations` lets it fall oldest-first. The keys are invoice ids
    and the values integer paise — never a float, and never a share.
    """

    allocations: dict[UUID, int] | None = None


class AllocationOut(BaseModel):
    invoice_id: UUID
    amount_paise: int
    amount_display: str
    rule: str


class AllocationPlanOut(BaseModel):
    payment_id: UUID
    allocated_paise: int
    allocated_display: str
    on_account_paise: int
    on_account_display: str
    allocations: list[AllocationOut] = []


class InvoiceClosureIn(BaseModel):
    """Why this debt stopped being pursued.

    Mandatory, and `app.trade.reduction` refuses without it. It is the only
    account of the decision that survives, and it is what an audit of an
    abandoned debt reads first.
    """

    reason: str = Field(min_length=3, max_length=500)


class InvoiceOut(BaseModel):
    id: UUID
    buyer_id: UUID
    invoice_number: str
    status: str
    net_paise: int
    net_display: str
    outstanding_paise: int
    outstanding_display: str
    closure_reason: str | None = None
    closed_at: datetime | None = None
    # The recovery projection, returned alongside so a caller can see that the
    # ladder followed the ledger rather than having to ask separately.
    account_status: str | None = None


class DisputeClearIn(BaseModel):
    resolution: str | None = Field(default=None, max_length=500)


# ------------------------------------------------- corrections and cessation


class BuyerPatchIn(BaseModel):
    """A correction to what we hold. Three fields, and no more.

    The whitelist is `app.trade.consent.CORRECTABLE_FIELDS`; anything else is
    refused there by name. Absent and null are different: a field left out is
    untouched, `email: null` clears the address — which is deliberately allowed,
    because no address is better than one that delivers a demand to a stranger.
    """

    name: str | None = None
    email: str | None = None
    language: str | None = None

    # Unknown fields are refused rather than dropped. Pydantic's default is to
    # ignore them, which would let `{"name": ..., "gstin": ...}` succeed with the
    # name written and the GSTIN silently discarded — and the caller believing
    # both landed. That is the one failure mode a correction endpoint must not
    # have.
    model_config = {"extra": "forbid"}


class ConsentIn(BaseModel):
    """Who told us, and how. Both required, and neither is decoration.

    `source` is a closed set (`app.trade.consent.ConsentSource`) because "how do
    you know they asked" is the first question when a withdrawal is disputed,
    and a free-text answer is not evidence of anything.
    """

    reason: str = Field(min_length=1, max_length=500)
    source: str


class SuppressIn(BaseModel):
    """A temporary hold. `until` must carry an offset — a naive timestamp is a
    guess between UTC and IST, and the guess is worth five and a half hours of
    contact in one direction or the other."""

    until: datetime
    reason: str = Field(min_length=1, max_length=500)


class ChannelOptOutIn(BaseModel):
    channel: str
    source: str


class PhoneRetireIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ContactChangeOut(BaseModel):
    """What moved, in the same shape the audit row records.

    Returned rather than a bare status so the caller can show the operator the
    before and after they just caused — the two dicts are empty when a
    re-submitted form changed nothing.
    """

    entity_type: str
    entity_id: UUID
    before: dict = {}
    after: dict = {}
    detail: str
    changed: bool


# ------------------------------------------------------------------ promises


class PromiseIn(BaseModel):
    """A promise to pay, as recorded on the call.

    `promised_on` defaults to today in the promise timezone rather than being
    required: the person entering this is on the phone, and the date they would
    have to type is the date it already is.
    """

    buyer_id: UUID
    amount: str
    promised_by_date: date
    account_id: UUID | None = None
    promised_on: date | None = None


class PromiseSettleIn(BaseModel):
    kept: bool
    note: str | None = Field(default=None, max_length=500)


class PromiseOut(BaseModel):
    id: UUID
    buyer_id: UUID
    account_id: UUID | None = None
    promised_amount_paise: int
    promised_amount_display: str
    promised_on: date
    promised_by_date: date
    status: str
    settled_at: datetime | None = None
    # When the ladder may speak to this buyer again. The promised date plus
    # grace, converted from IST, so it is not the same thing as the date above.
    chase_resumes_at: datetime | None = None


class BehaviourOut(BaseModel):
    """One appended payment-behaviour row. Rates are 0..1, never percentages."""

    id: UUID
    buyer_id: UUID
    as_of: date
    invoices_settled: int
    mean_days_to_pay: float | None = None
    median_days_to_pay: float | None = None
    days_to_pay_trend: float | None = None
    part_payment_rate: float | None = None
    promise_kept_rate: float | None = None
    dispute_rate: float | None = None


# ------------------------------------------------------- scheduling settings


class BlackoutDateIn(BaseModel):
    day: date
    label: str | None = Field(default=None, max_length=100)


class BlackoutDateOut(BaseModel):
    id: UUID
    day: date
    label: str | None = None

    model_config = {"from_attributes": True}


class CampaignPatchIn(BaseModel):
    """Every field optional; absent means untouched.

    The window bounds are strings for the same reason `CampaignIn` uses them —
    "10:00" is what a person types — and the 08:00-19:00 ceiling is a check
    constraint on the table rather than a rule this schema can be talked out of.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    call_on_weekends: bool | None = None
    max_attempts_per_day: int | None = Field(default=None, ge=1, le=5)
    max_attempts_per_week: int | None = Field(default=None, ge=1, le=20)
    min_hours_between_calls: int | None = Field(default=None, ge=1, le=168)
    channels: list[str] | None = None

    # As with `BuyerPatchIn`: an unknown key is refused, not dropped. `status` is
    # the one somebody will send, expecting to start a campaign, and silently
    # returning 200 to that is worse than refusing it.
    model_config = {"extra": "forbid"}


# -------------------------------------------------------- the review queue


class ReviewItemOut(BaseModel):
    """One account waiting for a person.

    `ever_disputed` is read from the ladder history rather than from
    `disputed_reason`, which clearing nulls — it is the answer publication is
    gated on, and it is the one thing a reviewer must not have to infer.
    """

    account_id: UUID
    buyer_id: UUID
    buyer_name: str
    invoice_ref: str | None = None
    outstanding_paise: int
    outstanding_display: str
    status: str
    level: str
    disputed_reason: str | None = None
    ever_disputed: bool = False
    last_event: dict | None = None


class ReviewResolveIn(BaseModel):
    """What the person saw. Required: an empty queue with no note beside it is
    indistinguishable from a queue somebody cleared without reading."""

    note: str = Field(min_length=3, max_length=500)


# ----------------------------------------------------------- court records


class LegalLinkOut(BaseModel):
    case_id: UUID
    court_id: str
    case_number: str
    case_type: str | None = None
    link_id: UUID | None = None
    status: str
    # The grade the evidence actually reached. Only IDENTIFIER can ever be
    # confirmed, and the field is returned so a reviewer sees why the button is
    # refused rather than discovering it on the 409.
    tier: str
    confidence: float
    reasons: list[str] = []


class CaseSearchOut(BaseModel):
    profile_id: UUID | None = None
    searched_name: str | None = None
    cases_seen: int = 0
    links: list[LegalLinkOut] = []
    refusals: list[str] = []
    notes: list[str] = []
    history_id: UUID | None = None
    confirmed_legal_cases: int = 0
    recovery_suits: int = 0
    insolvency_cases: int = 0
    awaiting_review: int = 0


class LegalLinkReviewIn(BaseModel):
    """Two words, as with an entity candidate, and for the same reason."""

    decision: Literal["CONFIRM", "REJECT"]
    reason: str | None = Field(default=None, max_length=500)


class PrelegalIn(BaseModel):
    """The evidence flags a pre-legal assessment cannot read for itself.

    Every one defaults to the unhelpful answer, because each is a blocker in
    `app.legal.prelegal` and a caller who forgets one should get "not ready"
    rather than a recommendation resting on a default.
    """

    delivered_contacts_at_l2: int = Field(default=0, ge=0, le=99)
    has_delivery_proof: bool = False
    last_acknowledgement: date | None = None
    last_part_payment: date | None = None


class PrelegalOut(BaseModel):
    assessment_id: UUID
    account_id: UUID
    outcome: str
    factors: list[str] = []
    blockers: list[str] = []
    estimated_cost_paise: int | None = None
    estimated_cost_display: str | None = None
    recoverable_paise: int | None = None
    recoverable_display: str | None = None
    limitation_expires_on: date | None = None
    limitation_urgent: bool = False
    confirmed_legal_cases: int = 0
    recovery_suits: int = 0
    insolvency_cases: int = 0
    awaiting_review: int = 0
