"""Court records, and the seam a real eCourts client will sit behind.

Unlike the other backend protocols this one lives beside its implementations
rather than in `app.providers.base`; nothing depends on that, and it can move
once that file settles.

The shape of the data is the whole hazard. An Indian court record identifies a
party by a name a clerk typed and, if you are lucky, an address. There is no
GSTIN, no CIN and no stable party id — and no public court search accepts one
either, which is why `CaseQuery` carries no identifier field. Putting one there
would let a caller believe the search had already established whose case this
is. It has not. A party-name search returns every Sharma Traders in the state
and hands the hard question, unanswered, to `app.legal.cases`.

`CourtParty.identifier` is the exception, and the only field here that can
establish identity rather than resemblance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from app.company.resolution import normalise_company_name
from app.providers.base import PROVENANCES, REGISTRY, USER_PROVIDED, UnknownProvenance


@dataclass(frozen=True)
class CourtParty:
    """One party as the filing named it."""

    name: str
    role: str | None = None  # petitioner | respondent | accused | corporate debtor
    # An identifier the filing itself recorded against *this* party — a GSTIN
    # pleaded in a commercial cause title, a CIN on an insolvency petition.
    # Usually absent. When present it is the only evidence in a court record
    # that says which company a party is rather than what it is called, and
    # `app.legal.cases` will confirm a link on nothing else.
    identifier: str | None = None


@dataclass(frozen=True)
class CourtRecord:
    """One case as some source describes it."""

    court_id: str
    court_name: str | None
    case_number: str
    case_type: str | None
    filing_date: date | None
    status: str | None
    stage: str | None
    next_hearing: date | None
    parties: tuple[CourtParty, ...]
    # Cause title and order text as published. An identifier found in here is
    # weaker than one on a party: it says the number appears somewhere in the
    # papers, not which side of the case it is on.
    text: str
    provenance: str  # REGISTRY | USER_PROVIDED

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise UnknownProvenance(
                f"CourtRecord.provenance must be one of {PROVENANCES}; got "
                f"{self.provenance!r}. A source that cannot say where its data came "
                f"from does not get to claim it came from a court."
            )


@dataclass(frozen=True)
class CaseQuery:
    party_name: str
    filed_after: date | None = None


@runtime_checkable
class CourtBackend(Protocol):
    name: str

    def search(self, query: CaseQuery) -> list[CourtRecord]: ...


# --------------------------------------------------------------------- fixtures

# Shaped against the GST and MCA fixtures in `app.company`, so the same three
# companies can be asked about across all three registers, and so that every
# grade `app.legal.cases` can award has a case that earns it:
#
#   COMM.SUIT/412/2024   a GSTIN pleaded against the respondent — identifier-grade
#   CC/1187/2025         a CIN on the accused — identifier-grade, criminal
#   CP(IB)/233/2025      a CIN on the corporate debtor — identifier-grade, insolvency
#   OA/119/2023          the same GSTIN, in the recital only, party named by name
#   OS/778/2025          a *different* Sharma Traders, by name alone
#
# The last two are the ones worth keeping. A recital identifier and a matching
# name both look conclusive on a screen, and neither is.
COURT_FIXTURES: tuple[dict, ...] = (
    {
        "court_id": "MHCC01",
        "court_name": "Bombay City Civil Court",
        "case_number": "COMM.SUIT/412/2024",
        "case_type": "COMMERCIAL SUIT",
        "filing_date": date(2024, 8, 19),
        "status": "Pending",
        "stage": "Written Statement",
        "next_hearing": date(2026, 10, 14),
        "parties": (
            {"name": "Hindustan Polymers Private Limited", "role": "petitioner"},
            {
                "name": "Sharma Traders Private Limited",
                "role": "respondent",
                "identifier": "27AABCS1429B1ZU",
            },
        ),
        "text": (
            "Suit for recovery of Rs. 41,20,000 towards goods sold and delivered. "
            "The Defendant, Sharma Traders Private Limited, GSTIN 27AABCS1429B1ZU, "
            "carries on business at 14 Kalbadevi Road, Marine Lines, Mumbai."
        ),
    },
    {
        "court_id": "KAJM07",
        "court_name": "Court of the IV Addl. Chief Metropolitan Magistrate, Bengaluru",
        "case_number": "CC/1187/2025",
        "case_type": "SECTION 138 NI ACT",
        "filing_date": date(2025, 6, 24),
        "status": "Pending",
        "stage": "Summons",
        "next_hearing": date(2026, 9, 30),
        "parties": (
            {"name": "Deccan Metals LLP", "role": "complainant"},
            {
                "name": "Verma Steel Works Private Limited",
                "role": "accused",
                "identifier": "U27109KA2009PTC050123",
            },
        ),
        "text": (
            "Complaint under Section 138 of the Negotiable Instruments Act, 1881 in "
            "respect of cheque no. 004521 for Rs. 8,75,000 returned unpaid for "
            "insufficiency of funds. Accused No. 1 is a company incorporated under "
            "CIN U27109KA2009PTC050123."
        ),
    },
    {
        "court_id": "NCLTCHN",
        "court_name": "National Company Law Tribunal, Chennai Bench",
        "case_number": "CP(IB)/233/2025",
        "case_type": "IBC SECTION 9",
        "filing_date": date(2025, 9, 2),
        "status": "Admitted",
        "stage": "Moratorium in force",
        "next_hearing": date(2026, 11, 6),
        "parties": (
            {"name": "Coromandel Cables Private Limited", "role": "operational creditor"},
            {
                "name": "Nair Electricals Private Limited",
                "role": "corporate debtor",
                "identifier": "U31200TN2007PTC064512",
            },
        ),
        "text": (
            "Application under Section 9 of the Insolvency and Bankruptcy Code, 2016 "
            "admitted. Moratorium under Section 14 declared against the Corporate "
            "Debtor, CIN U31200TN2007PTC064512."
        ),
    },
    {
        "court_id": "DRTMUM2",
        "court_name": "Debts Recovery Tribunal II, Mumbai",
        "case_number": "OA/119/2023",
        "case_type": "ORIGINAL APPLICATION",
        "filing_date": date(2023, 11, 3),
        "status": "Pending",
        "stage": "Final Hearing",
        "next_hearing": date(2026, 10, 2),
        # The borrower is a company; the guarantor named here is a natural
        # person who happens to be its director. The GSTIN sits in the recital
        # and is attributed to nobody.
        "parties": (
            {"name": "State Bank of India", "role": "applicant"},
            {"name": "Rajesh Sharma", "role": "respondent"},
        ),
        "text": (
            "Application for recovery of Rs. 2,50,00,000 against the borrower and the "
            "personal guarantor. The credit facility was sanctioned against stock "
            "declared under GSTIN 27AABCS1429B1ZU."
        ),
    },
    {
        "court_id": "TNCC03",
        "court_name": "City Civil Court, Chennai",
        "case_number": "OS/778/2025",
        "case_type": "MONEY SUIT",
        "filing_date": date(2025, 2, 11),
        "status": "Pending",
        "stage": "Evidence",
        "next_hearing": date(2026, 9, 22),
        # A different business entirely: a Chennai proprietorship trading under
        # a name thousands of unrelated firms use.
        "parties": (
            {"name": "Kavery Agencies", "role": "petitioner"},
            {"name": "Sharma Traders", "role": "respondent"},
        ),
        "text": (
            "Suit for recovery of Rs. 3,10,000. The Defendant, Sharma Traders, a "
            "proprietary concern of Thiru K. Sharma, carries on business at 22 "
            "Govindappa Naicken Street, Chennai."
        ),
    },
)


def _record_from_row(row: dict, provenance: str) -> CourtRecord:
    return CourtRecord(
        court_id=row["court_id"],
        court_name=row.get("court_name"),
        case_number=row["case_number"],
        case_type=row.get("case_type"),
        filing_date=row.get("filing_date"),
        status=row.get("status"),
        stage=row.get("stage"),
        next_hearing=row.get("next_hearing"),
        parties=tuple(
            CourtParty(
                name=p["name"], role=p.get("role"), identifier=p.get("identifier")
            )
            for p in row.get("parties") or ()
        ),
        text=row.get("text") or "",
        provenance=provenance,
    )


def _search(rows: tuple[dict, ...], query: CaseQuery, provenance: str) -> list[CourtRecord]:
    """Every case sharing one normalised word with the party searched for.

    A deliberately terrible filter, and roughly what a party-name search on a
    court portal actually gives you. A backend that returned only the good
    matches would be doing the matching — which is the decision this module must
    never make quietly on somebody's behalf.

    It normalises with the platform's own `normalise_company_name`, so what
    comes back here says nothing about how any real portal behaves; it exists to
    hand `app.legal.cases` a realistic pile of near-misses to refuse.
    """
    wanted = set(normalise_company_name(query.party_name).split())
    if not wanted:
        return []

    found: list[CourtRecord] = []
    for row in rows:
        record = _record_from_row(row, provenance)
        if (
            query.filed_after
            and record.filing_date
            and record.filing_date < query.filed_after
        ):
            continue
        if any(
            wanted & set(normalise_company_name(party.name).split())
            for party in record.parties
        ):
            found.append(record)
    return found


class LocalCourtBackend:
    """The cause lists, standing in for themselves. REGISTRY provenance.

    What it knows is a fixture; what it claims about where that knowledge came
    from is what a real eCourts client will claim from the same seam.
    """

    name = "local"

    def __init__(self, rows: tuple[dict, ...] | None = None):
        self.rows = COURT_FIXTURES if rows is None else tuple(rows)

    def search(self, query: CaseQuery) -> list[CourtRecord]:
        return _search(self.rows, query, REGISTRY)


class ManualCourtBackend:
    """What a person read off a court portal and typed in.

    As in `ManualGstBackend`, provenance is fixed on the class rather than
    passed to `__init__`: an operator supplies the cases, never the claim about
    where they came from. Court portals are the place this matters most — they
    are captcha-gated and scrape-hostile, so hand transcription is the realistic
    way records arrive, and a transcribed case number attached to a company by a
    transcribed identifier is two typing errors away from a stranger's
    litigation on somebody's file.
    """

    name = "manual"

    def __init__(self, rows: tuple[dict, ...] | None = None):
        self.rows = tuple(rows or ())

    def search(self, query: CaseQuery) -> list[CourtRecord]:
        return _search(self.rows, query, USER_PROVIDED)
