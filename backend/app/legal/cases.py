"""Searching the courts for a buyer, and deciding whose case it is.

`app.legal.matching` scores the resemblance between a company and a party name.
This module is what happens around that score: it searches, it keeps what came
back, and it decides what the resemblance is *allowed to do*.

The decision is the point. A court search takes a name and returns every case
filed against that name in the state, so the searching is trivial and the
attribution is where the harm lives. Attaching a stranger's recovery suit to a
debtor's file is defamation, and it is the most likely way this platform gets
sued.

So confirmation runs on identifiers and nothing else. A case may only reach
CONFIRMED when the filing itself recorded an identifier against a party and that
identifier is one the registry holds for this company — `Tier.IDENTIFIER` on
`app.company.resolution`'s ladder, whose `publishable` property is the single
place that rule is written down. Everything softer — a name that matches
perfectly, a director named as a respondent, the company's own GSTIN sitting in
a recital — is stored as a PROPOSED `LegalLink` for a person to read, counts for
nothing, and cannot be confirmed even by someone who wants to: `confirm_link`
recomputes the grade and refuses.

**A buyer whose own identity is unresolved can have nothing confirmed against
them.** Identifier-grade means an identifier that is *theirs*, and until
verification has established which company this buyer is, there is no such
identifier — only a number somebody typed off an invoice.

What is append-only, and what is not. `court_cases` is keyed on
(company_id, court_id, case_number), so a case found again refreshes that row: a
stage moves, a hearing date changes, and a second row would just be a second
answer to "what is the position today". The evidential trail lives beside it —
every search writes a fresh `ProviderFetch` holding the payload verbatim and a
fresh `LegalHistory` recording what was claimed on the day. "What did we know on
12 March" is answered from those, never from the case row.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.company.resolution import Tier, normalise_company_name
from app.company.verification import BuyerNotFound, cap_tier_for_provenance
from app.identity import audit
from app.legal.matching import CaseRef, PartyRef, ProfileRef, link_candidates
from app.models import (
    Buyer,
    CaseParty,
    CompanyProfile,
    CourtCase,
    LegalHistory,
    LegalLink,
    McaRecord,
    ProviderFetch,
)
from app.providers.courts import CaseQuery, CourtBackend, CourtRecord

ACTION_SEARCHED = "legal.cases_searched"
ACTION_LINK_CONFIRMED = "legal.link_confirmed"
ACTION_LINK_REJECTED = "legal.link_rejected"

_PROVIDER = "courts"

PROPOSED = "PROPOSED"
CONFIRMED = "CONFIRMED"
REJECTED = "REJECTED"

# Printed on every history. A legal history that does not say what it is missing
# invites the reader to treat it as a clean bill, which is the one thing it can
# never be.
CAVEAT = (
    "Only cases confirmed against an identifier the registry holds for this company "
    "are counted. Court records name parties by name alone, so cases belonging to "
    "this company may be absent, and a case awaiting review has not been established "
    "as theirs."
)


class Refusal(str, Enum):
    """Every way this module declines to act, named so a caller can branch."""

    NO_BACKEND = "NO_BACKEND"
    NO_PROFILE = "NO_PROFILE"
    PROFILE_NOT_IDENTIFIER_RESOLVED = "PROFILE_NOT_IDENTIFIER_RESOLVED"
    LINK_NOT_FOUND = "LINK_NOT_FOUND"
    # A link whose case has gone. The foreign key should make this unreachable,
    # which is why no test exercises it; it is here so the failure is a named
    # refusal rather than an attribute error on None.
    CASE_NOT_FOUND = "CASE_NOT_FOUND"
    ALREADY_REJECTED = "ALREADY_REJECTED"
    RECORD_NOT_REGISTRY_BACKED = "RECORD_NOT_REGISTRY_BACKED"
    MATCH_NOT_IDENTIFIER_GRADE = "MATCH_NOT_IDENTIFIER_GRADE"


class LinkRefused(RuntimeError):
    def __init__(self, reason: Refusal, detail: str):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# ------------------------------------------------------------------ case types

# Types carrying the heavier `recovery_suits` weight in
# `app.intelligence.scoring`. Insolvency proceedings are in the set because an
# operational creditor's Section 9 petition is a recovery route with a court
# behind it, and an admitted one is the worst news the register can give.
#
# Membership is exact after normalisation, not a substring test. An unrecognised
# type still counts as a confirmed case; it simply does not collect the heavier
# weight. Guessing upward escalates a real debtor faster on the strength of a
# label nobody has checked.
INSOLVENCY_CASE_TYPES = frozenset(
    {"IBC SECTION 7", "IBC SECTION 9", "INSOLVENCY PETITION", "WINDING UP PETITION"}
)
RECOVERY_CASE_TYPES = (
    frozenset(
        {
            "MONEY SUIT",
            "RECOVERY SUIT",
            "SUMMARY SUIT",
            "COMMERCIAL SUIT",
            "ORDER XXXVII",
            "ORIGINAL APPLICATION",
            # A dishonoured-cheque prosecution is criminal rather than a suit,
            # and for trade credit it is the most direct evidence there is that
            # this buyer's payments have already failed somebody else.
            "SECTION 138 NI ACT",
        }
    )
    | INSOLVENCY_CASE_TYPES
)


def normalise_case_type(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()


def is_recovery_case_type(value: str | None) -> bool:
    return normalise_case_type(value) in RECOVERY_CASE_TYPES


def is_insolvency_case_type(value: str | None) -> bool:
    return normalise_case_type(value) in INSOLVENCY_CASE_TYPES


# ------------------------------------------------------------------- the grade


@dataclass(frozen=True)
class Grade:
    tier: Tier
    reasons: tuple[str, ...] = ()
    matched_identifier: str | None = None
    matched_party: str | None = None

    @property
    def confirmable(self) -> bool:
        """The resolution ladder's own rule, asked rather than restated."""
        return self.tier.publishable


