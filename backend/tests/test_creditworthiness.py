"""Credit eligibility: the scorecard, and the guarantees around it.

The tests that matter here are the ones about what the tool must *refuse* to
do. A recommendation engine that is merely usually right is a liability when
the wrong answer means extending credit to someone already in arrears.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.identity.auth import create_access_token
from app.intelligence.creditworthiness import (
    MODEL_VERSION,
    CreditApplication,
    LedgerFacts,
    Recommendation,
    assess,
)
from app.intelligence.scoring import ScoringContext, score_buyer
from app.main import app

TODAY = date(2026, 9, 6)
LAKH = 1_00_000_00  # one lakh rupees, in paise


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "auth_backend", "local")


@pytest.fixture
def client():
    return TestClient(app)


def _auth(t, roles=("admin",)):
    token = create_access_token(
        user_id=t.user_id, company_id=t.company_id, roles=list(roles)
    )
    return {"Authorization": f"Bearer {token}"}


def _good_applicant(**over):
    base = dict(
        requested_limit_paise=5 * LAKH,
        annual_turnover_paise=200 * LAKH,
        years_trading=6.0,
        trade_references=2,
        gst_registered=True,
        turnover_verified=True,
    )
    base.update(over)
    return CreditApplication(**base)


def _clean_history():
    return LedgerFacts(
        invoices_settled=14, mean_days_to_pay=22.0, promise_kept_rate=0.95
    )


# ------------------------------------------------------------- the scorecard


def test_a_solid_buyer_is_recommended():
    risk = score_buyer(
        ScoringContext(as_of=TODAY, mean_days_to_pay=22, promise_kept_rate=0.95)
    )
    r = assess(_good_applicant(), as_of=TODAY, risk=risk, ledger=_clean_history())
    assert r.recommendation is Recommendation.STRONG
    assert r.suggested_limit_paise > 0
    assert not r.blockers


def test_arrears_override_a_high_score():
    """The single most important behaviour in this module.

    A long history and a strong turnover produce a high score. None of that
    makes it sensible to extend *more* credit to someone who has not paid what
    they already owe you — and averaged into a score, that fact disappears.
    """
    risk = score_buyer(
        ScoringContext(as_of=TODAY, mean_days_to_pay=22, promise_kept_rate=0.95)
    )
    ledger = LedgerFacts(
        invoices_settled=14,
        mean_days_to_pay=22.0,
        promise_kept_rate=0.95,
        currently_overdue_paise=2 * LAKH,
        max_days_past_due=120,
    )
    r = assess(_good_applicant(), as_of=TODAY, risk=risk, ledger=ledger)

    assert r.score > 70, "the score itself is still high — that is the point"
    assert r.recommendation is Recommendation.REFER
    assert r.suggested_limit_paise == 0
    assert any("overdue" in b for b in r.blockers)


def test_a_large_typed_turnover_cannot_unlock_an_unlimited_limit():
    """Declared figures are claims, not evidence.

    If typing a bigger number produced a bigger limit without bound, the check
    would be a formality.
    """
    modest = assess(
        _good_applicant(annual_turnover_paise=200 * LAKH, turnover_verified=False),
        as_of=TODAY,
        ledger=_clean_history(),
    )
    absurd = assess(
        _good_applicant(annual_turnover_paise=200_000 * LAKH, turnover_verified=False),
        as_of=TODAY,
        ledger=_clean_history(),
    )
    # Both are capped by what was actually requested.
    assert absurd.suggested_limit_paise <= absurd.requested_limit_paise
    assert modest.suggested_limit_paise <= modest.requested_limit_paise


def test_an_unverified_turnover_buys_a_smaller_limit_than_a_verified_one():
    kw = dict(as_of=TODAY, ledger=_clean_history())
    verified = assess(_good_applicant(turnover_verified=True), **kw)
    unverified = assess(_good_applicant(turnover_verified=False), **kw)
    assert unverified.suggested_limit_paise < verified.suggested_limit_paise


def test_the_suggestion_never_exceeds_the_request():
    """This recommends a ceiling; it does not upsell."""
    r = assess(
        _good_applicant(requested_limit_paise=1 * LAKH, annual_turnover_paise=900 * LAKH),
        as_of=TODAY,
        ledger=_clean_history(),
    )
    assert r.suggested_limit_paise <= 1 * LAKH


def test_no_history_is_not_treated_as_good_history():
    with_history = assess(_good_applicant(), as_of=TODAY, ledger=_clean_history())
    brand_new = assess(_good_applicant(), as_of=TODAY, ledger=LedgerFacts())
    assert brand_new.score < with_history.score
    assert brand_new.suggested_limit_paise < with_history.suggested_limit_paise


def test_every_factor_carries_an_explanation():
    """A score that changes how someone is treated without a stated reason is
    not defensible — to the customer, the buyer, or a regulator."""
    r = assess(_good_applicant(), as_of=TODAY, ledger=_clean_history())
    assert r.factors
    for f in r.factors:
        assert f.explanation.strip(), f"{f.name} has no explanation"
        assert f.weight > 0
        assert f.contribution == pytest.approx(f.value * f.weight)


def test_the_same_inputs_always_give_the_same_answer():
    """Reproducibility is the whole reason this is a scorecard.

    A decision made in March has to be reconstructable in December.
    """
    a = assess(_good_applicant(), as_of=TODAY, ledger=_clean_history())
    b = assess(_good_applicant(), as_of=TODAY, ledger=_clean_history())
    assert a.as_dict() == b.as_dict()
    assert a.model_version == MODEL_VERSION


def test_recommendations_are_never_approve_or_reject():
    """The vocabulary is deliberate: this tool does not decide."""
    values = {r.value for r in Recommendation}
    assert values == {"STRONG", "ACCEPTABLE", "CAUTION", "REFER"}
    assert not values & {"APPROVE", "APPROVED", "REJECT", "REJECTED", "DECLINE"}


# ------------------------------------------------------------------ the API


def _new_buyer(client, tenant, **extra):
    body = {"name": "Applicant Ltd", "phones": ["98765 43230"]}
    body.update(extra)
    res = client.post("/api/trade/buyers", headers=_auth(tenant), json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_a_check_can_be_run_and_is_kept(client, tenants):
    buyer_id = _new_buyer(client, tenants.a)
    res = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={
            "requested_limit": "5,00,000",
            "annual_turnover": "2,00,00,000",
            "years_trading": 6,
            "trade_references": 2,
            "gst_registered": True,
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["recommendation"] in {"STRONG", "ACCEPTABLE", "CAUTION", "REFER"}
    assert body["factors"], "no reasons returned"
    assert body["requested_limit_display"] == "₹5,00,000"

    history = client.get(
        f"/api/trade/buyers/{buyer_id}/credit-check", headers=_auth(tenants.a)
    ).json()
    assert len(history) == 1


def test_the_applicant_cannot_supply_their_own_payment_history(client, tenants):
    """Arrears come from our ledger, so they cannot be omitted on the form.

    A buyer created with a long-overdue invoice must be blocked even though the
    submitted application says nothing about it.
    """
    buyer_id = _new_buyer(
        client,
        tenants.a,
        name="Owes Us Ltd",
        amount="2,00,000",
        due_date=str(date.today() - timedelta(days=200)),
    )
    res = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={"requested_limit": "5,00,000", "annual_turnover": "2,00,00,000",
              "years_trading": 9},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["recommendation"] == "REFER"
    assert body["suggested_limit_paise"] == 0
    assert any("overdue" in b for b in body["blockers"])


def test_a_decision_is_recorded_separately_from_the_suggestion(client, tenants):
    buyer_id = _new_buyer(client, tenants.a)
    check = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={"requested_limit": "5,00,000", "annual_turnover": "2,00,00,000",
              "years_trading": 6},
    ).json()

    res = client.post(
        f"/api/trade/credit-check/{check['id']}/decide",
        headers=_auth(tenants.a),
        json={"approved_limit": "1,50,000", "note": "Start small, review in 90 days"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decided_limit_paise"] == 1_50_000_00
    # The suggestion is preserved alongside the decision; where they disagree,
    # the disagreement is the audit trail.
    assert body["suggested_limit_paise"] == check["suggested_limit_paise"]
    assert body["decided_at"] is not None


def test_a_decision_cannot_be_quietly_rewritten(client, tenants):
    buyer_id = _new_buyer(client, tenants.a)
    check = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={"requested_limit": "5,00,000", "annual_turnover": "2,00,00,000"},
    ).json()
    first = client.post(
        f"/api/trade/credit-check/{check['id']}/decide",
        headers=_auth(tenants.a),
        json={"approved_limit": "1,00,000"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/trade/credit-check/{check['id']}/decide",
        headers=_auth(tenants.a),
        json={"approved_limit": "9,00,000"},
    )
    assert second.status_code == 409


def test_an_operator_cannot_grant_a_limit(client, tenants):
    """Running a check is operator work; granting credit is not."""
    buyer_id = _new_buyer(client, tenants.a)
    check = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"requested_limit": "5,00,000", "annual_turnover": "2,00,00,000"},
    )
    assert check.status_code == 201, "an operator may run the check"

    res = client.post(
        f"/api/trade/credit-check/{check.json()['id']}/decide",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"approved_limit": "5,00,000"},
    )
    assert res.status_code == 403


def test_one_tenant_cannot_read_anothers_assessments(client, tenants):
    buyer_id = _new_buyer(client, tenants.a)
    client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={"requested_limit": "5,00,000", "annual_turnover": "2,00,00,000"},
    )
    res = client.get(
        f"/api/trade/buyers/{buyer_id}/credit-check", headers=_auth(tenants.b)
    )
    assert res.status_code == 404


def test_an_unreadable_limit_is_refused(client, tenants):
    buyer_id = _new_buyer(client, tenants.a)
    res = client.post(
        f"/api/trade/buyers/{buyer_id}/credit-check",
        headers=_auth(tenants.a),
        json={"requested_limit": "about five lakh"},
    )
    assert res.status_code == 422
