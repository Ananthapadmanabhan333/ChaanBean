"""Corrections, cessation and promises, over HTTP.

The other direction from the rest of the API: the debtor saying "that is not my
name" or "stop calling me". Until these routes existed none of it could be
recorded except a keypress during a live call, and a withdrawal arriving by
letter is the ordinary case rather than the exotic one.

The asymmetry running through this file is deliberate and is what most of these
tests are about. Anything that *reduces* contact is operator work, because the
person who just took the call should be able to write it down without waiting
for an admin. Anything that turns contact back on is not.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import admin_session
from app.identity.auth import create_access_token
from app.main import app
from app.models import (
    AuditLog,
    BlackoutDate,
    Buyer,
    BuyerPhone,
    Channel,
    ChannelOptOut,
    Promise,
)


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


def _buyer(buyer_id) -> Buyer:
    with admin_session() as s:
        return s.execute(select(Buyer).where(Buyer.id == buyer_id)).scalar_one()


def _phone_id(buyer_id):
    with admin_session() as s:
        return s.execute(
            select(BuyerPhone.id).where(BuyerPhone.buyer_id == buyer_id)
        ).scalars().first()


# ------------------------------------------------------------------ corrections


def test_a_buyer_can_be_corrected(client, tenants):
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"name": "Sharma Traders Private Limited", "language": "hi-IN"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Sharma Traders Private Limited"
    assert res.json()["language"] == "hi-IN"


def test_correcting_a_buyer_is_audited_with_both_sides(client, tenants):
    """The audit matters more than the edit.

    `name` is what a legal notice is addressed to, so "who was it addressed to
    before, and who changed it" has to survive the correction.
    """
    was = _buyer(tenants.a.buyer_id).name
    client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a),
        json={"name": "Verma Steel Works"},
    )
    with admin_session() as s:
        row = s.execute(
            select(AuditLog).where(
                AuditLog.company_id == tenants.a.company_id,
                AuditLog.action == "buyer.corrected",
            )
        ).scalars().one()
    assert row.before == {"name": was}
    assert row.after == {"name": "Verma Steel Works"}


def test_a_correction_that_changes_nothing_writes_no_audit_row(client, tenants):
    """A resubmitted form is not a write, and a row saying a name stayed the
    same is noise in the one trail that has to stay readable."""
    name = _buyer(tenants.a.buyer_id).name
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a),
        json={"name": name},
    )
    assert res.status_code == 200
    with admin_session() as s:
        rows = list(
            s.execute(
                select(AuditLog).where(
                    AuditLog.company_id == tenants.a.company_id,
                    AuditLog.action == "buyer.corrected",
                )
            ).scalars()
        )
    assert rows == []


def test_an_uncorrectable_field_is_refused_rather_than_dropped(client, tenants):
    """Editing a declared GSTIN is what voids a verification tier, so it goes
    through the identity route and not through here. Refused by name: dropping
    it silently would report success on a write that never happened."""
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a),
        json={"name": "Anything", "gstin": "27AABCS1429B1ZU"},
    )
    assert res.status_code == 422
    assert "gstin" in res.text
    assert _buyer(tenants.a.buyer_id).name != "Anything"


def test_a_correction_is_all_or_nothing(client, tenants):
    """A request naming one good field and one bad must leave neither applied.

    The good field is the one validated first — fields go in sorted order, so a
    good `name` with a bad `email` would be rejected before the name was
    reached and would pass even against an implementation that wrote as it
    validated.
    """
    before = _buyer(tenants.a.buyer_id).email
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a),
        json={"email": "accounts@kumaragencies.test", "name": ""},
    )
    assert res.status_code == 422
    assert "VALUE_REJECTED" in res.json()["detail"]
    assert _buyer(tenants.a.buyer_id).email == before


def test_a_language_that_is_not_a_tag_is_refused(client, tenants):
    """A typo here does not fail closed — the engine falls back to any template
    at the level — so `hindi` instead of `hi-IN` plays the wrong language at the
    debtor rather than stopping the call."""
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}",
        headers=_auth(tenants.a),
        json={"language": "hindi"},
    )
    assert res.status_code == 422
    assert "VALUE_REJECTED" in res.json()["detail"]


def test_an_empty_correction_is_refused(client, tenants):
    res = client.patch(
        f"/api/trade/buyers/{tenants.a.buyer_id}", headers=_auth(tenants.a), json={}
    )
    assert res.status_code == 422


# --------------------------------------------------------------------- consent


def _withdraw(client, t, roles=("operator",), **overrides):
    body = {"reason": "letter received, dated 3 March", "source": "letter"}
    body.update(overrides)
    return client.post(
        f"/api/trade/buyers/{t.buyer_id}/consent/withdraw",
        headers=_auth(t, roles=roles),
        json=body,
    )


def test_an_operator_can_record_a_withdrawal(client, tenants):
    """Deliberately not an admin act. A withdrawal waiting in a queue is a
    campaign that keeps dialling somebody who has already said stop."""
    with admin_session() as s:
        buyer = s.execute(
            select(Buyer).where(Buyer.id == tenants.a.buyer_id)
        ).scalar_one()
        buyer.next_action_at = datetime.now(timezone.utc) + timedelta(hours=2)

    res = _withdraw(client, tenants.a)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed"] is True
    assert body["after"]["consent_withdrawn"] is True
    # Cleared, or the scheduler wakes on this buyer every few hours to write the
    # same refusal.
    assert body["after"]["next_action_at"] is None

    buyer = _buyer(tenants.a.buyer_id)
    assert buyer.consent_withdrawn is True
    assert buyer.consent_withdrawn_at is not None


def test_a_second_withdrawal_does_not_move_the_first_date(client, tenants):
    """The date that matters is the first one: it is the date from which every
    later contact was contact they had already refused."""
    _withdraw(client, tenants.a)
    first = _buyer(tenants.a.buyer_id).consent_withdrawn_at

    again = _withdraw(client, tenants.a, reason="second letter, dated 20 March")
    assert again.status_code == 200
    assert _buyer(tenants.a.buyer_id).consent_withdrawn_at == first


def test_an_unrecognised_source_is_refused_by_name(client, tenants):
    """"Who told you, and how" is the first question when a withdrawal is
    disputed, and a free-text answer is not evidence of anything."""
    res = _withdraw(client, tenants.a, source="a colleague mentioned it")
    assert res.status_code == 422
    assert "SOURCE_NOT_RECOGNISED" in res.json()["detail"]
    assert _buyer(tenants.a.buyer_id).consent_withdrawn is False


def test_a_withdrawal_touches_no_balance(client, tenants):
    """A debtor who says "stop calling" owes precisely what they owed a moment
    before."""
    from app.models import CreditAccount

    with admin_session() as s:
        before = s.execute(
            select(CreditAccount.outstanding_paise).where(
                CreditAccount.id == tenants.a.account_id
            )
        ).scalar_one()
    _withdraw(client, tenants.a)
    with admin_session() as s:
        after = s.execute(
            select(CreditAccount.outstanding_paise).where(
                CreditAccount.id == tenants.a.account_id
            )
        ).scalar_one()
    assert after == before


def test_restoring_consent_is_admin_only(client, tenants):
    """The one act in the product that turns contact back on."""
    _withdraw(client, tenants.a)

    refused = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/consent/restore",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"reason": "buyer rang to say carry on", "source": "phone"},
    )
    assert refused.status_code == 403
    assert "consent:restore" in refused.json()["detail"]
    assert _buyer(tenants.a.buyer_id).consent_withdrawn is True

    allowed = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/consent/restore",
        headers=_auth(tenants.a),
        json={"reason": "buyer rang to say carry on", "source": "phone"},
    )
    assert allowed.status_code == 200, allowed.text
    buyer = _buyer(tenants.a.buyer_id)
    assert buyer.consent_withdrawn is False
    assert buyer.consent_withdrawn_at is None


def test_a_restore_keeps_the_withdrawal_in_the_audit_trail(client, tenants):
    """Restoring clears `consent_withdrawn_at`, so the audit row written from
    `before` is the only surviving evidence a withdrawal ever happened."""
    _withdraw(client, tenants.a)
    client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/consent/restore",
        headers=_auth(tenants.a),
        json={"reason": "buyer rang to say carry on", "source": "phone"},
    )
    with admin_session() as s:
        rows = list(
            s.execute(
                select(AuditLog)
                .where(
                    AuditLog.company_id == tenants.a.company_id,
                    AuditLog.action == "buyer.consent_changed",
                )
                .order_by(AuditLog.occurred_at)
            ).scalars()
        )
    assert len(rows) == 2
    assert rows[1].before["consent_withdrawn"] is True
    assert rows[1].before["consent_withdrawn_at"] is not None
    assert rows[1].after["consent_withdrawn"] is False


# ----------------------------------------------------------------- suppression


def test_a_hold_is_recorded_and_never_shortened(client, tenants):
    """Two people work the same file — one records a fortnight of hospital
    leave, the other a promise to pay on Friday. The shorter must not resume
    calling in the middle of the longer."""
    long_hold = datetime.now(timezone.utc) + timedelta(days=30)
    res = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/suppress",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"until": long_hold.isoformat(), "reason": "in hospital until month end"},
    )
    assert res.status_code == 200, res.text

    short_hold = datetime.now(timezone.utc) + timedelta(days=2)
    second = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/suppress",
        headers=_auth(tenants.a),
        json={"until": short_hold.isoformat(), "reason": "promised to pay Friday"},
    )
    assert second.status_code == 200, second.text
    # The effective date comes back, and it is not the one that was asked for.
    # Compared as an instant rather than as a string: the database may hand the
    # value back in a different offset, and the same moment written two ways is
    # not a difference this test is about.
    effective = datetime.fromisoformat(second.json()["after"]["suppressed_until"])
    assert effective == long_hold
    assert "a longer hold already stood" in second.json()["detail"]


def test_a_hold_without_a_timezone_is_refused(client, tenants):
    """A naive value is a guess between UTC and IST, and the guess is worth five
    and a half hours of contact in one direction or the other."""
    naive = (datetime.now(timezone.utc) + timedelta(days=5)).replace(tzinfo=None)
    res = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/suppress",
        headers=_auth(tenants.a),
        json={"until": naive.isoformat(), "reason": "away"},
    )
    assert res.status_code == 422
    assert "NAIVE_TIMESTAMP" in res.json()["detail"]


def test_a_hold_that_has_already_expired_is_refused(client, tenants):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    res = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/suppress",
        headers=_auth(tenants.a),
        json={"until": past.isoformat(), "reason": "typed the wrong year"},
    )
    assert res.status_code == 422
    assert "SUPPRESSION_NOT_IN_FUTURE" in res.json()["detail"]


# ------------------------------------------------------- channels and numbers


def test_a_channel_opt_out_suppresses_only_that_channel(client, tenants):
    """STOP on SMS says nothing about the phone ringing.

    Until this route existed the engine's CHANNEL_OPTED_OUT refusal could never
    fire, because nothing wrote the row it reads.
    """
    res = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/channel-optout",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"channel": "SMS", "source": "sms_stop"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["after"]["opted_out"] is True

    with admin_session() as s:
        rows = list(
            s.execute(
                select(ChannelOptOut).where(
                    ChannelOptOut.buyer_id == tenants.a.buyer_id
                )
            ).scalars()
        )
    assert [r.channel for r in rows] == [Channel.SMS]
    # Consent is a property of the person; a channel preference is not.
    assert _buyer(tenants.a.buyer_id).consent_withdrawn is False


def test_a_repeated_stop_does_not_move_the_first_refusal(client, tenants):
    for _ in range(2):
        client.post(
            f"/api/trade/buyers/{tenants.a.buyer_id}/channel-optout",
            headers=_auth(tenants.a),
            json={"channel": "SMS", "source": "sms_stop"},
        )
    with admin_session() as s:
        rows = list(
            s.execute(
                select(ChannelOptOut).where(
                    ChannelOptOut.buyer_id == tenants.a.buyer_id
                )
            ).scalars()
        )
    assert len(rows) == 1


def test_an_unknown_channel_is_refused_by_name(client, tenants):
    res = client.post(
        f"/api/trade/buyers/{tenants.a.buyer_id}/channel-optout",
        headers=_auth(tenants.a),
        json={"channel": "POST", "source": "letter"},
    )
    assert res.status_code == 422
    assert "CHANNEL_NOT_RECOGNISED" in res.json()["detail"]


def test_retiring_a_number_flags_it_and_keeps_the_row(client, tenants):
    """Calls point at this row, and "who did we ring on 12 March, and on what
    number" has to stay answerable afterwards."""
    phone_id = _phone_id(tenants.a.buyer_id)
    res = client.post(
        f"/api/trade/phones/{phone_id}/retire",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"reason": "answered by a stranger; wrong number at import"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["after"]["is_valid"] is False

    with admin_session() as s:
        phone = s.execute(
            select(BuyerPhone).where(BuyerPhone.id == phone_id)
        ).scalar_one()
    assert phone.is_valid is False
    assert phone.e164  # the number itself is still there


def test_one_tenant_cannot_retire_anothers_number(client, tenants):
    phone_id = _phone_id(tenants.b.buyer_id)
    res = client.post(
        f"/api/trade/phones/{phone_id}/retire",
        headers=_auth(tenants.a),
        json={"reason": "not ours"},
    )
    assert res.status_code == 404


# -------------------------------------------------------------------- promises


def test_a_promise_pauses_the_chase_and_reports_when_it_resumes(client, tenants):
    due = date.today() + timedelta(days=7)
    res = client.post(
        "/api/trade/promises",
        headers=_auth(tenants.a, roles=("operator",)),
        json={
            "buyer_id": str(tenants.a.buyer_id),
            "amount": "1,50,000",
            "promised_by_date": due.isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["promised_amount_paise"] == 15_000_000
    assert body["promised_amount_display"] == "₹1,50,000"
    assert body["status"] == "OPEN"

    # The pause is the effect worth asserting: chasing stops until the promised
    # date plus grace, converted out of IST rather than resuming at UTC midnight.
    resumes = datetime.fromisoformat(body["chase_resumes_at"])
    suppressed = _buyer(tenants.a.buyer_id).suppressed_until
    assert suppressed == resumes
    assert resumes.date() > due


def test_a_promise_beyond_the_horizon_is_refused(client, tenants):
    """Otherwise "I will pay in 2029" is a way to switch the ladder off from the
    debtor's side."""
    res = client.post(
        "/api/trade/promises",
        headers=_auth(tenants.a),
        json={
            "buyer_id": str(tenants.a.buyer_id),
            "amount": "1,000",
            "promised_by_date": (date.today() + timedelta(days=200)).isoformat(),
        },
    )
    assert res.status_code == 422
    assert "HORIZON_TOO_FAR" in res.json()["detail"]


def test_a_promise_can_be_settled_once(client, tenants):
    created = client.post(
        "/api/trade/promises",
        headers=_auth(tenants.a),
        json={
            "buyer_id": str(tenants.a.buyer_id),
            "amount": "1,000",
            "promised_by_date": (date.today() + timedelta(days=5)).isoformat(),
        },
    )
    promise_id = created.json()["id"]

    settled = client.post(
        f"/api/trade/promises/{promise_id}/settle",
        headers=_auth(tenants.a),
        json={"kept": True, "note": "cheque handed over at the counter"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "KEPT"

    # Never re-opens: a promise that has been judged stays judged.
    again = client.post(
        f"/api/trade/promises/{promise_id}/settle",
        headers=_auth(tenants.a),
        json={"kept": False},
    )
    assert again.status_code == 409
    assert "ALREADY_SETTLED" in again.json()["detail"]


def test_the_sweep_writes_broken_from_the_ledger(client, tenants):
    """Nothing else writes BROKEN.

    A kept-rate that only counted successes would report every debtor as
    perfectly reliable right up to the day they stop answering.
    """
    created = client.post(
        "/api/trade/promises",
        headers=_auth(tenants.a),
        json={
            "buyer_id": str(tenants.a.buyer_id),
            "amount": "1,000",
            "promised_on": (date.today() - timedelta(days=20)).isoformat(),
            "promised_by_date": (date.today() - timedelta(days=10)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    promise_id = created.json()["id"]

    swept = client.post("/api/trade/promises/resolve-due", headers=_auth(tenants.a))
    assert swept.status_code == 200, swept.text
    assert [p["id"] for p in swept.json()] == [promise_id]
    assert swept.json()[0]["status"] == "BROKEN"

    with admin_session() as s:
        row = s.execute(
            select(Promise).where(Promise.id == uuid.UUID(promise_id))
        ).scalar_one()
    assert row.status == "BROKEN"
    assert row.settled_at is not None


def test_promises_are_listed_per_tenant_only(client, tenants):
    """Cross-tenant isolation on one of the new reads."""
    client.post(
        "/api/trade/promises",
        headers=_auth(tenants.a),
        json={
            "buyer_id": str(tenants.a.buyer_id),
            "amount": "1,000",
            "promised_by_date": (date.today() + timedelta(days=5)).isoformat(),
        },
    )
    mine = client.get(
        f"/api/trade/buyers/{tenants.a.buyer_id}/promises", headers=_auth(tenants.a)
    )
    assert mine.status_code == 200
    assert len(mine.json()) == 1

    theirs = client.get(
        f"/api/trade/buyers/{tenants.a.buyer_id}/promises", headers=_auth(tenants.b)
    )
    # Not an empty list: under RLS the other tenant's buyer is indistinguishable
    # from one that does not exist, and saying "no promises" would confirm the
    # buyer is real.
    assert theirs.status_code == 404


# ------------------------------------------------------------------ no-call days


def test_a_blackout_date_is_company_wide_and_admin_only(client, tenants):
    day = (date.today() + timedelta(days=14)).isoformat()

    refused = client.post(
        "/api/portal/blackout-dates",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"day": day, "label": "Diwali"},
    )
    assert refused.status_code == 403
    assert "company:settings" in refused.json()["detail"]

    res = client.post(
        "/api/portal/blackout-dates",
        headers=_auth(tenants.a),
        json={"day": day, "label": "Diwali"},
    )
    assert res.status_code == 201, res.text
    blackout_id = res.json()["id"]

    # Readable by anyone who can read campaigns: "why did nothing dial on
    # Tuesday" should not need an admin to answer.
    listed = client.get(
        "/api/portal/blackout-dates", headers=_auth(tenants.a, roles=("viewer",))
    )
    assert listed.status_code == 200
    assert [row["day"] for row in listed.json()] == [day]

    duplicate = client.post(
        "/api/portal/blackout-dates",
        headers=_auth(tenants.a),
        json={"day": day, "label": "Diwali (again)"},
    )
    assert duplicate.status_code == 409

    removed = client.delete(
        f"/api/portal/blackout-dates/{blackout_id}", headers=_auth(tenants.a)
    )
    assert removed.status_code == 200
    assert removed.json()["day"] == day
    with admin_session() as s:
        assert (
            s.execute(
                select(BlackoutDate).where(
                    BlackoutDate.company_id == tenants.a.company_id
                )
            ).scalars().all()
            == []
        )
    # Deleting the row must not delete the fact that somebody re-opened the day.
    with admin_session() as s:
        row = s.execute(
            select(AuditLog).where(
                AuditLog.company_id == tenants.a.company_id,
                AuditLog.action == "company.blackout_removed",
            )
        ).scalars().one()
    assert row.before == {"day": day, "label": "Diwali"}


def test_blackout_dates_are_not_visible_across_tenants(client, tenants):
    """Cross-tenant isolation on a second new read."""
    day = (date.today() + timedelta(days=21)).isoformat()
    client.post(
        "/api/portal/blackout-dates",
        headers=_auth(tenants.a),
        json={"day": day, "label": "Local holiday"},
    )
    theirs = client.get("/api/portal/blackout-dates", headers=_auth(tenants.b))
    assert theirs.status_code == 200
    assert theirs.json() == []


# ------------------------------------------------------------------- campaigns


def test_a_campaign_window_can_be_narrowed_but_not_widened(client, tenants):
    """The 08:00-19:00 ceiling is a property of the system, not a campaign
    setting — the route only turns the check constraint into a sentence."""
    created = client.post(
        "/api/campaigns",
        headers=_auth(tenants.a),
        json={"name": "March recovery"},
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    narrowed = client.patch(
        f"/api/campaigns/{campaign_id}",
        headers=_auth(tenants.a, roles=("operator",)),
        json={"window_start": "11:00", "window_end": "16:00", "channels": ["SMS"]},
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["channels"] == ["SMS"]

    widened = client.patch(
        f"/api/campaigns/{campaign_id}",
        headers=_auth(tenants.a),
        json={"window_end": "21:00"},
    )
    assert widened.status_code == 422
    assert "08:00 and 19:00" in widened.json()["detail"]

    backwards = client.patch(
        f"/api/campaigns/{campaign_id}",
        headers=_auth(tenants.a),
        json={"window_start": "17:00", "window_end": "12:00"},
    )
    assert backwards.status_code == 422


def test_an_unknown_campaign_field_is_refused_rather_than_ignored(client, tenants):
    """`status` is the one somebody will send, expecting to start a campaign."""
    created = client.post(
        "/api/campaigns", headers=_auth(tenants.a), json={"name": "April recovery"}
    )
    campaign_id = created.json()["id"]
    res = client.patch(
        f"/api/campaigns/{campaign_id}",
        headers=_auth(tenants.a),
        json={"status": "ACTIVE"},
    )
    assert res.status_code == 422
    assert "status" in res.text