def verified_identifiers(profile: CompanyProfile | None) -> frozenset[str]:
    """Identifiers a registry confirmed belong to this company.

    Only a profile stamped `Tier.IDENTIFIER` contributes: below that tier the
    numbers on the row are what somebody declared, and a court case matched
    against a declared identifier is a court case matched against a claim.

    PAN is absent because it is held hashed and last-four only — there is
    nothing here to compare a filing against, and reconstructing one to try
    would put a national identifier back in the clear.
    """
    if profile is None or profile.resolved_at is None:
        return frozenset()
    if profile.resolution_tier != Tier.IDENTIFIER.value:
        return frozenset()
    values = [profile.gstin, profile.cin, *(profile.additional_gstins or ())]
    return frozenset(v.strip().upper() for v in values if v and str(v).strip())


def grade_match(
    *,
    identifiers: frozenset[str],
    parties: list[dict],
    provenance: str | None,
    signals: dict,
) -> Grade:
    """How strong is the claim that this case belongs to this company?

    `signals` is what `app.legal.matching` produced. It decides the softer
    tiers; it can never reach the top one, because every signal in it is
    ultimately a name a clerk typed.
    """
    for party in parties:
        value = (party.get("identifier") or "").strip().upper()
        if not value or value not in identifiers:
            continue
        capped = cap_tier_for_provenance(Tier.IDENTIFIER, provenance)
        if capped is not Tier.IDENTIFIER:
            return Grade(
                capped,
                (
                    f"The filing records {value} against {party.get('name')}, which is "
                    f"this company's identifier — but the case itself was typed in by "
                    f"an operator rather than fetched from a court, so it identifies "
                    f"nothing on its own.",
                ),
                matched_identifier=value,
                matched_party=party.get("name"),
            )
        return Grade(
            Tier.IDENTIFIER,
            (
                f"The filing records {value} against {party.get('name')}, and the "
                f"registry holds that identifier for this company. This is the only "
                f"evidence a court record can offer that names a company rather than "
                f"a name.",
            ),
            matched_identifier=value,
            matched_party=party.get("name"),
        )

    if signals.get("identifier_in_text"):
        return Grade(
            Tier.STRUCTURAL,
            (
                f"{signals['identifier_in_text']} appears in the text of the filing but "
                f"is attributed to no party. It says this company is mentioned in the "
                f"papers, not which side of the case it is on — a guarantor's suit and "
                f"a suit against the company read identically here.",
            ),
        )

    if signals.get("director_is_party"):
        return Grade(
            Tier.NAME_STRONG,
            (
                f"{signals['director_is_party']} is a director of this company and is "
                f"named as a party. Strong for a proprietorship, where the firm and the "
                f"person are one litigant, and a coincidence of names otherwise.",
            ),
        )

    if signals.get("exact_normalised_name"):
        distinctive = bool(signals.get("distinctive"))
        return Grade(
            Tier.NAME_STRONG if distinctive else Tier.NAME_WEAK,
            (
                f"The party name matches exactly once normalised "
                f"({signals['exact_normalised_name']}), on a "
                f"{'distinctive' if distinctive else 'common'} name."
                + (
                    ""
                    if distinctive
                    else " Names this short repeat across hundreds of unrelated firms."
                ),
            ),
        )

    if signals.get("name_similarity"):
        return Grade(
            Tier.NAME_WEAK,
            (
                f"The party name and this company's share "
                f"{signals['name_similarity']} of their words, and nothing else "
                f"connects them.",
            ),
        )

    return Grade(Tier.NONE, ("Nothing in this case connects it to this company.",))


