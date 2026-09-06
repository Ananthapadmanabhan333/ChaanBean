"""MCA lookups: incorporation, status, directors, registered charges.

The same two backends as `app.company.gst`, for the same reason, with one extra
hazard worth stating: a CIN carries no check digit. A mistyped GSTIN is usually
caught before it reaches a registry; a mistyped CIN can only be caught by failing
to find it. So a CIN that *does* find a record proves less than a GSTIN that
does, and `ManualMcaBackend` — where the typing happens — is the backend where
that matters most.

The struck-off fixture is the case this module exists for. A company can be
struck off the register while its GST registration is still live, and a system
that checks one register and reports "verified" has told the user something
false in the most consequential direction.
"""

from __future__ import annotations

from datetime import date

from app.company.identifiers import validate_cin
from app.providers.base import REGISTRY, USER_PROVIDED, McaLookup

# Matched to app/company/gst.py by legal name, so the two registers can be asked
# about the same three companies and can be made to disagree.
MCA_FIXTURES: dict[str, dict] = {
    "U51909MH2011PTC219876": {
        "legal_name": "SHARMA TRADERS PRIVATE LIMITED",
        "status": "Active",
        "incorporation_date": date(2011, 6, 14),
        "registered_address": "14 Kalbadevi Road, Marine Lines, Mumbai, Maharashtra 400002",
        "directors": [
            {"name": "RAJESH SHARMA", "din": "01234567", "designation": "Director", "appointed_on": "2011-06-14"},
            {"name": "MEENA SHARMA", "din": "01234568", "designation": "Director", "appointed_on": "2014-03-02"},
        ],
        # Paise, like every other amount in this system.
        "charges": [
            {
                "charge_id": "100234567",
                "holder": "State Bank of India",
                "amount_paise": 2_50_00_000_00,
                "created_on": "2019-08-21",
                "status": "Open",
            },
        ],
    },
    "U27109KA2009PTC050123": {
        "legal_name": "VERMA STEEL WORKS PRIVATE LIMITED",
        "status": "Active",
        "incorporation_date": date(2009, 2, 9),
        "registered_address": "48 Peenya Industrial Area Phase II, Bengaluru, Karnataka 560058",
        "directors": [
            {"name": "ANIL VERMA", "din": "02345678", "designation": "Managing Director", "appointed_on": "2009-02-09"},
        ],
        "charges": [
            {
                "charge_id": "100345678",
                "holder": "Canara Bank",
                "amount_paise": 1_20_00_000_00,
                "created_on": "2021-11-04",
                "status": "Open",
            },
        ],
    },
    # Struck off, while 33AADCN7890F1ZB says the GST registration is Active.
    "U31200TN2007PTC064512": {
        "legal_name": "NAIR ELECTRICALS PRIVATE LIMITED",
        "status": "Struck Off",
        "incorporation_date": date(2007, 5, 30),
        "registered_address": "7 Anna Salai, Guindy, Chennai, Tamil Nadu 600032",
        "directors": [
            {"name": "SURESH NAIR", "din": "03456789", "designation": "Director", "appointed_on": "2007-05-30"},
        ],
        "charges": [],
    },
}


def _as_date(value) -> date | None:
    """See app.company.gst._as_date — an operator types strings, fixtures hold dates."""
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _lookup_from_row(cin: str, row: dict, provenance: str) -> McaLookup:
    return McaLookup(
        cin=cin,
        legal_name=row.get("legal_name"),
        status=row.get("status"),
        incorporation_date=_as_date(row.get("incorporation_date")),
        registered_address=row.get("registered_address"),
        directors=list(row.get("directors") or []),
        charges=list(row.get("charges") or []),
        provenance=provenance,
    )


def _normalise_keys(source: dict[str, dict]) -> dict[str, dict]:
    return {key.strip().upper(): value for key, value in source.items()}


class LocalMcaBackend:
    """The register, standing in for itself. REGISTRY provenance."""

    name = "local"

    def __init__(self, fixtures: dict[str, dict] | None = None):
        self.fixtures = _normalise_keys(MCA_FIXTURES if fixtures is None else fixtures)

    def lookup(self, cin: str) -> McaLookup | None:
        check = validate_cin(cin)
        if not check.ok:
            return None
        row = self.fixtures.get(check.value)
        if row is None:
            return None
        return _lookup_from_row(check.value, row, REGISTRY)


class ManualMcaBackend:
    """What a person read off the MCA portal.

    As in `ManualGstBackend`, provenance is a class-level fact and not a
    constructor argument: an operator supplies the values, never the claim about
    where they came from.
    """

    name = "manual"

    def __init__(self, entries: dict[str, dict] | None = None):
        self.entries = _normalise_keys(entries or {})

    def lookup(self, cin: str) -> McaLookup | None:
        check = validate_cin(cin)
        if not check.ok:
            return None
        row = self.entries.get(check.value)
        if row is None:
            return None
        return _lookup_from_row(check.value, row, USER_PROVIDED)
