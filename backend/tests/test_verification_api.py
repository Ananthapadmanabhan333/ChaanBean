"""The verification surface, exercised through HTTP.

These are route tests rather than unit tests on purpose. What
`app.company.verification` decides is covered where it lives; what matters here
is that the decision survives the trip out to a client — that the permission
holds, that one tenant cannot read another's answer, that a mistyped identifier
is refused before it is stored, and that a verified profile actually reaches the
credit engine while an unverified one does not.

Every GSTIN below is check-digit valid except the one that is deliberately not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import admin_session
from app.identity.auth import create_access_token
from app.main import app
from app.models import CompanyProfile, CreditAssessment, EntityCandidate

# Sharma Traders, Maharashtra, registration Active — matches a GST fixture.
GSTIN_ACTIVE = "27AABCS1429B1ZU"
# Verma Steel Works, Karnataka, registration Cancelled.
GSTIN_CANCELLED = "29AACCV3456D1ZB"
# GSTIN_ACTIVE with its check digit mistyped, and nothing else wrong: the exact
# shape of the typo that would otherwise fetch a stranger's filings.
GSTIN_BAD_CHECK_DIGIT = "27AABCS1429B1ZR"


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


def _declare(client, t, **fields):
    return client.put(
        f"/api/trade/buyers/{t.buyer_id}/identity", headers=_auth(t), json=fields
    )


def _propose_candidate(
    t, *, tier="NAME_STRONG", name="Sharma Traders Pvt Ltd", identifier=GSTIN_ACTIVE
):
    """A candidate as a verification run would have left it.

    Written directly rather than provoked through a lookup: what is under test
    is the review route, and a fixture that had to be coaxed into producing a
    near-miss would be testing the resolver instead. (Neither shipped backend can
    produce one at all — see the note in app/company/__init__.py — which is
    precisely why the route needs its own tests rather than incidental coverage.)
    """
    with admin_session() as s:
        candidate = EntityCandidate(
            company_id=t.company_id,
            buyer_id=t.buyer_id,
            candidate_name=name,
            identifier_kind="gstin",
            identifier_value=identifier,
            score=0.72,
            tier=tier,
            signals={"signals": [], "blockers": []},
            status="PROPOSED",
        )
        s.add(candidate)
        s.flush()
        return candidate.id


def _factor(assessment: dict, name: str) -> dict:
    return next(f for f in assessment["factors"] if f["name"] == name)


# ---------------------------------------------------- what may be declared


def test_a_mistyped_check_digit_is_refused(client, tenants):
    """One wrong character still leaves a structurally valid GSTIN.

    Roughly one time in thirty-six it is somebody else's, which is why this is
    refused at the door rather than looked up and quietly matched.
    """
    res = _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_BAD_CHECK_DIGIT
    )
    assert res.status_code == 422, res.text
    assert "check digit" in res.text.lower()


def test_a_state_code_contradicting_the_gstin_is_refused(client, tenants):
    """Two answers to "which state", and no way to tell which one is the typo."""
    res = _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_ACTIVE, state_code="33"
    )
    assert res.status_code == 422, res.text
    assert "state code" in res.text.lower()


def test_the_pan_goes_in_and_only_its_last_four_come_back(client, tenants):
    res = _declare(
        client,
        tenants.a,
        legal_name="Sharma Traders Private Limited",
        gstin=GSTIN_ACTIVE,
        pan="AABCS1429B",
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pan_last4"] == "429B"
    assert "pan" not in body, "the full PAN must not be readable back over HTTP"
    # Derived from the GSTIN when the form did not supply it.
    assert body["state_code"] == "27"


# ------------------------------------------------------------ permissions


def test_an_operator_may_verify_but_may_not_confirm_a_candidate(client, tenants):
    """Running the lookup is desk work; deciding whose company it is, is not."""
    assert _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_ACTIVE
    ).status_code == 200

    ran = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verify",
        headers=_auth(tenants.a, roles=("operator",)),
    )
    assert ran.status_code == 201, ran.text

    candidate_id = _propose_candidate(tenants.a)
    refused = client.post(
        f"/api/trade/entity-candidates/{candidate_id}/review",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"decision": "CONFIRM"},
    )
    assert refused.status_code == 403
    assert "entity:confirm" in refused.text


# ------------------------------------------------------------- the review


def test_a_candidate_is_decided_once_and_only_once(client, tenants):
    """A second opinion is a new verification, not an edit of the first."""
    candidate_id = _propose_candidate(tenants.a)
    first = client.post(
        f"/api/trade/entity-candidates/{candidate_id}/review",
        headers=_auth(tenants.a),
        json={"decision": "CONFIRM", "note": "matched the letterhead"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "CONFIRMED"

    second = client.post(
        f"/api/trade/entity-candidates/{candidate_id}/review",
        headers=_auth(tenants.a),
        json={"decision": "REJECT"},
    )
    assert second.status_code == 409
    assert "already confirmed" in second.text.lower()


def test_confirming_a_candidate_does_not_make_it_an_identifier_match(client, tenants):
    """A person agreeing with a resemblance does not turn it into evidence.

    If confirming promoted the tier, the whole provenance rule would be one
    click away from being bypassed: propose a name match, confirm it, and the
    profile is suddenly publishable.
    """
    candidate_id = _propose_candidate(tenants.a, tier="NAME_STRONG")
    assert (
        client.post(
            f"/api/trade/entity-candidates/{candidate_id}/review",
            headers=_auth(tenants.a),
            json={"decision": "CONFIRM"},
        ).status_code
        == 200
    )

    with admin_session() as s:
        profile = s.execute(
            select(CompanyProfile).where(CompanyProfile.buyer_id == tenants.a.buyer_id)
        ).scalars().first()
        assert profile is not None
        assert profile.resolution_tier == "NAME_STRONG"


def test_confirming_a_candidate_cannot_replace_a_verified_identifier(client, tenants):
    """The "never downgrade" guard has to cover the identifier, not just the tier.

    A candidate outlives the run that proposed it: nothing closes it when a later
    verification succeeds. If confirming one could still write its GSTIN onto a
    profile a registry has since settled, the row would read IDENTIFIER while
    carrying a number nothing checked — and `_registry_signals` and
    `_gst_filing_regular` both trust that stamp rather than re-deriving it.
    """
    assert _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_ACTIVE
    ).status_code == 200
    ran = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verify", headers=_auth(tenants.a)
    )
    assert ran.status_code == 201, ran.text
    assert ran.json()["tier"] == "IDENTIFIER"

    # Proposed against the same buyer, carrying somebody else's number.
    candidate_id = _propose_candidate(tenants.a, identifier=GSTIN_CANCELLED)
    assert (
        client.post(
            f"/api/trade/entity-candidates/{candidate_id}/review",
            headers=_auth(tenants.a),
            json={"decision": "CONFIRM"},
        ).status_code
        == 200
    )

    with admin_session() as s:
        profile = s.execute(
            select(CompanyProfile).where(CompanyProfile.buyer_id == tenants.a.buyer_id)
        ).scalars().first()
        assert profile.resolution_tier == "IDENTIFIER"
        assert profile.gstin == GSTIN_ACTIVE, (
            "a confirmed candidate replaced the identifier a registry had already "
            "settled, on a row still stamped IDENTIFIER"
        )


# ---------------------------------------------------------------- reading


def test_a_verification_is_404_until_one_has_been_run(client, tenants):
    """Never checked and checked-and-failed are different facts.

    A caller handed a body either way ends up treating the first as the second.
    """
    res = client.get(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verification", headers=_auth(tenants.a)
    )
    assert res.status_code == 404


def test_one_tenant_cannot_read_anothers_verification(client, tenants):
    assert _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_ACTIVE
    ).status_code == 200
    ran = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verify", headers=_auth(tenants.a)
    )
    assert ran.status_code == 201, ran.text
    assert ran.json()["tier"] == "IDENTIFIER"

    mine = client.get(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verification", headers=_auth(tenants.a)
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["gst_status"] == "Active"

    theirs = client.get(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verification", headers=_auth(tenants.b)
    )
    assert theirs.status_code == 404


# ------------------------------------------------------------- the payoff
#
# The reason any of this exists. A verification that does not change what the
# credit engine is told is a report nobody reads.


def _scored(assessment: dict):
    """Everything the engine was told, as it came back out.

    The whole factor list rather than one factor: the property being asserted is
    that an unverified profile moves *nothing*, and a test that watches a single
    value passes happily while a different one moves.
    """
    return assessment["score"], [(f["name"], f["value"]) for f in assessment["factors"]]


def test_only_a_verified_profile_changes_the_credit_inputs(client, tenants):
    application = {
        "requested_limit": "5,00,000",
        "annual_turnover": "1,20,00,000",
        "years_trading": 6,
    }

    def check() -> dict:
        res = client.post(
            f"/api/trade/buyers/{tenants.a.buyer_id}/credit-check",
            headers=_auth(tenants.a),
            json=application,
        )
        assert res.status_code == 201, res.text
        return res.json()

    unresolved = check()

    # The same cancelled registration, typed in rather than looked up. It is a
    # claim at this point, and a claim must not move a credit score.
    assert _declare(
        client, tenants.a, legal_name="Verma Steel Works", gstin=GSTIN_CANCELLED
    ).status_code == 200
    assert _scored(check()) == _scored(unresolved), (
        "an unverified profile moved the score — self-declared data is being "
        "scored as though a registry had confirmed it"
    )

    ran = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verify", headers=_auth(tenants.a)
    )
    assert ran.status_code == 201, ran.text
    assert ran.json()["tier"] == "IDENTIFIER"

    verified = _factor(check(), "recovery_risk")["value"]
    assert verified < _factor(unresolved, "recovery_risk")["value"], (
        "a registry-confirmed cancelled registration left the credit inputs "
        "unchanged — the verification is not reaching the engine"
    )


def test_a_verified_profile_settles_the_gst_question_not_the_form(client, tenants):
    """One question, one answer — once a registry has answered it.

    The form and the profile can disagree, and an assessment recording both is
    unreadable when someone later asks which one the decision rested on. What
    settles it is the verification and not the typing: until then the profile
    holds the applicant's own claim about themselves, and scoring that as a fact
    is the whole failure this feature exists to prevent.
    """
    assert _declare(
        client, tenants.a, legal_name="Sharma Traders", gstin=GSTIN_ACTIVE
    ).status_code == 200

    def check() -> dict:
        res = client.post(
            f"/api/trade/buyers/{tenants.a.buyer_id}/credit-check",
            headers=_auth(tenants.a),
            json={"requested_limit": "5,00,000", "gst_registered": False},
        )
        assert res.status_code == 201, res.text
        return res.json()

    assert _factor(check(), "gst_registered")["value"] == 0.0, (
        "a GSTIN somebody typed into the identity form overrode the form itself "
        "— nothing had checked it against anything"
    )

    ran = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/verify", headers=_auth(tenants.a)
    )
    assert ran.status_code == 201, ran.text
    assert ran.json()["tier"] == "IDENTIFIER"

    assert _factor(check(), "gst_registered")["value"] == 1.0

    # And each assessment says which of the two answers it used, honestly.
    with admin_session() as s:
        used = {
            (row.application["gst_registered"], row.application["gst_registered_source"])
            for row in s.execute(
                select(CreditAssessment).where(
                    CreditAssessment.buyer_id == tenants.a.buyer_id
                )
            ).scalars()
        }
    assert used == {(False, "declared"), (True, "resolved_profile")}