# ------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class LinkOutcome:
    case_id: UUID
    court_id: str
    case_number: str
    case_type: str | None
    link_id: UUID | None
    status: str
    tier: str
    confidence: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchOutcome:
    profile_id: UUID | None
    searched_name: str | None
    cases_seen: int
    links: tuple[LinkOutcome, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    notes: tuple[str, ...] = ()
    history_id: UUID | None = None

    @property
    def confirmable(self) -> tuple[LinkOutcome, ...]:
        return tuple(link for link in self.links if link.tier == Tier.IDENTIFIER.value)


@dataclass(frozen=True)
class LegalSignals:
    """What the risk engine and the pre-legal assessment are allowed to see."""

    confirmed_legal_cases: int = 0
    recovery_suits: int = 0
    insolvency_cases: int = 0
    awaiting_review: int = 0

    def for_scoring(self) -> dict:
        """The two fields `app.intelligence.scoring.ScoringContext` reads.

        `awaiting_review` is deliberately not one of them. An unreviewed
        proposal is a question, and a question must not move a score.
        """
        return {
            "confirmed_legal_cases": self.confirmed_legal_cases,
            "recovery_suits": self.recovery_suits,
        }


# ------------------------------------------------------------- small helpers


def _jsonable(value):
    """JSONB takes no dates or UUIDs, and a fetched payload must survive storage."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _party_dicts(record: CourtRecord) -> list[dict]:
    return [
        {"name": p.name, "role": p.role, "identifier": p.identifier}
        for p in record.parties
    ]


def _latest_profile(session: Session, buyer_id: UUID) -> CompanyProfile | None:
    return (
        session.execute(
            select(CompanyProfile)
            .where(CompanyProfile.buyer_id == buyer_id)
            .order_by(CompanyProfile.created_at.desc())
        )
        .scalars()
        .first()
    )


def _director_names(session: Session, profile_id: UUID) -> tuple[str, ...]:
    """Directors as the MCA filed them, from the record attached to this profile.

    Records are only attached at `Tier.IDENTIFIER`, so these names are the
    registry's and not an operator's.
    """
    record = (
        session.execute(
            select(McaRecord)
            .where(McaRecord.profile_id == profile_id)
            .order_by(McaRecord.fetched_at.desc())
        )
        .scalars()
        .first()
    )
    if record is None:
        return ()
    return tuple(
        d["name"] for d in (record.directors or []) if isinstance(d, dict) and d.get("name")
    )


def _profile_ref(
    profile: CompanyProfile, identifiers: frozenset[str], directors: tuple[str, ...]
) -> ProfileRef:
    def held(value: str | None) -> str | None:
        return value if value and value.strip().upper() in identifiers else None

    return ProfileRef(
        profile_id=str(profile.id),
        legal_name=profile.legal_name,
        trade_names=tuple(profile.trade_names or ()),
        gstin=held(profile.gstin),
        cin=held(profile.cin),
        pan=None,
        director_names=directors,
        state_code=profile.state_code,
    )


# --------------------------------------------------------------- what is kept


def _record_case(
    session: Session,
    *,
    company_id: UUID,
    record: CourtRecord,
    parties: list[dict],
    now: datetime,
) -> CourtCase:
    """The current position of one case, plus this fetch's payload beside it.

    The case row is refreshed rather than duplicated — a hearing date that moved
    is not a second case. The payload that produced this state is written to
    `provider_fetches` on every run, so the earlier answer survives the refresh.
    """
    case = (
        session.execute(
            select(CourtCase).where(
                CourtCase.court_id == record.court_id,
                CourtCase.case_number == record.case_number,
            )
        )
        .scalars()
        .first()
    )
    if case is None:
        case = CourtCase(
            company_id=company_id,
            court_id=record.court_id,
            case_number=record.case_number,
        )
        session.add(case)

    case.court_name = record.court_name
    case.case_type = record.case_type
    case.filing_date = record.filing_date
    case.status = record.status
    case.stage = record.stage
    case.next_hearing = record.next_hearing
    case.parties = _jsonable(parties)
    case.raw = _jsonable(asdict(record))
    session.flush()

    session.add(
        ProviderFetch(
            company_id=company_id,
            provider=_PROVIDER,
            resource="case",
            external_id=f"{record.court_id}/{record.case_number}"[:128],
            raw=_jsonable(asdict(record)),
            parsed={
                "provenance": record.provenance,
                "status": record.status,
                "stage": record.stage,
                "case_type": record.case_type,
            },
            fetched_at=now,
        )
    )

    # Parties accumulate and are never removed. A name dropped from a later
    # cause list is still the name the earlier one carried, and both are how a
    # reviewer judges whether this is the same business.
    known = {
        (p.normalised_name, p.role)
        for p in session.execute(
            select(CaseParty).where(CaseParty.case_id == case.id)
        ).scalars()
    }
    for party in parties:
        normalised = normalise_company_name(party["name"])
        if (normalised, party.get("role")) in known:
            continue
        session.add(
            CaseParty(
                company_id=company_id,
                case_id=case.id,
                raw_name=party["name"][:400],
                normalised_name=normalised[:400],
                role=party.get("role"),
            )
        )
    return case


def _write_link(
    session: Session,
    *,
    company_id: UUID,
    case: CourtCase,
    profile: CompanyProfile,
    grade: Grade,
    confidence: float,
    matching_signals: dict,
    provenance: str | None,
) -> LegalLink:
    """Propose a link, or leave a reviewed one exactly as the reviewer left it.

    A re-run must never move a link a person has already decided. Re-proposing a
    rejected case is how litigation somebody explicitly disowned walks back onto
    their file three months later.
    """
    link = (
        session.execute(
            select(LegalLink).where(
                LegalLink.case_id == case.id, LegalLink.profile_id == profile.id
            )
        )
        .scalars()
        .first()
    )
    if link is not None and link.status != PROPOSED:
        return link

    if link is None:
        link = LegalLink(
            company_id=company_id,
            case_id=case.id,
            profile_id=profile.id,
            status=PROPOSED,
        )
        session.add(link)

    link.confidence = confidence
    link.signals = _jsonable(
        {
            "matching": matching_signals,
            "grade": grade.tier.value,
            "grade_reasons": list(grade.reasons),
            "matched_identifier": grade.matched_identifier,
            "matched_party": grade.matched_party,
            "case_provenance": provenance,
            "court_id": case.court_id,
            "case_number": case.case_number,
        }
    )
    session.flush()
    return link


def issue_history(
    session: Session, *, company_id: UUID, profile_id: UUID, now: datetime
) -> LegalHistory:
    """A point-in-time report. Never edited, so a re-run writes a new one."""
    rows = session.execute(
        select(LegalLink, CourtCase)
        .join(CourtCase, CourtCase.id == LegalLink.case_id)
        .where(LegalLink.profile_id == profile_id)
    ).all()

    def summary(link: LegalLink, case: CourtCase) -> dict:
        return {
            "case_id": str(case.id),
            "court_id": case.court_id,
            "court_name": case.court_name,
            "case_number": case.case_number,
            "case_type": case.case_type,
            "filing_date": case.filing_date.isoformat() if case.filing_date else None,
            "status": case.status,
            "stage": case.stage,
            "grade": (link.signals or {}).get("grade"),
            "link_status": link.status,
        }

    confirmed = [summary(link, case) for link, case in rows if link.status == CONFIRMED]
    awaiting = [summary(link, case) for link, case in rows if link.status == PROPOSED]
    rejected = [summary(link, case) for link, case in rows if link.status == REJECTED]

    history = LegalHistory(
        company_id=company_id,
        profile_id=profile_id,
        issued_at=now,
        payload={
            "issued_at": now.isoformat(),
            "confirmed": confirmed,
            "awaiting_review": awaiting,
            "rejected": rejected,
            "signals": asdict(_signals_from_rows(rows)),
        },
        confirmed_case_count=len(confirmed),
        caveat=CAVEAT,
    )
    session.add(history)
    session.flush()
    return history


# ------------------------------------------------------------------ the search


def search_and_link(
    session: Session,
    buyer_id: UUID,
    *,
    company_id: UUID,
    backend: CourtBackend | None,
    now: datetime,
    filed_after: date | None = None,
    actor_label: str | None = None,
) -> SearchOutcome:
    """Search the courts for a buyer and propose links for a person to review.

    Flushes but never commits: the caller owns the transaction.

    Raises `BuyerNotFound` when the buyer is not this tenant's. Everything else
    that stops the run comes back as a named `Refusal` on the outcome, because
    "we searched and could confirm nothing" is a result a caller has to be able
    to show, not an error to swallow.
    """
    buyer = session.execute(select(Buyer).where(Buyer.id == buyer_id)).scalar_one_or_none()
    if buyer is None:
        raise BuyerNotFound(f"no buyer {buyer_id} in company {company_id}")

    profile = _latest_profile(session, buyer_id)
    if profile is None:
        # Nothing to hang a link on, and a court case recorded against no
        # company is a stranger's litigation sitting in a customer's database.
        return SearchOutcome(
            profile_id=None,
            searched_name=None,
            cases_seen=0,
            refusals=(Refusal.NO_PROFILE,),
            notes=(
                f"{buyer.name} has no company profile, so there is nothing a case "
                f"could be attached to. Verify the buyer first.",
            ),
        )

    refusals: list[Refusal] = []
    notes: list[str] = []

    identifiers = verified_identifiers(profile)
    if not identifiers:
        refusals.append(Refusal.PROFILE_NOT_IDENTIFIER_RESOLVED)
        notes.append(
            f"{profile.legal_name} is not resolved to a registry identifier, so no case "
            f"found here can be confirmed against them. Anything matched is a name."
        )

    if backend is None:
        refusals.append(Refusal.NO_BACKEND)
        notes.append("No court backend is configured, so nothing was searched.")
        return SearchOutcome(
            profile_id=profile.id,
            searched_name=None,
            cases_seen=0,
            refusals=tuple(refusals),
            notes=tuple(notes),
        )

    searched_name = profile.legal_name or buyer.name
    records = backend.search(CaseQuery(party_name=searched_name, filed_after=filed_after))

    directors = _director_names(session, profile.id)
    profile_ref = _profile_ref(profile, identifiers, directors)
    outcomes: list[LinkOutcome] = []

    for record in records:
        parties = _party_dicts(record)
        case = _record_case(
            session, company_id=company_id, record=record, parties=parties, now=now
        )
        proposals = link_candidates(
            profile_ref,
            [
                CaseRef(
                    case_id=str(case.id),
                    court_id=record.court_id,
                    case_number=record.case_number,
                    parties=tuple(PartyRef(p["name"], p.get("role")) for p in parties),
                    text=record.text,
                    case_type=record.case_type,
                )
            ],
        )
        proposal = proposals[0] if proposals else None
        grade = grade_match(
            identifiers=identifiers,
            parties=parties,
            provenance=record.provenance,
            signals=proposal.signals if proposal else {},
        )

        # An identifier-grade case links even when the names share nothing —
        # that is the case the whole module is for. Below it, `link_candidates`
        # decides what is worth a reviewer's attention.
        if proposal is None and not grade.confirmable:
            continue

        confidence = max(
            proposal.confidence if proposal else 0.0, 1.0 if grade.confirmable else 0.0
        )
        link = _write_link(
            session,
            company_id=company_id,
            case=case,
            profile=profile,
            grade=grade,
            confidence=confidence,
            matching_signals=proposal.signals if proposal else {},
            provenance=record.provenance,
        )
        outcomes.append(
            LinkOutcome(
                case_id=case.id,
                court_id=case.court_id,
                case_number=case.case_number,
                case_type=case.case_type,
                link_id=link.id,
                status=link.status,
                tier=grade.tier.value,
                confidence=float(link.confidence),
                reasons=grade.reasons,
            )
        )

    history = issue_history(
        session, company_id=company_id, profile_id=profile.id, now=now
    )

    audit.record(
        session,
        action=ACTION_SEARCHED,
        company_id=company_id,
        actor_label=actor_label,
        entity_type="buyer",
        entity_id=buyer.id,
        after={
            "profile_id": str(profile.id),
            "searched_name": searched_name,
            "cases_seen": len(records),
            "links": len(outcomes),
            "confirmable": sum(1 for o in outcomes if o.tier == Tier.IDENTIFIER.value),
            "refusals": [r.value for r in refusals],
        },
        detail=f"{searched_name}: {len(records)} case(s) seen, {len(outcomes)} link(s) proposed",
    )

    return SearchOutcome(
        profile_id=profile.id,
        searched_name=searched_name,
        cases_seen=len(records),
        links=tuple(outcomes),
        refusals=tuple(refusals),
        notes=tuple(notes),
        history_id=history.id,
    )


# ------------------------------------------------------------------- reviewing


def confirm_link(
    session: Session,
    link_id: UUID,
    *,
    company_id: UUID,
    reviewed_by: UUID | None,
    now: datetime,
    actor_label: str | None = None,
) -> LegalLink:
    """Record that a person has accepted this case as this company's.

    The grade is **recomputed** from the stored case and the profile's current
    identifiers rather than read back from the link's signals. A stored grade is
    a claim from an earlier run; if the profile has since lost its identifier
    resolution, or the case was refreshed with different parties, the claim is
    stale and confirming on it would attach a case nobody can still stand
    behind.
    """
    link = session.execute(
        select(LegalLink).where(LegalLink.id == link_id)
    ).scalar_one_or_none()
    if link is None:
        raise LinkRefused(Refusal.LINK_NOT_FOUND, f"no legal link {link_id}")
    if link.status == CONFIRMED:
        return link
    if link.status == REJECTED:
        raise LinkRefused(
            Refusal.ALREADY_REJECTED,
            "this link was rejected; a rejected case is reopened by a new review, not "
            "by confirming over the top of it",
        )

    case = session.execute(
        select(CourtCase).where(CourtCase.id == link.case_id)
    ).scalar_one_or_none()
    if case is None:
        raise LinkRefused(Refusal.CASE_NOT_FOUND, f"no court case {link.case_id}")

    provenance = (case.raw or {}).get("provenance")
    if provenance is not None and cap_tier_for_provenance(
        Tier.IDENTIFIER, provenance
    ) is not Tier.IDENTIFIER:
        raise LinkRefused(
            Refusal.RECORD_NOT_REGISTRY_BACKED,
            f"case {case.case_number} was supplied by an operator rather than fetched "
            f"from a court, so it cannot identify anybody",
        )

    profile = session.execute(
        select(CompanyProfile).where(CompanyProfile.id == link.profile_id)
    ).scalar_one_or_none()
    grade = grade_match(
        identifiers=verified_identifiers(profile),
        parties=list(case.parties or []),
        provenance=provenance,
        signals=(link.signals or {}).get("matching", {}),
    )
    if not grade.confirmable:
        raise LinkRefused(
            Refusal.MATCH_NOT_IDENTIFIER_GRADE,
            f"case {case.case_number} matches this company at {grade.tier.value}, not "
            f"{Tier.IDENTIFIER.value}. "
            + (grade.reasons[0] if grade.reasons else ""),
        )

    link.status = CONFIRMED
    link.reviewed_by = reviewed_by
    link.reviewed_at = now
    link.confidence = 1.0
    link.signals = _jsonable(
        {
            **(link.signals or {}),
            "grade": grade.tier.value,
            "grade_reasons": list(grade.reasons),
            "matched_identifier": grade.matched_identifier,
            "matched_party": grade.matched_party,
            "confirmed_at": now.isoformat(),
        }
    )
    session.flush()

    audit.record(
        session,
        action=ACTION_LINK_CONFIRMED,
        company_id=company_id,
        actor_id=reviewed_by,
        actor_label=actor_label,
        entity_type="legal_link",
        entity_id=link.id,
        after={
            "case_id": str(case.id),
            "case_number": case.case_number,
            "court_id": case.court_id,
            "profile_id": str(link.profile_id),
            "grade": grade.tier.value,
            "matched_identifier": grade.matched_identifier,
        },
        detail=f"{case.case_number} confirmed on {grade.matched_identifier}",
    )
    return link


def reject_link(
    session: Session,
    link_id: UUID,
    *,
    company_id: UUID,
    reviewed_by: UUID | None,
    reason: str,
    now: datetime,
    actor_label: str | None = None,
) -> LegalLink:
    """Record that this case is not this company's.

    Unconditional, unlike confirming. Detaching a case is always safe, and a
    rejection that needed to clear a gate is a rejection somebody gives up on.
    """
    link = session.execute(
        select(LegalLink).where(LegalLink.id == link_id)
    ).scalar_one_or_none()
    if link is None:
        raise LinkRefused(Refusal.LINK_NOT_FOUND, f"no legal link {link_id}")

    link.status = REJECTED
    link.reviewed_by = reviewed_by
    link.reviewed_at = now
    link.reject_reason = reason
    session.flush()

    audit.record(
        session,
        action=ACTION_LINK_REJECTED,
        company_id=company_id,
        actor_id=reviewed_by,
        actor_label=actor_label,
        entity_type="legal_link",
        entity_id=link.id,
        after={"case_id": str(link.case_id), "reason": reason},
        detail=reason,
    )
    return link


# -------------------------------------------------------- what scoring may see


def _signals_from_rows(rows) -> LegalSignals:
    confirmed = [case for link, case in rows if link.status == CONFIRMED]
    return LegalSignals(
        confirmed_legal_cases=len(confirmed),
        recovery_suits=sum(1 for c in confirmed if is_recovery_case_type(c.case_type)),
        insolvency_cases=sum(1 for c in confirmed if is_insolvency_case_type(c.case_type)),
        awaiting_review=sum(1 for link, _ in rows if link.status == PROPOSED),
    )


def legal_signals(session: Session, profile_id: UUID) -> LegalSignals:
    """Confirmed cases only, counted for the risk score and the assessment.

    The status filter is the whole function. A PROPOSED link is a question
    somebody has not answered, and a question that moves a score is a score
    nobody can defend.
    """
    rows = session.execute(
        select(LegalLink, CourtCase)
        .join(CourtCase, CourtCase.id == LegalLink.case_id)
        .where(LegalLink.profile_id == profile_id)
    ).all()
    return _signals_from_rows(rows)
