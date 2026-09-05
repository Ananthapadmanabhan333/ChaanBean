"""Finding a current business contact, from sources the business published itself.

**Scope, stated as a boundary.**

In scope: contact details a business published or filed about itself.
Out of scope: anything about a person in their private capacity, anything
inferred from behaviour or location, anything bought from a data broker.

This is *business* contact discovery. A proprietor's mobile filed with the MCA
as a company contact is in scope because the business filed it; the same number
obtained anywhere else is not.

Under the DPDP Act 2023 this is personal-data processing, and it needs a lawful
basis and a stated purpose. Every source declares its basis and every lookup is
recorded — which is the difference between a recovery tool and a lookup service,
and the first thing anyone reviewing a complaint asks to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TraceHit:
    contact_type: str  # phone | email | address
    value: str
    source: str
    source_reference: str | None
    confidence: float
    lawful_basis: str


@runtime_checkable
class TraceSource(Protocol):
    name: str
    lawful_basis: str

    def lookup(self, profile) -> list[TraceHit]: ...


class McaFilingSource:
    """Registered office and director contacts, as filed with the MCA."""

    name = "mca_filing"
    lawful_basis = "public statutory filing made by the business itself"

    def __init__(self, records: dict):
        self.records = records

    def lookup(self, profile) -> list[TraceHit]:
        record = self.records.get(getattr(profile, "cin", None) or "")
        if not record:
            return []
        hits = []
        if record.get("registered_address"):
            hits.append(
                TraceHit("address", record["registered_address"], self.name,
                         record.get("cin"), 0.8, self.lawful_basis)
            )
        for director in record.get("directors", []):
            if director.get("phone"):
                hits.append(
                    TraceHit("phone", director["phone"], self.name,
                             f"director:{director.get('name')}", 0.6, self.lawful_basis)
                )
        return hits


class GstRegistrationSource:
    name = "gst_registration"
    lawful_basis = "public statutory registration made by the business itself"

    def __init__(self, records: dict):
        self.records = records

    def lookup(self, profile) -> list[TraceHit]:
        record = self.records.get(getattr(profile, "gstin", None) or "")
        if not record:
            return []
        hits = []
        if record.get("address"):
            hits.append(
                TraceHit("address", record["address"], self.name,
                         record.get("gstin"), 0.85, self.lawful_basis)
            )
        if record.get("contact"):
            hits.append(
                TraceHit("phone", record["contact"], self.name,
                         record.get("gstin"), 0.7, self.lawful_basis)
            )
        return hits


class OwnLedgerSource:
    """Contacts already in your own records, from historical correspondence."""

    name = "own_ledger"
    lawful_basis = "existing contractual relationship with the data principal"

    def __init__(self, contacts: list[dict]):
        self.contacts = contacts

    def lookup(self, profile) -> list[TraceHit]:
        return [
            TraceHit(c["type"], c["value"], self.name, c.get("reference"),
                     0.9, self.lawful_basis)
            for c in self.contacts
        ]


class PublishedWebsiteSource:
    """Contact details a business publishes on its own website."""

    name = "own_website"
    lawful_basis = "self-published by the business"

    def __init__(self, published: dict):
        self.published = published

    def lookup(self, profile) -> list[TraceHit]:
        record = self.published.get(getattr(profile, "profile_id", None) or "")
        if not record:
            return []
        return [
            TraceHit(kind, value, self.name, record.get("url"), 0.65, self.lawful_basis)
            for kind, value in record.items()
            if kind in ("phone", "email", "address")
        ]


# Deliberately absent, and named here so that nobody adds one by accident and
# so that a reviewer can see the boundary was drawn on purpose.
FORBIDDEN_SOURCES = (
    "data_broker",
    "social_profiling",
    "location_inference",
    "telecom_records",
    "credit_bureau_personal",
    "neighbour_enquiry",
)


class ForbiddenSourceError(RuntimeError):
    """Refused: several of these are unlawful, most are unreliable, and all of
    them change what this product is."""


def run_trace(sources: list, profile) -> tuple[list[TraceHit], list[dict]]:
    """Query every permitted source, recording what each was asked and returned."""
    hits: list[TraceHit] = []
    audit: list[dict] = []

    for source in sources:
        if source.name in FORBIDDEN_SOURCES:
            raise ForbiddenSourceError(
                f"{source.name!r} is not a lawful source for this product"
            )
        found = source.lookup(profile)
        hits.extend(found)
        audit.append(
            {
                "source": source.name,
                "lawful_basis": source.lawful_basis,
                "result_count": len(found),
            }
        )

    # Highest confidence first, de-duplicated by value.
    seen: set[str] = set()
    unique: list[TraceHit] = []
    for hit in sorted(hits, key=lambda h: h.confidence, reverse=True):
        if hit.value in seen:
            continue
        seen.add(hit.value)
        unique.append(hit)
    return unique, audit
