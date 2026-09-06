"""The DND scrub pipeline.

The policy engine fails closed on DND twice over, so this job is the difference
between a system that is correct and a system that can lawfully place a call at
all. Four things are pinned here.

* The cadence arithmetic, tested without a database, because an interval shorter
  than `DND_MAX_AGE` is the entire reason the job exists.
* The boundary, which is due rather than fresh. Off by one in the safe direction
  costs a registry lookup; off by one the other way blocks a campaign with a
  refusal nobody configured.
* What a vendor failure does, which is nothing. Losing an answer is not the same
  as being told the answer changed, and a scrub that downgraded CLEAR to UNKNOWN
  on a timeout would turn a five-minute outage into a week of blocked calls.
* `test_the_scrub_is_what_makes_a_call_possible`, which is the gap this builder
  fills, asserted end to end through the real engine.

Time is injected, never read from the clock. `NOW` is a Wednesday at 11:30 IST —
a weekday inside the calling window, so any refusal a test observes is about DND
and not about the hour it ran.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest

from app.config import settings
from app.db import admin_session
from app.models import (
    AccountStatus,
    BuyerPhone,
    Channel,
    DndStatus,
    EscalationLevel,
)
from app.policy.engine import (
    DND_MAX_AGE,
    AccountRef,
    BlockReason,
    DecisionContext,
    EscalationPolicy,
    PhoneRef,
    TemplateRef,
    evaluate,
)
from app.providers.base import DndCheck, UnknownDndStatus
from app.providers.dnd import DndUnavailable, LocalDnd, build_dnd_backend
from app.scheduler.scrub import (
    SCRUB_INTERVAL,
    is_due,
    scrub_cutoff,
    scrub_due,
)

NOW = datetime(2026, 3, 11, 6, 0, tzinfo=timezone.utc)  # Wed 11:30 IST, in-window

# The sweep is global by design, so a batch limit small enough to miss this
# test's own rows would make the assertions depend on whatever else is in the
# database.
BATCH = 500


class StubDnd:
    """Answers only about the numbers a test created.

    Anything else raises, and the scrub leaves a raising number untouched — so a
    test's global sweep cannot disturb rows it does not own. That is the failure
    behaviour under test doing double duty as test isolation.
    """

    name = "stub"

    def __init__(self, answers: dict[str, DndStatus], *, at: datetime | None = None):
        self.answers = answers
        self.at = at or NOW
        self.asked: list[str] = []
        self._lock = threading.Lock()

    def check(self, e164: str) -> DndCheck:
        with self._lock:
            self.asked.append(e164)
        if e164 not in self.answers:
            raise DndUnavailable(f"stub was not told about {e164}")
        return DndCheck(e164=e164, status=self.answers[e164], checked_at=self.at)


def _number() -> str:
    return f"+9199{uuid.uuid4().int % 100_000_000:08d}"


# ------------------------------------------------------------------- planning


def test_a_never_scrubbed_number_is_due():
    assert is_due(None, now=NOW)


def test_a_number_scrubbed_inside_the_window_is_not_due():
    assert not is_due(NOW - SCRUB_INTERVAL / 2, now=NOW)


def test_a_number_scrubbed_exactly_at_the_boundary_is_due():
    """Equality re-scrubs. The alternative is a number that is fresh to the
    planner and stale to the policy engine one tick later."""
    assert is_due(NOW - SCRUB_INTERVAL, now=NOW)


def test_a_number_scrubbed_past_the_boundary_is_due():
    assert is_due(NOW - SCRUB_INTERVAL - timedelta(seconds=1), now=NOW)


def test_the_cadence_leaves_room_for_a_missed_run():
    """The reason the job exists at all.

    One whole interval of slack: a run that is skipped, throttled or emptied by
    a vendor outage still has a full cycle to recover before the policy engine
    stops trusting the answer and blocks the number.
    """
    assert SCRUB_INTERVAL < DND_MAX_AGE
    assert SCRUB_INTERVAL * 2 <= DND_MAX_AGE


def test_the_cutoff_is_the_window_behind_now():
    assert scrub_cutoff(NOW) == NOW - SCRUB_INTERVAL


# ------------------------------------------------------------ the local backend


def test_the_local_registry_gives_one_number_the_same_answer_every_time():
    """Two instances, because the builtin `hash` would not manage this across
    two processes."""
    number = "+919812345678"
    assert LocalDnd().check(number).status is LocalDnd().check(number).status


def test_the_local_registry_reports_both_answers():
    """A fixture that cleared every number would hide the branch of the policy
    gate that keeps this product lawful."""
    backend = LocalDnd()
    statuses = {backend.check(f"+9198765{n:05d}").status for n in range(300)}
    assert statuses == {DndStatus.CLEAR, DndStatus.REGISTERED}


def test_the_local_registry_honours_a_pinned_number():
    backend = LocalDnd(registered=["+919000000001"], clear=["+919000000002"])
    assert backend.check("+919000000001").status is DndStatus.REGISTERED
    assert backend.check("+919000000002").status is DndStatus.CLEAR


def test_a_number_pinned_to_both_lists_is_registered():
    backend = LocalDnd(registered=["+919000000003"], clear=["+919000000003"])
    assert backend.check("+919000000003").status is DndStatus.REGISTERED


def test_the_local_registry_answers_with_an_aware_timestamp():
    assert LocalDnd().check("+919812345678").checked_at.tzinfo is not None


def test_an_unwired_vendor_refuses_rather_than_answering_from_the_fixture(monkeypatch):
    """A production deployment naming a vendor nobody built must not be handed
    invented CLEAR answers for real debtor numbers."""
    monkeypatch.setattr(settings, "dnd_backend", "trai")
    with pytest.raises(RuntimeError):
        build_dnd_backend()


# ------------------------------------------------------------- the answer shape


def test_a_naive_timestamp_is_refused():
    """It would raise inside the policy engine instead, on the dispatch path."""
    with pytest.raises(ValueError):
        DndCheck(
            e164="+919812345678",
            status=DndStatus.CLEAR,
            checked_at=datetime(2026, 3, 11, 6, 0),
        )


def test_a_status_outside_the_closed_set_is_refused():
    with pytest.raises(UnknownDndStatus):
        DndCheck(e164="+919812345678", status="CLEAR", checked_at=NOW)


# --------------------------------------------------------------- the scrub job


@pytest.fixture
def phones(tenants):
    """Three numbers on one buyer: never scrubbed, fresh, and exactly at the
    boundary. Torn down with the tenant's own rows."""
    made: dict[str, tuple[uuid.UUID, str]] = {}
    with admin_session() as s:
        for index, (label, checked) in enumerate(
            (
                ("never", None),
                ("fresh", NOW - SCRUB_INTERVAL / 2),
                ("boundary", NOW - SCRUB_INTERVAL),
            )
        ):
            e164 = _number()
            phone = BuyerPhone(
                company_id=tenants.a.company_id,
                buyer_id=tenants.a.buyer_id,
                e164=e164,
                number_type="mobile",
                priority=index + 1,
                dnd_status=DndStatus.UNKNOWN if checked is None else DndStatus.CLEAR,
                dnd_checked_at=checked,
            )
            s.add(phone)
            s.flush()
            made[label] = (phone.id, e164)
    return made


