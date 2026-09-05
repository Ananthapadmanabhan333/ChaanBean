"""Risk scoring — transparent, weighted, and explainable by construction.

**A score must never be a black box that changes how someone is treated without
a stated reason.** If a debtor is escalated faster because of a score, you have
to be able to say which factors caused it and by how much — to the customer, to
the debtor, and to a regulator.

That rules out an opaque model here. When there are thousands of resolved
outcomes a learned model becomes defensible as a *ranking* aid inside this same
explainable envelope — still never as the sole cause of an escalation.

The ledger is weighted heaviest on purpose: your own payment history is both the
most predictive signal available and the only one no competitor can buy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

MODEL_VERSION = "rules-1.0"


class Band(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


@dataclass(frozen=True)
class Factor:
    name: str
    value: float
    weight: float
    contribution: float
    explanation: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "weight": self.weight,
            "contribution": round(self.contribution, 2),
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class ScoringContext:
    as_of: date
    # --- ledger (heaviest)
    mean_days_to_pay: float | None = None
    days_to_pay_trend: float | None = None  # positive means slowing down
    promise_kept_rate: float | None = None
    part_payment_rate: float | None = None
    dispute_rate: float | None = None
    contact_response_rate: float | None = None
    # --- exposure and ageing
    outstanding_paise: int = 0
    historical_average_paise: int = 0
    max_days_past_due: int = 0
    # --- external, only when confidently resolved
    gst_filing_regular: bool | None = None
    company_status: str | None = None  # Active | Struck Off | Under Liquidation
    confirmed_legal_cases: int = 0
    recovery_suits: int = 0


@dataclass(frozen=True)
class RiskResult:
    score: float
    band: Band
    factors: tuple[Factor, ...] = ()
    model_version: str = MODEL_VERSION
    as_of: date | None = None

    @property
    def explanation(self) -> str:
        """Plain-language reasons, biggest contributor first."""
        ranked = sorted(self.factors, key=lambda f: abs(f.contribution), reverse=True)
        return "; ".join(f"{f.explanation} ({f.contribution:+.0f})" for f in ranked[:5])

    def as_dicts(self) -> list[dict]:
        return [f.as_dict() for f in self.factors]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_buyer(ctx: ScoringContext) -> RiskResult:
    """0 (safe) to 100 (highest risk), with every point attributable."""
    factors: list[Factor] = []

    def add(name: str, value: float, weight: float, explanation: str) -> None:
        factors.append(
            Factor(name, value, weight, value * weight, explanation)
        )

    # --- payment behaviour: the heaviest group, because it is your own data
    if ctx.mean_days_to_pay is not None:
        # 30 days is unremarkable; 120 is severe.
        value = _clamp((ctx.mean_days_to_pay - 30) / 90)
        add("mean_days_to_pay", value, 18,
            f"pays on average in {ctx.mean_days_to_pay:.0f} days")

    if ctx.days_to_pay_trend is not None:
        value = _clamp(ctx.days_to_pay_trend / 45)
        add("days_to_pay_trend", value, 14,
            f"payment time trending {ctx.days_to_pay_trend:+.0f} days — "
            f"the earliest reliable distress signal")

    if ctx.promise_kept_rate is not None:
        value = _clamp(1 - ctx.promise_kept_rate)
        add("promise_kept_rate", value, 16,
            f"keeps {ctx.promise_kept_rate:.0%} of payment promises")

    if ctx.part_payment_rate is not None:
        # Deliberately light. Part payment usually signals cash-flow strain
        # rather than unwillingness, and those deserve different treatment.
        value = _clamp(ctx.part_payment_rate)
        add("part_payment_rate", value, 5,
            f"{ctx.part_payment_rate:.0%} of settlements were part payments — "
            f"often strain rather than refusal")

    if ctx.contact_response_rate is not None:
        value = _clamp(1 - ctx.contact_response_rate)
        add("contact_response_rate", value, 8,
            f"responds to {ctx.contact_response_rate:.0%} of contact attempts")

    if ctx.dispute_rate is not None:
        value = _clamp(ctx.dispute_rate)
        add("dispute_rate", value, 4,
            f"{ctx.dispute_rate:.0%} of invoices disputed — a high rate may be "
            f"your own invoicing problem, not theirs")

    # --- exposure
    if ctx.historical_average_paise > 0:
        ratio = ctx.outstanding_paise / ctx.historical_average_paise
        value = _clamp((ratio - 1) / 3)
        add("exposure_vs_history", value, 10,
            f"owes {ratio:.1f}x their historical average balance")

    # --- ageing
    value = _clamp(ctx.max_days_past_due / 180)
    add("ageing", value, 15, f"oldest item is {ctx.max_days_past_due} days past due")

    # --- external signals, medium weight and only when present
    if ctx.company_status and ctx.company_status.lower() not in ("active", ""):
        add("company_status", 1.0, 12,
            f"registry status is {ctx.company_status!r}")

    if ctx.gst_filing_regular is False:
        add("gst_filing", 1.0, 8, "GST filings are irregular")

    if ctx.recovery_suits:
        value = _clamp(ctx.recovery_suits / 3)
        add("recovery_suits", value, 10,
            f"{ctx.recovery_suits} confirmed recovery suit(s) against them")
    elif ctx.confirmed_legal_cases:
        value = _clamp(ctx.confirmed_legal_cases / 5)
        add("legal_cases", value, 5,
            f"{ctx.confirmed_legal_cases} confirmed case(s) — only links a human "
            f"reviewed are counted")

    total_weight = sum(f.weight for f in factors) or 1
    raw = sum(f.contribution for f in factors)
    score = round(_clamp(raw / total_weight) * 100, 2)

    if score < 25:
        band = Band.LOW
    elif score < 50:
        band = Band.MODERATE
    elif score < 75:
        band = Band.ELEVATED
    else:
        band = Band.HIGH

    return RiskResult(score, band, tuple(factors), MODEL_VERSION, ctx.as_of)
