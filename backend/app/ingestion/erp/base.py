"""The ERP connector interface.

Deliberately shaped against two real integrations rather than one. Tally is XML
over HTTP to a locally-running instance; Zoho Books is REST with OAuth. An
interface designed against only the first would have baked in assumptions that
the second breaks — chiefly that the accounting system is reachable from a
server at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConnectionStatus:
    ok: bool
    detail: str = ""
    provider_version: str | None = None


@dataclass(frozen=True)
class RawParty:
    external_id: str
    name: str
    phones: tuple[str, ...] = ()
    email: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RawInvoice:
    external_id: str
    invoice_number: str
    party_external_id: str
    issue_date: date
    due_date: date
    amount_paise: int
    voided: bool = False
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RawPayment:
    external_id: str
    party_external_id: str
    amount_paise: int
    received_date: date
    reference: str | None = None
    against_invoice_number: str | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class ErpConnector(Protocol):
    provider: str

    def test_connection(self) -> ConnectionStatus: ...

    def fetch_parties(self, since: datetime | None) -> Iterator[RawParty]: ...

    def fetch_invoices(self, since: datetime | None) -> Iterator[RawInvoice]: ...

    def fetch_payments(self, since: datetime | None) -> Iterator[RawPayment]: ...
