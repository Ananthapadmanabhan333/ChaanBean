"""Whether a debt may be published, and how fast it comes down.

**Every gate here is a barrier to publication, never to removal.** Listing is
slow, evidenced and reversible. Delisting is fast and can be triggered by the
listed party. That asymmetry is the entire design: with it, an error is a delay;
without it, an error is a defamation claim.

Publishing that a named business owes money is a statement of fact about a real
company. If it is wrong — a disputed invoice, a mismatched entity, a debt paid
but unreconciled — you are the defendant. So every one of the eight gates below
must pass, and `entity_confidence_tier` must be the *top* tier: a name-only
match must never reach publication, because that is exactly how you publish
about the wrong company.

Do not enable publication without counsel. `can_publish` is the switch, and it
defaults off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Small sums do not justify a public statement about a business.
DEFAULT_MINIMUM_PAISE = 10_000_000  # ₹1,00,000
LEDGER_RECENCY = timedelta(days=7)
NO_PAYMENT_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class ListingInput:
    as_of: date
    account_status: str
    ever_disputed: bool
    outstanding_paise: int
    notice_dispatched: bool
    # Proof of delivery, not merely dispatch. "We sent it" is not evidence that
    # they received it, and the whole listing rests on their having been told.
    notice_delivery_proof: bool
    notice_response_deadline: date | None
    notice_response_received: bool
    entity_confidence_publishable: bool
    ledger_reconciled_on: date | None
    last_payment_on: date | None
    human_signed_off_by: str | None
    minimum_paise: int = DEFAULT_MINIMUM_PAISE


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    blockers: tuple[str, ...] = ()
    checked: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "blockers": list(self.blockers),
            "checked": list(self.checked),
        }


def evaluate(data: ListingInput) -> Eligibility:
    """All eight gates, every one required."""
    blockers: list[str] = []
    checked: list[str] = []

    status = (data.account_status or "").upper()
    if status != "OVERDUE":
        blockers.append(f"account status is {status!r}, not OVERDUE")
    else:
        checked.append("account is OVERDUE")

    # Ever disputed, not merely currently. A withdrawn dispute still means the
    # debt was contested, and a contested debt is not a fact to publish.
    if data.ever_disputed:
        blockers.append("the account has been disputed at some point in its history")
    else:
        checked.append("never disputed")

    if data.outstanding_paise < data.minimum_paise:
        blockers.append(
            f"{data.outstanding_paise} paise is below the "
            f"{data.minimum_paise} paise publication threshold"
        )
    else:
        checked.append("above the amount threshold")

    if not data.notice_dispatched:
        blockers.append("no legal notice has been dispatched")
    elif not data.notice_delivery_proof:
        blockers.append("the notice has no proof of delivery — dispatch is not receipt")
    else:
        checked.append("notice dispatched with delivery proof")

    if data.notice_response_received:
        blockers.append("the debtor responded to the notice substantively")
    elif data.notice_response_deadline is None:
        blockers.append("no response deadline recorded on the notice")
    elif data.as_of <= data.notice_response_deadline:
        blockers.append(
            f"the response period runs until {data.notice_response_deadline.isoformat()}"
        )
    else:
        checked.append("response period expired with no substantive response")

    # The gate that stops a mismatched entity becoming defamation.
    if not data.entity_confidence_publishable:
        blockers.append(
            "entity resolution is below the top tier — a name-only match must "
            "never be published"
        )
    else:
        checked.append("entity resolved on an identifier match")

    if data.ledger_reconciled_on is None:
        blockers.append("the ledger has never been reconciled for this account")
    elif data.as_of - data.ledger_reconciled_on > LEDGER_RECENCY:
        blockers.append(
            f"ledger last reconciled {data.ledger_reconciled_on.isoformat()}; a stale "
            f"balance is the already-paid scenario"
        )
    else:
        checked.append("ledger reconciled recently")

    if data.last_payment_on and (data.as_of - data.last_payment_on) <= NO_PAYMENT_WINDOW:
        blockers.append(
            f"a payment was received on {data.last_payment_on.isoformat()}"
        )
    else:
        checked.append("no payment in the last 30 days")

    if not data.human_signed_off_by:
        blockers.append("no recorded human sign-off")
    else:
        checked.append(f"signed off by {data.human_signed_off_by}")

    return Eligibility(not blockers, tuple(blockers), tuple(checked))


# ------------------------------------------------------------------ removal


@dataclass(frozen=True)
class DelistReason:
    code: str
    detail: str
    immediate: bool = True


def should_delist(
    *,
    paid_in_full: bool = False,
    dispute_raised: bool = False,
    entity_challenged: bool = False,
    listing_party_withdrew: bool = False,
    ledger_shows_settled: bool = False,
) -> DelistReason | None:
    """Any one of these removes a listing immediately.

    Note the shape: `evaluate` above requires *all* gates to publish, and this
    requires *any* to remove. That is the asymmetry stated as code — and none of
    these wait for a review, because a wrong listing does damage every day it
    stands.
    """
    if paid_in_full or ledger_shows_settled:
        return DelistReason("PAID", "the debt has been settled")
    if dispute_raised:
        return DelistReason(
            "DISPUTED",
            "the listed party disputes the debt; publication is suspended while "
            "it is examined, not after",
        )
    if entity_challenged:
        return DelistReason("ENTITY_CHALLENGED", "the identity of the listed business is contested")
    if listing_party_withdrew:
        return DelistReason("WITHDRAWN", "the listing company withdrew the claim")
    return None
