"""Deciding whether a court case belongs to a company.

Indian court records identify parties by name strings typed by court clerks: no
GSTIN, no CIN, no stable identifier. "Sharma Traders" appears in hundreds of
cases belonging to dozens of unrelated businesses.

So this is 20% ingestion and 80% deciding, defensibly, whose case it is.
Attaching a recovery or criminal matter to the wrong business is defamation, and
the most likely way this platform gets sued.

**Design bias: prefer a missed match to a false one.** An incomplete legal
history is a product limitation; a wrong one is a lawsuit. Everything here
produces a PROPOSED link for a human to confirm — nothing auto-confirms, and
only confirmed links reach a report or move a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.company.resolution import normalise_company_name, token_set_similarity

# Above this a link is worth putting in front of a reviewer. Below it, silence
# beats a queue of noise nobody reads.
PROPOSE_THRESHOLD = 0.55

# Nothing is ever auto-confirmed. The constant exists to say so out loud, so
# that adding one later is a visible decision rather than a quiet default.
AUTO_CONFIRM_THRESHOLD = None


@dataclass(frozen=True)
class PartyRef:
    raw_name: str
    role: str | None = None


@dataclass(frozen=True)
class CaseRef:
    case_id: str
    court_id: str
    case_number: str
    parties: tuple[PartyRef, ...] = ()
    text: str = ""
    case_type: str | None = None


@dataclass(frozen=True)
class ProfileRef:
    profile_id: str
    legal_name: str
    trade_names: tuple[str, ...] = ()
    gstin: str | None = None
    cin: str | None = None
    pan: str | None = None
    director_names: tuple[str, ...] = ()
    state_code: str | None = None


@dataclass(frozen=True)
class LinkProposal:
    case_id: str
    profile_id: str
    confidence: float
    signals: dict = field(default_factory=dict)
    status: str = "PROPOSED"
    matched_party: str | None = None


def _identifier_in_text(profile: ProfileRef, text: str) -> str | None:
    """Rare in Indian filings, but conclusive when present."""
    haystack = (text or "").upper()
    for value in (profile.gstin, profile.cin, profile.pan):
        if value and value.upper() in haystack:
            return value.upper()
    return None


def link_candidates(profile: ProfileRef, cases: list[CaseRef]) -> list[LinkProposal]:
    proposals: list[LinkProposal] = []
    names = (profile.legal_name,) + tuple(profile.trade_names)

    for case in cases:
        signals: dict = {}
        confidence = 0.0
        matched_party = None

        identifier = _identifier_in_text(profile, case.text)
        if identifier:
            signals["identifier_in_text"] = identifier
            confidence = 0.95

        best_name_score = 0.0
        for party in case.parties:
            for name in names:
                score = token_set_similarity(name, party.raw_name)
                if score > best_name_score:
                    best_name_score = score
                    matched_party = party.raw_name
        if best_name_score:
            signals["name_similarity"] = round(best_name_score, 3)
            # A name, however well it matches, caps well below certainty. This
            # is the ceiling that makes a missed match more likely than a false
            # one, which is the trade this module deliberately takes.
            confidence = max(confidence, min(0.6, best_name_score * 0.7))

        # A director appearing as a named party is strong evidence, especially
        # for proprietorships where the firm and the person are one litigant.
        for party in case.parties:
            for director in profile.director_names:
                if token_set_similarity(director, party.raw_name) >= 0.9:
                    signals["director_is_party"] = director
                    confidence = max(confidence, 0.8)
                    matched_party = party.raw_name

        # An exact normalised match on a *distinctive* name is worth more than
        # on a generic two-word one — "Sharma Traders" being exact means very
        # little, because so many of them exist.
        for party in case.parties:
            for name in names:
                normalised = normalise_company_name(name)
                if normalised and normalised == normalise_company_name(party.raw_name):
                    distinctive = len(normalised.split()) >= 3
                    signals["exact_normalised_name"] = normalised
                    signals["distinctive"] = distinctive
                    confidence = max(confidence, 0.7 if distinctive else 0.55)

        if confidence >= PROPOSE_THRESHOLD:
            proposals.append(
                LinkProposal(
                    case_id=case.case_id,
                    profile_id=profile.profile_id,
                    confidence=round(confidence, 3),
                    signals=signals,
                    matched_party=matched_party,
                )
            )

    return sorted(proposals, key=lambda p: p.confidence, reverse=True)


def confirmed_only(links) -> list:
    """Only human-confirmed links may appear in a report or move a score."""
    return [link for link in links if getattr(link, "status", None) == "CONFIRMED"]
