"""Adding credit buyers, and the creditor's own profile.

The two things a new tenant does before anything else works: say who *they*
are, and tell the system who owes them money.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.identity.auth import create_access_token
from app.main import app


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    """These tests exercise our own tokens.

    The checked-in .env selects the Supabase backend, which rejects them — so
    the backend is pinned here rather than left to whatever the developer's
    environment happens to say.
    """
    monkeypatch.setattr(settings, "auth_backend", "local")


@pytest.fixture
def client():
    return TestClient(app)


def _auth(t, roles=("admin",)):
    token = create_access_token(
        user_id=t.user_id, company_id=t.company_id, roles=list(roles)
    )
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- buyers


def test_a_buyer_can_be_added_with_a_phone_in_one_call(client, tenants):
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Kumar Traders", "phones": ["98765 43210"], "language": "ta-IN"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "Kumar Traders"
    # Typed as a human types it; stored dialable.
    assert [p["e164"] for p in body["phones"]] == ["+919876543210"]


def test_the_number_is_stored_dialable_however_it_was_typed(client, tenants):
    for typed in ("+91-98765-43211", "091 98765 43211", "9876543211"):
        res = client.post(
            "/api/trade/buyers",
            headers=_auth(tenants.a),
            json={"name": f"Form {typed}", "phones": [typed]},
        )
        assert res.status_code == 201, res.text
        assert res.json()["phones"][0]["e164"] == "+919876543211"


def test_a_new_number_is_never_assumed_scrubbed(client, tenants):
    """DND must come from a scrub, not from a form.

    If a number added by hand defaulted to CLEAR, the operator would have
    silently granted consent the debtor never gave.
    """
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Fresh", "phones": ["98765 43212"]},
    )
    assert res.json()["phones"][0]["dnd_status"] == "UNKNOWN"


def test_an_unusable_number_rejects_the_whole_buyer(client, tenants):
    """No half-saved buyer.

    A buyer row that exists with the bad number dropped is worse: nobody
    notices until the campaign runs and that debtor is silently never called.
    """
    before = len(client.get("/api/trade/buyers", headers=_auth(tenants.a)).json())

    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Bad Number Ltd", "phones": ["12345"]},
    )
    assert res.status_code == 422

    after = client.get("/api/trade/buyers", headers=_auth(tenants.a)).json()
    assert len(after) == before
    assert not any(b["name"] == "Bad Number Ltd" for b in after)


def test_a_second_number_can_be_added_later(client, tenants):
    created = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Two Numbers", "phones": ["98765 43213"]},
    ).json()

    res = client.post(
        f"/api/trade/buyers/{created['id']}/phones",
        headers=_auth(tenants.a),
        json={"raw": "044 2851 4000", "priority": 1},
    )
    assert res.status_code == 201, res.text
    assert len(res.json()["phones"]) == 2

    # Adding the same number twice is a no-op, not a 500 from the unique index.
    again = client.post(
        f"/api/trade/buyers/{created['id']}/phones",
        headers=_auth(tenants.a),
        json={"raw": "+91 44 2851 4000"},
    )
    assert again.status_code == 201
    assert len(again.json()["phones"]) == 2


def test_one_tenant_cannot_read_anothers_buyer(client, tenants):
    created = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Private A", "phones": ["98765 43214"]},
    ).json()

    res = client.get(f"/api/trade/buyers/{created['id']}", headers=_auth(tenants.b))
    assert res.status_code == 404


def test_a_buyer_can_be_added_with_what_they_owe(client, tenants):
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={
            "name": "Owes Money Ltd",
            "phones": ["98765 43220"],
            "amount": "4,28,600",
            "due_date": "2026-01-15",
            "invoice_number": "INV-2026-0412",
        },
    )
    assert res.status_code == 201, res.text
    buyer_id = res.json()["id"]

    rows = client.get("/api/portal/list/buyers", headers=_auth(tenants.a)).json()
    row = next(b for b in rows if b["id"] == buyer_id)
    # 4,28,600 rupees is four lakh twenty-eight thousand six hundred.
    assert row["outstanding_paise"] == 42_860_000
    assert row["outstanding_display"] == "₹4,28,600"


def test_indian_grouping_is_not_read_as_western(client, tenants):
    """`4,50,000` is four lakh fifty thousand.

    A parser assuming Western grouping reads it as forty-five thousand — an
    order of magnitude out, in a number that ends up in a legal notice.
    """
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={
            "name": "Grouping Ltd",
            "amount": "4,50,000",
            "due_date": "2026-02-01",
        },
    )
    assert res.status_code == 201, res.text
    rows = client.get("/api/portal/list/buyers", headers=_auth(tenants.a)).json()
    row = next(b for b in rows if b["id"] == res.json()["id"])
    assert row["outstanding_paise"] == 45_000_000, "read as ₹45,000 — off by 10x"


@pytest.mark.parametrize("typed", ["₹ 4,28,600", "428600", "Rs.4,28,600/-"])
def test_money_is_accepted_however_it_is_typed(client, tenants, typed):
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": f"Typed {typed}", "amount": typed, "due_date": "2026-01-15"},
    )
    assert res.status_code == 201, res.text
    rows = client.get("/api/portal/list/buyers", headers=_auth(tenants.a)).json()
    row = next(b for b in rows if b["id"] == res.json()["id"])
    assert row["outstanding_paise"] == 42_860_000


def test_an_amount_without_a_due_date_is_refused(client, tenants):
    """Ageing and escalation are both measured from the due date.

    Defaulting it to today would silently make every imported debt current.
    """
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "No Due Date", "amount": "50,000"},
    )
    assert res.status_code == 422
    assert "due date" in res.text.lower()


def test_an_unreadable_amount_creates_nothing(client, tenants):
    before = len(client.get("/api/trade/buyers", headers=_auth(tenants.a)).json())
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Bad Amount Ltd", "amount": "about five lakh-ish",
              "due_date": "2026-01-15"},
    )
    assert res.status_code == 422
    after = client.get("/api/trade/buyers", headers=_auth(tenants.a)).json()
    assert len(after) == before
    assert not any(b["name"] == "Bad Amount Ltd" for b in after)


def test_an_overdue_invoice_is_not_dated_today(client, tenants):
    """An invoice due in the past was issued before it fell due.

    Dating it today would make a long-overdue debt show as brand new in the
    ageing, which is the report the whole ladder keys off.
    """
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Long Overdue Ltd", "amount": "1,00,000",
              "due_date": "2020-01-15"},
    )
    assert res.status_code == 201, res.text
    rows = client.get("/api/portal/list/buyers", headers=_auth(tenants.a)).json()
    row = next(b for b in rows if b["id"] == res.json()["id"])
    assert row["days_past_due"] > 1000


def test_a_buyer_with_no_amount_is_still_allowed(client, tenants):
    """A buyer may exist before their first invoice."""
    res = client.post(
        "/api/trade/buyers",
        headers=_auth(tenants.a),
        json={"name": "Not Yet Owing", "phones": ["98765 43221"]},
    )
    assert res.status_code == 201, res.text
    rows = client.get("/api/portal/list/buyers", headers=_auth(tenants.a)).json()
    row = next(b for b in rows if b["id"] == res.json()["id"])
    assert row["outstanding_paise"] == 0


# ------------------------------------------------------------------ profile


def test_profile_round_trips(client, tenants):
    res = client.put(
        "/api/portal/profile",
        headers=_auth(tenants.a),
        json={
            "legal_name": "Sundaram Textiles Private Limited",
            "gstin": "33AABCS1429B1ZQ",
            "registered_address": "12 Mount Road, Chennai",
            "state_code": "33",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["legal_name"] == "Sundaram Textiles Private Limited"

    got = client.get("/api/portal/profile", headers=_auth(tenants.a)).json()
    assert got["gstin"] == "33AABCS1429B1ZQ"
    assert got["state_code"] == "33"


def test_editing_a_profile_twice_does_not_create_a_second_one(client, tenants):
    for name in ("First Name Ltd", "Renamed Ltd"):
        client.put(
            "/api/portal/profile", headers=_auth(tenants.a), json={"legal_name": name}
        )
    got = client.get("/api/portal/profile", headers=_auth(tenants.a)).json()
    assert got["legal_name"] == "Renamed Ltd"


def test_a_profile_is_not_visible_to_another_tenant(client, tenants):
    client.put(
        "/api/portal/profile",
        headers=_auth(tenants.a),
        json={"legal_name": "Tenant A Legal Name"},
    )
    other = client.get("/api/portal/profile", headers=_auth(tenants.b)).json()
    assert other["legal_name"] != "Tenant A Legal Name"


def test_a_read_only_user_cannot_edit_the_profile(client, tenants):
    res = client.put(
        "/api/portal/profile",
        headers=_auth(tenants.a, roles=("viewer",)),
        json={"legal_name": "Should Not Stick"},
    )
    assert res.status_code == 403
