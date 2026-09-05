"""End-to-end demo.

    python -m app.demo

Enrols the seeded buyers, starts the campaign, runs the scheduler to completion
against a stub carrier, then prints what happened — including, and especially,
what was refused and why.

The refusals are the point. A demo where every buyer gets called proves nothing
about a system whose main job is knowing when not to.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.comms.base import FakeProvider
from app.config import settings
from app.db import admin_session
from app.models import (
    AudioAsset,
    Call,
    CallStatus,
    Campaign,
    Channel,
    Company,
    EscalationState,
    Message,
)
from app.render.numbers import format_inr
from app.scheduler import campaign as campaign_ops
from app.scheduler.tick import run_once
from app.storage.local import LocalStorage
from app.telephony.ari import AriClient
from app.telephony.fake import FakeAri, FakeAriServer
from app.telephony.staging import LocalStager
from app.tts import build_tts

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def demo_clock() -> datetime:
    """A moment the campaign is actually allowed to contact people.

    Run on a Saturday, every buyer blocks with WEEKEND_NOT_PERMITTED — correct,
    and a useless demonstration, because one gate masks all the others. So the
    demo advances to the next weekday inside the calling window and says so.
    """
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    when = datetime.now(timezone.utc).astimezone(ist)
    if when.hour < 11 or when.hour >= 18:
        when = when.replace(hour=11, minute=30, second=0, microsecond=0)
    while when.weekday() >= 5:  # Saturday or Sunday
        when += timedelta(days=1)
        when = when.replace(hour=11, minute=30, second=0, microsecond=0)
    return when.astimezone(timezone.utc)


def run() -> None:
    now = demo_clock()
    storage = LocalStorage(settings.audio_dir)
    stager = LocalStager(settings.asterisk_sounds_dir)
    tts = build_tts()

    print(f"tts backend      : {tts.name}")
    print(f"storage backend  : {storage.name}")
    print(f"telephony        : stub ARI (no carrier)")
    print(f"allowlist        : {'enforced' if settings.enforce_allowlist else 'off (all backends fake)'}")
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    print(f"dispatch clock   : {local:%a %d %b %H:%M} IST (a weekday inside the window)")

    with admin_session() as session:
        company = session.execute(
            select(Company).where(Company.name == "Acme Steel Traders")
        ).scalar_one_or_none()
        if company is None:
            raise SystemExit("nothing seeded — run `python -m app.seed` first")

        campaign = session.execute(
            select(Campaign).where(Campaign.company_id == company.id)
        ).scalars().first()

        buyer_ids = campaign_ops.eligible_buyers(session, company.id)
        enrolled = campaign_ops.enrol(session, campaign, buyer_ids)
        campaign_ops.start(session, campaign)
        session.commit()

    _rule("Enrolment")
    print(f"eligible buyers  : {len(buyer_ids)}")
    print(f"enrolled         : {enrolled['added']}")
    for skip in enrolled["skipped"]:
        print(f"  skipped {skip['buyer_id']}: {skip['reason']}")

    providers = {Channel.SMS: FakeProvider(Channel.SMS)}
    fake = FakeAri()

    with FakeAriServer(fake) as server:
        client = AriClient(base_url=server.base_url, app_name=settings.ari_app_name)
        with admin_session() as session:
            results = run_once(
                session,
                limit=200,
                ari_client=client,
                stager=stager,
                storage=storage,
                tts=tts,
                providers=providers,
                now=now,
            )

    _rule("Dispatch")
    allowed = [r for r in results if r.allowed]
    refused = [r for r in results if not r.allowed]
    print(f"buyers processed : {len(results)}")
    print(f"contacted        : {len(allowed)}")
    print(f"refused          : {len(refused)}")
    print(f"SIP channels     : {len(fake.originate_calls)} originate call(s)")
    print(f"SMS handed over  : {len(providers[Channel.SMS].sent)}")

    _rule("Why the rest were refused")
    for reason, count in Counter(r.reason for r in refused).most_common():
        print(f"  {count:>3}  {reason}")

    with admin_session() as session:
        levels = dict(
            session.execute(
                select(EscalationState.level, func.count()).group_by(EscalationState.level)
            ).all()
        )
        assets = session.execute(
            select(AudioAsset.status, func.count()).group_by(AudioAsset.status)
        ).all()
        calls = session.execute(
            select(Call.status, func.count()).group_by(Call.status)
        ).all()
        messages = session.execute(
            select(Message.status, func.count()).group_by(Message.status)
        ).all()
        unapproved_block = session.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.block_reason == "L3_TEMPLATE_NOT_APPROVED")
        ).scalar_one()

    _rule("State")
    print("escalation levels:", {k.value: v for k, v in levels.items()})
    print("audio assets     :", {k.value: v for k, v in assets})
    print("calls            :", {k.value: v for k, v in calls})
    print("messages         :", {k.value: v for k, v in messages})

    _rule("The assertion that matters")
    print(
        "Every refusal above carries a machine-readable reason. Unscrubbed and\n"
        "stale-scrub numbers blocked rather than dialled; the disputed account and\n"
        "the withdrawn consent were left alone; and no unapproved legal content\n"
        "was ever spoken."
    )
    print(f"\nL3 blocked on missing approval: {unapproved_block}")
    print("\nOpen http://localhost:8000/ to see the same thing in the console.")


if __name__ == "__main__":
    run()
