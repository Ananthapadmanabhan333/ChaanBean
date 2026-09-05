"""Should this buyer be given credit, and how much?

This is **decision support, not a decision.** It returns a recommendation, a
suggested limit and the reasons for both. A person sets the limit. Nothing here
approves or declines anything on its own, and the API records who decided.

That is a deliberate constraint rather than a missing feature:

* An automated adverse decision about a real business, on data they cannot see
  or correct, is what turns a credit tool into a liability. Under the DPDP Act
  the subject can ask what was held about them and why; "the model said so" is
  not an answer.
* The buyer's declared figures are unverified. Turnover typed into a form is a
  claim, not evidence, so its influence is deliberately capped — otherwise the
  way to unlock a large limit is to type a large number.

**Why a scorecard rather than a learned model.** Every output here has to
survive the question "why did you refuse them?", asked by the customer, the
buyer, or a regulator. A weighted scorecard answers it by construction: each
factor states its value, its weight and its contribution in points. The same
inputs always produce the same output, and MODEL_VERSION records which rules
ran, so a decision made in March can be reconstructed in December. An opaque
model can do none of that, and for a credit decision that is disqualifying
rather than merely inconvenient. This mirrors `app.intelligence.scoring`, which
takes the same position for recovery risk.

The recovery risk score is an *input* here. The two questions differ — "how hard
will this be to collect" versus "should we extend more" — but the first is the
best evidence available for the second, and it comes from your own ledger rather
than from anyone's declaration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.intelligence.scoring import Band, Factor, RiskResult

MODEL_VERSION = "credit-rules-1.0"

# A quarter of one month's turnover is a conventional opening trade-credit
# exposure. It is intentionally conservative: a limit set too low costs a phone
# call, and one set too high becomes the debt this product exists to chase.
MONTHS_PER_YEAR = 12
OPENING_EXPOSURE_FRACTION = 0.25

# Limits are granted in round rupees, not to the paise. An arithmetic result of
# 1,57,524.99 is not a credit limit anybody would set, and printing it that way
# makes a considered recommendation look like a spreadsheet artefact. Rounded
# *down* to the nearest thousand rupees, because every other rounding decision
# in this module errs the same way.
LIMIT_ROUNDING_PAISE = 1_000_00


class Recommendation(str, Enum):
    """Never APPROVE/REJECT — those are decisions, and a person makes them."""

    STRONG = "STRONG"          # comfortable at the suggested limit
    ACCEPTABLE = "ACCEPTABLE"  # workable, usually below what was asked for
    CAUTION = "CAUTION"        # only with security, prepayment, or a small limit
    REFER = "REFER"            # not on this evidence; a human must look


@dataclass(frozen=True)
class CreditApplication:
    """What the buyer, or your salesperson, tells you.

    All of it is unverified by definition. `turnover_verified` exists so that a
    figure confirmed against a GST return or a bank statement can be weighted
    differently from one typed into a form.
    """

    requested_limit_paise: int
    annual_turnover_paise: int | None = None
    years_trading: float | None = None
    existing_exposure_paise: int = 0        # what they already owe you
    other_creditor_exposure_paise: int = 0  # what they say they owe elsewhere
    trade_references: int = 0
    gst_registered: bool | None = None
    turnover_verified: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class LedgerFacts:
    """What your own books already know. Empty for a brand-new buyer."""

    invoices_settled: int = 0
    mean_days_to_pay: float | None = None
    promise_kept_rate: float | None = None
    currently_overdue_paise: int = 0
    max_days_past_due: int = 0


@dataclass(frozen=True)
class AssessmentResult:
    recommendation: Recommendation
    score: float                      # 0 (weak) to 100 (strong)
    suggested_limit_paise: int
    requested_limit_paise: int
    factors: tuple[Factor, ...]
    blockers: tuple[str, ...]
    model_version: str
    as_of: date

    @property
    def within_request(self) -> bool:
        return self.suggested_limit_paise >= self.requested_limit_paise

    def as_dict(self) -> dict:
        return {
            "recommendation": self.recommendation.value,
            "score": self.score,
            "suggested_limit_paise": self.suggested_limit_paise,
            "requested_limit_paise": self.requested_limit_paise,
            "factors": [f.as_dict() for f in self.factors],
            "blockers": list(self.blockers),
            "model_version": self.model_version,
            "as_of": self.as_of.isoformat(),
        }


def _clamp(v: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, v))


def assess(
    application: CreditApplication,
    *,
    as_of: date,
    risk: RiskResult | None = None,
    ledger: LedgerFacts | None = None,
) -> AssessmentResult:
    """Score creditworthiness from 0 (weak) to 100 (strong).

    Note the direction. `scoring.score_buyer` runs 0 = safe to 100 = risky; this
    runs the other way, because here a high number is good news. The risk score
    is inverted where it is consumed rather than either scale being bent to match
    the other — two scales that mean opposite things but look identical are how
    someone eventually reads one as the other.
    """
    ledger = ledger or LedgerFacts()
    factors: list[Factor] = []
    blockers: list[str] = []

    def add(name: str, value: float, weight: float, explanation: str) -> None:
        factors.append(Factor(name, value, weight, value * weight, explanation))

    # --- what your own ledger knows: heaviest, because it is the only evidence
    #     here that nobody typed in hoping for a particular answer.
    if risk is not None:
        strength = _clamp(1 - (risk.score / 100))
        add("recovery_risk", strength, 30,
            f"recovery risk is {risk.band.value} ({risk.score:.0f}/100) "
            f"on your own history")

    if ledger.invoices_settled:
        # Twelve settled invoices is a track record; one is an anecdote.
        add("track_record", _clamp(ledger.invoices_settled / 12), 12,
            f"{ledger.invoices_settled} invoice(s) already settled with you")
    else:
        add("track_record", 0.0, 12,
            "no settled invoices with you yet — nothing to judge them on")

    if ledger.mean_days_to_pay is not None:
        add("payment_speed", _clamp(1 - ((ledger.mean_days_to_pay - 15) / 75)), 10,
            f"pays in {ledger.mean_days_to_pay:.0f} days on average")

    if ledger.promise_kept_rate is not None:
        add("promises_kept", _clamp(ledger.promise_kept_rate), 8,
            f"keeps {ledger.promise_kept_rate:.0%} of payment promises")

    # --- declared, and weighted lower because it is unverified
    if application.annual_turnover_paise:
        monthly = application.annual_turnover_paise / MONTHS_PER_YEAR
        cover = monthly / max(application.requested_limit_paise, 1)
        value = _clamp(cover / 4)  # a month's turnover at 4x the limit is ample
        weight = 14 if application.turnover_verified else 7
        add("turnover_cover", value, weight,
            f"declared turnover covers the requested limit {cover:.1f}x per month"
            + ("" if application.turnover_verified else " (unverified)"))

    if application.years_trading is not None:
        add("trading_history", _clamp(application.years_trading / 5), 10,
            f"trading for {application.years_trading:.1f} year(s)")

    if application.trade_references:
        add("trade_references", _clamp(application.trade_references / 3), 6,
            f"{application.trade_references} trade reference(s) offered")

    if application.gst_registered is not None:
        add("gst_registered", 1.0 if application.gst_registered else 0.0, 6,
            "GST registered" if application.gst_registered else "not GST registered")

    total_exposure = (
        application.existing_exposure_paise + application.other_creditor_exposure_paise
    )
    if application.annual_turnover_paise and total_exposure:
        monthly = application.annual_turnover_paise / MONTHS_PER_YEAR
        strain = total_exposure / max(monthly, 1)
        add("existing_exposure", _clamp(1 - (strain / 3)), 10,
            f"already carrying {strain:.1f} month(s) of turnover in credit")

    total_weight = sum(f.weight for f in factors) or 1
    score = round(_clamp(sum(f.contribution for f in factors) / total_weight) * 100, 2)

    # --- blockers: facts a score must not be allowed to average away.
    #
    # A strong turnover and a long history still do not make it sensible to
    # extend more credit to someone who has not paid what they already owe you.
    # Averaged into a score, that fact disappears; kept separate, it cannot.
    if ledger.currently_overdue_paise > 0:
        blockers.append(
            f"already overdue to you by "
            f"{ledger.currently_overdue_paise / 100:,.0f} rupees"
        )
    if ledger.max_days_past_due >= 90:
        blockers.append(
            f"an item is {ledger.max_days_past_due} days past due — past the "
            f"point where the ladder reaches legal escalation"
        )
    if risk is not None and risk.band is Band.HIGH:
        blockers.append("recovery risk is HIGH on your own payment history")
    if application.requested_limit_paise <= 0:
        blockers.append("no limit was requested")

    # --- the suggested limit
    #
    # A conservative opening exposure, discounted by risk and by how long they
    # have traded, then reduced by what they already owe you. Never more than
    # was asked for: this recommends a ceiling, it does not upsell.
    suggested = 0
    if application.annual_turnover_paise and application.requested_limit_paise > 0:
        monthly = application.annual_turnover_paise / MONTHS_PER_YEAR
        base = monthly * OPENING_EXPOSURE_FRACTION
        # The score carries the evidence — track record, payment speed, promises
        # kept, turnover cover. Scaling the limit by it is what stops a buyer
        # with no history at all being offered the same limit as one with
        # fourteen settled invoices, which is what happened when the limit was
        # derived from turnover and vintage alone.
        strength = score / 100
        risk_multiplier = (
            {Band.LOW: 1.0, Band.MODERATE: 0.6, Band.ELEVATED: 0.3, Band.HIGH: 0.0}[
                risk.band
            ]
            if risk is not None
            else 1.0  # absence of a risk score is already priced into `strength`
        )
        if application.years_trading is None:
            vintage = 0.7
        elif application.years_trading < 1:
            vintage = 0.5
        elif application.years_trading < 3:
            vintage = 0.8
        else:
            vintage = 1.0
        if not application.turnover_verified:
            # An unverified figure buys a smaller limit, never a bigger one.
            base *= 0.6
        suggested = int(base * strength * risk_multiplier * vintage)
        suggested = max(0, suggested - application.existing_exposure_paise)
        suggested = min(suggested, application.requested_limit_paise)
        suggested -= suggested % LIMIT_ROUNDING_PAISE

    if blockers:
        # A blocker caps the recommendation regardless of score, and must not
        # read as an endorsement. The limit goes to zero: anything else is this
        # tool suggesting more credit for someone already in arrears with you.
        recommendation = Recommendation.REFER
        suggested = 0
    elif score >= 70:
        recommendation = Recommendation.STRONG
    elif score >= 50:
        recommendation = Recommendation.ACCEPTABLE
    elif score >= 30:
        recommendation = Recommendation.CAUTION
    else:
        recommendation = Recommendation.REFER

    return AssessmentResult(
        recommendation=recommendation,
        score=score,
        suggested_limit_paise=suggested,
        requested_limit_paise=application.requested_limit_paise,
        factors=tuple(factors),
        blockers=tuple(blockers),
        model_version=MODEL_VERSION,
        as_of=as_of,
    )
