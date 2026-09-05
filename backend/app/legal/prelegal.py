"""Deciding whether an account justifies legal action.

**This module prepares legal action. It does not take it.** Every output is a
draft for a qualified human to approve, sign and send. The job is to make that
human fast and accurate, never to replace them.

The limitation arithmetic below is a legal question implemented in code. Under
the Limitation Act 1963 the ordinary period for a suit on a debt is three years
from when the cause of action accrued, and Section 18/19 acknowledgements or
part payments can restart it. **Have counsel verify this implementation — do not
treat the code as the authority.** A claim that lapses in a queue is a total loss
and entirely preventable, which is why anything within six months of expiry is
flagged urgent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

# Limitation Act 1963, Article 14/15 — three years for a suit on a debt.
LIMITATION_YEARS = 3
URGENT_WINDOW = timedelta(days=182)

# Below this, legal recovery costs more than the debt. Placeholder — confirm
# with the business and with counsel before production.
DEFAULT_ECONOMIC_FLOOR_PAISE = 2_500_000  # ₹25,000
DEFAULT_NOTICE_COST_PAISE = 350_000  # ₹3,500 — advocate fee plus dispatch


class Outcome(str, Enum):
    NOT_READY = "NOT_READY"
    RECOMMEND_NOTICE = "RECOMMEND_NOTICE"
    RECOMMEND_WRITE_OFF = "RECOMMEND_WRITE_OFF"
    ROUTE_TO_HUMAN = "ROUTE_TO_HUMAN"


@dataclass(frozen=True)
class AssessmentInput:
    as_of: date
    account_status: str
    outstanding_paise: int
    due_date: date
    # Section 18/19: a written acknowledgement or a part payment restarts the
    # clock from that date.
    last_acknowledgement: date | None = None
    last_part_payment: date | None = None
    delivered_contacts_at_l2: int = 0
    has_invoice: bool = False
    has_delivery_proof: bool = False
    economic_floor_paise: int = DEFAULT_ECONOMIC_FLOOR_PAISE
    estimated_cost_paise: int = DEFAULT_NOTICE_COST_PAISE


@dataclass(frozen=True)
class Assessment:
    outcome: Outcome
    blockers: tuple[str, ...] = ()
    factors: tuple[str, ...] = ()
    limitation_expires_on: date | None = None
    limitation_urgent: bool = False
    limitation_expired: bool = False
    estimated_cost_paise: int = 0
    recoverable_paise: int = 0
    rationale: str = ""

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "blockers": list(self.blockers),
            "factors": list(self.factors),
            "limitation_expires_on": (
                self.limitation_expires_on.isoformat() if self.limitation_expires_on else None
            ),
            "limitation_urgent": self.limitation_urgent,
            "limitation_expired": self.limitation_expired,
            "estimated_cost_paise": self.estimated_cost_paise,
            "recoverable_paise": self.recoverable_paise,
        }


def limitation_expiry(data: AssessmentInput) -> date:
    """Three years from the latest event that started or restarted the clock.

    The cause of action accrues at the due date; an acknowledgement in writing
    or a part payment (Limitation Act ss. 18-19) restarts it from that date.
    """
    started = max(
        d for d in (data.due_date, data.last_acknowledgement, data.last_part_payment) if d
    )
    try:
        return started.replace(year=started.year + LIMITATION_YEARS)
    except ValueError:  # 29 February
        return started.replace(year=started.year + LIMITATION_YEARS, day=28)


def assess(data: AssessmentInput) -> Assessment:
    """Every precondition is required. A missing one is a blocker, not a warning."""
    blockers: list[str] = []
    factors: list[str] = []

    expires = limitation_expiry(data)
    expired = data.as_of > expires
    urgent = not expired and (expires - data.as_of) <= URGENT_WINDOW

    if expired:
        blockers.append(
            f"limitation period expired on {expires.isoformat()} — a suit is time-barred"
        )
    elif urgent:
        factors.append(
            f"URGENT: limitation expires {expires.isoformat()}, "
            f"{(expires - data.as_of).days} days away"
        )

    status = (data.account_status or "").upper()
    if status == "IN_DISPUTE":
        blockers.append("account is in dispute; automated legal escalation halts")
    elif status in ("SETTLED", "WRITTEN_OFF"):
        blockers.append(f"account is {status}")
    elif status != "OVERDUE":
        blockers.append(f"account status {status!r} is not OVERDUE")

    # A notice to someone never actually reached is procedurally weak and
    # ethically poor.
    if data.delivered_contacts_at_l2 < 1:
        blockers.append(
            "no delivered contact at L2 — the debtor has never actually been reached"
        )
    else:
        factors.append(f"{data.delivered_contacts_at_l2} delivered contact(s) at L2")

    if not data.has_invoice:
        blockers.append("no invoice on file to found a claim on")
    if not data.has_delivery_proof:
        factors.append("no proof of delivery — the claim is weaker without it")

    recoverable = data.outstanding_paise - data.estimated_cost_paise
    below_floor = data.outstanding_paise < data.economic_floor_paise

    if below_floor or recoverable <= 0:
        # Recommending a notice that costs more than it recovers is the failure
        # that makes customers distrust the whole product.
        return Assessment(
            outcome=Outcome.RECOMMEND_WRITE_OFF,
            blockers=tuple(blockers),
            factors=tuple(
                factors
                + [
                    f"outstanding {data.outstanding_paise} paise against an estimated "
                    f"cost of {data.estimated_cost_paise} paise leaves "
                    f"{recoverable} paise recoverable"
                ]
            ),
            limitation_expires_on=expires,
            limitation_urgent=urgent,
            limitation_expired=expired,
            estimated_cost_paise=data.estimated_cost_paise,
            recoverable_paise=max(0, recoverable),
            rationale="legal action costs more than it would recover",
        )

    if blockers:
        # An expired limitation is a decision for a human, not a quiet refusal.
        outcome = Outcome.ROUTE_TO_HUMAN if expired else Outcome.NOT_READY
        return Assessment(
            outcome=outcome,
            blockers=tuple(blockers),
            factors=tuple(factors),
            limitation_expires_on=expires,
            limitation_urgent=urgent,
            limitation_expired=expired,
            estimated_cost_paise=data.estimated_cost_paise,
            recoverable_paise=max(0, recoverable),
            rationale=blockers[0],
        )

    return Assessment(
        outcome=Outcome.RECOMMEND_NOTICE,
        factors=tuple(factors),
        limitation_expires_on=expires,
        limitation_urgent=urgent,
        limitation_expired=False,
        estimated_cost_paise=data.estimated_cost_paise,
        recoverable_paise=recoverable,
        rationale=(
            f"every precondition met; {recoverable} paise recoverable after costs"
        ),
    )
