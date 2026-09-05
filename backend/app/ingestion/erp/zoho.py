"""Zoho Books connector.

The second integration, and the one that makes the `ErpConnector` interface
honest. Tally and Zoho disagree about nearly everything, which is exactly why
building both was worth it:

| | Tally | Zoho Books |
|---|---|---|
| Transport | XML over HTTP | JSON REST |
| Reachability | a desktop on someone's LAN | a cloud API |
| Auth | none (it trusts the LAN) | OAuth 2 refresh tokens |
| Paging | none — it hands you everything | cursor, 200 per page |
| Amounts | strings, signed by ledger side | floats in rupees |
| Identity | GUIDs, sometimes absent | stable numeric ids |

Two of those forced interface changes that a Tally-only design would have got
wrong: `since` had to be a real incremental cursor rather than a Tally date
filter, and `fetch_*` had to be an iterator rather than a list, because Zoho
pages and Tally does not.

**Amounts arrive as floats.** They are converted to integer paise at the
boundary, here, and never propagate inward — a float rupee value that reaches
the ledger is a rounding error in a legal notice.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterator

import httpx

from app.ingestion.erp.base import (
    ConnectionStatus,
    RawInvoice,
    RawParty,
    RawPayment,
)

log = logging.getLogger(__name__)

API_BASE = "https://www.zohoapis.in/books/v3"  # .in — Indian data residency
ACCOUNTS_BASE = "https://accounts.zoho.in"
PAGE_SIZE = 200


def rupees_to_paise(value) -> int:
    """Float rupees to integer paise, rounded once, at the boundary.

    `round` rather than `int`: 4500.005 must not silently become 450000 paise
    when the source meant 450001.
    """
    if value is None:
        return 0
    return int(round(float(value) * 100))


class ZohoAuth:
    """Refresh-token flow. Access tokens last an hour, so they are fetched on
    demand and cached until they expire rather than stored."""

    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._token: str | None = None
        self._expires_at: float = 0.0

    def access_token(self) -> str:
        import time

        if self._token and time.time() < self._expires_at - 60:
            return self._token
        response = httpx.post(
            f"{ACCOUNTS_BASE}/oauth/v2/token",
            params={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if "access_token" not in payload:
            raise RuntimeError(f"Zoho refused the refresh token: {payload}")
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token


class ZohoFixtureTransport:
    """Replays captured Zoho responses, including paging."""

    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.requests: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict) -> dict:
        self.requests.append((path, dict(params)))
        rows = self.pages.get(path, [])
        page = int(params.get("page", 1))
        start = (page - 1) * PAGE_SIZE
        chunk = rows[start : start + PAGE_SIZE]
        return {
            path.strip("/"): chunk,
            "page_context": {"has_more_page": start + PAGE_SIZE < len(rows)},
        }


class ZohoHttpTransport:
    def __init__(self, auth: ZohoAuth, organization_id: str, *, timeout: float = 30.0):
        self.auth = auth
        self.organization_id = organization_id
        self.timeout = timeout

    def get(self, path: str, params: dict) -> dict:
        response = httpx.get(
            f"{API_BASE}{path}",
            params={**params, "organization_id": self.organization_id},
            headers={"Authorization": f"Zoho-oauthtoken {self.auth.access_token()}"},
            timeout=self.timeout,
        )
        if response.status_code == 429:
            # Zoho rate-limits per organisation per minute. Surfaced rather than
            # retried blindly, so a sync run records that it was throttled.
            raise RuntimeError("Zoho rate limit reached")
        response.raise_for_status()
        return response.json()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


class ZohoConnector:
    provider = "zoho_books"

    def __init__(self, transport):
        self.transport = transport

    def test_connection(self) -> ConnectionStatus:
        try:
            self.transport.get("/contacts", {"page": 1, "per_page": 1})
        except Exception as exc:
            return ConnectionStatus(False, f"unreachable: {exc}")
        return ConnectionStatus(True, "connected")

    def _paged(self, path: str, since: datetime | None) -> Iterator[dict]:
        """Zoho pages; Tally does not. This is why the interface yields."""
        page = 1
        params: dict = {"per_page": PAGE_SIZE}
        if since is not None:
            params["last_modified_time"] = since.strftime("%Y-%m-%dT%H:%M:%S%z") or since.isoformat()
        while True:
            payload = self.transport.get(path, {**params, "page": page})
            rows = payload.get(path.strip("/"), [])
            yield from rows
            if not payload.get("page_context", {}).get("has_more_page"):
                return
            page += 1

    def fetch_parties(self, since: datetime | None) -> Iterator[RawParty]:
        for row in self._paged("/contacts", since):
            phones = tuple(
                p for p in (row.get("mobile"), row.get("phone")) if p
            )
            yield RawParty(
                external_id=str(row.get("contact_id")),
                name=row.get("contact_name") or row.get("company_name") or "",
                phones=phones,
                email=row.get("email") or None,
                raw=row,
            )

    def fetch_invoices(self, since: datetime | None) -> Iterator[RawInvoice]:
        for row in self._paged("/invoices", since):
            issue = _parse_date(row.get("date"))
            due = _parse_date(row.get("due_date")) or issue
            if issue is None:
                log.warning("skipping Zoho invoice %s with no date", row.get("invoice_id"))
                continue
            yield RawInvoice(
                external_id=str(row.get("invoice_id")),
                invoice_number=row.get("invoice_number") or str(row.get("invoice_id")),
                party_external_id=str(row.get("customer_id")),
                issue_date=issue,
                due_date=due,
                # Floats in, integer paise out — converted once, at the boundary.
                amount_paise=rupees_to_paise(row.get("total")),
                voided=row.get("status") in ("void", "cancelled"),
                raw=row,
            )

    def fetch_payments(self, since: datetime | None) -> Iterator[RawPayment]:
        for row in self._paged("/customerpayments", since):
            received = _parse_date(row.get("date"))
            if received is None:
                continue
            invoices = row.get("invoices") or []
            yield RawPayment(
                external_id=str(row.get("payment_id")),
                party_external_id=str(row.get("customer_id")),
                amount_paise=rupees_to_paise(row.get("amount")),
                received_date=received,
                reference=row.get("reference_number") or None,
                # Zoho tells us which invoice a receipt was against; Tally often
                # does not. The interface carries it as optional for that reason.
                against_invoice_number=(
                    invoices[0].get("invoice_number") if len(invoices) == 1 else None
                ),
                raw=row,
            )


def build_zoho(config: dict, secret: str) -> ZohoConnector:
    auth = ZohoAuth(
        client_id=config["client_id"],
        client_secret=secret,
        refresh_token=config["refresh_token"],
    )
    return ZohoConnector(ZohoHttpTransport(auth, config["organization_id"]))