def test_the_scrub_writes_both_the_status_and_the_timestamp(phones):
    phone_id, e164 = phones["never"]
    backend = StubDnd({e164: DndStatus.CLEAR}, at=NOW)

    with admin_session() as s:
        report = scrub_due(s, backend, now=NOW, limit=BATCH)

    assert report.updated == 1
    with admin_session() as s:
        row = s.get(BuyerPhone, phone_id)
        assert row.dnd_status is DndStatus.CLEAR
        # Both, not just the status: a status without a fresh timestamp is a
        # number the staleness gate goes on refusing.
        assert row.dnd_checked_at == NOW


def test_a_number_scrubbed_inside_the_window_is_not_claimed(phones):
    _, fresh = phones["fresh"]
    backend = StubDnd({})

    with admin_session() as s:
        scrub_due(s, backend, now=NOW, limit=BATCH)

    assert fresh not in backend.asked


def test_a_number_at_the_boundary_is_claimed(phones):
    """The SQL predicate agrees with `is_due` — same boundary, same direction."""
    _, boundary = phones["boundary"]
    backend = StubDnd({})

    with admin_session() as s:
        scrub_due(s, backend, now=NOW, limit=BATCH)

    assert boundary in backend.asked


def test_a_failing_backend_leaves_the_previous_answer_untouched(phones):
    """Losing an answer is not being told the answer changed.

    The number is CLEAR and due. The registry cannot be reached. Downgrading it
    to UNKNOWN here would block a callable debtor on the strength of our own
    outage — and the block would outlive the outage by a week.
    """
    phone_id, e164 = phones["boundary"]
    backend = StubDnd({})  # knows nothing, so every check raises

    with admin_session() as s:
        report = scrub_due(s, backend, now=NOW, limit=BATCH)

    assert e164 in backend.asked, "the row under test was never claimed"
    assert report.updated == 0
    assert report.failed >= 1
    with admin_session() as s:
        row = s.get(BuyerPhone, phone_id)
        assert row.dnd_status is DndStatus.CLEAR
        assert row.dnd_checked_at == NOW - SCRUB_INTERVAL


