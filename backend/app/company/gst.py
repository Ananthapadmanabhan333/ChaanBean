"""GST lookups, and the difference between fetching one and being told one.

Two backends live here, and the gap between them is the whole point of the
module.

`LocalGstBackend` stands in for the GST registry. It answers from a fixture set
and reports REGISTRY provenance, because the seam it occupies holds a backend
that really calls the portal once there is an account to call it with. Swapping
them is a settings change, not a rewrite.

`ManualGstBackend` models what the reference implementation calls its public-data
mode: nobody calls anything. A human opens the government portal, reads the
screen and types what they saw. That is an honest way to work, and it stops being
honest the moment the result is stored without saying so — because some later
step will read a self-declared GSTIN as proof of identity, and the company on the
other end of that GSTIN will find out when the notice arrives.

So this backend reports USER_PROVIDED, and takes no argument that could change
that. An operator controls what it says; they do not control where it claims to
have come from. `app.company.verification` must never let USER_PROVIDED data
reach `Tier.IDENTIFIER`, and this field is the only thing that rule stands on.
"""

from __future__ import annotations

from datetime import date

from app.company.identifiers import validate_gstin
from app.providers.base import REGISTRY, USER_PROVIDED, GstLookup

# A small set of plausible Indian registrations, shaped so the interesting cases
# exist: an active company, the same PAN registered in a second state (ordinary,
# and treated by resolution.py as structural corroboration rather than a
# contradiction), a cancelled registration, and a company whose GST is live while
# the MCA has struck it off. Every check digit here is real; the tests recompute
# them, so a hand-edited fixture fails loudly rather than teaching the validator
# to accept nonsense.
GST_FIXTURES: dict[str, dict] = {
    "27AABCS1429B1ZU": {
        "legal_name": "SHARMA TRADERS PRIVATE LIMITED",
        "trade_name": "Sharma Traders",
        "status": "Active",
        "registration_date": date(2017, 7, 1),
        "address": "14 Kalbadevi Road, Marine Lines, Mumbai, Maharashtra 400002",
        "filing_history": [
            {"period": "2026-03", "return_type": "GSTR-3B", "filed_on": "2026-04-18", "status": "Filed"},
            {"period": "2026-02", "return_type": "GSTR-3B", "filed_on": "2026-03-16", "status": "Filed"},
            {"period": "2026-01", "return_type": "GSTR-3B", "filed_on": "2026-02-22", "status": "Filed Late"},
        ],
    },
    # Same PAN (AABCS1429B), second state. Two GSTINs, one legal person.
    "24AABCS1429B1Z0": {
        "legal_name": "SHARMA TRADERS PRIVATE LIMITED",
        "trade_name": "Sharma Traders Gujarat Branch",
        "status": "Active",
        "registration_date": date(2019, 4, 12),
        "address": "Plot 22, GIDC Estate, Vatva, Ahmedabad, Gujarat 382445",
        "filing_history": [
            {"period": "2026-03", "return_type": "GSTR-3B", "filed_on": "2026-04-20", "status": "Filed"},
        ],
    },
    "29AACCV3456D1ZB": {
        "legal_name": "VERMA STEEL WORKS PRIVATE LIMITED",
        "trade_name": "Verma Steel Works",
        "status": "Cancelled",
        "registration_date": date(2018, 1, 22),
        "address": "48 Peenya Industrial Area Phase II, Bengaluru, Karnataka 560058",
        "filing_history": [
            {"period": "2025-09", "return_type": "GSTR-3B", "filed_on": "2025-10-24", "status": "Filed Late"},
            {"period": "2025-10", "return_type": "GSTR-3B", "filed_on": None, "status": "Not Filed"},
        ],
    },
    # GST live, MCA struck off. The two registers disagree, which is exactly the
    # case a single-source check reports as verified.
    "33AADCN7890F1ZB": {
        "legal_name": "NAIR ELECTRICALS PRIVATE LIMITED",
        "trade_name": "Nair Electricals",
        "status": "Active",
        "registration_date": date(2016, 9, 5),
        "address": "7 Anna Salai, Guindy, Chennai, Tamil Nadu 600032",
        "filing_history": [
            {"period": "2026-03", "return_type": "GSTR-3B", "filed_on": "2026-04-19", "status": "Filed"},
        ],
    },
    "07AAFCK4521M1ZE": {
        "legal_name": "KHAN HARDWARE LLP",
        "trade_name": "Khan Hardware",
        "status": "Suspended",
        "registration_date": date(2020, 11, 30),
        "address": "112 Chawri Bazar Road, New Delhi, Delhi 110006",
        "filing_history": [],
    },
}


def _as_date(value) -> date | None:
    """Fixtures carry real dates; an operator types a string. Take both.

    Anything unparseable becomes None — we do not know the registration date —
    rather than an exception. A mistyped date is not a reason to refuse the rest
    of a lookup, and a guessed one would be worse than an absent one.
    """
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _lookup_from_row(gstin: str, row: dict, provenance: str) -> GstLookup:
    return GstLookup(
        gstin=gstin,
        legal_name=row.get("legal_name"),
        trade_name=row.get("trade_name"),
        status=row.get("status"),
        registration_date=_as_date(row.get("registration_date")),
        address=row.get("address"),
        # Taken from the GSTIN rather than the row. The first two characters are
        # the state, the GSTIN has already passed its checksum, and a row that
        # disagreed with its own key would be a typo rather than a second opinion.
        state_code=gstin[:2],
        filing_history=list(row.get("filing_history") or []),
        provenance=provenance,
    )


def _normalise_keys(source: dict[str, dict]) -> dict[str, dict]:
    return {key.strip().upper(): value for key, value in source.items()}


class LocalGstBackend:
    """The registry, standing in for itself.

    Reports REGISTRY provenance because this seam is where a real registry client
    goes. What it knows is a fixture; what it claims about where that knowledge
    came from is what production will claim.
    """

    name = "local"

    def __init__(self, fixtures: dict[str, dict] | None = None):
        self.fixtures = _normalise_keys(GST_FIXTURES if fixtures is None else fixtures)

    def lookup(self, gstin: str) -> GstLookup | None:
        check = validate_gstin(gstin)
        if not check.ok:
            # Not a miss — there was never anything to look up. A caller that
            # needs the reason asks `validate_gstin` itself, which is also where
            # it should have asked before storing the value.
            return None
        row = self.fixtures.get(check.value)
        if row is None:
            return None
        return _lookup_from_row(check.value, row, REGISTRY)


class ManualGstBackend:
    """What a person read off the government portal, and nothing more.

    `name` and the USER_PROVIDED provenance are fixed on the class rather than
    passed to `__init__` on purpose: a `provenance` argument here would let a
    deployment relabel typed-in data as fetched, which is the one failure this
    module is arranged around. Constructed empty it answers nothing, which is the
    truthful default — a manual backend knows only what somebody has actually
    looked up.
    """

    name = "manual"

    def __init__(self, entries: dict[str, dict] | None = None):
        self.entries = _normalise_keys(entries or {})

    def lookup(self, gstin: str) -> GstLookup | None:
        # The checksum matters more here than anywhere else in the system: this
        # is the path where fifteen characters pass through human fingers.
        check = validate_gstin(gstin)
        if not check.ok:
            return None
        row = self.entries.get(check.value)
        if row is None:
            return None
        return _lookup_from_row(check.value, row, USER_PROVIDED)
