"""Running a pre-legal assessment against a real account, and keeping it.

`app.legal.prelegal` holds the arithmetic and the preconditions. This module is
what feeds it: it reads the account, converts the ledger's UTC into the dates a
court actually reads, adds what the court records say about this buyer, and
writes the result down.

The court records it adds are **confirmed links only**. `app.legal.cases` will
propose a link on a name that matches, and a name that matches is not a case
against this debtor — folding one into a recommendation to serve a demand notice
would be aiming a legal accusation on the strength of a coincidence. Cases
awaiting review are reported as a count, so the reader knows the picture is
incomplete, and they change nothing.

Assessments are append-only. A re-run in September leaves March's row alone,
because the question that gets asked afterwards is always "what did you know
when you decided to send it".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.identity import audit
from app.legal.cases import LegalSignals, legal_signals
from app.legal.prelegal import (
    DEFAULT_ECONOMIC_FLOOR_PAISE,
    DEFAULT_NOTICE_COST_PAISE,
    Assessment,
    AssessmentInput,
    Outcome,
    assess,
)
from app.models import CompanyProfile, CreditAccount, PrelegalAssessment

ACTION_ASSESSED = "legal.prelegal_assessed"

# Limitation runs on calendar dates in India, and the calendar is the court's.
COURT_TZ = ZoneInfo("Asia/Kolkata")


class AccountNotFound(LookupError):
    """No such account in this tenant. Named so a route can turn it into a 404."""


@dataclass(frozen=True)
class PrelegalOutcome:
    assessment_id: UUID
    assessment: Assessment
    legal: LegalSignals


def _court_date(moment: datetime) -> date:
    """The date this instant falls on in the court's own calendar.

    A claim that expires on 31 March expires at the end of that day in Kolkata,
    not at the end of it in UTC, and the five and a half hours between the two
    are a whole day of limitation on either side of midnight.
    `app.policy.engine` converts to the campaign timezone before checking a
    calling window for the same reason.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(COURT_TZ).date()


def _legal_factors(signals: LegalSignals) -> tuple[list[str], list[str]]:
    factors: list[str] = []
    blockers: list[str] = []

    if signals.recovery_suits:
        factors.append(
            f"{signals.recovery_suits} confirmed recovery matter(s) already on record "
            f"against this buyer — an unsecured claim joins the back of that queue"
        )
    elif signals.confirmed_legal_cases:
        factors.append(
            f"{signals.confirmed_legal_cases} confirmed court case(s) on record against "
            f"this buyer"
        )

    if signals.awaiting_review:
        # Named rather than hidden. The reader is deciding whether to accuse
        # somebody in writing, and "there are five unreviewed matches" is
        # material to that even though not one of them counts.
        factors.append(
            f"{signals.awaiting_review} court case(s) matched this buyer by name and "
            f"are awaiting review; none of them are counted here"
        )

    if signals.insolvency_cases:
        blockers.append(
            f"{signals.insolvency_cases} confirmed insolvency proceeding(s) against this "
            f"buyer. A moratorium under Section 14 of the Insolvency and Bankruptcy Code "
            f"bars a separate recovery suit while it stands — counsel must confirm the "
            f"position before anything is sent"
        )

    return factors, blockers


def assess_account(
    session: Session,
    account_id: UUID,
    *,
    company_id: UUID,
    now: datetime,
    delivered_contacts_at_l2: int = 0,
    has_delivery_proof: bool = False,
    last_acknowledgement: date | None = None,
    last_part_payment: date | None = None,
    economic_floor_paise: int = DEFAULT_ECONOMIC_FLOOR_PAISE,
    estimated_cost_paise: int = DEFAULT_NOTICE_COST_PAISE,
    assessed_by: UUID | None = None,
    actor_label: str | None = None,
) -> PrelegalOutcome:
    """Assess one account and write the result down.

    The evidence flags default to the unhelpful answer — nobody reached, nothing
    delivered — because each one is a blocker in `prelegal.assess`, and a caller
    that forgets to pass one should get "not ready" rather than a recommendation
    resting on a default.

    Flushes but never commits; the caller owns the transaction.
    """
    account = session.execute(
        select(CreditAccount).where(CreditAccount.id == account_id)
    ).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(f"no credit account {account_id} in company {company_id}")

    profile = (
        session.execute(
            select(CompanyProfile)
            .where(CompanyProfile.buyer_id == account.buyer_id)
            .order_by(CompanyProfile.created_at.desc())
        )
        .scalars()
        .first()
    )
    signals = legal_signals(session, profile.id) if profile is not None else LegalSignals()

    assessment = assess(
        AssessmentInput(
            as_of=_court_date(now),
            account_status=account.status.value,
            outstanding_paise=account.outstanding_paise,
            due_date=_court_date(account.due_date),
            last_acknowledgement=last_acknowledgement,
            last_part_payment=last_part_payment,
            delivered_contacts_at_l2=delivered_contacts_at_l2,
            # An invoice reference is a number somebody typed on the account.
            # Founding a claim needs the document.
            has_invoice=account.invoice_id is not None,
            has_delivery_proof=has_delivery_proof,
            economic_floor_paise=economic_floor_paise,
            estimated_cost_paise=estimated_cost_paise,
        )
    )

    factors, blockers = _legal_factors(signals)
    if profile is None or profile.resolved_at is None:
        factors.append(
            "this buyer's identity has not been resolved against a registry, so the "
            "notice would be addressed to a name rather than to a confirmed company"
        )

    outcome = assessment.outcome
    if blockers and outcome is Outcome.RECOMMEND_NOTICE:
        # A moratorium is a legal question about whether the recommended action
        # is even available. That belongs to a person, not to a refusal.
        outcome = Outcome.ROUTE_TO_HUMAN

    final = replace(
        assessment,
        outcome=outcome,
        factors=assessment.factors + tuple(factors),
        blockers=assessment.blockers + tuple(blockers),
    )

    # Always an insert. An assessment is what was believed on a day, and a day
    # cannot be edited.
    row = PrelegalAssessment(
        company_id=company_id,
        account_id=account.id,
        outcome=final.outcome.value,
        factors=list(final.factors),
        blockers=list(final.blockers),
        estimated_cost_paise=final.estimated_cost_paise,
        recoverable_paise=final.recoverable_paise,
        limitation_expires_on=final.limitation_expires_on,
        limitation_urgent=final.limitation_urgent,
        assessed_at=now,
        assessed_by=assessed_by,
    )
    session.add(row)
    session.flush()

    audit.record(
        session,
        action=ACTION_ASSESSED,
        company_id=company_id,
        actor_id=assessed_by,
        actor_label=actor_label,
        entity_type="credit_account",
        entity_id=account.id,
        after={
            "assessment_id": str(row.id),
            "outcome": final.outcome.value,
            "confirmed_legal_cases": signals.confirmed_legal_cases,
            "recovery_suits": signals.recovery_suits,
            "awaiting_review": signals.awaiting_review,
            "limitation_expires_on": (
                final.limitation_expires_on.isoformat()
                if final.limitation_expires_on
                else None
            ),
            "blockers": list(final.blockers),
        },
        detail=f"{final.outcome.value}: {final.rationale}",
    )

    return PrelegalOutcome(assessment_id=row.id, assessment=final, legal=signals)