def test_an_answer_about_a_different_number_is_discarded(phones):
    """A bulk client that misaligns its rows by one marks the wrong debtor
    callable, and nothing downstream can tell."""
    phone_id, _ = phones["never"]

    class Muddled:
        name = "muddled"

        def check(self, e164: str) -> DndCheck:
            return DndCheck(
                e164="+919999999999", status=DndStatus.CLEAR, checked_at=NOW
            )

    with admin_session() as s:
        report = scrub_due(s, Muddled(), now=NOW, limit=BATCH)

    assert report.updated == 0
    with admin_session() as s:
        row = s.get(BuyerPhone, phone_id)
        assert row.dnd_status is DndStatus.UNKNOWN
        assert row.dnd_checked_at is None


def test_a_registered_answer_is_recorded_and_refused_by_name(phones):
    phone_id, e164 = phones["never"]
    backend = StubDnd({e164: DndStatus.REGISTERED}, at=NOW)

    with admin_session() as s:
        report = scrub_due(s, backend, now=NOW, limit=BATCH)

    assert report.registered == 1
    with admin_session() as s:
        decision = evaluate(_context(s.get(BuyerPhone, phone_id)))
    assert decision.reason is BlockReason.DND_REGISTERED


def test_two_workers_never_scrub_the_same_number(tenants):
    """`SKIP LOCKED`, for the reason the tick has it.

    Run genuinely concurrently: a sequential test proves nothing about a claim.
    A vendor bills per lookup, and two workers writing the same row is how one
    of them silently loses to the other.
    """
    e164s = []
    with admin_session() as s:
        for n in range(6):
            e164 = _number()
            s.add(
                BuyerPhone(
                    company_id=tenants.a.company_id,
                    buyer_id=tenants.a.buyer_id,
                    e164=e164,
                    number_type="mobile",
                    priority=10 + n,
                    dnd_status=DndStatus.UNKNOWN,
                    dnd_checked_at=None,
                )
            )
            e164s.append(e164)

    backend = StubDnd({e: DndStatus.CLEAR for e in e164s}, at=NOW)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        try:
            with admin_session() as s:
                barrier.wait(timeout=15)
                scrub_due(s, backend, now=NOW, limit=BATCH)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    for e164 in e164s:
        assert backend.asked.count(e164) == 1, f"{e164} was scrubbed twice"


def test_the_scrub_is_what_makes_a_call_possible(phones):
    """The gap this pipeline fills, end to end.

    An imported number arrives UNKNOWN, the engine refuses it by name, and the
    only thing between that refusal and a lawful call is this job.
    """
    phone_id, e164 = phones["never"]

    with admin_session() as s:
        before = evaluate(_context(s.get(BuyerPhone, phone_id)))
    assert before.reason is BlockReason.DND_UNKNOWN

    with admin_session() as s:
        scrub_due(s, StubDnd({e164: DndStatus.CLEAR}, at=NOW), now=NOW, limit=BATCH)

    with admin_session() as s:
        after = evaluate(_context(s.get(BuyerPhone, phone_id)))
    assert after.allowed, after.reason


# -------------------------------------------------------------------- builders


def _context(phone_row: BuyerPhone, *, now: datetime = NOW) -> DecisionContext:
    """A decision whose only interesting variable is the phone.

    Everything else is deliberately permissive, so a refusal from `evaluate`
    here is about DND and could not be about anything else.
    """
    return DecisionContext(
        now=now,
        campaign_active=True,
        timezone="Asia/Kolkata",
        window_start=time(9, 0),
        window_end=time(19, 0),
        call_on_weekends=False,
        blackout_days=frozenset(),
        max_attempts_per_day=3,
        max_attempts_per_week=10,
        min_hours_between_calls=24,
        consent_withdrawn=False,
        suppressed_until=None,
        phones=(
            PhoneRef(
                id=phone_row.id,
                e164=phone_row.e164,
                priority=phone_row.priority,
                is_valid=phone_row.is_valid,
                dnd_status=phone_row.dnd_status,
                dnd_checked_at=phone_row.dnd_checked_at,
                number_type=phone_row.number_type,
            ),
        ),
        accounts=(
            AccountRef(
                id=uuid.uuid4(),
                status=AccountStatus.OVERDUE,
                outstanding_paise=5_00_000_00,
                due_date=now - timedelta(days=30),
                level=EscalationLevel.L1,
                level_entered_at=now - timedelta(days=1),
                attempts_at_level=0,
                delivered_at_level=0,
            ),
        ),
        attempts_today=0,
        attempts_this_week=0,
        templates=(
            TemplateRef(
                key="l1_voice",
                level=EscalationLevel.L1,
                language="en-IN",
                version_id=uuid.uuid4(),
                is_approved=True,
                channel=Channel.VOICE,
            ),
        ),
        policy=EscalationPolicy(),
        channels_enabled=(Channel.VOICE,),
    )
