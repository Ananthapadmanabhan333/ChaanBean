"""Tally connector.

Tally dominates Indian SMB accounting and it is the awkward one: XML over HTTP
to a Tally instance running on someone's desktop, not a cloud API. There is no
public endpoint to call, which is the fact that shapes deployment — you need
either an on-premise agent or a customer-initiated push, and discovering that at
integration time is expensive.

This connector speaks the XML. Where it points is a deployment question:

* `http://localhost:9000` when an agent runs on the customer's machine
* a tunnel or relay the customer opens
* `TallyFixtureTransport` in tests and demos, which replays captured responses

The parsing is the same in all three cases, which is the point of the split.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterator
import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.ingestion.erp.base import (
    ConnectionStatus,
    RawInvoice,
    RawParty,
    RawPayment,
)
from app.ingestion.normalise import NormalisationError, normalise_amount

# Tally speaks its own date format and its own request envelope.
TALLY_DATE = "%Y%m%d"


def _tally_request(report: str, since: datetime | None) -> str:
    from_date = since.strftime(TALLY_DATE) if since else "20000101"
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        f"<REPORTNAME>{report}</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVFROMDATE>{from_date}</SVFROMDATE>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"
    )


class TallyHttpTransport:
    """Talks to a real Tally instance."""

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, payload: str) -> str:
        response = httpx.post(
            self.base_url, content=payload.encode("utf-8"), timeout=self.timeout
        )
        response.raise_for_status()
        return response.text


class TallyFixtureTransport:
    """Replays captured Tally XML. Used by tests and the demo.

    Kept faithful to real responses — including Tally's habit of returning
    amounts as strings with a leading minus for credits — so the parser is
    exercised against the shapes that actually arrive.
    """

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.requests: list[str] = []

    def post(self, payload: str) -> str:
        self.requests.append(payload)
        for report, body in self.responses.items():
            if f"<REPORTNAME>{report}</REPORTNAME>" in payload:
                return body
        return "<ENVELOPE></ENVELOPE>"


def _text(node, tag: str, default: str = "") -> str:
    found = node.find(tag)
    return (found.text or default).strip() if found is not None else default


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), TALLY_DATE).date()


class TallyConnector:
    provider = "tally"

    def __init__(self, transport):
        self.transport = transport

    def test_connection(self) -> ConnectionStatus:
        try:
            body = self.transport.post(_tally_request("List of Companies", None))
        except Exception as exc:
            return ConnectionStatus(False, f"unreachable: {exc}")
        if "<ENVELOPE>" not in body:
            return ConnectionStatus(False, "unexpected response; is this Tally?")
        return ConnectionStatus(True, "connected")

    def _fetch(self, report: str, since: datetime | None):
        body = self.transport.post(_tally_request(report, since))
        # defusedxml, not the stdlib parser: this XML arrives over HTTP from a
        # machine we do not control, and the stdlib parser resolves external
        # entities and expands nested ones by default.
        try:
            return ElementTree.fromstring(body)
        except (DefusedXmlException, ElementTree.ParseError) as exc:
            raise NormalisationError(f"Tally returned unparseable XML: {exc}") from exc

    def fetch_parties(self, since: datetime | None) -> Iterator[RawParty]:
        root = self._fetch("List of Accounts", since)
        for node in root.iter("LEDGER"):
            name = node.get("NAME") or _text(node, "NAME")
            if not name:
                continue
            phones = tuple(
                p for p in (_text(node, "LEDGERMOBILE"), _text(node, "LEDGERPHONE")) if p
            )
            yield RawParty(
                external_id=node.get("GUID") or name,
                name=name,
                phones=phones,
                email=_text(node, "EMAIL") or None,
                raw={
                    "NAME": name,
                    "GUID": node.get("GUID"),
                    "LEDGERMOBILE": _text(node, "LEDGERMOBILE"),
                    "LEDGERPHONE": _text(node, "LEDGERPHONE"),
                    "EMAIL": _text(node, "EMAIL"),
                },
            )

    def fetch_invoices(self, since: datetime | None) -> Iterator[RawInvoice]:
        root = self._fetch("Sales Register", since)
        for node in root.iter("VOUCHER"):
            number = _text(node, "VOUCHERNUMBER")
            party = _text(node, "PARTYLEDGERNAME") or _text(node, "PARTYNAME")
            if not number or not party:
                continue
            amount = _text(node, "AMOUNT")
            issue = _parse_date(_text(node, "DATE"))
            due_raw = _text(node, "DUEDATE")
            raw = {
                "VOUCHERNUMBER": number,
                "PARTYLEDGERNAME": party,
                "AMOUNT": amount,
                "DATE": _text(node, "DATE"),
                "DUEDATE": due_raw,
                "GUID": node.get("GUID"),
                "ISCANCELLED": _text(node, "ISCANCELLED"),
            }
            yield RawInvoice(
                external_id=node.get("GUID") or number,
                invoice_number=number,
                party_external_id=party,
                issue_date=issue,
                due_date=_parse_date(due_raw) if due_raw else issue,
                # Tally signs sales as negative from the ledger's point of view.
                amount_paise=abs(normalise_amount(amount)),
                voided=_text(node, "ISCANCELLED").lower() in ("yes", "true", "1"),
                raw=raw,
            )

    def fetch_payments(self, since: datetime | None) -> Iterator[RawPayment]:
        root = self._fetch("Receipts Register", since)
        for node in root.iter("VOUCHER"):
            party = _text(node, "PARTYLEDGERNAME") or _text(node, "PARTYNAME")
            amount = _text(node, "AMOUNT")
            if not party or not amount:
                continue
            raw = {
                "PARTYLEDGERNAME": party,
                "AMOUNT": amount,
                "DATE": _text(node, "DATE"),
                "VOUCHERNUMBER": _text(node, "VOUCHERNUMBER"),
                "GUID": node.get("GUID"),
                "REFERENCE": _text(node, "REFERENCE"),
            }
            yield RawPayment(
                external_id=node.get("GUID") or _text(node, "VOUCHERNUMBER"),
                party_external_id=party,
                amount_paise=abs(normalise_amount(amount)),
                received_date=_parse_date(_text(node, "DATE")),
                reference=_text(node, "REFERENCE") or _text(node, "VOUCHERNUMBER") or None,
                against_invoice_number=_text(node, "BILLNAME") or None,
                raw=raw,
            )
