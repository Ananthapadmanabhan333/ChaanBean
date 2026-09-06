"""The court and legal-link routes, over HTTP.

What `app.legal.cases` decides is covered where it lives. These are the two
things that only exist at the edge, and neither had a test:

* confirming a link is admin work. The permission is the whole separation —
  whoever is chasing the money should not also be deciding, on a name
  resemblance, whose litigation it is — and an operator reaching it is the
  failure that ends in a defamation claim rather than a bad row.
* `court_backend` defaults to `none`, and a route that searches nothing has to
  say so. Silence and "we found nothing" are different answers, and an operator
  who cannot tell them apart concludes the debtor is clean.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import admin_session
from app.identity.auth import create_access_token
from app.main import app
from app.models import CompanyProfile


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    """The checked-in .env may select Supabase, which rejects our own tokens."""
    monkeypatch.setattr(settings, "auth_backend", "local")


@pytest.fixture
def client():
    return TestClient(app)


def _auth(t, roles=("admin",)):
    token = create_access_token(
        user_id=t.user_id, company_id=t.company_id, roles=list(roles)
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def profiled(tenants):
    """The tenants' buyer, with a profile to hang a search on.

    Without one the search refuses at NO_PROFILE and never reaches the backend,
    which is a different refusal from the one under test.
    """
    with admin_session() as s:
        s.add(
            CompanyProfile(
                company_id=tenants.a.company_id,
                buyer_id=tenants.a.buyer_id,
                legal_name="Kumar Traders Private Limited",
            )
        )
    return tenants


def test_an_operator_cannot_confirm_a_legal_link(client, tenants):
    """The refusal the permission was minted for.

    Refused before the link is looked up, so the id here need not exist: the
    permission is the gate, and an operator must not learn whether a particular
    case is on file either.
    """
    res = client.post(
        f"/api/trade/legal-links/{uuid.uuid4()}/review",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"decision": "CONFIRM"},
    )
    assert res.status_code == 403
    assert "legal:link_confirm" in res.json()["detail"]


def test_with_courts_switched_off_the_route_says_so(client, profiled, monkeypatch):
    """`none` is the default, and it is an answer rather than an outage.

    The alternative default was the local fixture, whose invented filings carry
    REGISTRY provenance and the same identifiers `app.seed` gives its demo
    buyers — an invented recovery suit, confirmable, on a real customer's file.
    """
    monkeypatch.setattr(settings, "court_backend", "none")

    res = client.post(
        f"/api/trade/buyers/{profiled.a.buyer_id}/court-cases/search",
        headers=_auth(profiled.a),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "NO_BACKEND" in body["refusals"]
    assert body["cases_seen"] == 0
    assert body["links"] == []
